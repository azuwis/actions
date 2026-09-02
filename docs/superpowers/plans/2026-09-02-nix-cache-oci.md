# nix/cache — GHCR/OCI 二进制缓存 Action 实现计划（rev 2）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `nix/cache/` 新增 pull（`nix/cache`）与 push（`nix/cache/post`）两个 composite action，把 Nix 二进制缓存放到 GHCR OCI 仓库（不依赖 Cachix 服务）。

**Architecture:** pull 启动 vendored 的 `nixcache-proxy.py`（本地只读代理，从 GHCR `ghcr.io/<repo>/nix-cache` 的 `cache-index` + NAR blob 服务 Nix 二进制缓存协议），用幂等 marker 块配置 nix.conf 并重启 daemon；push 直接调 GHCR OCI API（纯 curl）：候选路径（`paths` 闭包或整店无外部签名）→ 过滤已入 index/外部缓存路径 → `nix-store --dump` + python3 lzma 压缩 → 上传 NAR blob → 合并重建 `cache-index`。

**Tech Stack:** Bash **POSIX 兼容**脚本（darwin runner 是 Bash 3.2.57、ubuntu 的 `/bin/sh` 是 dash）、Python 3.10+（stdlib lzma/hashlib）、curl + jq、Nix（`nix-store --dump`、`nix path-info --json --json-format 1`、`nix store sign`、`nix hash file`、`nix hash convert --to base32`）、GHCR OCI Registry API v2（无 skopeo）。

**Spec:** [docs/superpowers/specs/2026-09-02-nix-cache-oci-design.md](../specs/2026-09-02-nix-cache-oci-design.md)（rev 2 + 2026-09-02 修订说明，见计划尾部的"Spec 修订"）—— 计划从 spec 论证，执行者须同时读两者。

## Global Constraints

- 单租户 runner 假设（GitHub-hosted 或独占机）；自托管共享 runner 用 `paths` 模式。
- registry 仅 `ghcr.io`（不提供输入）；`repo` 输入脚本内 tolower。
- vendored `nixcache-proxy.py` 钉上游 commit `2e21568cf2cf0824ea6f5e9ce54179aee19cbf6e`，逐字拷贝（唯一改动：文件头注释）。
- Python 必须 ≥3.10（proxy 用 PEP 604）；**bash 脚本 POSIX 兼容**：禁 assoc array/mapfile/`${var,,}`/nameref/`${@:2}`（dash `sh -c` 会 Bad substitution）、**禁 `xargs -a`**（GNU 扩展，macOS xargs 无此选项，一律 `<file` 重定向）、`sed -i` 统一 `-i.bak` + 删除备份（GNU/BSD 唯一兼容写法）。
- `NIXCACHE_PORT` 必须同时传给 proxy 环境、nix.conf 与 GITHUB_ENV（同一值）。
- token 只经 `-H "Authorization: Basic <b64>"`，脚本首行 `echo "::add-mask::${TOKEN}"`，不打印；`fail_or_skip` 类消息一律写 **stderr**（禁止在命令替换 `$(...)` 里 echo 诊断到 stdout）。
- nix.conf 用 `# nix-cache begin`/`# nix-cache end` marker 块；post 清理时整块删除。
- 失败可见性：`::warning::`/`::error::` + `GITHUB_STEP_SUMMARY`；不得静默降级。
- narinfo 的 `NarHash` 必须带 `sha256:` 前缀（`nix hash convert --to base32` 输出裸 b32；Nix 客户端 `parseAnyPrefixed` 拒绝裸值——上游 cache-builder.sh 的裸值写法是缺陷，勿跟随）。
- 外部 action 一律 SHA 钉住；本设计不引入新外部 action。
- 测试：纯逻辑（nar_xz.py）用 `python3 -m unittest`，端到端由 `.github/workflows/nix.yml` 验证。

---

### Task 1: Vendor nixcache-oci proxy（含接口核验）

**Files:**
- Create: `nix/cache/nixcache-proxy.py`

**Interfaces:**
- Produces: `nix/cache/nixcache-proxy.py` —— 由 `cache.sh` 以 `python3` 运行；读取环境变量 `NIXCACHE_REPO`、`NIXCACHE_PORT`、`NIXCACHE_UPSTREAM`、`NIXCACHE_INDEX_DIR`、`GITHUB_TOKEN`；监听 `127.0.0.1:$NIXCACHE_PORT`，提供 `GET /nix-cache-info`、`/public-key`、`/_status`（JSON: `repo`/`index_entries` 等）、`/<hash>.narinfo`、`/nar/*`、`POST /_refresh`。

- [ ] **Step 1: 下载上游文件（钉 commit 的 URL）**

```bash
curl -fsSL \
  https://raw.githubusercontent.com/cmspam/nixcache-oci/2e21568cf2cf0824ea6f5e9ce54179aee19cbf6e/proxy/main.py \
  -o /tmp/nixcache-main.py
sha256sum /tmp/nixcache-main.py   # 记录输出，下一步嵌入文件头
wc -l /tmp/nixcache-main.py       # 预期 372 行
```

