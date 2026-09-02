# nix/cache — GHCR/OCI 后端 Nix 二进制缓存 Action 设计

日期：2026-09-02（rev 2：吸收 5 方独立审查的修正意见）
状态：设计已确认（两轮：用户决策 + 批量审查），待实现

## 背景与目标

本仓库提供自定义 composite GitHub Actions 用于 Nix CI。现有缓存机制
`nix/` + `nix/post/` 基于 `actions/cache` 对 `/nix/store` 做**全量快照**。
新增 `nix/cache`：实现类似 `cachix/cachix-action` 的 pull（配置 substituter）
+ push（上传本地新构建路径），但**不使用 Cachix 服务**：

- **pull**：运行 vendored 的 `cmspam/nixcache-oci/proxy/main.py`（本地只读代理，
  实现 Nix 二进制缓存协议；后端为 GHCR OCI 仓库 `ghcr.io/<repo>/nix-cache`）。
- **push**：直接调 GHCR OCI API（纯 curl，同上游 `lib/cache-builder.sh`），
  上传 NAR blob 并重建 `cache-index` manifest。

## 已确认的决策

| 决策点 | 选择 |
|---|---|
| 目录位置 | `nix/cache/`（与 `nix/`、`nix/post/`、`nix/debug/` 平级） |
| 入口形态 | 两个 action：`nix/cache`（pull）+ `nix/cache/post`（push），同 `nix/` + `nix/post/` 模式 |
| push 候选集 | **整店无签名扫描**（无快照 diff）：候选 = 未入 GHCR index 且无外部签名的 store 路径全集；`paths` 输入保留为显式/安全模式（multi-user 逃生舱） |
| 签名 | 可选 `signing_key`；**信任锚本地化**：pull 用 `public_key` 输入，不做网络自报公钥的自动信任 |
| registry | 仅 ghcr.io（不设输入，token 换发不指向任意主机） |
| proxy 交付 | vendor（钉 commit SHA + 文件头出处注释）；上游仓库无 LICENSE，AGENTS.md 记录该风险并保持文件头完整出处 |
| 运行前提 | 单租户 runner（GitHub-hosted）；自托管共享/多 job runner 需用 `paths` 模式并自行评估 |

## 架构

```
Job 开始                                    Job 结束
┌──────────────┐  启动 + 配置   ┌──────────┐   ┌──────────────────┐
│ nix/cache    │ ────────────▶ │ proxy    │   │ nix/cache/post    │
│ (pull)       │               │ 127.0.0.1│   │ (push)            │
│              │               │ :PORT    │   │                  │
│ 无快照        │               │          │   │ 整店无签名扫描    │
└──────────────┘               │          │   │ + paths 输入      │
       │                       └────┬─────┘   │ 过滤: index已存/  │
       │ nix.conf marker 块          │(NAR 流式) │ 外部签名         │
       ▼                            │         │ dump+python3 lzma │
  Nix 构建 ───── substitutes ────────┘         │ GHCR OCI 上传     │
                                               └────────┬────────┘
                                                        ▼
                                             ghcr.io/<repo>/nix-cache
                                             (NAR blobs + cache-index)
```

与现有 `nix/`、`nix/post/`（actions/cache 全量快照）**互补**，不修改它们。

## 组件

### 1. `nix/cache/action.yml`（pull）

输入：

| 输入 | 默认 | 说明 |
|---|---|---|
| `repo` | `${{ github.repository }}` | GHCR 的 `owner/repo`（脚本 tolower 兜底；GHCR 包名必须小写）。指向其他仓库 = 跨仓库共享缓存，文档要求仅用 fine-grained PAT 且**不得来自不可信输入**（如 fork 的 head repo） |
| `token` | `${{ github.token }}` | 私有缓存 pull 用（scope pull）；无需 `packages: read` 之外权限 |
| `public_key` | 空 | **本地信任锚**：本缓存的签名公钥（`name:base64`）。提供时信任该 key；不提供 = 未签名模式（见步骤 4） |

`cache.sh` 执行（单模式，无需 dispatch）：

1. **前置检查**：`python3 -c 'import sys; assert sys.version_info >= (3, 10)'`——
   vendored 代理用 PEP 604 注解，Python <3.10 直接 `::error::` 退出（不静默降级）。
2. **选端口**：恒用 `python3 -c 'socket.bind(0)'` 取空闲端口（不尝试固定 37515，
   避免抢占竞态），记 `$PORT`。
