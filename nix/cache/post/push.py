#!/usr/bin/env python3
"""Push locally built Nix store paths to a GHCR OCI binary cache.

NIXCACHE_REPO comes from nix/cache via GITHUB_ENV; NIXCACHE_SIGNING_KEY /
NIXCACHE_PATHS are action inputs; GITHUB_TOKEN is passed by the action (not
a default env var); RUNNER_TEMP.
"""
import base64
import contextlib
import hashlib
import http.client
import json
import lzma
import os
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass

REGISTRY = "ghcr.io"
MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
INDEX_MEDIA_TYPE = "application/vnd.nix.cache.index.v1+json"

STORE_PATH_RE = re.compile(r"^/nix/store/[a-z0-9]{32}-")
CHUNK = 1 << 20                     # 1 MiB

try:  # compression.zstd is stdlib from Python 3.14; older versions use xz
    from compression.zstd import ZstdCompressor as _ZstdCompressor
    COMPRESSION = "zstd"
    COMPRESSION_EXT = "zst"
except ImportError:
    _ZstdCompressor = None
    COMPRESSION = "xz"
    COMPRESSION_EXT = "xz"
MAX_NAR_SIZE = 10737418240          # ~10 GiB GHCR layer limit
MAX_RETRIES = 3
RETRY_DELAY = 2
CLOSURE_BATCH = 64                  # closure expansion batch size (ARG_MAX)
STD_BATCH = 128                     # path-info / signing batch size (ARG_MAX)
READBACK_TRIES = 30
READBACK_SLEEP = 2
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 5


# ------------------------------------------------------------ control flow

class SkipRound(Exception):
    """Abort the whole round as a warning (exit 0)."""


class Fatal(Exception):
    """Abort with an error (exit 1)."""


class SkipPath(Exception):
    """Skip one store path (warn + continue)."""


class NixError(Exception):
    """`nix` command failed or returned unparseable JSON."""


def fail_or_skip(code: int, msg: str) -> None:
    """401/403 -> SkipRound; anything else -> Fatal."""
    if code in (401, 403):
        raise SkipRound(
            f"{msg} (HTTP {code}: insufficient permission; fork PRs and "
            "missing packages:* permissions are skipped)")
    raise Fatal(f"{msg} (HTTP {code})")


# ------------------------------------------------------------ small helpers

def warn(msg: str) -> None:
    print(f"::warning::{msg}", file=sys.stderr)


def notice(msg: str) -> None:
    print(f"::notice::{msg}", file=sys.stderr)


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


@contextlib.contextmanager
def log_group(name: str):
    print(f"::group::{name}", file=sys.stderr)
    try:
        yield
    finally:
        print("::endgroup::", file=sys.stderr)


def header_value(headers, name: str) -> str:
    for k, v in headers or []:
        if k.lower() == name.lower():
            return v
    return ""


# --------------------------------------------------------------------- nix

def nix(*args, input_text: str = None) -> subprocess.CompletedProcess:
    """Run `nix ...`; NixError on failure; `input_text` feeds stdin."""
    p = subprocess.run(["nix", *args], capture_output=True, text=True,
                       input=input_text)
    if p.returncode != 0:
        stderr = (p.stderr or "").strip()
        cmd = " ".join(args)
        raise NixError(f"`nix {cmd}` failed: {stderr}" if stderr
                       else f"`nix {cmd}` failed")
    return p


def nix_json(*args):
    """`nix ... --json` output parsed; NixError on failure or bad JSON."""
    p = nix(*args)
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise NixError(f"`nix {' '.join(args)}` returned unparseable JSON") \
            from None


def nix_key_public(secret: str) -> str:
    try:
        return nix("key", "convert-secret-to-public",
                   input_text=secret).stdout.strip()
    except NixError:
        return ""


def nix_hash_convert(h: str) -> str:
    try:
        return nix("hash", "convert", "--to", "base32", h).stdout.strip()
    except NixError as e:
        raise ValueError(e) from None


def to_base32(h: str, convert=nix_hash_convert) -> str:
    """SRI (sha256-<b64>) -> bare nix-base32; others pass through."""
    if h.startswith("sha256-"):
        return convert(h)
    return h


def path_info_items(data):
    """(path, info-dict) pairs from `nix path-info --json --json-format 1`
    (always a map keyed by store path)."""
    if not isinstance(data, dict):
        raise ValueError("unexpected path-info JSON")
    for path, info in data.items():
        if not isinstance(info, dict):
            raise ValueError("unexpected path-info map value")
        yield path, info