- [ ] **Step 2: 生成带文件头的 vendored 文件**（9 行头部：1 行 shebang + 8 行注释；用上一步 SHA 替换 `<SHA256>`）

```bash
mkdir -p nix/cache
{ cat <<EOF
#!/usr/bin/env python3
# Vendored from cmspam/nixcache-oci (read-only proxy).
# Upstream: https://github.com/cmspam/nixcache-oci/blob/2e21568cf2cf0824ea6f5e9ce54179aee19cbf6e/proxy/main.py
# Pinned upstream commit: 2e21568cf2cf0824ea6f5e9ce54179aee19cbf6e (main, 2026-08-31)
# Upstream file sha256: <SHA256>
# Sync policy: re-copy from this pinned commit, update SHA and commit message,
# and review the diff by hand. Do NOT modify this file otherwise.
# NOTE: upstream repo has NO LICENSE file; treat as third-party code with
# attribution risk (see AGENTS.md).
EOF
  cat /tmp/nixcache-main.py
} > nix/cache/nixcache-proxy.py
chmod 755 nix/cache/nixcache-proxy.py
```

- [ ] **Step 3: 验证内容与上游逐字一致**（头部 9 行 → `tail -n +10`）

```bash
diff <(tail -n +10 nix/cache/nixcache-proxy.py) /tmp/nixcache-main.py && echo OK
python3 -c 'import ast; ast.parse(open("nix/cache/nixcache-proxy.py").read())'
```

- [ ] **Step 4: 显式核验 vendored proxy 的接口假设**（把"假设"变"检查项"）

```bash
grep -n "NIXCACHE_REPO\|NIXCACHE_REGISTRY\|NIXCACHE_PORT\|NIXCACHE_LISTEN\|NIXCACHE_UPSTREAM\|NIXCACHE_INDEX_DIR\|GITHUB_TOKEN\|INDEX_TTL" nix/cache/nixcache-proxy.py
# 应确认：全部环境变量名与 cache.sh/push.sh 使用一致
grep -n "/_status\|/public-key\|/_refresh\|/nix-cache-info" nix/cache/nixcache-proxy.py
grep -n '"repo"\|"index_entries"\|"public_key"\|"entries"\|"nar_digest"\|"narinfo"' nix/cache/nixcache-proxy.py
# 应确认：_status 返回 .repo/.index_entries；public-key 无 key 时 404；
#         entries 条目含 narinfo/nar_digest；find_nar_digest 按 URL 行子串匹配
grep -n "def get_oci_token\|scope=.*pull\b" nix/cache/nixcache-proxy.py
# 应确认：代理 token scope 为 pull（只读）
```

- [ ] **Step 5: Commit**

```bash
git add nix/cache/nixcache-proxy.py
git commit -m "nix/cache: vendor nixcache-oci proxy at 2e21568"
```

---

### Task 2: nar_xz.py 流式压缩器 + 单元测试

**Files:**
- Create: `nix/cache/nar_xz.py`
- Create: `nix/cache/tests/test_nar_xz.py`

**Interfaces:**
- Produces: `nix/cache/nar_xz.py` —— `python3 nar_xz.py`：stdin 读原始 NAR，stdout 写 `.xz`（`FORMAT_XZ`，preset 1）。
- Consumes: 无。

- [ ] **Step 1: 写失败测试** `nix/cache/tests/test_nar_xz.py`

```python
import lzma
import subprocess
import sys
import unittest
from pathlib import Path

NAR_XZ = Path(__file__).resolve().parents[1] / "nar_xz.py"


def compress(data: bytes) -> bytes:
    return subprocess.run(
        [sys.executable, str(NAR_XZ)],
        input=data, capture_output=True, check=True,
    ).stdout


class NarXzTest(unittest.TestCase):
    def test_round_trip(self):
        data = b"hello world\n" * 100000  # ~1.2 MiB 可压缩
        self.assertEqual(lzma.decompress(compress(data)), data)

    def test_xz_magic(self):
        self.assertEqual(compress(b"abc")[:6], b"\xfd7zXZ\x00")

    def test_empty_input(self):
        self.assertEqual(lzma.decompress(compress(b"")), b"")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python3 -m unittest discover -s nix/cache/tests -v
# Expected: FAILED (FileNotFoundError: nar_xz.py 不存在)
```

- [ ] **Step 3: 实现** `nix/cache/nar_xz.py`

```python
#!/usr/bin/env python3
"""Streaming xz compressor (FORMAT_XZ, preset 1) — stdin to stdout.

Used by nix/cache/post/push.sh to compress NAR dumps without depending on
the `xz` binary (Linux/macOS CI runners both ship python3).
"""
import lzma
import sys

CHUNK = 1 << 20  # 1 MiB


def main() -> None:
    compressor = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=1)
    while True:
        chunk = sys.stdin.buffer.read(CHUNK)
        if not chunk:
            break
        out = compressor.compress(chunk)
        if out:
            sys.stdout.buffer.write(out)
    sys.stdout.buffer.write(compressor.flush())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python3 -m unittest discover -s nix/cache/tests -v
# Expected: OK (3 tests)
```

- [ ] **Step 5: Commit**