3. **启动代理**（后台，写 PID 到 `$RUNNER_TEMP/nixcache-proxy.pid`）：
   - `NIXCACHE_REPO=<repo小写>`、`NIXCACHE_PORT=$PORT`、`NIXCACHE_UPSTREAM=""`、
     `NIXCACHE_INDEX_DIR=$RUNNER_TEMP/nixcache-proxy`（0700，非 `~/.cache`）、
     `GITHUB_TOKEN=<token>`。
   - **注意**：`NIXCACHE_PORT` 必须与所选端口同一值（历史 bug：端口回退分支下
     漏传该变量导致代理与配置指向不同端口 → 缓存静默失效）。
4. **健康检查与身份校验**（用 `$PORT`，最多 15×1s）：
   - `GET /nix-cache-info` 200；
   - `GET /_status`，`jq -e '.repo'` 与输入 `repo`（小写）一致；
   - 任一失败 → kill 代理、`::warning::` + step summary，**并跳到步骤 8**（绝不把
     死端口/不可信端口写进 substituter 配置；此时代理未启动，PID 记为空值）。
5. **配置 Nix**（幂等 marker 块 `# nix-cache begin`/`# nix-cache end`；先 `sed` 删旧块
   再追加新块；写入 daemon 配置 `/etc/nix/nix.conf`（`sudo tee`）与
   `~/.config/nix/nix.conf` 两份，两处包含同一块）：
   - `extra-substituters = http://127.0.0.1:$PORT`
   - `extra-trusted-substituters = http://127.0.0.1:$PORT`
   - 若提供了 `public_key`：`extra-trusted-public-keys = <public_key>`；并对比
     `GET /public-key`（index 自报）：不一致 → `::warning::`（以本地输入为准继续）。
   - 若未提供 `public_key`：`require-sigs = false` + `::warning::`（影响所有
     substituter，仅建议单租户 CI；文档写明替代：提供 `public_key`）。**该行必须
     写入 daemon 配置**（用户级配置对 root daemon 的验签不生效）。
6. **重启 daemon**（探测 `/nix/var/nix/daemon-socket`，沿用 `nix/restore.sh` 方式）：
   Linux `sudo systemctl restart nix-daemon`；macOS `launchctl unload` + `load -w`。
   失败 → `::warning::` + step summary 写明"配置未生效"（不静默）。
7. **自检**：`nix show-config` 输出中 substituters 含 `http://127.0.0.1:$PORT`，
   否则 `::warning::`。
8. 写 `GITHUB_ENV`：`NIXCACHE_REPO`、`NIXCACHE_PORT`、`NIXCACHE_PROXY_PID`。

使用约束：在 `nix/`（安装 Nix）之后、任何 `nix build` 之前使用；`nix/cache/post`
不是本 action 的后置钩子（分体设计），使用时需按文档示例成对出现。

### 2. `nix/cache/post/action.yml`（push）

输入：`repo`（默认 `${{ github.repository }}`）、`token`（默认 `${{ github.token }}`）、
`signing_key`（可选 secret；由调用方 `with: signing_key: ${{ secrets.* }}` 传入——
**composite action 不能直接访问 `secrets` context**）、`paths`（可选，空格分隔的
store 路径；`--` 分隔 + 逐一校验 `/nix/store/<32hex>-*` 前缀，防参数注入）。

`push.sh` 执行顺序：

1. **清理**：若 `NIXCACHE_PROXY_PID` 非空且 `kill -0` 有效（先 `kill -0` 探测 + 端口
   探测双重确认，防误杀同 runner 其他 job），kill 之；随后移除两处 nix.conf 的
   marker 块（best-effort，`sed` 失败只警告——自托管 runner 恢复原状，避免残留
   死端口 substituter 与 `require-sigs=false` 跨 job 累积）。
2. **候选集**：
   - `paths` 输入 → `nix path-info --recursive --json <paths>` 闭包；
   - 否则 → 整店：`shopt -s nullglob; printf '%s\n' /nix/store/*/`（过滤空行），
     列表写临时文件。
   - 路径数可能上千：所有 `nix path-info`/`nix store sign` 一律 `xargs -n 128` 分批
     （macOS ARG_MAX 仅 256KiB），结果写文件，避免 argv 超限。
3. **拉取现有 index**（manifest → layer digest → blob，token 换发见步骤 6）：
   - 404 → 视为空 index，正常继续；
   - 其他错误（网络/认证）→ `::warning::` + 退出 0，**跳过本轮上传**（防止按空
     index 合并丢光旧条目）。
