#!/usr/bin/env python3
"""Push locally built Nix store paths to a GHCR OCI binary cache.

Pure-Python port of the former `push.sh` (bash + inline python).  Every
semantic of the bash version is preserved: cleanup on exit, token exchange,
OCI manifest/blob upload (HEAD-fast-path, POST-init, PUT with digest query),
cache-index merge, readback verification and GH Actions step summary.

Environment (same as the bash version):
  INPUT_REPO / INPUT_TOKEN / INPUT_SIGNING_KEY / INPUT_PATHS  (action inputs)
  GITHUB_REPOSITORY / GITHUB_TOKEN                            (fallbacks)
  NIXCACHE_REPO / NIXCACHE_PORT / NIXCACHE_PROXY_PID           (from nix/cache)
  RUNNER_TEMP / GITHUB_STEP_SUMMARY / RUNNER_OS / HOME

Output discipline: all diagnostics (::warning::/::error::/::group::) go to
stderr; ::add-mask:: goes to stdout; bulk data flows through files and return
values and never mixes into stdout.
"""
import base64
import glob
import hashlib
import http.client
import json
import lzma
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

REGISTRY = "ghcr.io"
MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
INDEX_MEDIA_TYPE = "application/vnd.nix.cache.index.v1+json"

STORE_PATH_RE = re.compile(r"^/nix/store/[a-z0-9]{32}-")
CHUNK = 1 << 20                     # 1 MiB

try:  # zstd 进标准库是 Python 3.14；旧版回退 lzma
    from compression.zstd import ZstdCompressor as _ZstdCompressor
    COMPRESSION = "zstd"
    COMPRESSION_EXT = "zst"
except ImportError:
    _ZstdCompressor = None
    COMPRESSION = "xz"
    COMPRESSION_EXT = "xz"
MAX_NAR_SIZE = 10737418240          # ~10 GiB GHCR blob limit
MAX_RETRIES = 3                     # curl --retry 3 (→ 4 attempts)
RETRY_DELAY = 2                     # curl --retry-delay 2
CLOSURE_BATCH = 64                  # xargs -n 64 in the bash version
STD_BATCH = 128                     # xargs -n 128 in the bash version
READBACK_TRIES = 30                 # readback poll loop
READBACK_SLEEP = 2
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 5                   # curl -L default hop limit

PROXY_MARKER = "nixcache-proxy.py"
NIX_CONF_BEGIN = "# nix-cache begin"
NIX_CONF_END = "# nix-cache end"

# ---------------------------------------------------------------- diagnostics

def warn(msg: str) -> None:
    print(f"::warning::{msg}", file=sys.stderr)


# 401/403 (insufficient permission: fork PRs, missing packages:* perms)
# -> warning + exit 0; anything else -> error + exit 1.
def fail_or_skip(code, msg: str) -> None:
    if code in (401, 403):
        print(
            f"::warning::{msg} (HTTP {code}: insufficient permission; "
            "fork PRs and missing packages:* permissions are skipped)",
            file=sys.stderr,
        )
        sys.exit(0)
    print(f"::error::{msg} (HTTP {code})", file=sys.stderr)
    sys.exit(1)


# ------------------------------------------------------------ small helpers

def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def header_value(headers, name: str) -> str:
    for k, v in headers or []:
        if k.lower() == name.lower():
            return v
    return ""


# --------------------------------------------------------------------- HTTP

def http_request(method, url, headers=None, body=None, timeout=30.0,
                 retries=0, retry_delay=2.0):
    """One HTTP request with curl `-L` redirect following.  Returns
    (status, headers, body).

    `body` may be bytes or a seekable file object (streamed, with
    Content-Length set by the caller; re-seek(0) before every request and
    every redirect hop, so a retried or redirected PUT replays the file).
    Redirects (301/302/303/307/308 with a Location header) are followed up
    to MAX_REDIRECTS hops inside a single attempt, curl `-L` style: GET/HEAD
    follow any 30x; PUT/POST only 307/308, preserving the method and body;
    Authorization is dropped when the redirect leaves the original host.
    Retries `retries` times on transport errors and on HTTP 408/429/5xx
    responses — curl `--retry 3 --retry-all-errors` semantics (one shared
    retry budget); a persistent failure returns (0, [], b'') for transport
    errors and the final status otherwise.
    """
    attempt = 0
    while True:
        try:
            status, hdrs, data = _send_request(method, url, headers or {},
                                               body, timeout)
            if (status >= 500 or status in (408, 429)) and attempt < retries:
                attempt += 1
                time.sleep(retry_delay)
                continue
            return status, hdrs, data
        except (OSError, http.client.HTTPException):
            if attempt < retries:
                attempt += 1
                time.sleep(retry_delay)
                continue
            return 0, [], b""