```bash
git add nix/cache/nar_xz.py nix/cache/tests/test_nar_xz.py
git commit -m "nix/cache: add streaming xz compressor with tests"
```

---

### Task 3: pull action（`nix/cache`）

**Files:**
- Create: `nix/cache/action.yml`
- Create: `nix/cache/cache.sh`

**Interfaces:**
- Consumes: `nix/cache/nixcache-proxy.py`（Task 1）。
- Produces（GITHUB_ENV，供 `nix/cache/post/push.sh` 用）: `NIXCACHE_REPO`（小写）、`NIXCACHE_PORT`（空闲端口）、`NIXCACHE_PROXY_PID`（可为空）；另写 `$RUNTEMP/nixcache-proxy/proxy.pid`（PID 文件）。
- Inputs（action.yml）：`repo`（默认 `${{ github.repository }}`）、`token`（默认 `${{ github.token }}`）、`public_key`（默认空）。

- [ ] **Step 1: 写 action.yml** `nix/cache/action.yml`

```yaml
name: Nix OCI cache substituter (pull)
inputs:
  repo:
    description: GitHub owner/repo of the GHCR cache image (ghcr.io/<repo>/nix-cache); lowercase. Must not come from untrusted input (e.g. fork head repo)
    default: ${{ github.repository }}
  token:
    description: "GitHub token for pulling a private cache (scopes: packages: read)"
    default: ${{ github.token }}
  public_key:
    description: Local trust anchor. Cache signing public key (name:base64). Leave empty for unsigned mode (require-sigs = false)
    default: ""
runs:
  using: composite
  steps:
    - name: Start OCI cache proxy and configure substituter
      shell: bash
      env:
        INPUT_REPO: ${{ inputs.repo }}
        INPUT_TOKEN: ${{ inputs.token }}
        INPUT_PUBLIC_KEY: ${{ inputs.public_key }}
      run: exec ${{ github.action_path }}/cache.sh
```

- [ ] **Step 2: 写 cache.sh —— 第 1 部分（Python 检查 / 端口 / 启动代理 / 健康检查）**

```bash
#!/usr/bin/env bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${INPUT_REPO:-$GITHUB_REPOSITORY}"
REPO="$(printf '%s' "$REPO" | tr '[:upper:]' '[:lower:]')"
TOKEN="${INPUT_TOKEN:-$GITHUB_TOKEN}"
PUBLIC_KEY="${INPUT_PUBLIC_KEY:-}"
PROXY_PID=""
PORT=""
READY=0

echo "::add-mask::${TOKEN}"

# 1. vendored proxy requires Python >= 3.10 (PEP 604 annotations)
if ! python3 -c 'import sys; assert sys.version_info >= (3, 10)' 2>/dev/null; then
  echo "::error::nix/cache requires Python >= 3.10 (found: $(python3 --version 2>&1 || echo unknown))"
  exit 1
fi

# 2. always pick a free port (never race for 37515)
PORT="$(python3 - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"

# 3. start vendored proxy
INDEX_DIR="$RUNNER_TEMP/nixcache-proxy"
mkdir -p "$INDEX_DIR"
chmod 700 "$INDEX_DIR"
NIXCACHE_REPO="$REPO" \
NIXCACHE_PORT="$PORT" \
NIXCACHE_UPSTREAM="" \
NIXCACHE_INDEX_DIR="$INDEX_DIR" \
GITHUB_TOKEN="$TOKEN" \
  python3 "$SCRIPT_DIR/nixcache-proxy.py" >"$INDEX_DIR/proxy.log" 2>&1 &
PROXY_PID=$!

# 4. health check + identity check (must use the chosen port;
#    _status can block on the index prefetch lock up to ~60s on cold start)
if kill -0 "$PROXY_PID" 2>/dev/null; then
  for _ in $(seq 1 15); do
    if curl -fs --max-time 2 "http://127.0.0.1:$PORT/nix-cache-info" >/dev/null 2>&1; then
      READY=1
      break
    fi
    sleep 1
  done
  if [ "$READY" = 1 ]; then
    STATUS_REPO="$(curl -fs --max-time 30 "http://127.0.0.1:$PORT/_status" 2>/dev/null | jq -r '.repo // empty' 2>/dev/null || true)"
    if [ "$STATUS_REPO" != "$REPO" ]; then
      echo "::warning::proxy identity mismatch (serves repo=$STATUS_REPO, expected=$REPO); not configuring substituter"
      READY=0
    fi
  fi
fi
if [ "$READY" != 1 ]; then
  echo "::warning::nix cache proxy not ready; skipping substituter configuration (proxy.log: $INDEX_DIR/proxy.log)"
  if [ -n "$PROXY_PID" ]; then
    kill "$PROXY_PID" 2>/dev/null || true
    PROXY_PID=""
  fi
fi

# 5. write PID file + warn-to-summary helper
printf '%s\n' "$PROXY_PID" >"$INDEX_DIR/proxy.pid"
echo "$REPO" >"$INDEX_DIR/repo"   # 供验证 job 使用
echo "$PORT" >"$INDEX_DIR/port"

warn_to_summary() { # message
  echo "::warning::$1"
  {
    echo "## nix/cache"
    echo ""
    echo "> $1"
  } >>"${GITHUB_STEP_SUMMARY:-/dev/null}" 2>/dev/null || true
}
```

