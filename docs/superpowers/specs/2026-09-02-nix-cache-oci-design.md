# nix/cache — GHCR/OCI 后端 Nix 二进制缓存 Action 设计

日期：2026-09-02
状态：已与用户确认，待实现

## 背景与目标

本仓库提供自定义 composite GitHub Actions 用于 Nix CI。现有缓存机制
`nix/` + `nix/post/` 基于 `actions/cache` 对 `/nix/store` 做**全量快照**
（按 `CACHE_KEY` 存取，有体积/时效限制，仅限同一仓库）。

目标：新增 `nix/cache` 目录，实现类似 `cachix/cachix-action` 的功能
（pull：启动 substituter 并配置 Nix；push：job 结束时上传本地新构建路径），
但**不使用 Cachix 服务**，而是：

- **pull**：使用 vendored 的
  `https://github.com/cmspam/nixcache-oci/blob/main/proxy/main.py`
  （本地只读代理，实现 Nix 二进制缓存协议，后端为 GHCR OCI 仓库
  `ghcr.io/<owner>/<repo>/nix-cache`）。
- **push**：直接调 GHCR OCI API（纯 curl，同上游 `lib/cache-builder.sh`
  的做法），上传 NAR blob 并重建 `cache-index` manifest。

## 已确认的决策

| 决策点 | 选择 |
|---|---|
| 目录位置 | `nix/cache/`（与 `nix/`、`nix/post/`、`nix/debug/` 平级，符合仓库约定） |
| 入口形态 | 两个 action：`nix/cache`（pull）+ `nix/cache/post`（push），同 `nix/` + `nix/post/` 模式 |
| push 范围 | store 快照 diff（预开始时快照 /nix/store，post 时对比新增），过滤外部签名/已在 GHCR index 的路径；可选 `paths` 输入覆盖 |
| proxy 交付 | vendor `main.py` 进仓库，文件头记录上游 commit SHA |
| 签名 | 可选 `signing_key`；默认不签名（pull 端 `require-sigs = false`） |

## 架构

```
Job 开始                                    Job 结束
┌──────────────┐  启动 + 配置   ┌──────────┐   ┌──────────────────┐
│ nix/cache    │ ────────────▶ │ proxy    │   │ nix/cache/post    │
│ (pull)       │               │ 127.0.0.1│   │ (push)            │
│              │               │ :PORT    │   │                  │
│ 快照 /nix/store ────────────▶│          │   │ store diff + paths│
└──────────────┘               │          │   │ 过滤签名/GHCR已存 │
       │                       └────┬─────┘   │ nix-store --dump  │
       │ nix.conf: extra-substituters│         │ + python3 lzma    │
       ▼                            │         │ GHCR OCI 上传     │
  Nix 构建 ───── substitutes ────────┘ (NAR 流式转发)  └────────┬───────┘
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
| `repo` | `${{ github.repository }}` | GHCR 上的 `owner/repo`（必须小写，脚本 tolower 兜底），缓存镜像为 `ghcr.io/<repo>/nix-cache`；指向其他仓库可实现跨仓库共享 |
| `registry` | `ghcr.io` | OCI registry |
| `token` | `${{ github.token }}` | 拉取私有缓存（scope pull） |

`cache.sh pre` 的执行顺序：

1. 端口选择：尝试 `37515`，被占用则用 python3 `socket.bind(0)` 找空闲端口；端口写入 `GITHUB_ENV`。
2. 启动 vendored `nixcache-proxy.py`（后台进程），环境：
   - `NIXCACHE_REPO` / `NIXCACHE_REGISTRY`（来自输入）
   - `NIXCACHE_UPSTREAM=""`（Nix 并行查询 cache.nixos.org；避免代理内串行回退）
   - `GITHUB_TOKEN` = token 输入
3. 等待 `GET /nix-cache-info` 返回 200（最多 15 次、间隔 1s）；失败则打警告继续，**不 fail**（缓存尽力而为）。
4. 配置 Nix：
   - 多用户 daemon：`sudo tee -a /etc/nix/nix.conf`；同时写 `~/.config/nix/nix.conf`（覆盖 single-user 场景）。
   - 始终追加：`extra-substituters = http://127.0.0.1:PORT`、`extra-trusted-substituters = http://127.0.0.1:PORT`。
   - 取 `GET http://127.0.0.1:PORT/public-key`：200 → 追加 `extra-trusted-public-keys = <key>`；404 → 追加 `require-sigs = false` 并打印警告（影响所有 substituter）。
