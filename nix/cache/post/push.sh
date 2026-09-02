#!/usr/bin/env bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${INPUT_REPO:-$GITHUB_REPOSITORY}"
REPO="$(printf '%s' "$REPO" | tr '[:upper:]' '[:lower:]')"
TOKEN="${INPUT_TOKEN:-$GITHUB_TOKEN}"
SIGNING_KEY="${INPUT_SIGNING_KEY:-}"
PATHS_INPUT="${INPUT_PATHS:-}"
PORT="${NIXCACHE_PORT:-}"
PROXY_PID="${NIXCACHE_PROXY_PID:-}"
REGISTRY="ghcr.io"
WORK_DIR="${RUNNER_TEMP:-/tmp}/nixcache-work"
OCI_TOKEN=""
OWN_KEY=""
OWN_KEY_NAME=""
UPLOADED=0
SKIPPED=0

echo "::add-mask::${TOKEN}"
mkdir -p "$WORK_DIR"
[ -n "$RUNNER_TEMP" ] || echo "::warning::RUNNER_TEMP unset; using /tmp"
# 磁盘预检：剩余 < 5GiB 时警告（大 NAR 会打爆 tmpfs/小磁盘）
AVAIL_KB="$(df -Pk "$WORK_DIR" 2>/dev/null | awk 'NR == 2 {print $4}' || echo 0)"
if [ "${AVAIL_KB:-0}" -gt 0 ] && [ "${AVAIL_KB:-0}" -lt 5242880 ]; then
  echo "::warning::less than 5GiB free in $WORK_DIR; large NARs may fail"
fi