- [ ] **Step 3: 写 cache.sh —— 第 2 部分（nix.conf 配置 / daemon 重启 / 自检 / GITHUB_ENV）**（追加到第 1 部分之后）

```bash
# 6. idempotent marker block in nix.conf (daemon config + user config)
if [ "$READY" = 1 ]; then
  BLOCK="extra-substituters = http://127.0.0.1:$PORT
extra-trusted-substituters = http://127.0.0.1:$PORT"
  if [ -n "$PUBLIC_KEY" ]; then
    IDX_KEY="$(curl -fs --max-time 10 "http://127.0.0.1:$PORT/public-key" 2>/dev/null | tr -d '\n' || true)"
    if [ -n "$IDX_KEY" ] && [ "$IDX_KEY" != "$PUBLIC_KEY" ]; then
      echo "::warning::index advertises a different public key than the public_key input; trusting the local input"
    fi
    BLOCK="$BLOCK
extra-trusted-public-keys = $PUBLIC_KEY"
  else
    warn_to_summary "no public_key input: adding require-sigs = false (disables signature verification for ALL substituters)"
    BLOCK="$BLOCK
require-sigs = false"
  fi

  # GNU/BSD 兼容的幂等 marker 替换：sed -i.bak + 删除备份
  apply_config() { # $1 = file, $2 = use sudo (1/0)
    local file="$1" use_sudo="$2"
    if [ "$use_sudo" = 1 ]; then
      sudo sed -i.bak '/^# nix-cache begin$/,/^# nix-cache end$/d' "$file" 2>/dev/null || true
      sudo rm -f "$file.bak" 2>/dev/null || true
      printf '\n# nix-cache begin\n%s\n# nix-cache end\n' "$BLOCK" | sudo tee -a "$file" >/dev/null
    else
      mkdir -p "$(dirname "$file")"
      sed -i.bak '/^# nix-cache begin$/,/^# nix-cache end$/d' "$file" 2>/dev/null || true
      rm -f "$file.bak" 2>/dev/null || true
      printf '\n# nix-cache begin\n%s\n# nix-cache end\n' "$BLOCK" >>"$file"
    fi
  }

  if [ -e /nix/var/nix/daemon-socket ]; then
    sudo mkdir -p /etc/nix
    [ -e /etc/nix/nix.conf ] || sudo touch /etc/nix/nix.conf
    apply_config /etc/nix/nix.conf 1
  else
    warn_to_summary "no nix daemon socket found; configuring user-level nix.conf only"
  fi
  apply_config "${HOME}/.config/nix/nix.conf" 0

  # 7. restart daemon so the config takes effect
  if [ -e /nix/var/nix/daemon-socket ]; then
    case "$RUNNER_OS" in
    macOS)
      sudo launchctl unload /Library/LaunchDaemons/org.nixos.nix-daemon.plist 2>/dev/null || true
      sudo launchctl load -w /Library/LaunchDaemons/org.nixos.nix-daemon.plist 2>/dev/null || true
      ;;
    *)
      if ! sudo systemctl restart nix-daemon 2>/dev/null; then
        warn_to_summary "failed to restart nix-daemon; substituter config may not be effective"
      fi
      ;;
    esac
  fi

  # 8. self-check (soft: merged view of client+daemon config)
  if nix show-config 2>/dev/null | grep -q "127.0.0.1:$PORT"; then
    echo "::group::nix/cache"
    echo "OCI substituter configured: http://127.0.0.1:$PORT (repo=$REPO)"
    [ -n "$PUBLIC_KEY" ] || echo "unsigned mode: require-sigs = false"
    echo "::endgroup::"
  else
    warn_to_summary "nix does not report the cache substituter (port $PORT); check nix.conf and daemon restart"
  fi
fi

# 9. state for nix/cache/post
{
  echo "NIXCACHE_REPO=$REPO"
  echo "NIXCACHE_PORT=$PORT"
  echo "NIXCACHE_PROXY_PID=$PROXY_PID"
} >>"$GITHUB_ENV"
```

- [ ] **Step 4: 语法与静态检查**

```bash
bash -n nix/cache/action.yml 2>/dev/null || true
bash -n nix/cache/cache.sh
command -v shellcheck >/dev/null && shellcheck nix/cache/cache.sh || true
# POSIX 兼容性检查（应全为 0 行；macOS runner 为 Bash 3.2.57）
grep -nE 'declare -A|mapfile|readarray|\$\{[a-zA-Z_]+,,|nameref|\$\{[a-zA-Z_]+@|\$\{[a-zA-Z_]+:[0-9]' nix/cache/cache.sh || true
```

- [ ] **Step 5: Commit**

```bash
git add nix/cache/action.yml nix/cache/cache.sh
git commit -m "nix/cache: add pull action (vendored proxy + substituter config)"
```

---

### Task 4: push action（`nix/cache/post`）