4. **前置守卫**：
   - index 携带 `public_key` 而本轮未提供 `signing_key` → `::warning::` + 退出 0
     （禁止向已签名缓存注入无签名 narinfo）。
   - 提供 `signing_key` 而 index 已有**不同** `public_key` → `::error::` + 退出 1
     （key 轮换视为冲突，不静默覆盖）。
5. **签名**（仅当提供 `signing_key`）：
   - `nix store sign --key-file`（分批）；
   - **校验**：`nix path-info --json` 逐路径确认含本缓存 key 名的签名；任一缺失 →
     `::error::` + 退出 1（历史 bug：签名失败静默继续会写出"无 Sig 但 index 带
     public_key"→ 验签客户端永久拒收）；
   - `OWN_KEY_NAME` = `nix key convert-secret-to-public` 输出的 key 名前缀。
6. **过滤**：
   - 路径的 32 位 hash ∈ index entries → 跳过（上次已传）；
   - 剩余路径：**存在非本缓存 key 名的签名 → 跳过**（= 从 cache.nixos.org 等外部
     缓存替换来的；无 key 模式即"存在任何签名"）。本缓存自己签名但未入 index 的
     路径**保留**（上轮传了 blob 没更新 index 的路径本轮自动重试——修复"永久漏传"）。
   - 多余候选（未入 index、无外部签名）= 本轮上传集。
7. **导出与计算**（顺序执行；工作目录 `$RUNNER_TEMP/nixcache-work`，`df` 预检剩余
   空间 ≥ 预计量，每路径完成后删除 nar 文件）：
   - `nix-store --dump <path> | python3 nix/cache/nar_xz.py > <hash>.nar.xz`
     （stdlib `lzma`，preset=1，**FORMAT_XZ** —— 流式压缩，不整文件进内存；
     写死 `FORMAT_XZ` 也不依赖 `xz` 二进制，Linux/macOS 通用）；
   - FileHash：`nix hash file --type sha256 --base32 <nar.xz>`（Nix-base32；降级链：
     `nix-hash --flat --type sha256 --base32`）；
   - **NarHash 归一化**：`nix path-info --json` 的 `narHash` 在 Nix ≥2.34 可能为
     SRI（`sha256-<b64>`），写入 narinfo 前统一 `nix hash convert --to base32`
     转 Nix-base32（协议规范要求）；注意 `nix hash convert` 输出**裸 base32**，
     narinfo 中必须补 `sha256:` 前缀——Nix 客户端 `parseAnyPrefixed` 拒绝裸值，
     上游 cache-builder.sh 的裸值写法是**上游缺陷**，勿跟随；
   - narinfo 生成（python；字段与上游一致，除上述 NarHash 修正外）：`StorePath`、
     `URL: nar/<hash>.nar.xz`、`Compression: xz`、`FileHash: sha256:<b32>`、
     `FileSize`、`NarHash: sha256:<b32>`、`NarSize`（**必须 >0**，否则跳过并警告——
     客户端将 `NarSize: 0` 视为 corrupt）、`References`（basename 空格分隔）、
     分隔）、`Deriver`（basename）、`Sig`*（来自 path-info，仅签名模式）。
8. **上传**（纯 curl，OCI 协议；`--retry 3 --retry-all-errors --retry-delay 2
   --max-time`，PUT 按 digest 幂等可安全重试）：
   - 认证：`token` → `GET https://ghcr.io/token?scope=repository:<repo>/nix-cache:pull,push
     &service=ghcr.io`；凭据只经 `-H "Authorization: Basic <b64>"`（不进
     `/proc/<pid>/cmdline`）；脚本首行 `echo "::add-mask::${TOKEN}"`，全程不打印 token；
   - blob：HEAD 查存在 → 不存在 POST `blobs/uploads/` → 解析 **Location（可能相对
     路径，按上游 cache-builder.sh 处理拼成绝对 URL）** → PUT（digest 参数，
     **`?`/`&` 分隔符处理**）；
   - blob 预检：> ~10 GiB 跳过 + `::warning::`（GHCR 单 blob 上限）；
   - manifest：以 `cache-index` 为 tag PUT（config 空 blob + 1 个 index layer）。
9. **重建 index**（python，**全部经文件传递**——receipts JSONL + 现有 index 文件，
   防 ARG_MAX，照抄上游模式）：
   - 合并 entries（新条目覆盖同 hash），`generated`=UTC now，`gc_roots: []`；
   - `public_key`：签名模式 = 本缓存 key 的公开部分；非签名模式 = 保留现有值
     （步骤 4 守卫已保证此时必为空）；字段格式与上游一致（version/repo/registry/
     image/generated/public_key/entries/gc_roots）→ **vendored proxy 零改动可读**。