5. 重启 nix-daemon 使配置生效：Linux `sudo systemctl restart nix-daemon || true`；macOS `launchctl unload/load`（探测方式沿用 `nix/restore.sh`）。
6. 快照 store：`shopt -s nullglob` 后用 glob `printf '%s\n' /nix/store/*/`（空 store 时得空列表，
   不输出字面模式）排序写入 `$RUNNER_TEMP/nix-cache-store-<timestamp>`，路径写入
   `GITHUB_ENV`（`NIXCACHE_STORE_SNAPSHOT`）。
7. `GITHUB_ENV` 另写：`NIXCACHE_REPO`、`NIXCACHE_REGISTRY`、`NIXCACHE_PORT`。

使用约束：**必须先于 `nix/cache/post` 且在 `nix/`（安装 Nix）之后使用**。

### 2. `nix/cache/post/action.yml`（push）

输入：`repo`、`registry`、`token`（默认 `${{ github.token }}`）、`signing_key`
（可选 secret）、`paths`（可选，空格分隔的 store 路径；默认空 = 用快照 diff）。

`push.sh` 执行顺序：

1. **候选集**：若 `paths` 非空 → `nix path-info --recursive <paths>` 的闭包；
   否则 → 快照 diff（`comm` 两个排序列表的新增项）。
2. **前置守卫**（避免破坏既有缓存）：
   - 拉取当前 `cache-index`（manifest → layer digest → blob）：**404 = 空 index 正常继续**；
     **其他错误（网络/认证）→ 打印警告并退出 0，跳过本轮上传**（当作空 index 合并会丢失全部旧条目）。
   - 现有 index 携带 `public_key` 但本轮未提供 `signing_key` → 打印警告并退出 0，跳过上传
     （向已签名缓存注入无签名 narinfo 会导致客户端验签永久拒绝这些新条目）。
3. **过滤**：
   - 已存在 entries 里的 hash 跳过（上次已传）。
   - 其余路径 `nix path-info --json`，带 `signatures` 的跳过
     （= 从 cache.nixos.org 等外部缓存替换来的，上游可继续直接服务）。
   - 剩余 = 本次本地新构建路径。
4. 若提供 `signing_key`：先 `nix store sign --key-file` 这些路径（签名进入 narinfo 的 `Sig`）。
5. 逐路径导出（顺序执行，v1 不做并行）：
   - `nix-store --dump <path> | python3 -m <lzma 流式压缩器>`（python3 stdlib `lzma`，preset=1；
     避免依赖 `xz` 二进制，Linux/macOS 通用；流式压缩不整文件进内存）。
   - FileHash：直接调 `nix hash file --type sha256 --base32 <nar_file>`（Nix-base32 输出，
     与上游 `cache-builder.sh` 一致；Nix ≥ 2.18，本仓库使用 26.05 channel，已满足）。
   - narinfo 生成（格式与上游 `cache-builder.sh` 完全一致）：
     `StorePath`、`URL: nar/<hash>.nar.xz`、`Compression: xz`、`FileHash: sha256:<b32>`、
     `FileSize`、`NarHash`、`NarSize`、`References`（basename 空格分隔）、`Deriver`（basename）、`Sig`*。
   - narHash/narSize/references/deriver/signatures 取自 `nix path-info --json`。
6. **上传**（纯 curl，OCI 协议）：
   - 认证：`token`（GITHUB_TOKEN 或 PAT）→
     `GET https://<registry>/token?scope=repository:<repo>/nix-cache:pull,push&service=<registry>`
     换 registry token（Basic `token:<cred>`）。
   - blob：HEAD 查存在 → 不存在则 POST `blobs/uploads/` 取 Location → PUT（digest 参数）。
   - manifest：以 `cache-index` 为 tag PUT OCI manifest（config 空 blob + 1 个 index layer）。
7. **重建 index**：下载现有 `cache-index` blob，合并 entries（新条目覆盖同 hash），
   `public_key` = 本 repo 脚本传入的公钥（无 key 时保留现有值），`generated` = UTC now；
   字段格式与上游一致（version/repo/registry/image/generated/public_key/entries/gc_roots），
   **保证 vendored proxy 零改动可读**。
8. 清理：kill 代理进程（若有）；输出统计（上传/跳过/hash 列表）到 `GITHUB_STEP_SUMMARY`。