cleanup() {
  # stop the proxy started by nix/cache (only if it is really ours)
  if [ -n "$PROXY_PID" ] && kill -0 "$PROXY_PID" 2>/dev/null; then
    if ps -p "$PROXY_PID" -o command= 2>/dev/null | grep -q "nixcache-proxy.py"; then
      kill "$PROXY_PID" 2>/dev/null || true
    else
      echo "::warning::PID $PROXY_PID is not our proxy; not killing" >&2
    fi
  fi
  # remove nix.conf marker blocks (idempotency across self-hosted runs)
  for conf in "/etc/nix/nix.conf" "${HOME}/.config/nix/nix.conf"; do
    if [ -e "$conf" ]; then
      if [ "$conf" = "/etc/nix/nix.conf" ]; then
        sudo sed -i.bak '/^# nix-cache begin$/,/^# nix-cache end$/d' "$conf" 2>/dev/null || true
        sudo rm -f "$conf.bak" 2>/dev/null || true
      else
        sed -i.bak '/^# nix-cache begin$/,/^# nix-cache end$/d' "$conf" 2>/dev/null || true
        rm -f "$conf.bak" 2>/dev/null || true
      fi
    fi
  done
  rm -rf "$WORK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

# 401/403（权限不足：fork PR、缺 packages:*）→ 警告+跳过；其余 → error+退出。
# 注意：所有消息必须走 stderr（本函数可能在输出重定向的普通函数调用里被调用，
# 也可能在命令替换里被调用；stdout 属于数据通道）。
fail_or_skip() { # code, message
  local code="$1" msg="$2"
  if [ "$code" = 401 ] || [ "$code" = 403 ]; then
    echo "::warning::$msg (HTTP $code: insufficient permission; fork PRs and missing packages:* permissions are skipped)" >&2
    exit 0
  fi
  echo "::error::$msg (HTTP $code)" >&2
  exit 1
}

oci_get_token() {
  local scope="repository:${REPO}/nix-cache:pull,push"
  local b64 resp
  b64="$(printf 'token:%s' "$TOKEN" | base64 | tr -d '\n')"
  resp="$(curl -fsS --max-time 30 --retry 3 --retry-all-errors --retry-delay 2 \
    -H "Authorization: Basic ${b64}" \
    "https://${REGISTRY}/token?scope=${scope}&service=${REGISTRY}" 2>/dev/null || true)"
  OCI_TOKEN="$(printf '%s' "$resp" | jq -r '.token // empty' 2>/dev/null || true)"
  if [ -z "$OCI_TOKEN" ]; then
    echo "::error::failed to obtain GHCR registry token (scope: $scope)" >&2
    exit 1
  fi
}

fetch_manifest() { # tag outfile -> 0 ok, 1 not found, 2 other error
  local tag="$1" out="$2" code
  code="$(curl -sS -o "$out" -w '%{http_code}' --max-time 30 \
    -H "Authorization: Bearer $OCI_TOKEN" \
    -H "Accept: application/vnd.oci.image.manifest.v1+json" \
    "https://${REGISTRY}/v2/${REPO}/nix-cache/manifests/${tag}" 2>/dev/null || true)"
  if [ "$code" = 200 ]; then return 0; fi
  if [ "$code" = 404 ]; then return 1; fi
  echo "::warning::failed to fetch OCI manifest $tag (HTTP $code); skipping upload" >&2
  return 2
}

put_manifest() { # tag body
  local tag="$1" body="$2" code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 60 --retry 3 --retry-all-errors --retry-delay 2 -X PUT \
    -H "Authorization: Bearer $OCI_TOKEN" \
    -H "Content-Type: application/vnd.oci.image.manifest.v1+json" \
    --data-binary "$body" \
    "https://${REGISTRY}/v2/${REPO}/nix-cache/manifests/${tag}" 2>/dev/null || true)"
  if [ "$code" != 201 ] && [ "$code" != 200 ]; then
    fail_or_skip "$code" "OCI manifest push failed ($tag)"
  fi
}
# push_blob <file> <outfile>：成功 → digest 写入 outfile；
# 401/403 → 警告+跳过（exit 0，正常函数调用，非命令替换子 shell）；
# 其他失败 → error+exit 1。
push_blob() { # file, outfile
  local file="$1" out="$2"
  local digest size code headers status tmpurl sep
  digest="sha256:$(python3 -c '
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    while True:
        b = f.read(1 << 20)
        if not b:
            break
        h.update(b)
print(h.hexdigest())
' "$file")"
  size="$(wc -c <"$file" | tr -d ' ')"
  # HEAD 检查（-I）：blob 已存在时绝不下载内容
  code="$(curl -sSI -o /dev/null -w '%{http_code}' --max-time 30 \
    -H "Authorization: Bearer $OCI_TOKEN" \
    "https://${REGISTRY}/v2/${REPO}/nix-cache/blobs/$digest" 2>/dev/null || true)"
  if [ "$code" = 200 ]; then
    printf '%s\n' "$digest" >"$out"
    return 0
  fi
  headers="$(mktemp)"
  status="$(curl -sS -D "$headers" -o /dev/null -w '%{http_code}' --max-time 30 --retry 3 --retry-all-errors --retry-delay 2 -X POST \
    -H "Authorization: Bearer $OCI_TOKEN" \
    "https://${REGISTRY}/v2/${REPO}/nix-cache/blobs/uploads/" 2>/dev/null || true)"
  if [ "$status" != 202 ]; then
    rm -f "$headers"
    fail_or_skip "$status" "failed to initiate blob upload"
  fi
  tmpurl="$(grep -i '^location:' "$headers" 2>/dev/null | head -1 | tr -d '\r' | sed 's/^[Ll]ocation: *//' || true)"
  rm -f "$headers"
  case "$tmpurl" in
  /*) tmpurl="https://${REGISTRY}${tmpurl}" ;;
  esac
  if [ -z "$tmpurl" ]; then
    fail_or_skip 0 "no upload location returned by registry"
  fi
  sep="?"
  case "$tmpurl" in
  *\?*) sep="&" ;;
  esac
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 300 --retry 3 --retry-all-errors --retry-delay 2 -X PUT \
    -H "Authorization: Bearer $OCI_TOKEN" \
    -H "Content-Type: application/octet-stream" \
    --data-binary "@$file" \
    "${tmpurl}${sep}digest=${digest}" 2>/dev/null || true)"
  if [ "$code" != 201 ] && [ "$code" != 202 ]; then
    fail_or_skip "$code" "blob upload failed for $digest"
  fi
  printf '%s\n' "$digest" >"$out"
}
# ---- 1. fetch existing index ----
oci_get_token
IDX_FILE="$WORK_DIR/existing-index.json"
echo '{}' >"$IDX_FILE"
if fetch_manifest cache-index "$WORK_DIR/cache-index-manifest.json"; then
  IDX_DIGEST="$(jq -r '.layers[0].digest // empty' "$WORK_DIR/cache-index-manifest.json" 2>/dev/null)"
  if [ -n "$IDX_DIGEST" ]; then
    code="$(curl -sS -o "$IDX_FILE" -w '%{http_code}' --max-time 120 \
      -H "Authorization: Bearer $OCI_TOKEN" \
      "https://${REGISTRY}/v2/${REPO}/nix-cache/blobs/${IDX_DIGEST}" 2>/dev/null || true)"
    if [ "$code" != 200 ]; then
      echo "::warning::failed to download existing cache-index blob (HTTP $code); skipping upload" >&2
      exit 0
    fi
  fi
else
  rc=$?
  [ "$rc" = 1 ] || exit 0  # 404 = 空 index；其他错误已打印 warning，跳过本轮
fi

IDX_PUBKEY="$(jq -r '.public_key // ""' "$IDX_FILE")"

# ---- 2. signing setup + guards ----
if [ -n "$SIGNING_KEY" ]; then
  umask 077
  printf '%s\n' "$SIGNING_KEY" >"$WORK_DIR/signing.key"
  umask 022
  OWN_KEY="$(nix key convert-secret-to-public <"$WORK_DIR/signing.key" 2>/dev/null || true)"
  if [ -z "$OWN_KEY" ]; then
    echo "::error::cannot derive public key from signing_key" >&2
    exit 1
  fi
  OWN_KEY_NAME="$(printf '%s' "$OWN_KEY" | cut -d: -f1)"
  if [ -n "$IDX_PUBKEY" ] && [ "$IDX_PUBKEY" != "$OWN_KEY" ]; then
    echo "::error::index public_key differs from provided signing key (key rotation is not supported)" >&2
    exit 1
  fi
else
  if [ -n "$IDX_PUBKEY" ]; then
    echo "::warning::cache index is signed but no signing_key provided; skipping upload (refusing unsigned entries)" >&2
    exit 0
  fi
fi

# ---- 3. candidate paths ----
CAND="$WORK_DIR/cand-paths"
: >"$CAND"
if [ -n "$PATHS_INPUT" ]; then
  # 校验：完整 store 路径前缀（32 位 [a-z0-9] hash + "-"）
  for p in $PATHS_INPUT; do
    if ! printf '%s' "$p" | grep -Eq '^/nix/store/[a-z0-9]{32}-'; then
      echo "::error::invalid store path: $p" >&2
      exit 1
    fi
    echo "$p" >>"$CAND"
  done
  # 展开闭包（spec 要求 --recursive）；macOS 无 xargs -a → stdin 重定向；
  # POSIX sh -c（禁 bashism）；-- 结束选项解析
  xargs -n 64 sh -c 'nix path-info --recursive --json --json-format 1 -- "$@" 2>/dev/null | jq -r "if type==\"array\" then . else to_entries|map({path:.key}+.value) end | .[] | .path"' _ <"$CAND" \
    | sort -u >"$CAND.closure" 2>/dev/null || true
  if [ -s "$CAND.closure" ]; then
    mv "$CAND.closure" "$CAND"
  fi
else
  shopt -s nullglob
  printf '%s\n' /nix/store/*/ | sed 's#/$##' | awk 'length > 0' >"$CAND"
fi
if [ ! -s "$CAND" ]; then
  echo "Nothing to upload"
  exit 0
fi

# ---- 4. sign (if key) ----
if [ -n "$SIGNING_KEY" ]; then
  echo "::group::nix/cache sign" >&2
  # POSIX：$1=keyfile，$2..=paths；禁用 xargs -a 与 ${@:2}
  if ! xargs -n 128 sh -c 'key="$1"; shift; nix store sign --key-file "$key" "$@"' _ "$WORK_DIR/signing.key" <"$CAND" 2>/dev/null; then
    echo "::error::nix store sign failed" >&2
    exit 1
  fi
  echo "::endgroup::" >&2
fi

# ---- 5. path-info (with signatures) -> TSV, then filter ----
PIN="$WORK_DIR/pathinfo.tsv"
: >"$PIN"
xargs -n 128 sh -c 'nix path-info --json --json-format 1 -- "$@" 2>/dev/null | jq -r '\''if type=="array" then . else to_entries|map({path:.key}+.value) end | .[] | [.path, ((.signatures // []) | join(" "))] | @tsv'\''' _ <"$CAND" >>"$PIN" 2>/dev/null || true

python3 - "$PIN" "$IDX_FILE" "$OWN_KEY_NAME" >"$WORK_DIR/upload-paths" <<'PY'
import json
import sys

tsv_file, idx_file, own_key_name = sys.argv[1], sys.argv[2], sys.argv[3]
index = json.load(open(idx_file))
known = set(index.get("entries", {}).keys())
uploaded_input = []
missing_sig = []
for line in open(tsv_file):
    line = line.rstrip("\n")
    if not line:
        continue
    path, sigs = line.split("\t", 1)
    h = path.rsplit("/", 1)[-1][:32]
    if h in known:
        continue  # already in our GHCR index
    sigs = sigs.split()
    if own_key_name:
        # skip only if a signature NOT from our own key exists (external cache)
        if any(not s.startswith(own_key_name + ":") for s in sigs):
            continue
        # own signature must exist after signing; otherwise signing failed
        if not any(s.startswith(own_key_name + ":") for s in sigs):
            missing_sig.append(path)
            continue
    else:
        if sigs:
            continue  # any signature => came from an external cache
    uploaded_input.append(path)
if missing_sig:
    print("::error::signing failed for: %s" % ", ".join(missing_sig), file=sys.stderr)
    sys.exit(2)
sys.stdout.write("\n".join(uploaded_input))
if uploaded_input:
    sys.stdout.write("\n")
PY
rc=$?
if [ "$rc" = 2 ]; then
  echo "::error::one or more paths carry no signature from this cache after signing" >&2
  exit 1
fi
[ "$rc" = 0 ] || exit 1

UPLIST="$WORK_DIR/upload-paths"
if [ ! -s "$UPLIST" ]; then
  echo "Nothing to upload"
  exit 0
fi
# ---- 6. export + narinfo + upload ----
CACHE_DIR="$WORK_DIR/cache"
mkdir -p "$CACHE_DIR/nar"
: >"$WORK_DIR/receipts.jsonl"
NAR_XZ="$SCRIPT_DIR/../nar_xz.py"

# make_narinfo <path> <hash> <nar_file> <file_size> <file_hash> <path_info> <cache_dir>
# 退出码：0=成功；3=NarSize 非法（跳过）；4=其他生成失败
make_narinfo() {
  python3 - "$1" "$2" "$3" "$4" "$5" "$6" "$7" <<'PY'
import json
import os
import subprocess
import sys

store_path, hash_prefix, nar_file, file_size, file_hash, path_info, cache_dir = sys.argv[1:]
info = json.loads(path_info)
if isinstance(info, dict):
    info = info.get(store_path) or (next(iter(info.values())) if info else {})
else:
    info = info[0] if info else {}

def to_base32(h):
    # Nix >= 2.34 path-info --json emits SRI (sha256-<b64>); nix hash convert
    # --to base32 prints BARE nix-base32 (add the sha256: prefix ourselves:
    # Nix's narinfo parser parseAnyPrefixed rejects prefixed-less values).
    if h.startswith("sha256-"):
        h = subprocess.check_output(
            ["nix", "hash", "convert", "--to", "base32", h], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    return h

nar_hash = to_base32(info.get("narHash", ""))
nar_size = int(info.get("narSize", 0))
if nar_size <= 0:
    print("::warning::narSize <= 0 for %s; skipping" % store_path, file=sys.stderr)
    sys.exit(3)
refs = info.get("references", []) or []
deriver = info.get("deriver", "")
sigs = info.get("signatures", info.get("sigs", [])) or []

lines = [
    "StorePath: " + store_path,
    "URL: nar/" + hash_prefix + ".nar.xz",
    "Compression: xz",
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
PY
}

while IFS= read -r path; do
  [ -n "$path" ] || continue
  hash="$(basename "$path" | cut -c1-32)"
  nar_file="$CACHE_DIR/nar/$hash.nar.xz"
  rm -f "$nar_file"
  echo "::group::nix/cache export $hash" >&2
  if ! nix-store --dump "$path" 2>/dev/null | python3 "$NAR_XZ" >"$nar_file"; then
    echo "::warning::failed to dump $path; skipping" >&2
    SKIPPED=$((SKIPPED + 1))
    echo "::endgroup::" >&2
    continue
  fi
  size="$(wc -c <"$nar_file" | tr -d ' ')"
  if [ "$size" -gt 10737418240 ] || [ -z "$size" ]; then
    echo "::warning::$path nar missing or exceeds ~10GiB GHCR blob limit; skipping" >&2
    rm -f "$nar_file"
    SKIPPED=$((SKIPPED + 1))
    echo "::endgroup::" >&2
    continue
  fi
  file_hash="$(nix hash file --type sha256 --base32 "$nar_file" 2>/dev/null || nix-hash --flat --type sha256 --base32 "$nar_file")"
  path_info="$(nix path-info --json --json-format 1 "$path" 2>/dev/null || true)"
  if [ -z "$path_info" ]; then
    echo "::warning::nix path-info failed for $path; skipping" >&2
    rm -f "$nar_file"
    SKIPPED=$((SKIPPED + 1))
    echo "::endgroup::" >&2
    continue
  fi
  if make_narinfo "$path" "$hash" "$nar_file" "$size" "$file_hash" "$path_info" "$CACHE_DIR"; then
    :
  else
    rc=$?
    echo "::warning::narinfo generation failed for $path (exit $rc); skipping" >&2
    rm -f "$nar_file"
    SKIPPED=$((SKIPPED + 1))
    echo "::endgroup::" >&2
    continue
  fi
  narinfo_file="$CACHE_DIR/$hash.narinfo"
  push_blob "$nar_file" "$WORK_DIR/last-digest"
  nar_digest="$(cat "$WORK_DIR/last-digest")"
  if [ -z "$nar_digest" ]; then
    echo "::error::blob push failed for $hash" >&2
    exit 1
  fi
  jq -n -c \
    --arg hash "$hash" \
    --arg narinfo_file "$narinfo_file" \
    --arg nar_digest "$nar_digest" \
    --argjson nar_size "$size" \
    '{hash: $hash, narinfo_file: $narinfo_file, nar_digest: $nar_digest, nar_size: $nar_size}' \
    >>"$WORK_DIR/receipts.jsonl"
  UPLOADED=$((UPLOADED + 1))
  rm -f "$nar_file"
  echo "::endgroup::" >&2
  echo "uploaded $hash ($size bytes)" >&2
done <"$UPLIST"

if [ "$UPLOADED" = 0 ]; then
  echo "Nothing new to upload"
  exit 0
fi
# ---- 7. rebuild + push index ----
INDEX_JSON="$WORK_DIR/cache-index.json"
PUBKEY="$OWN_KEY" \
EXISTING_FILE="$IDX_FILE" \
RECEIPTS="$WORK_DIR/receipts.jsonl" \
OUT_FILE="$INDEX_JSON" \
REPO="$REPO" \
REGISTRY="$REGISTRY" \
GENERATED="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
python3 - <<'PY'
import json
import os

existing = {}
with open(os.environ["EXISTING_FILE"]) as f:
    try:
        existing = json.load(f) or {}
    except json.JSONDecodeError:
        existing = {}

new_entries = {}
with open(os.environ["RECEIPTS"]) as f:
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
            "added": os.environ["GENERATED"],
        }

index = {
    "version": 1,
    "repo": os.environ["REPO"],
    "registry": os.environ["REGISTRY"],
    "image": "%s/%s/nix-cache" % (os.environ["REGISTRY"], os.environ["REPO"]),
    "generated": os.environ["GENERATED"],
    "public_key": os.environ.get("PUBKEY", "") or existing.get("public_key", ""),
    "entries": {},
    "gc_roots": [],
}
index["entries"].update(existing.get("entries", {}))
index["entries"].update(new_entries)

with open(os.environ["OUT_FILE"], "w") as f:
    json.dump(index, f, indent=2, sort_keys=True)
print("index: %d total entries (%d new)" % (len(index["entries"]), len(new_entries)))
PY

push_blob "$INDEX_JSON" "$WORK_DIR/last-digest"
index_digest="$(cat "$WORK_DIR/last-digest")"
[ -n "$index_digest" ] || { echo "::error::index blob push failed" >&2; exit 1; }
config_file="$WORK_DIR/config.json"
echo '{}' >"$config_file"
push_blob "$config_file" "$WORK_DIR/last-digest"
config_digest="$(cat "$WORK_DIR/last-digest")"
[ -n "$config_digest" ] || { echo "::error::config blob push failed" >&2; exit 1; }
config_size="$(wc -c <"$config_file" | tr -d ' ')"
index_size="$(wc -c <"$INDEX_JSON" | tr -d ' ')"

manifest="$(jq -n \
  --arg config_digest "$config_digest" \
  --argjson config_size "$config_size" \
  --arg index_digest "$index_digest" \
  --argjson index_size "$index_size" \
  '{
    schemaVersion: 2,
    mediaType: "application/vnd.oci.image.manifest.v1+json",
    config: {
      mediaType: "application/vnd.oci.image.config.v1+json",
      digest: $config_digest,
      size: $config_size
    },
    layers: [{
      mediaType: "application/vnd.nix.cache.index.v1+json",
      digest: $index_digest,
      size: $index_size
    }]
  }')"
put_manifest cache-index "$manifest"

# ---- 8. readback verification (layer digest is stronger than `generated`) ----
OK=0
for _ in $(seq 1 30); do
  if fetch_manifest cache-index "$WORK_DIR/readback.json"; then
    RD="$(jq -r '.layers[0].digest // empty' "$WORK_DIR/readback.json")"
    if [ -n "$RD" ] && [ "$RD" = "$index_digest" ]; then
      OK=1
      break
    fi
  fi
  sleep 2
done
if [ "$OK" != 1 ]; then
  echo "::error::cache-index manifest readback did not confirm the new index (blobs uploaded, index not visible yet)" >&2
  exit 1
fi

echo "::group::nix/cache summary" >&2
echo "uploaded paths: $UPLOADED" >&2
echo "skipped paths: $SKIPPED" >&2
echo "index entries: $(jq '.entries | length' "$INDEX_JSON")" >&2
echo "::endgroup::" >&2
{
  echo "## nix/cache push"
  echo ""
  echo "| Metric | Value |"
  echo "|---|---|"
  echo "| Paths uploaded | $UPLOADED |"
  echo "| Paths skipped | $SKIPPED |"
  echo "| Index entries | $(jq '.entries | length' "$INDEX_JSON") |"
} >>"${GITHUB_STEP_SUMMARY:-/dev/null}"