**Files:**
- Create: `nix/cache/post/action.yml`
- Create: `nix/cache/post/push.sh`

**Interfaces:**
- Consumes: `NIXCACHE_REPO`/`NIXCACHE_PORT`/`NIXCACHE_PROXY_PID`（Task 3 写入 GITHUB_ENV）、`nix/cache/nar_xz.py`（Task 2）。
- Inputs（action.yml）：`repo`（默认 `${{ github.repository }}`）、`token`（默认 `${{ github.token }}`）、`signing_key`（可选 secret **内容**，composite 不能直接读 secrets context，调用方用 `with:` 传入）、`paths`（可选 store 路径，其**闭包**被推送）。

- [ ] **Step 1: 写 action.yml** `nix/cache/post/action.yml`

```yaml
name: Nix OCI cache push (post actions)
inputs:
  repo:
    description: GitHub owner/repo of the GHCR cache image (ghcr.io/<repo>/nix-cache); lowercase
    default: ${{ github.repository }}
  token:
    description: "GitHub token for pushing (needs job-level permissions: packages: write)"
    default: ${{ github.token }}
  signing_key:
    description: Cache signing key contents (name:base64); required if the cache index is signed; leave empty for unsigned mode
    default: ""
  paths:
    description: Whitespace-separated store paths whose closures are pushed; empty = scan the whole store for paths with no external signature
    default: ""
runs:
  using: composite
  steps:
    - name: Push local builds to GHCR OCI cache
      shell: bash
      env:
        INPUT_REPO: ${{ inputs.repo }}
        INPUT_TOKEN: ${{ inputs.token }}
        INPUT_SIGNING_KEY: ${{ inputs.signing_key }}
        INPUT_PATHS: ${{ inputs.paths }}
      run: exec ${{ github.action_path }}/push.sh
```

- [ ] **Step 2.1: 写 push.sh —— 头部 + 清理 + 认证/API helper**

```bash
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
```

- [ ] **Step 2.2: 写 push.sh —— blob 上传（输出文件，不用命令替换）**

```bash
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
```

- [ ] **Step 2.3: 写 push.sh —— index 拉取 + 签名守卫 + 候选集 + 签名 + 过滤**

```bash
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
```

要点（实现时按此核对）：
- `nix path-info --json --json-format 1` —— 显式 format 1（2.34 起 `--json` 无 `--json-format` 是弃用警告、未来成 error；format 1 = dict keyed by store path，本过滤器只归一化此形态）。
- `jq` 归一化对 array/map 双形态都处理（兼容旧版）；2.34/2.35 默认输出恒为 map。
- 过滤规则：已在 index 跳过；有非本缓存 key 的签名跳过（外部缓存）；本缓存签名但未入 index **保留**（自动重试上轮失败的上传）。
- 签名模式空候选守卫在 `[ ! -s "$CAND" ]`（防 GNU xargs 空输入仍执行一次）。

- [ ] **Step 2.4: 写 push.sh —— 导出 + narinfo 生成 + 上传循环**

```bash
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
```

- [ ] **Step 2.5: 写 push.sh —— index 合并 + 回读验证 + summary**

```bash
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
```

- [ ] **Step 3: 语法与静态检查（完整脚本）**

```bash
bash -n nix/cache/post/push.sh
command -v shellcheck >/dev/null && shellcheck nix/cache/post/push.sh || true
# POSIX 兼容性（应无输出）：禁 bashism / xargs -a
grep -nE 'declare -A|mapfile|readarray|\$\{[a-zA-Z_]+,,|\$\{[a-zA-Z_]+@|\$\{[a-zA-Z_]+:[0-9]|xargs -a ' nix/cache/post/push.sh || true
```

- [ ] **Step 4: Commit**

```bash
git add nix/cache/post/action.yml nix/cache/post/push.sh
git commit -m "nix/cache/post: add push action (GHCR OCI upload + index merge)"
```

---

### Task 5: CI 验证工作流

**Files:**
- Modify: `.github/workflows/nix.yml`

**Interfaces:**
- Consumes: Tasks 1-4 的全部 Action。

- [ ] **Step 1: 追加 `nix_cache` job**（先于 `nix_cache_with_cache`；fork guard + 双权限；用 `paths` 输入只推 leaf）