10. **回读验证**：PUT 后轮询 GET `cache-index` manifest 直到其 layer digest 等于
    本此上传的 index blob（最长 ~60s；digest 比 `generated` 更稳——同秒两次 push
    不会误判）；失败 → `::error::` + 退出 1（blob 已传、index 未更新时本轮 fail，
    下轮由步骤 6 的"本缓存签名保留"规则自动重试——不再永久丢）。
11. 统计（上传/跳过数）写 `GITHUB_STEP_SUMMARY`（**不含** narinfo 原文/凭据）；
    `::warning::`/`::error::` 保证失败可见（历史问题：403 静默降级让人误以为缓存成功）。

**权限要求**：push 需要 job 级 `permissions: packages: write`；私有缓存 pull 需要
`packages: read`。fork PR：GITHUB_TOKEN 只读、secrets 不可用 → push 被守卫跳过
（`::warning::`），私有缓存不可读（文档写明）。

### 3. `nix/cache/nixcache-proxy.py`（vendored）

- 逐字拷贝自 `cmspam/nixcache-oci/proxy/main.py`，钉住上游 commit
  `2e21568cf2cf0824ea6f5e9ce54179aee19cbf6e`（main，2026-08-31；URL 直接带 commit
  SHA，不用 main 分支）；文件头注释：来源 URL、commit SHA、文件 sha256、同步策略
  （上游变更 = 重新拷贝 + 人审 diff + 更新 SHA/校验值）、**上游无 LICENSE 的版权
  风险声明**（向作者提出申请，AGENTS.md 记录）。
- 唯一改动：不允许。运行时要求 Python ≥3.10（步骤 1 已硬检查）。

## 文件清单

```
nix/cache/action.yml
nix/cache/cache.sh           # pull 脚本（单模式执行体）
nix/cache/nar_xz.py          # lzma 流式压缩器（stdin→stdout，FORMAT_XZ, preset 1）
nix/cache/nixcache-proxy.py  # vendored
nix/cache/post/action.yml
nix/cache/post/push.sh
docs/superpowers/specs/2026-09-02-nix-cache-oci-design.md
```

新增内容遵循仓库约定：

- 脚本 `#!/usr/bin/env bash` + `set -eo pipefail`；**bash 3.2 兼容子集**（无 assoc
  array/mapfile/`${var,,}`/nameref——GitHub macOS runner 仍为 Bash 3.2.57）；
  2 空格缩进，120 列上限。
- action.yml 用 `runs.using: composite` + `shell: bash` + `exec ${{ github.action_path }}/xxx.sh`；
  每个输入带 `description`，YAML 过 `yamfmt`。
- 外部 action 一律 SHA 钉住（本设计不引入新外部 action）。
- `GITHUB_ENV` 传状态（`NIXCACHE_REPO/PORT/PROXY_PID`），与 `restore.sh`/`save.sh`
  模式一致；与既有 `CACHE_KEY`/`CACHE_TIMESTAMP` 等无命名冲突。

## 验证

在 `.github/workflows/nix.yml` 增加两个 job（沿用现有"先构建、再验证"模式）：

1. `nix_cache`（`needs: clear_cache`，`if: ${{ always() && !failure() && !cancelled() }}`
   + fork 守卫——fork PR 的 token 只读，push 必失败，两个 job 均跳过；
   job 级 `permissions: contents: read` + `packages: write`——job 级 permissions
   未声明的作用域**无权限**，缺 `contents: read` 连 checkout 都会 403）。
   矩阵为**单组合**（`ubuntu-latest` + `install_action: cachix`）：spec 原"复用
   现有矩阵输入"改为单组合，因顶层 `concurrency: group: nix` 串行化下多组合会
   显著拉长 CI，且 x86_64-linux 语义一致，其他组合后续扩展：
   - `./nix` → `./nix/cache`（未签名模式）→ 临时目录写最小 flake
     （nixpkgs 输入钉 rev `5dfba6236110080a54247d6460bc2ff5dda939cc`——
     **移动分支会在两次 job 之间漂移导致 leaf 路径不一致**；`mkDerivation`
     确定性输出，**不存在于 cache.nixos.org**）→ `nix build --print-out-paths`
     取 LEAF → `./nix/cache/post` **带 `paths: ${{ env.LEAF_PATH }}`**（CI 用
     paths 模式只推 leaf，避免整店扫描首轮回补上传安装器闭包；整店扫描默认
     路径的回归验证留给手动/后续）→ curl 换 token 后 GET `cache-index` 断言
     含 LEAF hash（带重试 ~60s，`GITHUB_REPOSITORY` 转小写）；
   - 可选：`./nix/post` 照旧（验证与 actions/cache 并存不冲突）。
