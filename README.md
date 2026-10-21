# media-hub

短视频平台统一 API 服务，给产品后端调用：

- **抖音数据**：关键词搜索（支持按热度排序）、热榜、分享链接解析、作品详情、一级评论与回复、作者资料与作品、按链接获取无水印视频（直链或服务端下载）
- **视频发布**：抖音、视频号（扫码绑定账号、上传素材、定时发布、视频号草稿），异步任务 + 幂等键 + HMAC 回调
- **视频号数据**：搜索 / 详情 / 评论接口已定义，数据供应商待接入（可先用 mock 联调）
- **商业化基础**：多租户、API Key（scope、IP 白名单、过期、吊销）、按 Key 限流、按天按接口用量统计

对接文档：[`docs/API.md`](docs/API.md)（机器可读版 [`docs/openapi.json`](docs/openapi.json)）。

## 架构

```text
产品后端 ──HTTPS/API Key──▶ gateway（本项目，FastAPI，无状态，可多实例）
                              │  鉴权 / 限流 / 计量 / 统一信封 / 热度重排 / 任务表
                              ├──▶ DTK（抖音数据，Apache-2.0，打了搜索+热榜补丁）
                              │      └ postgres(timescale) · redis · downloader
                              ├──▶ worker（本项目，发布任务队列：PG SKIP LOCKED）
                              │      └──▶ SAU（social-auto-upload，MIT，无头浏览器发布）
                              └──▶ 视频号数据 Provider（商业供应商，接口已定义）
存储：hub-postgres（租户/Key/任务/用量）· hub-redis（限流）· 本地卷（素材，可换对象存储）
```

