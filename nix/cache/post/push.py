#!/usr/bin/env python3
"""Push locally built Nix store paths to a GHCR OCI binary cache.

NIXCACHE_REPO comes from nix/cache via GITHUB_ENV. NIXCACHE_SIGNING_KEY and
NIXCACHE_PATHS are action inputs. GITHUB_TOKEN is passed by the action (not
a default env var).
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
import tempfile
import time
import urllib.parse
from dataclasses import dataclass

REGISTRY = "ghcr.io"
MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
INDEX_MEDIA_TYPE = "application/vnd.nix.cache.index.v1+json"

STORE_PATH_RE = re.compile(r"^/nix/store/[a-z0-9]{32}-")
CHUNK = 1 << 20                     # 1 MiB

try:
    from compression.zstd import ZstdCompressor as _ZstdCompressor
except ImportError:
    _ZstdCompressor = None

COMPRESSION, COMPRESSION_EXT = (
    ("zstd", "zst") if _ZstdCompressor is not None else ("xz", "xz"))

MAX_NAR_SIZE = 10737418240          # 10 GiB GHCR layer limit
MAX_RETRIES = 3
RETRY_DELAY = 2
CLOSURE_BATCH = 64                  # closure expansion batch size (ARG_MAX)
STD_BATCH = 128                     # path-info / signing batch size (ARG_MAX)
READBACK_TRIES = 30
READBACK_SLEEP = 2
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 5


# ------------------------------------------------------------ control flow

class SkipPush(Exception):
    """Abort the whole push as a warning (exit 0)."""


class PushError(Exception):
    """Abort with an error (exit 1)."""


class SkipPath(Exception):
    """Skip one store path (warn + continue)."""


@dataclass(frozen=True)
class Config:
    repo: str
    github_token: str
    signing_key: str
    paths: str
    runner_temp: str

    @classmethod
    def from_env(cls, env: dict) -> "Config":
        return cls(
            repo=env.get("NIXCACHE_REPO", "").lower(),
            github_token=env.get("GITHUB_TOKEN", ""),
            signing_key=env.get("NIXCACHE_SIGNING_KEY", ""),
            paths=env.get("NIXCACHE_PATHS", ""),
            runner_temp=env.get("RUNNER_TEMP", ""),
        )


@dataclass(frozen=True)
class Summary:
    uploaded: int = 0
    skipped: int = 0
    total: int = 0


def raise_http_error(code: int, msg: str) -> None:
    if code in (401, 403):
        raise SkipPush(
            f"{msg} (HTTP {code}: insufficient permission; fork PRs and "
            "missing packages:* permissions are skipped)")
    raise PushError(f"{msg} (HTTP {code})")


# ------------------------------------------------------------ small helpers

def warn(msg: str) -> None:
    print(f"::warning::{msg}", file=sys.stderr)


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
    """Run `nix ...`; all command failures are fatal to the push."""
    p = subprocess.run(["nix", *args], capture_output=True, text=True,
                       input=input_text)
    if p.returncode != 0:
        stderr = (p.stderr or "").strip()
        cmd = " ".join(args)
        raise PushError(f"`nix {cmd}` failed: {stderr}" if stderr
                        else f"`nix {cmd}` failed")
    return p


def nix_json(*args):
    """Run Nix and parse its JSON output."""
    p = nix(*args)
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise PushError(f"`nix {' '.join(args)}` returned unparseable JSON") \
            from None


def nix_hash_convert(h: str) -> str:
    return nix("hash", "convert", "--to", "base32", h).stdout.strip()


def path_infos(paths=None, recursive=False) -> dict:
    args = ["path-info"]
    if recursive:
        args.append("--recursive")
    if paths is None:
        args.append("--all")
    args += ["--json", "--json-format", "1"]
    if paths is not None:
        args += ["--", *paths]
    data = nix_json(*args)
    if not isinstance(data, dict) or any(
            not isinstance(info, dict) for info in data.values()):
        raise PushError("unexpected `nix path-info` JSON")
    return data


def sign_paths(key_file: str, paths) -> None:
    for batch in chunks(paths, STD_BATCH):
        nix("store", "sign", "--key-file", key_file, *batch)


# --------------------------------------------------------------------- HTTP
# Hand-rolled on purpose: urllib.request cannot replace this transport.
# Its HTTPRedirectHandler refuses to follow 307/308 for PUT/POST
# (https://bugs.python.org/issue47150), drops the request body on any
# redirect it does follow, and never strips Authorization when a redirect
# leaves the host.  GHCR blob uploads redirect (307) to a storage host, so
# following with the body replayed and without the bearer token is exactly
# what this layer must do.

def http_request(method, url, headers=None, body=None, timeout=30.0, retries=0):
    """One request with redirects and retries, returning (status, headers,
    body).  `body` may be bytes or a seekable file object replayed from 0.
    GET/HEAD follow 301/302/303/307/308, PUT/POST only 307/308.
    Authorization is dropped when a redirect leaves the host.  408/429/5xx
    and transport errors are retried, and a persistent transport failure
    returns (0, [], b'')."""
    for attempt in range(retries + 1):
        try:
            response = _send_request(method, url, headers or {}, body,
                                     timeout)
        except (OSError, http.client.HTTPException):
            response = (0, [], b"")
        status = response[0]
        retryable = status == 0 or status >= 500 or status in (408, 429)
        if not retryable or attempt == retries:
            return response
        time.sleep(RETRY_DELAY)


def _send_request(method, url, headers, body, timeout=30.0):
    """One transfer (request plus redirects).  Transport errors propagate
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
        conn = http.client.HTTPSConnection(parts.hostname, port,
                                           timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=current_headers)
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
            hdrs = resp.getheaders()
        finally:
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