2. `nix_cache_with_cache`（`needs: nix_cache`，同样守卫 + fork 守卫；
   job 级 `permissions: contents: read` + `packages: read`；**不用 `./nix/post`**——
   避免把 OCI 替换后的 store 写回 actions/cache）：
   - `./nix` → `./nix/cache` → **`nix eval --raw .#cache-test.outPath` 算 LEAF**
     （`nix path-info .#cache-test` 在路径不在 store 时会**直接失败**，不能用）→
     `nix store delete <LEAF>`（确保不在 store）→ `curl -X POST /_refresh` 并等待
     entries > 0（防冷启动索引未拉完/TTL；端口取 `$NIXCACHE_PORT`，不要 grep
     nix.conf）→ `nix build` 捕获输出 →
     **主断言（服务端证据，不依赖 Nix 版本文案）**：① curl
     `http://127.0.0.1:$port/<LEAF-hash>.narinfo` 200；② `proxy.log` 出现该
     narinfo 的 GET（代理逐请求记日志）。**辅助断言**：LEAF 出现在
     "will be fetched" 且不在 "will be built"（版本敏感文案，仅作辅助）。
   - flake 确定性：mkDerivation 的 store 路径由输入内容+derivation 决定
     （与源文件 mtime 无关）；两个 job 的 flake.nix 钉同一 nixpkgs rev 即可复现。
3. `AGENTS.md` 更新：actions 表补两行；"How the Nix cache works" 并列说明 GHCR
   机制与 actions/cache 快照的关系（互补、何时用哪个）；约定清单加 `nix/cache:`、
   `nix/cache/post:` 前缀；vendored 文件同步策略（钉 SHA、sha256 校验、人审）与
   上游无 LICENSE 的说明；`dependabot.yml` 目录清单补 `/nix/cache`、`/nix/cache/post`
   （顺带补 `/nix/debug`）。
4. trigger paths 无需改动（现有 `nix/**` 已覆盖 `nix/cache/**`）。

## 已知限制（v1 不做，文档化）

- **CI 验证覆盖面**：验证工作流用 `paths` 模式只推 leaf（确定性、快）；整店扫描
  默认路径的回归验证需手动/后续（首轮回补等行为见下）。
- **首轮回补**：无签名模式下首次启用会把从未入 index 的本地位路径（含 actions/cache
  恢复的）一次性上传；之后由 index 过滤自愈。大 store 首轮耗时/磁盘预算见下。
- **单租户假设**：仅支持 GitHub-hosted 或独占 runner。自托管多 job/共享 runner：
  整店扫描可能看到他人构建的路径（上传前有签名过滤兜底，但无签名路径仍会入选）、
  nix.conf 端口冲突、代理 PID 误判——此类环境请用 `paths` 模式并自行评估。
- **签名信任边界**：能写 GHCR 包 = 完全控制使用该缓存的 CI；未签名模式
  `require-sigs=false` 削弱全部 substituter 验签；跨仓库共享仅建议 fine-grained PAT
  + 显式 `public_key`；`repo` 不得来自不可信输入。
- **索引合并竞态**：跨仓库并发 push 仍是 last-write-wins（工作流级 `concurrency`
  可串行化本仓库内并发；`public_key` 单调规则防降级为无签名）。
- **规模预算**：大 NAR（数 GB）串行 dump+压缩+上传，时长/临时磁盘未做上限管理；
  GHCR 15GiB 级 blob 预检跳过；私有仓库存储/带宽配额。
- vendored 代理需要 Python ≥3.10（旧 macOS CLT 3.9 已硬检查 loud fail；可用
  nixpkgs 的 python3 规避）。
- fork PR：无 `packages: write`/secrets，私有缓存不可用（公开缓存 pull 匿名可用）。

## 非目标

- 修改 `nix/`、`nix/post/` 现有逻辑。
- 支持 Windows。
- 实现上游 `gc-cache.yml`（GC/保留策略）。
- 在 proxy 内实现写路径（保持上游只读代理契约）。
- key 轮换（视为冲突 fail；轮换 = 未来版本的手动流程）。
