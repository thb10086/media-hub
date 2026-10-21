# AGENTS.md — media-hub

短视频统一 API 服务（商业项目）：抖音数据 + 抖音/视频号发布 + 视频号数据 Provider，面向 Web SaaS 后端调用。
对外契约：`docs/API.md` + `docs/openapi.json`；部署：`deploy/`；用户说明：`README.md`。

## 命令（Windows 开发机，仓库根目录）

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"         # 首次
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider    # 全部测试（当前 94 项）
.\.venv\Scripts\python.exe -m ruff format src tests
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\media-hub.exe export-openapi                    # 改接口后必须执行，test_cli 会校验
.\.venv\Scripts\shellcheck.exe -x -s bash deploy/deploy.sh      # 改部署脚本后执行
```

- 代码需兼容 Python 3.10（ruff target py310）；镜像运行 3.12。3.12 验证环境：`.upstream/.venv-hub312`（uv 管理的 Python 在 `.upstream/.python`）。
- 本机 `bash` 指向 WSL 占位程序，要用 Git Bash：`D:\DevelopersTool\Git\bin\bash.exe`。
- 本机 `no_proxy` 含 `::1`，会让 httpx 抛 `InvalidURL: Invalid port`。网关用 `hub.http` 规避；跑上游测试时设置 `no_proxy=127.0.0.1,localhost`。
- 本机没有 Docker，镜像与编排只在服务器上验证。

## 架构与分层

```text
api/          路由：参数校验、scope、调用 service、ok() 信封。不写业务逻辑
services/     tasks（幂等/认领/终态）、data（200/202 结算、热度后处理）、publishing、accounts（扫码事件流）、
              login_sessions（轮询式扫码会话，Redis）、webhooks（HMAC）、ranking（纯函数）、usage、tenants、media、urls