def build_put_url(location: str, digest: str) -> str:
    """Upload Location -> final PUT URL (`?`/`&` separator, `digest=` query)."""
    url = f"https://{REGISTRY}{location}" if location.startswith("/") \
        else location
    return f"{url}{'&' if '?' in url else '?'}digest={digest}"


def layer_digest(manifest_body) -> str:
    try:
        digest = json.loads(manifest_body)["layers"][0]["digest"]
    except (ValueError, KeyError, TypeError, IndexError):
        raise PushError("invalid OCI manifest") from None
    if not isinstance(digest, str) or not digest:
        raise PushError("invalid OCI manifest")
    return digest


def blob_digest(path: str) -> str:
    """`sha256:<hex>` of a file.  The single hash pass also feeds FileHash."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return "sha256:" + h.hexdigest()


@dataclass(frozen=True)
class Registry:
    """Authenticated access to one GHCR cache image."""

    repo: str
    token: str

    @classmethod
    def login(cls, repo: str, github_token: str) -> "Registry":
        if not github_token:
            raise PushError(
                "GITHUB_TOKEN is unset; a composite action must pass it "
                "explicitly as GITHUB_TOKEN: ${{ github.token }}")
        basic = base64.b64encode(f"token:{github_token}".encode()).decode()
        status, _, body = http_request(
            "GET", token_url(repo),
            headers={"Authorization": f"Basic {basic}"},
            timeout=30.0, retries=MAX_RETRIES)
        if status != 200:
            raise_http_error(
                status, "failed to obtain GHCR registry token "
                f"(scope: repository:{repo}/nix-cache:pull,push)")
        try:
            token = json.loads(body)["token"]
        except (ValueError, KeyError, TypeError):
            raise PushError("GHCR token response is invalid") from None
        if not isinstance(token, str) or not token:
            raise PushError("GHCR token response is invalid")
        return cls(repo, token)

    def request(self, method: str, target: str, *, headers=None, **kwargs):
        url = target if "://" in target else self.url(target)
        request_headers = dict(headers or {})
        if urllib.parse.urlsplit(url).hostname == REGISTRY:
            request_headers["Authorization"] = f"Bearer {self.token}"
        else:
            request_headers = {
                key: value for key, value in request_headers.items()
                if key.lower() != "authorization"
            }
        return http_request(method, url, headers=request_headers, **kwargs)

    def url(self, path: str) -> str:
        return f"https://{REGISTRY}/v2/{self.repo}/nix-cache/{path}"

    def fetch_manifest(self, tag: str) -> tuple:
        """Return (status, body), with an empty body for non-200 responses."""
        status, _, body = self.request(
            "GET", f"manifests/{tag}",
            headers={"Accept": MANIFEST_MEDIA_TYPE}, timeout=30.0,
        )
        return status, body if status == 200 else b""

    def put_manifest(self, tag: str, body) -> None:
        if isinstance(body, str):
            body = body.encode()
        status, _, _ = self.request(
            "PUT", f"manifests/{tag}",
            headers={"Content-Type": MANIFEST_MEDIA_TYPE}, body=body,
            timeout=60.0, retries=MAX_RETRIES,
        )
        if status not in (200, 201):
            raise_http_error(status, f"OCI manifest push failed ({tag})")

    def fetch_index(self) -> dict:
        """Load the current cache index, or return {} when it does not exist."""
        status, manifest = self.fetch_manifest("cache-index")
        if status == 404:
            return {}
        if status != 200:
            raise_http_error(status, "failed to fetch cache-index manifest")
        digest = layer_digest(manifest)
        status, _, data = self.request(
            "GET", f"blobs/{digest}", timeout=120.0)
        if status != 200:
            raise_http_error(status, "failed to download cache-index blob")
        return parse_index(data)

    def push_blob(self, source, digest: str = "") -> str:
        """Upload bytes or a file unless already present; return its digest."""
        is_file = isinstance(source, (str, os.PathLike))
        if is_file:
            size = os.path.getsize(source)
            digest = digest or blob_digest(source)
        else:
            size = len(source)
            digest = digest or "sha256:" + hashlib.sha256(source).hexdigest()
        status, _, _ = self.request(
            "HEAD", f"blobs/{digest}", timeout=30.0)
        if status == 200:
            return digest
        status, headers, _ = self.request(
            "POST", "blobs/uploads/", body=b"", timeout=30.0,
            retries=MAX_RETRIES)
        if status != 202:
            raise_http_error(status, "failed to initiate blob upload")
        location = header_value(headers, "Location")
        if not location:
            raise PushError("no upload location returned by registry")
        source_context = (open(source, "rb") if is_file
                          else contextlib.nullcontext(source))
        with source_context as body:
            status, _, _ = self.request(
                "PUT", build_put_url(location, digest),
                headers={"Content-Type": "application/octet-stream",
                         "Content-Length": str(size)},
                body=body, timeout=300.0, retries=MAX_RETRIES)
        if status not in (201, 202):
            raise_http_error(status, f"blob upload failed for {digest}")
        return digest

    def publish_index(self, index: dict) -> str:
        """Upload an index and its OCI manifest, returning the index digest."""
        index_body = json.dumps(index, indent=2, sort_keys=True).encode()
        config_body = b"{}\n"
        descriptors = []
        for media_type, body in ((CONFIG_MEDIA_TYPE, config_body),
                                 (INDEX_MEDIA_TYPE, index_body)):
            descriptors.append({
                "mediaType": media_type,
                "digest": self.push_blob(body),
                "size": len(body),
            })
        config, layer = descriptors
        manifest = {
            "schemaVersion": 2,
            "mediaType": MANIFEST_MEDIA_TYPE,
            "config": config,
            "layers": [layer],
        }
        self.put_manifest("cache-index", json.dumps(
            manifest, separators=(",", ":")))
        return layer["digest"]

    def verify(self, index_digest: str) -> None:
        for _ in range(READBACK_TRIES):
            status, body = self.fetch_manifest("cache-index")
            if status == 200:
                try:
                    if layer_digest(body) == index_digest:
                        return
                except PushError:
                    pass
            elif status in (401, 403):
                raise_http_error(status, "cache-index readback failed")
            time.sleep(READBACK_SLEEP)
        raise PushError("cache-index manifest readback did not confirm the "
                        "new index (blobs uploaded, index not visible yet)")


# ------------------------------------------------------------------ export

def _compress_stream(src, dst) -> None:
    """Stream `src` into `dst`."""
    comp = (_ZstdCompressor() if _ZstdCompressor is not None
            else lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=1))
    while chunk := src.read(CHUNK):
        if data := comp.compress(chunk):
            dst.write(data)
    dst.write(comp.flush())


def dump_nar(path: str, nar_file: str) -> None:
    with subprocess.Popen(
            ["nix-store", "--dump", path], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL) as dumper:
        with dumper.stdout as src, open(nar_file, "wb") as dst:
            _compress_stream(src, dst)
    if dumper.returncode != 0:
        raise SkipPath(f"failed to dump {path}")


# ------------------------------------------------------- narinfo + filtering

def make_narinfo(store_path: str, hash_prefix: str, file_size: int,
                 file_hash: str, info: dict) -> str:
    """Render one narinfo from a path-info dict.  Raises SkipPath for paths
    that must not be uploaded."""
    nar_hash = info.get("narHash", "")
    if nar_hash.startswith("sha256-"):
        nar_hash = nix_hash_convert(nar_hash)
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
        f"StorePath: {store_path}",
        f"URL: nar/{hash_prefix}.nar.{COMPRESSION_EXT}",
        f"Compression: {COMPRESSION}",
        f"FileHash: sha256:{file_hash}",
        f"FileSize: {file_size}",
        f"NarHash: sha256:{nar_hash}",
        f"NarSize: {nar_size}",
    ]
    if refs:
        lines.append("References: " + " ".join(map(os.path.basename, refs)))
    if deriver:
        lines.append(f"Deriver: {os.path.basename(deriver)}")
    lines.extend(f"Sig: {sig}" for sig in sigs)

    return "\n".join(lines) + "\n"


def select_paths(infos: dict, index: dict, own_key_name: str) -> list:
    """Select paths not already cached or signed by another cache."""
    known_entries = set(index.get("entries") or {})
    keep = []
    missing = []
    for path, info in infos.items():
        sigs = info.get("signatures", []) or []
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
    if missing:
        raise PushError(
            f"signing failed for: {', '.join(missing)}; one or more paths "
            "carry no signature from this cache after signing")
    return keep


# ------------------------------------------------------------- index merge

def parse_index(data) -> dict:
    """Parse the cache-index blob, raising PushError when corrupt. `entries`
    is validated at this boundary."""
    try:
        index = json.loads(data)
    except ValueError:
        index = None
    if not isinstance(index, dict) or not isinstance(
            index.get("entries", {}), dict):
        raise PushError("failed to parse existing cache index")
    return index


def merge_index(existing: dict, new_entries: dict, pubkey: str,
                repo: str, generated: str) -> dict:
    return {
        "version": 1,
        "repo": repo,
        "registry": REGISTRY,
        "image": f"{REGISTRY}/{repo}/nix-cache",
        "generated": generated,
        "public_key": pubkey or existing.get("public_key", ""),
        "entries": {**(existing.get("entries") or {}), **new_entries},
        "gc_roots": [],
    }


# --------------------------------------------------------------------- flow


def signing_setup(signing_key: str, index: dict, work_dir: str) -> str:
    """Prepare and validate the optional cache signing key."""
    idx_pubkey = str(index.get("public_key") or "")
    if not signing_key:
        if idx_pubkey:
            raise SkipPush("cache index is signed but no signing_key "
                           "provided; skipping upload (refusing unsigned "
                           "entries)")
        return ""
    key_file = os.path.join(work_dir, "signing.key")
    with open(key_file, "w") as f:
        f.write(signing_key + "\n")
    os.chmod(key_file, 0o600)
    own_key = nix("key", "convert-secret-to-public",
                  input_text=signing_key).stdout.strip()
    if not own_key:
        raise PushError("cannot derive public key from signing_key")
    if idx_pubkey and idx_pubkey != own_key:
        raise PushError("index public_key differs from provided signing key "
                        "(key rotation is not supported)")
    return own_key


def load_path_infos(paths, *, recursive=False, batch_size=STD_BATCH) -> dict:
    infos = {}
    for batch in chunks(paths, batch_size):
        infos.update(path_infos(batch, recursive=recursive))
    return infos


def collect_path_infos(paths_input: str) -> dict:
    """Path info for an explicit closure, or for the whole store."""
    if paths_input:
        candidates = []
        for p in paths_input.split():
            if not STORE_PATH_RE.match(p):
                raise PushError(f"invalid store path: {p}")
            if not os.path.exists(p):
                warn(f"store path not found: {p}; skipping")
                continue
            candidates.append(p)
        infos = load_path_infos(
            candidates, recursive=True, batch_size=CLOSURE_BATCH)
        return dict(sorted(infos.items()))
    return path_infos()


def export_one(path: str, hash_prefix: str, info: dict, registry: Registry,
               nar_file: str, generated: str) -> dict:
    """Export and upload one path, returning its index entry."""
    dump_nar(path, nar_file)
    size = os.path.getsize(nar_file)
    if size > MAX_NAR_SIZE:
        raise SkipPath(f"{path} nar exceeds 10GiB GHCR blob limit")

    nar_digest = blob_digest(nar_file)
    try:
        narinfo = make_narinfo(path, hash_prefix, size,
                               nix_hash_convert(nar_digest), info)
    except (PushError, AttributeError, TypeError, ValueError) as e:
        raise SkipPath(
            f"narinfo generation failed for {path}: {e}") from None
    registry.push_blob(nar_file, nar_digest)
    return {
        "name": os.path.basename(path).split("-", 1)[-1],
        "narinfo": narinfo,
        "nar_digest": nar_digest,
        "nar_size": size,
        "added": generated,
    }


def export_paths(paths, info_by_path: dict, registry: Registry,
                 cache_dir: str, generated: str) -> tuple:
    """Export paths, returning (skipped, new_entries for the index merge)."""
    nar_dir = os.path.join(cache_dir, "nar")
    os.makedirs(nar_dir, exist_ok=True)
    new_entries = {}
    skipped = 0
    for path in paths:
        info = info_by_path[path]
        hash_prefix = os.path.basename(path)[:32]
        nar_file = os.path.join(nar_dir, f"{hash_prefix}.nar.{COMPRESSION_EXT}")
        with log_group(f"nix/cache export {hash_prefix}"):
            try:
                entry = export_one(path, hash_prefix, info, registry,
                                   nar_file, generated)
                new_entries[hash_prefix] = entry
                print(f"uploaded {hash_prefix} ({entry['nar_size']} bytes)",
                      file=sys.stderr)
            except SkipPath as e:
                warn(f"{e}; skipping")
                skipped += 1
            finally:
                with contextlib.suppress(OSError):
                    os.remove(nar_file)
    return skipped, new_entries


def run(config: Config, work_dir: str) -> Summary:
    registry = Registry.login(config.repo, config.github_token)
    existing = registry.fetch_index()
    public_key = signing_setup(config.signing_key, existing, work_dir)
    info_by_path = collect_path_infos(config.paths)
    paths = list(info_by_path)
    if not paths:
        print("Nothing to upload")
        return Summary(total=len(existing.get("entries") or {}))
    if public_key:
        with log_group("nix/cache sign"):
            sign_paths(os.path.join(work_dir, "signing.key"), paths)
        info_by_path = load_path_infos(paths)
    key_name = public_key.partition(":")[0]
    keep = select_paths(info_by_path, existing, key_name)
    if not keep:
        print("Nothing to upload")
        return Summary(total=len(existing.get("entries") or {}))
    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    skipped, new_entries = export_paths(
        keep, info_by_path, registry, work_dir, generated)
    if not new_entries:
        print("Nothing new to upload")
        return Summary(skipped=skipped,
                       total=len(existing.get("entries") or {}))
    index = merge_index(existing, new_entries, public_key, config.repo,
                        generated)
    print(f"index: {len(index['entries'])} total entries "
          f"({len(new_entries)} new)")
    index_digest = registry.publish_index(index)
    registry.verify(index_digest)
    return Summary(uploaded=len(new_entries), skipped=skipped,
                   total=len(index["entries"]))


def main(env=None) -> int:
    config = Config.from_env(os.environ if env is None else env)
    try:
        with tempfile.TemporaryDirectory(
                prefix="nixcache-", dir=config.runner_temp or None) as work_dir:
            summary = run(config, work_dir)
    except SkipPush as e:
        warn(str(e))
        return 0
    except (PushError, OSError) as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    if summary.uploaded:
        print(f"::notice::nix/cache: uploaded {summary.uploaded}, "
              f"skipped {summary.skipped}, index entries {summary.total}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