```yaml
  nix_cache:
    # fork guard：fork PR 的 token 只读，push 必失败；跳过（同 nix 模式的
    # always()/needs 结构，但 fork PR 不跑 push 相关断言）
    if: ${{ always() && !failure() && !cancelled()
          && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) }}
    needs: clear_cache
    permissions:
      contents: read
      packages: write
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - name: Unit tests
        run: python3 -m unittest discover -s nix/cache/tests -v
      - uses: ./nix
        with:
          install_action: cachix
      - uses: ./nix/cache
      - name: Write test flake
        run: |
          mkdir -p "$RUNNER_TEMP/ci-flake"
          cat > "$RUNNER_TEMP/ci-flake/flake.nix" <<'EOF'
          {
            # 钉 rev：移动分支会在两次 job 之间漂移，导致 leaf 路径不一致
            inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05?rev=5dfba6236110080a54247d6460bc2ff5dda939cc";
            outputs = { self, nixpkgs }: {
              packages.x86_64-linux.cache-test = nixpkgs.legacyPackages.x86_64-linux.stdenv.mkDerivation {
                pname = "cache-test";
                version = "1";
                buildCommand = "mkdir -p $out && echo hello > $out/hello";
              };
            };
          }
          EOF
      - name: Build and capture leaf path
        run: |
          cd "$RUNNER_TEMP/ci-flake"
          # stdout 只有 out-path（计划信息走 stderr），无管道 → 无 pipefail 问题
          leaf="$(nix build .#cache-test --no-link --print-out-paths)"
          echo "leaf=$leaf"
          echo "LEAF_PATH=$leaf" >> "$GITHUB_ENV"
      - name: Push leaf to GHCR cache
        uses: ./nix/cache/post
        with:
          paths: ${{ env.LEAF_PATH }}
      - name: Assert cache-index contains leaf
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          leaf="$LEAF_PATH"
          hash="$(basename "$leaf" | cut -c1-32)"
          repo="$(printf '%s' "$GITHUB_REPOSITORY" | tr '[:upper:]' '[:lower:]')"
          token="$(curl -fsSL -u "token:${GH_TOKEN}" \
            "https://ghcr.io/token?scope=repository:${repo}/nix-cache:pull&service=ghcr.io" \
            | jq -r '.token')"
          ok=""
          for _ in $(seq 1 30); do
            manifest="$(curl -fsSL -H "Authorization: Bearer $token" \
              -H "Accept: application/vnd.oci.image.manifest.v1+json" \
              "https://ghcr.io/v2/${repo}/nix-cache/manifests/cache-index" 2>/dev/null || true)"
            idx_digest="$(printf '%s' "$manifest" | jq -r '.layers[0].digest // empty')"
            if [ -n "$idx_digest" ]; then
              entries="$(curl -fsSL -H "Authorization: Bearer $token" \
                "https://ghcr.io/v2/${repo}/nix-cache/blobs/$idx_digest" 2>/dev/null | jq -r '.entries | keys[]' || true)"
              if printf '%s\n' "$entries" | grep -qx "$hash"; then ok=1; break; fi
            fi
            sleep 2
          done
          [ -n "$ok" ] || { echo "leaf $hash not found in cache-index"; exit 1; }
```

- [ ] **Step 2: 追加 `nix_cache_with_cache` job**（无 `./nix/post`；`nix eval --raw` 取路径；服务端证据为**主**断言）

```yaml
  nix_cache_with_cache:
    if: ${{ always() && !failure() && !cancelled()
          && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) }}
    needs: nix_cache
    permissions:
      contents: read
      packages: read
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: ./nix
        with:
          install_action: cachix
      - uses: ./nix/cache
      - name: Write test flake
        run: |
          mkdir -p "$RUNNER_TEMP/ci-flake"
          cat > "$RUNNER_TEMP/ci-flake/flake.nix" <<'EOF'
          {
            inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05?rev=5dfba6236110080a54247d6460bc2ff5dda939cc";
            outputs = { self, nixpkgs }: {
              packages.x86_64-linux.cache-test = nixpkgs.legacyPackages.x86_64-linux.stdenv.mkDerivation {
                pname = "cache-test";
                version = "1";
                buildCommand = "mkdir -p $out && echo hello > $out/hello";
              };
            };
          }
          EOF
      - name: Ensure leaf is not in store, then substitute from OCI cache
        run: |
          cd "$RUNNER_TEMP/ci-flake"
          # eval --raw 只算路径不构建（nix path-info 在路径不在 store 时会直接失败）
          leaf="$(nix eval --raw .#cache-test.outPath)"
          hash="$(basename "$leaf" | cut -c1-32)"
          # 无缓存时可删除（delete 失败=不存在，正常）
          nix store delete "$leaf" 2>/dev/null || true
          port="${NIXCACHE_PORT:-}"
          [ -n "$port" ] || { echo "no cache substituter configured (NIXCACHE_PORT empty)"; exit 1; }
          # 强制代理刷新索引（防冷启动索引未拉完 / TTL）；等 entries > 0
          curl -fsS -X POST "http://127.0.0.1:$port/_refresh" 2>/dev/null || true
          for _ in $(seq 1 20); do
            n="$(curl -fsS "http://127.0.0.1:$port/_status" 2>/dev/null | jq -r '.index_entries // 0' || true)"
            [ "${n:-0}" -gt 0 ] && break
            sleep 2
          done
          nix build .#cache-test --no-link --print-out-paths > "$RUNNER_TEMP/build.log" 2>&1
          # 主断言 1：CI 侧直接问我们的 proxy（服务端看到 narinfo 请求）
          curl -fsS "http://127.0.0.1:$port/$hash.narinfo" || { echo "proxy did not serve $hash.narinfo"; exit 1; }
          # 主断言 2：proxy 请求日志里确有该 narinfo 的 GET（服务端证据）
          grep -q "$hash.narinfo" "$RUNNER_TEMP/nixcache-proxy/proxy.log" \
            || { echo "proxy.log has no narinfo GET for $hash"; exit 1; }
          # 辅助断言：Nix 替换（而非本地构建）—— 版本敏感文案，仅供辅助
          grep -q "will be fetched" "$RUNNER_TEMP/build.log" \
            || { echo "no 'will be fetched' in build log"; exit 1; }
          if grep -q "will be built" "$RUNNER_TEMP/build.log"; then
            echo "leaf was built locally instead of substituted"; exit 1
          fi
```