providers/    dtk.py（DTK REST，错误码映射）、sau.py（SAU Flask）、channels.py（Protocol + none/mock）
platforms.py  平台注册表：显示名、数据后端与能力、草稿支持；/api/v1/platforms 由它生成
security/     auth（API Key → Principal）、keys、net（可信代理/IP 白名单）、ratelimit（Redis 固定窗口）
worker.py     发布任务执行器（PG `FOR UPDATE SKIP LOCKED`，可多进程）
cli.py        init-db / create-tenant / create-key / bootstrap / export-openapi
```

规则：
- 常量、枚举、协议值只放 `constants.py`；错误码只放 `errors.py`。按 code 分支，不按 message。
- 平台能力只在 `platforms.py` 登记；可发布平台由配置 `HUB_PUBLISH_PLATFORMS` 决定（路由层 `ensure_publishing`）。
- 所有 JSON 响应走 `envelope.ok/fail`；OpenAPI 由 `main.build_openapi` 修正（422→400 错误信封，声明 `ApiKeyAuth`）。
- 租户隔离：按 `tenant_id` 读取资源，别人的资源一律 `NOT_FOUND`（扫码会话同样）。
- 慢操作不阻塞请求：数据查询等待 ≤ `HUB_DTK_WAIT_SECONDS`，否则落 `data_fetch` 任务，轮询时惰性刷新。
- 扫码登录每次启动一个浏览器：SSE 与会话两条路径共用 `state.login_slots`（`HUB_LOGIN_MAX_CONCURRENCY`）。
- 发布任务不自动重试（平台无幂等），崩溃遗留的任务由 `fail_stale` 标记失败。
- 外部 URL（`video_url`、`callback_url`）必须经 `services/urls.ensure_public_url`（防 SSRF）。
- 测试：I/O 用 respx / fakeredis / SQLite 替身，纯函数直接测；新功能先写失败测试。

## 上游与补丁

版本钉死在 `deploy/versions.env`，只通过 HTTP 集成，禁止把上游代码复制进 `src/`。
**MediaCrawler 是非商业许可，任何代码都不能进入本项目。**

- **DTK**（Apache-2.0）：5.1.1 = 提交 `d21b92e`。`patches/dtk/0001-*.patch` 新增 `douyin.search`、`douyin.hot_search`。
  - `deploy/dtk/Dockerfile` 把打过补丁的 `src/dtk` 覆盖到官方镜像，基础镜像版本必须与 `DTK_COMMIT` 对应。
  - 修改补丁：`.upstream/dtk` 是带 git 基线的上游快照。改完后在该目录执行
    `git -c core.autocrlf=false add -A`、`git -c core.autocrlf=false diff --cached --output=<仓库>/patches/dtk/0001-douyin-keyword-search-and-hot-board.patch`、`git reset -q`。
  - 跑 DTK 测试：`.upstream/dtk/.venv`，需要 `DTK_SECRET_KEY`（≥32 位）、`DTK_DATABASE_URL`、`DTK_REDIS_URL`、`no_proxy`；
    `pytest tests/unit tests/replay -m "not integration and not live"`。Windows 上 25 个既有失败与补丁无关。
- **SAU**（MIT）：`deploy/sau/Dockerfile` 只构建后端；容器启动时 `entrypoint.sh` 依次执行 `patch-login.py`（扫码登录）和 `patch-publish.py`（抖音发布带上 `desc` 正文和竖版封面 `thumbnailPortrait`，2026-10-21），都按锚点幂等打补丁，锚点对不上就跳过并打印 skip。上传器用 patchright，登录/校验用 playwright + xhs，两者及浏览器都要装。SAU 无鉴权，只在内部网络可达。平台编号见 `constants.SAU_PLATFORM_TYPES`；定时发布依赖容器时区 `TZ=CST-8`。

## 部署要点

- `deploy/deploy.sh up`：拉源码 → 打补丁 → 生成/补全 `deploy/.env`、`deploy/dtk.env`（600）→ 构建 → 启动 `mediahub-dtk` →
  首次自动初始化 DTK（管理员写入 `deploy/dtk-admin.txt`，网关的 DTK Key 写入 `deploy/.env`）→ 启动 `mediahub` → 健康检查。
- `deploy.sh tenant NAME [CIDR]` 把租户 Key 写入 `deploy/tenant-NAME.json`（600），不打印到终端。
- 两个 compose 项目经外部网络 `mediahub-shared` 互通：网关以 `dtk-api:8000` 访问 DTK，网关在该网络上的别名是 `media-hub-gateway`。
- 所有容器有内存/CPU 上限和 `oom_score_adj=500`（SAU 600），宿主机内存紧张时先回收本栈，保护同机其他产品。
- compose 里 `HUB_AUTO_CREATE_SCHEMA=false`，建表由一次性 `migrate` 服务执行。
- 远程执行经验：Windows PowerShell 引号会破坏内联远程命令，改用“本地脚本 | ssh ... \"tr -d '\r' | bash -s\"”；
  大文件用 base64 走 stdin；脚本内调用 `docker compose run` 必须 `< /dev/null`，否则会吞掉管道里剩余的脚本。
- 旧发行版 curl（7.61）没有 `--retry-all-errors`，下载重试由 `deploy.sh` 自己实现。

## 当前部署（2026-10-01）

- 主部署服务器 `putian-erp-guangzhou`（118.89.27.233，腾讯云广州，Ubuntu，2 vCPU / 8 GiB，4 GiB swap）；目录 `/opt/media-hub`。
- 网关 `0.0.0.0:18090`（需在云安全组放行，建议只放行产品后端出口 IP）；DTK 控制台 `127.0.0.1:18091`（SSH 隧道访问）。
- 已签发广州实例的 `saas-main` Key（服务器 `deploy/tenant-saas-main.json`，未绑定 IP 白名单）。
- 已验证：所有容器健康、`/readyz`、租户鉴权和 `/api/v1/platforms`；外部访问仍需放行云安全组 TCP 18090。
- 广州实例目前是新数据卷；美国旧实例的 DTK Cookie 身份池和业务数据库尚未迁入。
- 身份池（2026-10-02 起）：扫码绑定的抖音账号自动导入 DTK（`services/data_identities.py`，表 `account_data_identities`，worker 每 5 分钟补导入/重试/完成退役）；
  `deploy.sh guest-identity` 放入游客身份（热榜、详情、下载可用，搜索要登录态）。网关的 DTK Key 需要 `identity:manage`。
- SSH：`ssh media-hub-gz`（ubuntu@118.89.27.233，免密 sudo、docker 组；本机密钥 `~/.ssh/id_ed25519_mediahub`）。服务器上的 `/opt/media-hub` 不是 git 仓库，部署 = 同步文件 + `deploy/deploy.sh up`。
- 2026-10-02 抖音扫码后的短信验证：弹窗里的“验证”是带样式的 `<div>`（不是 button），之前只按 button 角色找，验证码填好了却没人点，登录一直停在“还在确认”。`deploy/sau/patch-login.py` 的 `_submit_button` 改为先找按钮、再找整段文字等于标签且在最上层的元素，都没有时在验证码框按回车；已在同一 SAU 镜像的临时容器里用模拟弹窗验证（只点到弹窗里的“验证”，背景表单不受影响），并只重建了 `sau` 服务（回滚镜像标签 `media-hub-sau:before-verify-fix-20261002`）。
- 旧美国服务器 `43.162.114.123` 保留作迁移源；SSH 链路偶发握手超时，重试时使用 `-o IPQoS=none -o ConnectTimeout=60`。

## 待办

1. 用真实抖音账号扫码，确认登录态导入身份池后搜索可用；用测试账号实测抖音、视频号发布。
2. 服务器在美国：抖音/微信可能判定异地登录、数据接口风控更严；生产建议为 DTK 身份配置国内代理，或把 DTK+SAU 迁到国内节点。
3. 视频号商业数据供应商选型与 Provider 实现（接口已固定：`providers/channels.py`）；自己作品的评论/数据。
4. 素材改对象存储（OSS/COS/S3）；DTK 下载文件的过期清理策略。
5. 抖音开放平台发布驱动（用户暂不接，保留扩展位）；更多平台按 README“扩展新平台”接入。
6. 监控告警：发布失败率、`UPSTREAM_UNAVAILABLE` 比例、身份池健康度。