| 组件 | 来源 | 作用 |
|---|---|---|
| gateway / worker | 本仓库 `src/hub` | 对外唯一入口；业务规则、多租户、任务与回调 |
| DTK | [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) v5.1.1 + `patches/dtk` | 抖音数据：签名、身份池、调度、风控识别、缓存 |
| SAU | [dreammis/social-auto-upload](https://github.com/dreammis/social-auto-upload) | 抖音 / 视频号创作者后台的浏览器自动化发布 |

两个上游都只通过 HTTP 调用、以容器独立运行，版本钉死在 `deploy/versions.env`。原 MediaCrawler 为非商业许可，本项目不包含其任何代码。

## 目录

```text
src/hub/          网关与 worker：api/（路由）services/（业务）providers/（上游客户端）security/
tests/            pytest（respx 模拟上游，fakeredis，SQLite）
patches/dtk/      DTK 补丁：douyin.search / douyin.hot_search（附 DTK 自身测试）
deploy/           compose.yml、deploy.sh、env.example、DTK 覆盖、SAU 镜像、Caddy
docs/             API.md（对接文档）、openapi.json
```

## 部署（Linux）

**生产环境用 Linux**（推荐 Ubuntu 24.04 LTS x86_64）。所有组件都是 Linux 容器：DTK 只发布 Linux 镜像，Docker Desktop 不支持 Windows Server，Redis 没有官方 Windows 版，而 4G 内存下 Windows Server 自身就要占去一半。Windows 只适合作为开发机。

### 1. 服务器准备（一次性，需要 root）

```bash
# Docker Engine + compose 插件：https://docs.docker.com/engine/install/ubuntu/
# 4G 内存必须加 swap（发布时会启动无头浏览器）
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

国内服务器：为 Docker 配置镜像加速（`/etc/docker/daemon.json` 的 `registry-mirrors`），并在 `deploy/.env` 中把 `PIP_INDEX_URL` 改为国内源。GitHub 下载慢时可设置 `GITHUB_CODELOAD` 指向镜像。

要求：Docker Engine 24+、Compose v2.20+、2 核 4G 起步、40G 磁盘。

### 2. 部署

```bash
git clone <本仓库> media-hub && cd media-hub
deploy/deploy.sh up
```

脚本会：拉取钉死版本的 DTK / SAU 源码 → 打 DTK 补丁 → 生成 `deploy/.env` 与 `deploy/dtk.env`（随机密钥，权限 600）→ 在 DTK 官方镜像上叠加补丁构建 → 启动两套编排 → 等待健康检查 → 打印后续步骤：

1. DTK 首次启动时自动初始化管理员（`deploy/dtk-admin.txt`）并签发网关用的 DTK Key（scope：`douyin:read`、`media:read`、`media:write`、`identity:manage`，写入 `deploy/.env`）。旧 Key 缺 `identity:manage` 时，`up` 会自动换新 Key 并在网关切换后吊销旧的；
2. 给身份池放入第一个身份：`deploy/deploy.sh guest-identity`（用 SAU 的浏览器生成游客身份，热榜、作品详情、下载即可用）；
3. 为产品后端签发 Key：`deploy/deploy.sh tenant <租户名> <出口IP>/32`；
4. 扫码绑定抖音发布账号（`POST /api/v1/accounts/login-sessions`）：登录态同时导入身份池，搜索从此可用（搜索需要登录态，游客身份不行）。

其他命令：`deploy/deploy.sh status | logs [服务] | down`（`down` 不删除数据卷）。

#### 身份池怎么来

DTK 用身份池里的抖音网页端 Cookie 给数据请求签名，池子空时数据接口返回 `UPSTREAM_UNAVAILABLE`（`IDENTITY_POOL_EXHAUSTED`）。三个来源：

| 来源 | 能做什么 | 怎么来 |
|---|---|---|
| 扫码绑定的抖音账号 | 全部，含搜索 | 自动：登录成功后网关从 SAU 读回登录态并导入（`HUB_DATA_IDENTITY_FROM_LOGINS`，默认开）；删除账号时退役；失败由 worker 重试，`media-hub sync-identities` 可手动补一遍 |
| 游客身份 | 热榜、作品详情、下载（搜索不行） | `deploy/deploy.sh guest-identity` |
| 手动导入 | 看 Cookie 本身 | DTK 控制台，或 `media-hub import-jar <Playwright storage state>` |

SAU 浏览器的 User-Agent 和交给 DTK 的必须一致（DTK 按它选 TLS 指纹），compose 用同一个 `HUB_DOUYIN_USER_AGENT` 同时设置两边，升级 SAU 的 Playwright 时一起改。

### 3. 对外暴露

- 暂无备案域名：直接暴露 `8080`，**云安全组只放行产品后端的出口 IP**，Key 同时绑定 IP 白名单。产品后端与本服务在同一云内网时，走内网地址更好。
- 有备案域名后：`deploy/.env` 填 `HUB_DOMAIN`，`HUB_BIND_HOST=127.0.0.1`，`HUB_TRUSTED_PROXIES=["172.30.10.10/32"]`，再执行 `deploy/deploy.sh up`，Caddy 自动签发 HTTPS 证书。
- DTK 控制台只监听 `127.0.0.1:8000`；SAU、数据库、Redis 不暴露端口。

### 4. 资源预算（4G 单机）

| 服务 | 常驻 | 上限 |
|---|---|---|
| DTK：postgres / redis / api / worker / downloader | ≈ 350 MB | 1G / 256M / 512M / 512M / 256M |
| hub：gateway（2 进程）/ worker / postgres / redis | ≈ 250 MB | 512M / 256M / 384M / 96M |
| SAU（发布时启动 Chromium） | 空闲 ≈ 150 MB，发布时 +0.5–1 GB | 1.5G |

发布串行执行（`HUB_WORKER_CONCURRENCY=1`）；DTK 的浏览器身份铸造容器（预热后约 2.6G）在 4G 机器上不启用，身份来自扫码绑定的账号、`guest-identity` 和手动导入。

### 5. 扩容路线

1. **单机 4G**（当前）：全部服务一台机器。
2. **加一台 8G 浏览器机**：SAU 多实例并行发布；DTK `--profile browser` 自动铸造身份。
3. **多机**：gateway 多实例 + 负载均衡；数据库改云托管；素材改对象存储；worker 按队列长度扩容。服务之间只通过配置中的地址通信，拆分不需要改代码。

采集吞吐取决于**身份池（抖音账号 Cookie）和代理 IP 的规模**，不是 CPU。

### 6. 升级与备份

- 升级：修改 `deploy/versions.env`（DTK 需同时更新提交与镜像版本，并确认补丁可应用）→ `deploy/deploy.sh up`。
- 备份：`deploy/.env`、`deploy/dtk.env`（DTK 用它加密 Cookie，丢失后身份池无法解密）、数据卷 `hub_hub-postgres-data`、`dtk_postgres-data`、`hub_sau-cookies`。

## 开发

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # Linux/macOS：.venv/bin/pip
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff format src tests && .venv/Scripts/python -m ruff check src tests
```

本地直接运行（SQLite、无 Redis）：`uvicorn hub.main:create_app --factory --port 8080`，配置见 `src/hub/settings.py`（环境变量前缀 `HUB_`）。

改动接口后重新导出 OpenAPI（`tests/test_cli.py` 会检查仓库里的 `docs/openapi.json` 与代码一致）：

```bash
media-hub export-openapi
```

## 扩展新平台

平台能力集中登记在 `src/hub/platforms.py`（`PLATFORMS`），`GET /api/v1/platforms` 把它连同当前部署的可用状态发布给客户端，产品端据此动态展示平台，新增平台不需要改产品端代码。

| 要支持的能力 | 需要做的事 |
|---|---|
| 发布（SAU 已支持的平台：快手、小红书等） | `constants.Platform` 与 `SAU_PLATFORM_TYPES` 已有即可，在 `deploy/.env` 的 `HUB_PUBLISH_PLATFORMS` 中加入平台，`deploy.sh up` |
| 发布（SAU 不支持的平台，或改走官方开放平台） | 实现一个发布驱动（与 `providers/sau.py` 同等接口），在 `services/publishing.py` 按平台选择驱动 |
| 数据查询 | 实现数据 Provider（参考 `providers/channels.py` 的 Protocol），在 `PLATFORMS` 中登记 `data_backend` 与 `data_capabilities`，新增 `/api/v1/{platform}/...` 路由并返回统一的 Content / Author / Comment 模型 |

每个新平台都要补测试（respx 模拟上游），并执行 `media-hub export-openapi` 更新接口文档。

## 已知限制

- 视频号的搜索、他人作品与评论、按链接下载依赖商业数据供应商，接入前返回 `501 NOT_CONFIGURED`；自己作品的评论与数据尚未实现。
- 抖音发布走浏览器自动化，未接抖音开放平台（`providers/` 预留了扩展位置）；平台改版可能导致短期失败。
- DTK 补丁的搜索参数与热榜解析基于网页端接口的已知格式编写，已通过 DTK 全量单元测试，上线前需用真实账号做一次联调（`deploy/deploy.sh up` 后调用 `/api/v1/douyin/search` 与 `/api/v1/douyin/hot`）。