def _send_request(method, url, headers, body, timeout=30.0):
    """One transfer: a single request plus curl `-L` redirect following
    (relative Locations resolved against the current URL, max MAX_REDIRECTS
    hops; a 30x without Location or over the hop limit is returned as-is).
    Transport errors propagate to the caller's retry loop."""
    current_url = url
    current_headers = dict(headers)
    hops = 0
    while True:
        parts = urllib.parse.urlsplit(current_url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if hasattr(body, "seek"):
            body.seek(0)
        conn = http.client.HTTPSConnection(parts.hostname, port, timeout=timeout)
        conn.request(method, path, body=body, headers=current_headers)
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        hdrs = resp.getheaders()
        conn.close()
        location = header_value(hdrs, "Location")
        follows = method in ("GET", "HEAD") or status in (307, 308)
        if (status not in REDIRECT_STATUSES or not location
                or not follows or hops >= MAX_REDIRECTS):
            return status, hdrs, data
        new_url = urllib.parse.urljoin(current_url, location)
        if urllib.parse.urlsplit(new_url).hostname != parts.hostname:
            current_headers = {
                k: v for k, v in current_headers.items()
                if k.lower() != "authorization"
            }
        current_url = new_url
        hops += 1


def token_url(repo: str, registry: str = REGISTRY) -> str:
    scope = f"repository:{repo}/nix-cache:pull,push"
    return f"https://{registry}/token?scope={scope}&service={registry}"


def manifest_url(repo: str, tag: str, registry: str = REGISTRY) -> str:
    return f"https://{registry}/v2/{repo}/nix-cache/manifests/{tag}"


def blob_url(repo: str, digest: str, registry: str = REGISTRY) -> str:
    return f"https://{registry}/v2/{repo}/nix-cache/blobs/{digest}"


def uploads_url(repo: str, registry: str = REGISTRY) -> str:
    return f"https://{registry}/v2/{repo}/nix-cache/blobs/uploads/"


def build_put_url(location: str, registry: str = REGISTRY, digest: str = "") -> str:
    """Join an upload Location into the final PUT URL (relative/absolute,
    `?`/`&` separator, `digest=` query parameter)."""
    url = location
    if url.startswith("/"):
        url = f"https://{registry}{url}"
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}digest={digest}"


def oci_get_token(repo: str, token: str, registry: str = REGISTRY) -> str:
    auth = "Basic " + base64.b64encode(f"token:{token}".encode()).decode()
    st, _, body = http_request(
        "GET", token_url(repo, registry),
        headers={"Authorization": auth},
        timeout=30.0, retries=MAX_RETRIES, retry_delay=RETRY_DELAY,
    )
    oci_token = ""
    if st == 200:
        try:
            oci_token = (json.loads(body) or {}).get("token", "") or ""
        except (ValueError, AttributeError, TypeError):
            oci_token = ""
    if not oci_token:
        scope = f"repository:{repo}/nix-cache:pull,push"
        print(f"::error::failed to obtain GHCR registry token (scope: {scope})",
              file=sys.stderr)
        sys.exit(1)
    return oci_token


def fetch_manifest(tag: str, token: str, repo: str, registry: str = REGISTRY,
                   quiet: bool = False):
    """GET the manifest; returns (status, body).  404 = not found (empty
    index); any other non-200 is an error: warn (unless quiet) + exit 0 at the
    caller."""
    st, _, body = http_request(
        "GET", manifest_url(repo, tag, registry),
        headers={"Authorization": f"Bearer {token}", "Accept": MANIFEST_MEDIA_TYPE},
        timeout=30.0,
    )
    if st == 200:
        return st, body
    if st == 404:
        return st, b""
    if not quiet:
        warn(f"failed to fetch OCI manifest {tag} (HTTP {st}); skipping upload")
    return st, b""


def put_manifest(tag: str, manifest_body, token: str, repo: str,
                 registry: str = REGISTRY) -> None:
    body = manifest_body.encode() if isinstance(manifest_body, str) else manifest_body
    st, _, _ = http_request(
        "PUT", manifest_url(repo, tag, registry),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": MANIFEST_MEDIA_TYPE},
        body=body, timeout=60.0, retries=MAX_RETRIES, retry_delay=RETRY_DELAY,
    )
    if st not in (201, 200):
        fail_or_skip(st, f"OCI manifest push failed ({tag})")


def blob_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return "sha256:" + h.hexdigest()