- [ ] **Step 3: 语法检查**

```bash
python3 -c 'import yaml, sys; yaml.safe_load(open(".github/workflows/nix.yml")); print("yaml ok")' 2>/dev/null \
  || echo "skipped (PyYAML not available; GitHub will parse on push)"
```

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/nix.yml
git commit -m "CI/nix: add nix/cache pull and push validation jobs"
```

---

### Task 6: 文档与配置同步

**Files:**
- Modify: `AGENTS.md`
- Modify: `.github/dependabot.yml`

- [ ] **Step 1: `AGENTS.md` actions 表补两行**（在 `nix/post/` 行下）

```markdown
| `nix/cache/` | Start a local OCI-backed substituter (GHCR `nix-cache` image, see the cache section). |
| `nix/cache/post/` | Post-step counterpart that uploads locally-built store paths to the GHCR OCI cache. |
```

- [ ] **Step 2: `AGENTS.md` 在 "How the Nix cache works" 追加一节**（内容：机制描述、vendored 契约与 LICENCE 风险、post 非 hook 而是独立 action、push 失败 loud、单租户假设、两个缓存的选型指引——actions/cache 全量快照 vs nix/cache 按路径跨仓库；整店扫描 vs `paths` 模式。）

```markdown
### The OCI binary cache (nix/cache + nix/cache/post)

`nix/cache` and `nix/cache/post` implement a complementary binary cache on top of
GHCR OCI (`ghcr.io/<repo>/nix-cache`), without any Cachix service:

1. `nix/cache` starts a vendored read-only proxy (`nix/cache/nixcache-proxy.py`,
   pinned from cmspam/nixcache-oci, see its header) on a free port and appends an
   idempotent `# nix-cache begin/end` block to `/etc/nix/nix.conf` and
   `~/.config/nix/nix.conf` (`extra-substituters`/`extra-trusted-substituters`
   plus `extra-trusted-public-keys` or `require-sigs = false`), then restarts the
   daemon. `NIXCACHE_REPO`, `NIXCACHE_PORT`, `NIXCACHE_PROXY_PID` are written to
   `$GITHUB_ENV`.
2. `nix/cache/post` pushes either the closure of the `paths` input or store
   paths with no external signature that are not yet in the GHCR `cache-index`,
   exports them (`nix-store --dump` + `nix/cache/nar_xz.py`), uploads the NARs as
   OCI blobs, and merges a new `cache-index` manifest. It kills the proxy and
   removes the nix.conf marker block afterwards.

Watch for subtle invariants:

- The vendored proxy must stay byte-identical to upstream (sync via re-copy +
  review; upstream has NO LICENSE, third-party attribution risk).
- `nix/cache/post` is a separate action, NOT a post hook: it only runs if the
  workflow includes it and must run after `nix/cache`. Push failures are loud
  (`::warning::`/`::error::`); 401/403 (fork PR, missing `packages: write`) is a
  warning + skip, not a failure.
- Default `paths` is empty = whole-store scan; use the explicit `paths` input on
  shared/self-hosted runners.
- Choose between the two caches: `actions/cache` snapshots the whole store (fast,
  same-repo, quota-limited); `nix/cache` is per-path, cross-repo, without the
  `actions/cache` size limits, and survives `actions/cache` expiry.
- First run after enabling may backfill pre-existing unsigned paths (bounded by
  store size); subsequent runs are incremental via the index filter.
```

- [ ] **Step 3: `AGENTS.md` 约定清单加前缀**：`nix/cache:`、`nix/cache/post:`。

- [ ] **Step 4: `.github/dependabot.yml` 目录清单补全**：`directories:` 增加 `- /nix/cache`、`- /nix/cache/post`，并补上缺失的 `- /nix/debug`。

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md .github/dependabot.yml
git commit -m "nix/cache: document OCI cache mechanism and dependabot dirs"
```

---

### Task 7: 端到端验证（推送后运行 CI）

**Files:** 无。

- [ ] **Step 1: 本地最后检查**

```bash
bash -n nix/cache/cache.sh nix/cache/post/push.sh
python3 -m unittest discover -s nix/cache/tests -v
git status --short  # 期望干净
```

- [ ] **Step 2: 推送并触发 `Nix` workflow**（`workflow_dispatch`；或 push 到分支）

预期：
- `nix_cache`：unit tests OK → 代理启动 → leaf 构建 → `paths` 模式只推 leaf → curl 断言 index entry；`nix_cache_with_cache`：`nix eval --raw` 取 leaf → delete → `_refresh` → build → **curl narinfo 200 + proxy.log GET** 两条主断言 + `will be fetched`/无 `will be built` 辅助断言。
- 失败排查入口：`$RUNNER_TEMP/nixcache-proxy/proxy.log`、`::group::nix/cache` 输出、job 日志。