def expand_closure_batch(batch):
    try:
        data = nix_json("path-info", "--recursive", "--json",
                        "--json-format", "1", "--", *batch)
        paths = [path for path, _ in path_info_items(data)]
    except (NixError, ValueError):
        return [], True
    return paths, False


def sign_paths(key_file: str, paths) -> None:
    for batch in chunks(paths, STD_BATCH):
        try:
            nix("store", "sign", "--key-file", key_file, *batch)
        except NixError as e:
            raise Fatal(f"nix store sign failed ({e})") from None


def store_scan_candidates():
    try:
        return [path for path, _ in path_info_items(
            nix_json("path-info", "--all", "--json", "--json-format", "1"))]
    except ValueError as e:
        raise Fatal(f"unexpected `nix path-info --all` output ({e})") from None


# --------------------------------------------------------------------- HTTP
# Hand-rolled on purpose: urllib.request cannot replace this transport.
# Its HTTPRedirectHandler refuses to follow 307/308 for PUT/POST
# (https://bugs.python.org/issue47150), drops the request body on any
# redirect it does follow, and never strips Authorization when a redirect
# leaves the host.  GHCR blob uploads redirect (307) to a storage host, so
# following with the body replayed and without the bearer token is exactly
# what this layer must do.

def http_request(method, url, headers=None, body=None, timeout=30.0, retries=0):
    """One request with redirects and retries; returns (status, headers,
    body).  `body` may be bytes or a seekable file object replayed from 0.
    GET/HEAD follow any 30x, PUT/POST only 307/308; Authorization is dropped
    when a redirect leaves the host; 408/429/5xx and transport errors are
    retried, a persistent transport failure returns (0, [], b'')."""
    attempt = 0
    while True:
        try:
            status, hdrs, data = _send_request(method, url, headers or {},
                                               body, timeout)
            if (status >= 500 or status in (408, 429)) and attempt < retries:
                attempt += 1
                time.sleep(RETRY_DELAY)
                continue
            return status, hdrs, data
        except (OSError, http.client.HTTPException):
            if attempt < retries:
                attempt += 1
                time.sleep(RETRY_DELAY)
                continue
            return 0, [], b""


def _send_request(method, url, headers, body, timeout=30.0):
    """One transfer: a request plus redirects; transport errors propagate
    to the retry loop."""
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


def token_url(repo: str) -> str:
    scope = f"repository:{repo}/nix-cache:pull,push"
    return f"https://{REGISTRY}/token?scope={scope}&service={REGISTRY}"


def manifest_url(repo: str, tag: str) -> str:
    return f"https://{REGISTRY}/v2/{repo}/nix-cache/manifests/{tag}"


def blob_url(repo: str, digest: str) -> str:
    return f"https://{REGISTRY}/v2/{repo}/nix-cache/blobs/{digest}"


def uploads_url(repo: str) -> str:
    return f"https://{REGISTRY}/v2/{repo}/nix-cache/blobs/uploads/"


def build_put_url(location: str, digest: str) -> str:
    """Upload Location -> final PUT URL (`?`/`&` separator, `digest=` query)."""
    url = f"https://{REGISTRY}{location}" if location.startswith("/") \
        else location
    return f"{url}{'&' if '?' in url else '?'}digest={digest}"


def oci_get_token(repo: str, token: str) -> str:
    if not token:
        raise Fatal("GITHUB_TOKEN is unset; a composite action must pass it "
                    "explicitly as GITHUB_TOKEN: ${{ github.token }}")
    auth = "Basic " + base64.b64encode(f"token:{token}".encode()).decode()
    st, _, body = http_request(
        "GET", token_url(repo),
        headers={"Authorization": auth},
        timeout=30.0, retries=MAX_RETRIES,
    )
    if st == 200:
        try:
            oci_token = (json.loads(body) or {}).get("token", "") or ""
        except ValueError:
            oci_token = ""
        if oci_token:
            return oci_token
    raise Fatal(f"failed to obtain GHCR registry token (HTTP {st}, "
                f"scope: repository:{repo}/nix-cache:pull,push)")


def fetch_manifest(tag: str, token: str, repo: str) -> tuple:
    """GET the manifest; 404 = not found (empty index), body empty on any
    non-200."""
    st, _, body = http_request(
        "GET", manifest_url(repo, tag),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": MANIFEST_MEDIA_TYPE},
        timeout=30.0,
    )
    return st, body if st == 200 else b""