def push_blob(file_path: str, token: str, repo: str,
              registry: str = REGISTRY) -> str:
    """Upload one blob (HEAD fast-path -> POST -> PUT) and return its digest.
    Exits via fail_or_skip on HTTP failure (401/403 -> warning + exit 0)."""
    digest = blob_digest(file_path)
    st, _, _ = http_request(
        "HEAD", blob_url(repo, digest, registry),
        headers={"Authorization": f"Bearer {token}"}, timeout=30.0,
    )
    if st == 200:
        return digest
    st, hdrs, _ = http_request(
        "POST", uploads_url(repo, registry),
        headers={"Authorization": f"Bearer {token}"},
        body=b"", timeout=30.0, retries=MAX_RETRIES, retry_delay=RETRY_DELAY,
    )
    if st != 202:
        fail_or_skip(st, "failed to initiate blob upload")
    location = header_value(hdrs, "Location")
    if not location:
        fail_or_skip(0, "no upload location returned by registry")
    put_url = build_put_url(location, registry, digest)
    size = os.path.getsize(file_path)
    with open(file_path, "rb") as f:
        st, _, _ = http_request(
            "PUT", put_url,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/octet-stream",
                     "Content-Length": str(size)},
            body=f, timeout=300.0, retries=MAX_RETRIES, retry_delay=RETRY_DELAY,
        )
    if st not in (201, 202):
        fail_or_skip(st, f"blob upload failed for {digest}")
    return digest


# ----------------------------------------------------------------- nix cli

def nix_key_public(secret_file: str) -> str:
    """`nix key convert-secret-to-public` reading secret_file on stdin."""
    with open(secret_file, "rb") as f:
        p = subprocess.run(["nix", "key", "convert-secret-to-public"],
                           stdin=f, capture_output=True)
    return p.stdout.decode().strip() if p.returncode == 0 else ""


def nix_hash_file(path: str) -> str:
    """FileHash (bare nix-base32); fallback to nix-hash exactly like bash."""
    p = subprocess.run(["nix", "hash", "file", "--type", "sha256", "--base32", path],
                       capture_output=True, text=True)
    if p.returncode == 0:
        return p.stdout.strip()
    p = subprocess.run(["nix-hash", "--flat", "--type", "sha256", "--base32", path],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def nix_hash_convert(h: str) -> str:
    p = subprocess.run(["nix", "hash", "convert", "--to", "base32", h],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode,
                                            ["nix", "hash", "convert", "--to", "base32", h])
    return p.stdout.strip()


def to_base32(h: str, convert=nix_hash_convert) -> str:
    """SRI (sha256-<b64>) -> bare nix-base32; others pass through."""
    if h.startswith("sha256-"):
        return convert(h)
    return h


def nix_path_info(paths, recursive: bool = False):
    """Run `nix path-info --json --json-format 1` for a batch of paths.
    Returns (stdout, stderr); the caller handles non-zero returncodes."""
    cmd = ["nix", "path-info"]
    if recursive:
        cmd.append("--recursive")
    cmd += ["--json", "--json-format", "1", "--", *paths]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p


def closure_paths(data):
    """paths from `nix path-info --recursive --json` — array or map form
    (jq 'if type=="array" then . else to_entries|... end | .[] | .path')."""
    if isinstance(data, list):
        out = []
        for it in data:
            if not isinstance(it, dict) or "path" not in it:
                raise ValueError("unexpected path-info array element")
            out.append(it["path"])
        return out
    if isinstance(data, dict):
        return list(data.keys())
    raise ValueError("unexpected path-info JSON")


def path_info_rows(data):
    """(path, signatures) rows from path-info JSON (array or map form,
    jq '.[] | [.path, ((.signatures // []) | join(" "))] | @tsv')."""
    if isinstance(data, list):
        rows = []
        for it in data:
            if not isinstance(it, dict) or "path" not in it:
                raise ValueError("unexpected path-info array element")
            rows.append((it.get("path", ""), list(it.get("signatures", []) or [])))
        return rows
    if isinstance(data, dict):
        rows = []
        for key, val in data.items():
            if not isinstance(val, dict):
                raise ValueError("unexpected path-info map value")
            rows.append((key, list(val.get("signatures", []) or [])))
        return rows
    raise ValueError("unexpected path-info JSON")


def expand_closure_batch(batch):
    """One xargs -n 64 batch: (paths, batch_failed).  Batch failure = nix
    non-zero exit, non-empty stderr, or unparseable JSON (the bash version
    surfaced all of these through the captured stderr + `jq` failure)."""
    p = nix_path_info(batch, recursive=True)
    failed = p.returncode != 0
    paths = []
    if p.returncode == 0:
        try:
            paths = closure_paths(json.loads(p.stdout))
        except (ValueError, json.JSONDecodeError):
            failed = True
    if not failed and p.stderr.strip():
        failed = True
    return paths, failed


def sign_paths(key_file: str, paths, batch: int = STD_BATCH) -> bool:
    """`nix store sign --key-file` for all paths; True iff every batch worked
    (xargs runs all units; overall status is the failure any unit produced)."""
    ok = True
    for b in chunks(paths, batch):
        p = subprocess.run(["nix", "store", "sign", "--key-file", key_file, *b],
                           capture_output=True)
        if p.returncode != 0:
            ok = False
    return ok


def store_scan_candidates():
    """Authoritative whole-store enumeration via `nix path-info --all`;
    on failure warn and fall back to the old trailing-slash glob scan."""
    p = subprocess.run(["nix", "path-info", "--all", "--json", "--json-format", "1"],
                       capture_output=True, text=True)
    if p.returncode == 0:
        try:
            data = json.loads(p.stdout)
            if isinstance(data, dict):
                return list(data.keys())
        except json.JSONDecodeError:
            pass
    warn("nix path-info --all failed; falling back to glob scan")
    out = []
    for d in glob.glob("/nix/store/*/"):
        p = d.rstrip("/")
        if p:
            out.append(p)
    return out


def _lzma_compress_stream(src, dst) -> None:
    comp = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=1)
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            break
        data = comp.compress(chunk)
        if data:
            dst.write(data)
    dst.write(comp.flush())