- [ ] **Step 3: 若有失败，按故障模式对照表修复**

| 症状 | 原因 | 处理 |
|---|---|---|
| checkout 403 | job 缺 `contents: read`（job 级 permissions 未声明即无权限） | 补 `contents: read` + `packages: write` |
| 代理未配置（NIXCACHE_PORT 空/无 substituter） | daemon 未重启/健康检查失败/身份校验失败 | 看 pull 步骤 `::warning::` 与 proxy.log；确认 `systemctl restart nix-daemon` |
| `proxy identity mismatch` | `_status` repo 字段不符 | `repo` 输入小写/名称检查 |
| 401/403 skipped（warning） | fork PR 或 job 无 `packages: write` | fork PR 应被 `if` 守卫跳过；本仓库 push 需 `packages: write` |
| `index public_key differs` / `signed but no signing_key` | 缓存签名状态与输入不匹配 | 按警告提供/去掉 `signing_key`；换 key 不被支持 |
| leaf 未入 index（断言失败） | 上传/回读失败 | 看 push 日志与 `push_blob` digest；重跑（候选自动重试） |
| `no narinfo GET in proxy.log` | 索引未刷新/代理未就绪 | 确认 `_refresh`/`_status` 循环；GHCR 读延迟（重跑即可） |
| `will be built`（辅助断言失败） | leaf 未替换成功 | 若 leaf 路径与上次不同 → nixpkgs rev 漂移（检查 flake.nix 钉的 rev）；否则看代理与索引 |
| 签名模式上传 0 路径 | `own_key_name` 过滤误判 | 检查 `nix key convert-secret-to-public` 输出与 path-info signatures 的 `name:` 前缀 |

---

## Self-Review（对照 spec 的覆盖核查）

- Spec 组件 1（pull）: Task 3 全覆盖（Python≥3.10、bind(0)、NIXCACHE_PORT 同源、健康+身份校验（`_status` 超时 30s）、失败不写配置、marker 块幂等（`sed -i.bak`）、daemon 重启显式告警、自检、PID 文件 + GITHUB_ENV、`::warning::` + summary）。
- Spec 组件 2（push）: Task 4 全覆盖（清理/杀代理、nix.conf 清理、候选集 **paths 闭包**/整店、POSIX xargs 分批、`--json-format 1`、index 404/错误守卫、签名+校验+`own_key_name` 过滤、导出/nar_xz、**NarHash `sha256:` 前缀**、NarSize>0 校验、OCI 上传（HEAD `-I`、相对 Location、`?&`、重试、10GiB 限额）、index 合并文件化、回读、summary 含跳过数）。
- Spec 组件 3（vendored）: Task 1（含接口假设显式核验步骤）。
- Spec 验证: Task 5（fork 守卫、双 job 权限、钉 nixpkgs rev、`paths` 模式只推 leaf、`nix eval --raw`、`$NIXCACHE_PORT`、主断言 curl narinfo + proxy.log、辅助 fetched/built、无 `./nix/post`）。
- Spec 文档: Task 6（AGENTS.md 表+机制+前缀+LICENSE 风险、dependabot 3 目录）。
- 占位符扫描：无 TBD/TODO；所有脚本内容为最终代码。
- 名字一致性: `nar_xz.py`（Task 2）在 push.sh 以 `$SCRIPT_DIR/../nar_xz.py` 引用；`NIXCACHE_REPO/PORT/PROXY_PID`（Task 3）在 push.sh 读取；`NIXCACHE_UPSTREAM=""`（空串 = 关闭上游回退）与 vendored 代理语义一致；`own_key_name` 命名贯穿 Task 4（修正了 rev 1 的歧义名）。

**rev 2 已知偏差（有意为之，均已在 spec 中同步）：**
- 验证 job 矩阵为单组合（ubuntu-latest + cachix）：spec 原文"复用矩阵输入"更改为单组合 + 说明（顶层 `concurrency: group: nix` 串行化下多组合会显著拉长 CI；x86_64-linux 语义一致，其他组合后续扩展）。
- CI 验证用 `paths` 模式只推 leaf：整店扫描默认路径的回归验证留给手动/后续（避免首次回补上传安装器闭包）。
- 局部断言从"文本 grep"升格为"服务端证据"（curl narinfo + proxy.log）为主：spec 措辞同步为"主断言 = 服务端证据，输出行 grep 仅为辅助"。

**Spec 修订（在 spec 文件中同步执行，本计划实现前先完成）：**
1. narinfo 字段列表：`NarHash` 明确 `sha256:<b32>` 前缀 + 注明"修正上游 cache-builder.sh 裸值缺陷"。
2. 验证节：矩阵单组合说明、CI 用 `paths` 模式、flake 钉 rev（`5dfba6236110080a54247d6460bc2ff5dda939cc`，可更新）、断言主次（服务端证据优先）。
3. 已知限制：补"CI 验证覆盖 paths 模式；整店扫描默认路径的回归验证需手动/后续"。