def put_manifest(tag: str, manifest_body, token: str, repo: str) -> None:
    body = manifest_body.encode() if isinstance(manifest_body, str) else manifest_body
    st, _, _ = http_request(
        "PUT", manifest_url(repo, tag),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": MANIFEST_MEDIA_TYPE},
        body=body, timeout=60.0, retries=MAX_RETRIES,
    )
    if st not in (201, 200):
        fail_or_skip(st, f"OCI manifest push failed ({tag})")


def layer_digest(manifest_body) -> str:
    try:
        layers = json.loads(manifest_body).get("layers") or []
        return layers[0].get("digest") or ""
    except (ValueError, AttributeError, TypeError, IndexError):
        return ""


def blob_digest(path: str) -> str:
    """`sha256:<hex>` of a file; the single hash pass, also feeding FileHash."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return "sha256:" + h.hexdigest()


def push_blob(file_path: str, digest: str, token: str, repo: str) -> None:
    """Upload one blob (HEAD fast-path -> POST -> PUT)."""
    st, _, _ = http_request(
        "HEAD", blob_url(repo, digest),
        headers={"Authorization": f"Bearer {token}"}, timeout=30.0,
    )
    if st == 200:
        return
    st, hdrs, _ = http_request(
        "POST", uploads_url(repo),
        headers={"Authorization": f"Bearer {token}"},
        body=b"", timeout=30.0, retries=MAX_RETRIES,
    )
    if st != 202:
        fail_or_skip(st, "failed to initiate blob upload")
    location = header_value(hdrs, "Location")
    if not location:
        fail_or_skip(0, "no upload location returned by registry")
    put_url = build_put_url(location, digest)
    size = os.path.getsize(file_path)
    with open(file_path, "rb") as f:
        st, _, _ = http_request(
            "PUT", put_url,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/octet-stream",
                     "Content-Length": str(size)},
            body=f, timeout=300.0, retries=MAX_RETRIES,
        )
    if st not in (201, 202):
        fail_or_skip(st, f"blob upload failed for {digest}")


# ------------------------------------------------------------------ export

def _compress_stream(src, dst) -> None:
    """Stream `src` into `dst`."""
    comp = (_ZstdCompressor() if _ZstdCompressor is not None
            else lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=1))
    while chunk := src.read(CHUNK):
        if data := comp.compress(chunk):
            dst.write(data)
    dst.write(comp.flush())


def dump_nar(path: str, nar_file: str) -> bool:
    """`nix-store --dump <path>` into `nar_file`; False if the dumper
    failed."""
    dumper = subprocess.Popen(["nix-store", "--dump", path],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    with dumper.stdout as src, open(nar_file, "wb") as dst:
        _compress_stream(src, dst)
    return dumper.wait() == 0


# ------------------------------------------------------- narinfo + filtering

def make_narinfo(store_path: str, hash_prefix: str, file_size: int,
                 file_hash: str, info: dict,
                 convert=nix_hash_convert) -> str:
    """Render one narinfo from a path-info dict; SkipPath for paths that
    must not be uploaded."""
    nar_hash = to_base32(info.get("narHash", ""), convert)
    nar_size = int(info.get("narSize", 0))
    if nar_size <= 0:
        raise SkipPath(f"narSize <= 0 for {store_path}")
    if not file_hash or not nar_hash:
        # empty hashes would poison the index entry (never retried)
        raise SkipPath(f"empty FileHash/NarHash for {store_path}")
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

    return "\n".join(lines) + "\n"


def filter_paths(rows, known_entries, own_key_name):
    """(path, signatures) rows -> (keep, missing_own_signature)."""
    keep = []
    missing = []
    for path, sigs in rows:
        if os.path.basename(path)[:32] in known_entries:
            continue
        if own_key_name:
            if any(not s.startswith(own_key_name + ":") for s in sigs):
                continue                     # another cache signed it
            if not sigs:
                missing.append(path)         # signing did not take effect
                continue
        elif sigs:
            continue
        keep.append(path)
    return keep, missing


# ------------------------------------------------------------- index merge

def load_existing_index(data) -> dict:
    """Parse the cache-index blob; corrupt -> Fatal; `entries` validated at
    this boundary."""
    try:
        index = json.loads(data)
    except ValueError:
        index = None
    if not isinstance(index, dict) \
            or not isinstance(index.get("entries") or {}, dict):
        raise Fatal("failed to parse existing cache index")
    return index


def merge_index(existing: dict, new_entries: dict, pubkey: str,
                repo: str, registry: str, generated: str) -> dict:
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


def index_public_key(index: dict) -> str:
    return str(index.get("public_key") or "")


# --------------------------------------------------------------------- flow

def fetch_existing_index(token: str, repo: str) -> dict:
    """Existing cache-index dict ({} when empty); SkipRound when unavailable."""
    st, body = fetch_manifest("cache-index", token, repo)
    if st == 200:
        idx_digest = layer_digest(body)
        if idx_digest:
            st, _, data = http_request(
                "GET", blob_url(repo, idx_digest),
                headers={"Authorization": f"Bearer {token}"}, timeout=120.0,
            )
            if st != 200:
                raise SkipRound(
                    f"failed to download existing cache-index blob "
                    f"(HTTP {st}); skipping upload")
            return load_existing_index(data)
        return {}
    if st == 404:
        return {}
    raise SkipRound(f"failed to fetch OCI manifest cache-index (HTTP {st}); "
                    "skipping upload")


def signing_setup(config: "Config", index: dict,
                  work_dir: str) -> tuple:
    """Own (key, key_name) from signing_key; SkipRound/Fatal for a signed
    index without a key / a key mismatch."""
    idx_pubkey = index_public_key(index)
    if not config.signing_key:
        if idx_pubkey:
            raise SkipRound("cache index is signed but no signing_key "
                            "provided; skipping upload (refusing unsigned "
                            "entries)")
        return "", ""
    key_file = os.path.join(work_dir, "signing.key")
    with open(key_file, "w") as f:
        f.write(config.signing_key + "\n")
    os.chmod(key_file, 0o600)
    own_key = nix_key_public(config.signing_key)
    if not own_key:
        raise Fatal("cannot derive public key from signing_key")
    own_key_name = own_key.split(":", 1)[0]
    if idx_pubkey and idx_pubkey != own_key:
        raise Fatal("index public_key differs from provided signing key "
                    "(key rotation is not supported)")
    return own_key, own_key_name


def collect_candidates(paths_input: str) -> list:
    """Candidate paths: closure expansion of paths_input, or whole-store
    scan when empty; Fatal on an invalid store path."""
    if paths_input:
        cand = []
        for p in paths_input.split():
            if not STORE_PATH_RE.match(p):
                raise Fatal(f"invalid store path: {p}")
            if not os.path.exists(p):
                warn(f"store path not found: {p}; skipping")
                continue
            cand.append(p)
        if cand:
            expanded = []
            any_failed = False
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


def filter_candidates(cands, index: dict, own_key_name: str) -> tuple:
    """(keep, info_by_path); info_by_path is reused by the export step (no
    second path-info pass); Fatal on paths left unsigned."""
    rows = []
    info_by_path = {}
    for batch in chunks(cands, STD_BATCH):
        try:
            data = nix_json("path-info", "--json", "--json-format", "1",
                            "--", *batch)
            items = list(path_info_items(data))
        except (NixError, ValueError) as e:
            warn(f"nix path-info failed for a batch; skipping: {e}")
            continue
        for path, info in items:
            rows.append((path, list(info.get("signatures", []) or [])))
            info_by_path[path] = info
    keep, missing = filter_paths(rows, set(index.get("entries") or {}),
                                 own_key_name)
    if missing:
        raise Fatal(f"signing failed for: {', '.join(missing)}; one or more "
                    "paths carry no signature from this cache after signing")
    return keep, info_by_path


def export_upload(paths, info_by_path: dict, token: str, repo: str,
                  cache_dir: str, generated: str) -> tuple:
    """Export + upload loop -> (uploaded, skipped, new_entries for the
    index merge); a SkipPath is warned about and counted as skipped."""
    nar_dir = os.path.join(cache_dir, "nar")
    os.makedirs(nar_dir, exist_ok=True)
    new_entries = {}
    uploaded = 0
    skipped = 0
    for path in paths:
        info = info_by_path.get(path)
        if info is None:
            warn(f"nix path-info missing for {path}; skipping")
            skipped += 1
            continue
        hash_prefix = os.path.basename(path)[:32]
        nar_file = os.path.join(nar_dir, f"{hash_prefix}.nar.{COMPRESSION_EXT}")
        with log_group(f"nix/cache export {hash_prefix}"):
            try:
                if not dump_nar(path, nar_file):
                    raise SkipPath(f"failed to dump {path}")
                size = os.path.getsize(nar_file)
                if size > MAX_NAR_SIZE:
                    raise SkipPath(f"{path} nar exceeds ~10GiB GHCR blob "
                                   "limit")
                nar_digest = blob_digest(nar_file)
                try:
                    narinfo = make_narinfo(path, hash_prefix, size,
                                           nix_hash_convert(nar_digest), info)
                except SkipPath:
                    raise
                except Exception as e:
                    raise SkipPath(
                        f"narinfo generation failed for {path}: {e}") from None
                push_blob(nar_file, nar_digest, token, repo)
                new_entries[hash_prefix] = {
                    "name": os.path.basename(path).split("-", 1)[-1],
                    "narinfo": narinfo,
                    "nar_digest": nar_digest,
                    "nar_size": size,
                    "added": generated,
                }
                uploaded += 1
                print(f"uploaded {hash_prefix} ({size} bytes)", file=sys.stderr)
            except SkipPath as e:
                warn(f"{e}; skipping")
                skipped += 1
            finally:
                with contextlib.suppress(OSError):
                    os.remove(nar_file)
    return uploaded, skipped, new_entries


def rebuild_index(work_dir: str, existing: dict, new_entries: dict,
                  own_key: str, repo: str, generated: str) -> dict:
    index = merge_index(existing, new_entries, own_key, repo, REGISTRY,
                        generated)
    index_json = os.path.join(work_dir, "cache-index.json")
    with open(index_json, "w") as f:
        json.dump(index, f, indent=2, sort_keys=True)
    print(f"index: {len(index['entries'])} total entries "
          f"({len(new_entries)} new)")
    return index


def push_index(index_json: str, token: str, repo: str,
               work_dir: str) -> str:
    """Push index blob + config blob + manifest.  Returns index digest."""
    index_digest = blob_digest(index_json)
    push_blob(index_json, index_digest, token, repo)
    config_file = os.path.join(work_dir, "config.json")
    with open(config_file, "w") as f:
        f.write("{}\n")
    config_digest = blob_digest(config_file)
    push_blob(config_file, config_digest, token, repo)
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
                 token, repo)
    return index_digest


def verify_readback(token: str, repo: str, index_digest: str) -> None:
    for _ in range(READBACK_TRIES):
        st, body = fetch_manifest("cache-index", token, repo)
        if st == 200 and layer_digest(body) == index_digest:
            return
        time.sleep(READBACK_SLEEP)
    raise Fatal("cache-index manifest readback did not confirm the new "
                "index (blobs uploaded, index not visible yet)")


@dataclass
class Config:
    repo: str
    token: str
    signing_key: str
    paths_input: str
    runner_temp: str

    @classmethod
    def from_env(cls, env: dict) -> "Config":
        return cls(
            repo=(env.get("NIXCACHE_REPO") or "").lower(),
            token=env.get("GITHUB_TOKEN") or "",
            signing_key=env.get("NIXCACHE_SIGNING_KEY") or "",
            paths_input=env.get("NIXCACHE_PATHS") or "",
            runner_temp=env.get("RUNNER_TEMP") or "",
        )


def run(config: "Config", work_dir: str, cache_dir: str) -> None:
    token = oci_get_token(config.repo, config.token)
    existing = fetch_existing_index(token, config.repo)
    own_key, own_key_name = signing_setup(config, existing, work_dir)
    cands = collect_candidates(config.paths_input)
    if not cands:
        print("Nothing to upload")
        return
    if config.signing_key:
        with log_group("nix/cache sign"):
            sign_paths(os.path.join(work_dir, "signing.key"), cands)
    keep, info_by_path = filter_candidates(cands, existing, own_key_name)
    if not keep:
        print("Nothing to upload")
        return
    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    uploaded, skipped, new_entries = export_upload(
        keep, info_by_path, token, config.repo, cache_dir, generated)
    if uploaded == 0:
        print("Nothing new to upload")
        return
    index = rebuild_index(work_dir, existing, new_entries, own_key,
                          config.repo, generated)
    index_digest = push_index(os.path.join(work_dir, "cache-index.json"),
                              token, config.repo, work_dir)
    verify_readback(token, config.repo, index_digest)
    notice(f"nix/cache: uploaded {uploaded}, skipped {skipped}, "
           f"index entries {len(index['entries'])}")


def main(env=None) -> None:
    if sys.version_info < (3, 10):
        print("::error::python >= 3.10 is required "
              f"(found {sys.version_info.major}.{sys.version_info.minor})",
              file=sys.stderr)
        sys.exit(1)
    config = Config.from_env(os.environ if env is None else env)
    work_dir = os.path.join(config.runner_temp or "/tmp", "nixcache-work")
    cache_dir = os.path.join(work_dir, "cache")
    os.makedirs(work_dir, exist_ok=True)
    try:
        run(config, work_dir, cache_dir)
    except SkipRound as e:
        warn(str(e))
        sys.exit(0)
    except (Fatal, NixError) as e:
        print(f"::error::{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