def _zstd_compress_stream(src, dst) -> None:
    comp = _ZstdCompressor()
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            break
        data = comp.compress(chunk)
        if data:
            dst.write(data)
    dst.write(comp.flush())


def _compress_stream(src, dst) -> None:
    """Stream `src` (bytes-like) into `dst` compressed; `Compression: zstd`
    on Python >= 3.14, xz fallback otherwise (narinfo self-describes)."""
    if _ZstdCompressor is not None:
        _zstd_compress_stream(src, dst)
    else:
        _lzma_compress_stream(src, dst)


def dump_nar(path: str, nar_file: str) -> bool:
    """`nix-store --dump <path>` -> xz, writing `nar_file`; pipefail semantics
    (the dumper must succeed and compression runs in-process)."""
    dumper = subprocess.Popen(["nix-store", "--dump", path],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    with dumper.stdout as src, open(nar_file, "wb") as dst:
        _compress_stream(src, dst)
    return dumper.wait() == 0


# ------------------------------------------------------- narinfo + filtering

class NarinfoSkip(Exception):
    """make_narinfo skip outcomes, mirroring bash exit codes 3/4."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def make_narinfo(store_path: str, hash_prefix: str, file_size: int,
                 file_hash: str, path_info: str, cache_dir: str,
                 convert=nix_hash_convert) -> None:
    """Write `<cache_dir>/<hash_prefix>.narinfo` (bash exit codes: 3 = bad
    NarSize, 4 = empty FileHash/NarHash; unexpected errors raise)."""
    info = json.loads(path_info)
    if isinstance(info, dict):
        info = info.get(store_path) or (next(iter(info.values())) if info else {})
    else:
        info = info[0] if info else {}
    nar_hash = to_base32(info.get("narHash", ""), convert)
    nar_size = int(info.get("narSize", 0))
    if nar_size <= 0:
        warn(f"narSize <= 0 for {store_path}; skipping")
        raise NarinfoSkip(3)
    if not file_hash or not nar_hash:
        # 空 FileHash/NarHash 会毒化 index 条目（入 index 后永不重试）
        warn(f"empty FileHash/NarHash for {store_path}; skipping")
        raise NarinfoSkip(4)
    refs = info.get("references", []) or []
    deriver = info.get("deriver", "")
    sigs = info.get("signatures", info.get("sigs", [])) or []

    lines = [
        "StorePath: " + store_path,
        "URL: nar/" + hash_prefix + ".nar." + COMPRESSION_EXT,
        "Compression: " + COMPRESSION,
        "FileHash: sha256:" + file_hash,
        "FileSize: " + str(file_size),
        "NarHash: sha256:" + nar_hash,
        "NarSize: " + str(nar_size),
    ]
    if refs:
        lines.append("References: " + " ".join(os.path.basename(r) for r in refs))
    if deriver:
        lines.append("Deriver: " + os.path.basename(deriver))
    for sig in sigs:
        lines.append("Sig: " + sig)

    with open(os.path.join(cache_dir, hash_prefix + ".narinfo"), "w") as f:
        f.write("\n".join(lines) + "\n")


def filter_paths(rows, known_entries, own_key_name):
    """(path, signatures) rows -> (keep, missing_own_signature).

    Bash rules: skip paths already in the index; with a key skip paths that
    carry any signature from another cache, keep own-signed, and report paths
    missing the own-key signature as an error; without a key skip everything
    signed.
    """
    keep = []
    missing = []
    for path, sigs in rows:
        h = os.path.basename(path)[:32]
        if h in known_entries:
            continue
        if own_key_name:
            if any(not s.startswith(own_key_name + ":") for s in sigs):
                continue
            if not any(s.startswith(own_key_name + ":") for s in sigs):
                missing.append(path)
                continue
        else:
            if sigs:
                continue
        keep.append(path)
    return keep, missing


# ------------------------------------------------------------- index merge

def parse_existing(text: str) -> dict:
    """Existing cache index JSON; a corrupt/empty blob is treated as empty
    (bash: `json.load or {}` on JSONDecodeError/null)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def load_existing(path: str) -> dict:
    with open(path) as f:
        return parse_existing(f.read())


def new_entries_from_receipts(receipts_path: str, generated: str) -> dict:
    """receipts.jsonl -> {hash: entry} by reading each narinfo file."""
    new_entries = {}
    with open(receipts_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            with open(r["narinfo_file"]) as nf:
                narinfo = nf.read()
            store_path = ""
            for l in narinfo.splitlines():
                if l.startswith("StorePath: "):
                    store_path = l[len("StorePath: "):].strip()
                    break
            name = (
                os.path.basename(store_path).split("-", 1)[-1]
                if store_path else r["hash"]
            )
            new_entries[r["hash"]] = {
                "name": name,
                "narinfo": narinfo,
                "nar_digest": r["nar_digest"],
                "nar_size": int(r["nar_size"]),
                "added": generated,
            }
    return new_entries


def merge_index(existing: dict, new_entries: dict, pubkey: str,
                repo: str, registry: str, generated: str) -> dict:
    """Build the merged cache index (bash: PUBKEY or existing public_key)."""
    index = {
        "version": 1,
        "repo": repo,
        "registry": registry,
        "image": f"{registry}/{repo}/nix-cache",
        "generated": generated,
        "public_key": pubkey or existing.get("public_key", ""),
        "entries": {},
        "gc_roots": [],
    }
    index["entries"].update(existing.get("entries", {}) or {})
    index["entries"].update(new_entries)
    return index


# ------------------------------------------------------------------ cleanup

def cleanup(proxy_pid: str, runner_os: str, work_dir: str, home: str) -> None:
    """EXIT-trap equivalent: kill our proxy, strip nix.conf marker blocks,
    best-effort daemon restart, remove the work directory.  Every action is
    best-effort (bash `|| true`): cleanup must never raise."""

    def best_effort(cmd, capture: bool = False):
        """bash `|| true` equivalent; returns the CompletedProcess or None."""
        try:
            if capture:
                return subprocess.run(cmd, capture_output=True, text=True)
            return subprocess.run(cmd, stderr=subprocess.DEVNULL)
        except OSError:
            return None

    if proxy_pid:
        alive = False
        try:
            os.kill(int(proxy_pid), 0)
            alive = True
        except (OSError, ValueError):
            alive = False
        if alive:
            p = best_effort(["ps", "-p", proxy_pid, "-o", "command="], capture=True)
            if p is not None and PROXY_MARKER in (p.stdout or ""):
                try:
                    os.kill(int(proxy_pid), signal.SIGTERM)
                except OSError:
                    pass
            else:
                warn(f"PID {proxy_pid} is not our proxy; not killing")
    for conf in (f"/etc/nix/nix.conf", f"{home}/.config/nix/nix.conf"):
        if os.path.exists(conf):
            sed = ["sudo", "sed", "-i.bak", f"/^{NIX_CONF_BEGIN}$/,/^{NIX_CONF_END}$/d", conf]
            if conf != "/etc/nix/nix.conf":
                sed = sed[1:]
            best_effort(sed)
            rm = ["sudo", "rm", "-f", conf + ".bak"]
            if conf != "/etc/nix/nix.conf":
                rm = rm[1:]
            best_effort(rm)
    if os.path.exists("/nix/var/nix/daemon-socket"):
        if runner_os == "macOS":
            plist = "/Library/LaunchDaemons/org.nixos.nix-daemon.plist"
            best_effort(["sudo", "launchctl", "unload", plist])
            best_effort(["sudo", "launchctl", "load", "-w", plist])
        else:
            best_effort(["sudo", "systemctl", "restart", "nix-daemon"])
    shutil.rmtree(work_dir, ignore_errors=True)


# ------------------------------------------------------------ step functions

def step_fetch_existing_index(oci_token: str, repo: str, work_dir: str) -> str:
    """Fetch/parse the existing cache-index manifest + index blob.
    Returns the path to the existing-index file; exits 0 (skip round) on
    manifest/blob fetch failure."""
    idx_file = os.path.join(work_dir, "existing-index.json")
    with open(idx_file, "w") as f:
        f.write("{}\n")                    # bash: echo '{}'
    st, body = fetch_manifest("cache-index", oci_token, repo)
    if st == 200:
        idx_digest = ""
        try:
            manifest = json.loads(body)
            if isinstance(manifest, dict):
                layers = manifest.get("layers") or []
                if layers and isinstance(layers[0], dict):
                    idx_digest = layers[0].get("digest", "") or ""
        except (ValueError, AttributeError, TypeError):
            idx_digest = ""
        if idx_digest:
            st, _, data = http_request(
                "GET", blob_url(repo, idx_digest),
                headers={"Authorization": f"Bearer {oci_token}"}, timeout=120.0,
            )
            if st != 200:
                warn(f"failed to download existing cache-index blob (HTTP {st}); "
                     "skipping upload")
                sys.exit(0)
            with open(idx_file, "wb") as f:
                f.write(data)
    elif st == 404:
        pass                                # 404 = empty index
    else:
        sys.exit(0)                         # warning printed by fetch_manifest
    return idx_file


def index_public_key(idx_file: str) -> str:
    """`.public_key // ""` over the existing index (jq semantics: non-string
    scalars are rendered as text; invalid JSON -> "")."""
    try:
        with open(idx_file) as f:
            data = json.load(f)
    except (ValueError, OSError):
        return ""
    if isinstance(data, dict):
        v = data.get("public_key", "")
        if v is None:
            return ""
        return v if isinstance(v, str) else str(v)
    return ""


def step_signing_setup(signing_key: str, idx_pubkey, work_dir: str):
    """Verify / derive the signing key (bash exit paths preserved)."""
    if not signing_key:
        if idx_pubkey:
            warn("cache index is signed but no signing_key provided; skipping "
                 "upload (refusing unsigned entries)")
            sys.exit(0)
        return "", ""
    old = os.umask(0o077)                   # bash umask 077 around the key write
    try:
        with open(os.path.join(work_dir, "signing.key"), "w") as f:
            f.write(signing_key + "\n")
    finally:
        os.umask(0o022)                     # bash umask 022 afterwards
    own_key = nix_key_public(os.path.join(work_dir, "signing.key"))
    if not own_key:
        print("::error::cannot derive public key from signing_key", file=sys.stderr)
        sys.exit(1)
    own_key_name = own_key.split(":", 1)[0]
    if idx_pubkey and idx_pubkey != own_key:
        print("::error::index public_key differs from provided signing key "
              "(key rotation is not supported)", file=sys.stderr)
        sys.exit(1)
    return own_key, own_key_name


def step_collect_candidates(paths_input: str):
    """Candidate store paths (paths-mode closure expansion or whole-store
    scan).  Returns the list; exits on invalid input or empty candidates."""
    if paths_input:
        cand = []
        for p in paths_input.split():
            if not STORE_PATH_RE.match(p):
                print(f"::error::invalid store path: {p}", file=sys.stderr)
                sys.exit(1)
            if not os.path.exists(p):
                warn(f"store path not found: {p}; skipping")
                continue
            cand.append(p)
        if cand:
            any_failed = False
            expanded = []
            for batch in chunks(cand, CLOSURE_BATCH):
                paths, failed = expand_closure_batch(batch)
                any_failed = any_failed or failed
                expanded.extend(paths)
            if any_failed:
                warn(f"closure expansion failed for a path in: {paths_input}")
            if expanded:
                cand = sorted(set(expanded))
        return cand
    return store_scan_candidates()


def step_filter_candidates(cand, idx_file: str, own_key_name: str):
    """path-info -> filters, mirroring the bash inline python + `rc=2` error
    handling.  Returns the keep list (exits on signing failure / empty)."""
    rows = []
    for batch in chunks(cand, STD_BATCH):
        p = nix_path_info(batch)
        if p.returncode != 0:
            continue
        try:
            rows.extend(path_info_rows(json.loads(p.stdout)))
        except (ValueError, json.JSONDecodeError):
            continue
    try:
        with open(idx_file) as f:
            index = json.load(f)
        if not isinstance(index, dict):
            raise ValueError("not a JSON object")
        entries = index.get("entries") or {}
        if not isinstance(entries, dict):
            raise ValueError("entries is not a JSON object")
        known = set(entries.keys())
    except (ValueError, OSError):
        print("::error::failed to parse existing cache index", file=sys.stderr)
        sys.exit(1)
    keep, missing = filter_paths(rows, known, own_key_name)
    if missing:
        print(f"::error::signing failed for: {', '.join(missing)}", file=sys.stderr)
        print("::error::one or more paths carry no signature from this cache "
              "after signing", file=sys.stderr)
        sys.exit(1)
    return keep


def step_export_upload(paths, oci_token: str, repo: str, work_dir: str,
                       cache_dir: str):
    """Export + narinfo + blob upload loop.
    Returns (uploaded, skipped, receipts path)."""
    os.makedirs(os.path.join(cache_dir, "nar"), exist_ok=True)
    receipts_path = os.path.join(work_dir, "receipts.jsonl")
    uploaded = 0
    skipped = 0
    for path in paths:
        hash_prefix = os.path.basename(path)[:32]
        nar_file = os.path.join(cache_dir, "nar", hash_prefix + ".nar." + COMPRESSION_EXT)
        remove_file(nar_file)
        print(f"::group::nix/cache export {hash_prefix}", file=sys.stderr)
        if not dump_nar(path, nar_file):
            warn(f"failed to dump {path}; skipping")
            skipped += 1
            print("::endgroup::", file=sys.stderr)
            continue
        size = os.path.getsize(nar_file)
        if size > MAX_NAR_SIZE:
            warn(f"{path} nar missing or exceeds ~10GiB GHCR blob limit; skipping")
            remove_file(nar_file)
            skipped += 1
            print("::endgroup::", file=sys.stderr)
            continue
        file_hash = nix_hash_file(nar_file)
        p = subprocess.run(["nix", "path-info", "--json", "--json-format", "1", path],
                           capture_output=True, text=True)
        path_info = p.stdout if p.returncode == 0 else ""
        if not path_info:
            warn(f"nix path-info failed for {path}; skipping")
            remove_file(nar_file)
            skipped += 1
            print("::endgroup::", file=sys.stderr)
            continue
        try:
            make_narinfo(path, hash_prefix, size, file_hash, path_info, cache_dir)
        except NarinfoSkip as e:
            warn(f"narinfo generation failed for {path} (exit {e.code}); skipping")
            remove_file(nar_file)
            skipped += 1
            print("::endgroup::", file=sys.stderr)
            continue
        except Exception:
            warn(f"narinfo generation failed for {path} (exit 1); skipping")
            remove_file(nar_file)
            skipped += 1
            print("::endgroup::", file=sys.stderr)
            continue
        narinfo_file = os.path.join(cache_dir, hash_prefix + ".narinfo")
        nar_digest = push_blob(nar_file, oci_token, repo)
        with open(receipts_path, "a") as f:
            f.write(json.dumps(
                {"hash": hash_prefix, "narinfo_file": narinfo_file,
                 "nar_digest": nar_digest, "nar_size": size},
                separators=(",", ":")) + "\n")
        uploaded += 1
        remove_file(nar_file)
        print("::endgroup::", file=sys.stderr)
        print(f"uploaded {hash_prefix} ({size} bytes)", file=sys.stderr)
    return uploaded, skipped, receipts_path


def step_rebuild_index(work_dir: str, idx_file: str, receipts_path: str,
                       own_key: str, repo: str) -> dict:
    """Merge receipts into the cache index and write cache-index.json."""
    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    existing = load_existing(idx_file)
    new_entries = new_entries_from_receipts(receipts_path, generated)
    index = merge_index(existing, new_entries, own_key, repo, REGISTRY, generated)
    index_json = os.path.join(work_dir, "cache-index.json")
    with open(index_json, "w") as f:
        json.dump(index, f, indent=2, sort_keys=True)
    print(f"index: {len(index['entries'])} total entries ({len(new_entries)} new)")
    return index


def step_push_index(index_json: str, oci_token: str, repo: str,
                    work_dir: str) -> str:
    """Push index blob + config blob + manifest.  Returns index digest."""
    index_digest = push_blob(index_json, oci_token, repo)
    config_file = os.path.join(work_dir, "config.json")
    with open(config_file, "w") as f:
        f.write("{}\n")                     # bash: echo '{}'
    config_digest = push_blob(config_file, oci_token, repo)
    config_size = os.path.getsize(config_file)
    index_size = os.path.getsize(index_json)
    manifest = {
        "schemaVersion": 2,
        "mediaType": MANIFEST_MEDIA_TYPE,
        "config": {
            "mediaType": CONFIG_MEDIA_TYPE,
            "digest": config_digest,
            "size": config_size,
        },
        "layers": [{
            "mediaType": INDEX_MEDIA_TYPE,
            "digest": index_digest,
            "size": index_size,
        }],
    }
    put_manifest("cache-index", json.dumps(manifest, separators=(",", ":")),
                 oci_token, repo)
    return index_digest


def verify_readback(oci_token: str, repo: str, index_digest: str) -> None:
    """Poll the cache-index manifest until its layer digest matches (the
    layer digest is stronger than `generated`)."""
    for _ in range(READBACK_TRIES):
        st, body = fetch_manifest("cache-index", oci_token, repo, quiet=True)
        if st == 200 and layer_digest(body) == index_digest:
            return
        time.sleep(READBACK_SLEEP)
    print("::error::cache-index manifest readback did not confirm the new index "
          "(blobs uploaded, index not visible yet)", file=sys.stderr)
    sys.exit(1)


def layer_digest(manifest_body) -> str:
    try:
        manifest = json.loads(manifest_body)
        if not isinstance(manifest, dict):
            return ""
        layers = manifest.get("layers") or []
        if layers and isinstance(layers[0], dict):
            return layers[0].get("digest", "") or ""
    except (ValueError, AttributeError, TypeError):
        pass
    return ""


def write_summary(summary_path: str, uploaded: int, skipped: int, entries: int) -> None:
    print("::group::nix/cache summary", file=sys.stderr)
    print(f"uploaded paths: {uploaded}", file=sys.stderr)
    print(f"skipped paths: {skipped}", file=sys.stderr)
    print(f"index entries: {entries}", file=sys.stderr)
    print("::endgroup::", file=sys.stderr)
    with open(summary_path or os.devnull, "a") as f:
        f.write("## nix/cache push\n\n")
        f.write("| Metric | Value |\n")
        f.write("|---|---|\n")
        f.write(f"| Paths uploaded | {uploaded} |\n")
        f.write(f"| Paths skipped | {skipped} |\n")
        f.write(f"| Index entries | {entries} |\n")


# --------------------------------------------------------------------- main

def config_from_env(env) -> dict:
    return {
        "repo": (env.get("INPUT_REPO") or env.get("GITHUB_REPOSITORY") or "").lower(),
        "token": env.get("INPUT_TOKEN") or env.get("GITHUB_TOKEN") or "",
        "signing_key": env.get("INPUT_SIGNING_KEY") or "",
        "paths_input": env.get("INPUT_PATHS") or "",
        "proxy_pid": env.get("NIXCACHE_PROXY_PID") or "",
        "runner_temp": env.get("RUNNER_TEMP") or "",
        "runner_os": env.get("RUNNER_OS") or "",
        "summary": env.get("GITHUB_STEP_SUMMARY") or "",
        "home": env.get("HOME") or "",
        # Interface parity with the bash version (read, like PORT, but unused).
        "cache_repo": env.get("NIXCACHE_REPO") or "",
        "port": env.get("NIXCACHE_PORT") or "",
    }


def main(env=None) -> None:
    if sys.version_info < (3, 10):
        print("::error::python >= 3.10 is required "
              f"(found {sys.version_info.major}.{sys.version_info.minor})",
              file=sys.stderr)
        sys.exit(1)
    # bash trap EXIT runs cleanup on SIGTERM too; the try/finally below only
    # sees exceptions, so map SIGTERM to SystemExit(143) (SIGINT already
    # arrives as KeyboardInterrupt inside the try).  SystemExit then unwinds
    # through the finally -> cleanup, and the process exits with 143.
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(143))
    cfg = config_from_env(os.environ if env is None else env)
    work_dir = os.path.join(cfg["runner_temp"] or "/tmp", "nixcache-work")
    cache_dir = os.path.join(work_dir, "cache")
    print(f"::add-mask::{cfg['token']}")
    os.makedirs(work_dir, exist_ok=True)
    if not cfg["runner_temp"]:
        warn("RUNNER_TEMP unset; using /tmp")
    try:
        avail_kb = shutil.disk_usage(work_dir).free // 1024
        if 0 < avail_kb < 5242880:
            warn(f"less than 5GiB free in {work_dir}; large NARs may fail")
    except OSError:
        pass

    try:
        oci_token = oci_get_token(cfg["repo"], cfg["token"])
        idx_file = step_fetch_existing_index(oci_token, cfg["repo"], work_dir)
        idx_pubkey = index_public_key(idx_file)
        own_key, own_key_name = step_signing_setup(
            cfg["signing_key"], idx_pubkey, work_dir)
        cand = step_collect_candidates(cfg["paths_input"])
        if not cand:
            print("Nothing to upload")
            sys.exit(0)
        if cfg["signing_key"]:
            print("::group::nix/cache sign", file=sys.stderr)
            if not sign_paths(os.path.join(work_dir, "signing.key"), cand):
                print("::error::nix store sign failed", file=sys.stderr)
                sys.exit(1)
            print("::endgroup::", file=sys.stderr)
        keep = step_filter_candidates(cand, idx_file, own_key_name)
        if not keep:
            print("Nothing to upload")
            sys.exit(0)
        uploaded, skipped, receipts_path = step_export_upload(
            keep, oci_token, cfg["repo"], work_dir, cache_dir)
        if uploaded == 0:
            print("Nothing new to upload")
            sys.exit(0)
        index = step_rebuild_index(work_dir, idx_file, receipts_path,
                                   own_key, cfg["repo"])
        index_digest = step_push_index(
            os.path.join(work_dir, "cache-index.json"), oci_token,
            cfg["repo"], work_dir)
        verify_readback(oci_token, cfg["repo"], index_digest)
        write_summary(cfg["summary"], uploaded, skipped, len(index["entries"]))
    finally:
        cleanup(cfg["proxy_pid"], cfg["runner_os"], work_dir, cfg["home"])


if __name__ == "__main__":
    main()