**权限要求**：push 需要 job 级 `permissions: packages: write`；私有缓存 pull 需要
`packages: read`（写入 README 与示例）。

### 3. `nix/cache/nixcache-proxy.py`（vendored）

- 逐字拷贝自 `cmspam/nixcache-oci/proxy/main.py`，钉住上游 commit
  `2e21568cf2cf0824ea6f5e9ce54179aee19cbf6e`（main，2026-08-31，
  https://github.com/cmspam/nixcache-oci/blob/2e21568cf2cf0824ea6f5e9ce54179aee19cbf6e/proxy/main.py）。
- 文件头注释注明：来源 URL、commit SHA、同步策略（上游更新时重新拷贝并更新 SHA）。
- 唯一改动：不允许（保持与上游一致，便于 diff/同步）。

## 文件清单

```
nix/cache/action.yml
nix/cache/cache.sh          # 主脚本（"$@" 分派，如 pre）
nix/cache/nixcache-proxy.py # vendored
nix/cache/post/action.yml
nix/cache/post/push.sh
docs/superpowers/specs/2026-09-02-nix-cache-oci-design.md
```

新增内容遵循仓库约定：

- 脚本 `#!/usr/bin/env bash` + `set -eo pipefail`；2 空格缩进，120 列上限。
- action.yml 用 `runs.using: composite` + `shell: bash` + `exec ${{ github.action_path }}/xxx.sh`。
- 外部 action 一律 SHA 钉住（本设计不引入新外部 action）。
- `GITHUB_ENV` 写状态（沿用 `restore.sh`/`save.sh` 模式）。

## 验证

在 `.github/workflows/nix.yml` 增加（复用现有"先构建、再验证"双 job 模式）：

1. `nix_cache` job（权限 `packages: write`）：
   - `./nix`（安装 Nix）→ `./nix/cache` → 在临时目录写一个最小 flake
     （`mkDerivation` 生成确定性输出，保证**不存在于 cache.nixos.org**）→ 构建该包 →
     `./nix/cache/post` → 用 curl 断言 `ghcr.io/<repo>/nix-cache` 的 `cache-index`
     包含该 store hash 的 entry（OCI token 用 GITHUB_TOKEN 换）。
2. `nix_cache_with_cache` job（needs `nix_cache`）：
   - `./nix` → `./nix/cache` → 同一 flake 构建（**不覆盖 substituters**：
     `nix/cache` 已把本缓存追加为 extra-substituter，cache.nixos.org 仍然并行服务 stdenv 等
     上游路径；若只留本缓存，job1 里替换来的 stdenv 闭包不在我们的 index 里，会全量重编译）。
   - 断言（leaf 包是确定性小包、**不存在于 cache.nixos.org**，故"被 fetched"的唯二来源
     只能是我们 proxy；构建输出默认会列出 `these N paths will be fetched` / `will be built`）：
     leaf 的 store 路径出现在 **fetched** 行列中、且不出现在 **built** 行列——
     证明 Nix 从本缓存替换成功而非重编译。
   - 依赖：上一个 job 的 push 已把 entry 写入 GHCR index；本 job 代理启动时预取索引。
3. trigger paths 追加 `nix/cache/**`。
4. `AGENTS.md` 的 actions 表补 `nix/cache` 与 `nix/cache/post` 两行，简述缓存机制。

## 已知限制（v1 不做，文档化）

- **索引合并竞态**：两 job 同时 push 时后写覆盖先写（最后一次 wins）；典型工作流串行，可接受。
- **未签名模式**需要 `require-sigs = false`，影响所有 substituter 的验签；
  长期使用建议配 `signing_key`。
- **签名状态切换**：存量缓存从"无签名"转为"有签名"后，旧的无签名条目对开启验签的客户端
  不可用（回退到本地重编译）；反向（有签名→无签名）由前置守卫阻止上传，只保留旧条目。
- GHCR 私有仓库存储/带宽有配额，公开仓库无限制（上游已注明）。
- proxy 仅缓存索引 + 流式转发 NAR，不在磁盘缓存 NAR；索引 TTL 300s 只影响长驻代理，
  每个 CI job 都是新进程，跨 job 无陈旧索引问题。

## 非目标

- 修改 `nix/`、`nix/post/` 现有逻辑。
- 支持 Windows。
- 实现上游 `gc-cache.yml`（GC/保留策略，后续可按需加）。
- 在 proxy 内实现写路径（保持上游只读代理契约）。
