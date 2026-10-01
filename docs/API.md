# media-hub 接口对接文档（v1）

面向：**产品后端服务**，服务端对服务端调用。

- 机器可读版本：[`docs/openapi.json`](openapi.json)（OpenAPI 3.1，可导入 Apifox / Postman，或用来生成客户端代码）。
- 服务开启文档时（`HUB_EXPOSE_DOCS=true`）也可以在线查看：`<Base URL>/docs`。
- 本文档与代码同步维护；接口字段以本文和 `openapi.json` 为准。

---

## 目录

1. [接入准备](#1-接入准备)
2. [通用约定](#2-通用约定)
3. [抖音数据](#3-抖音数据)
4. [视频发布（抖音 / 视频号）](#4-视频发布抖音--视频号)
5. [视频号数据](#5-视频号数据)
6. [运维管理接口](#6-运维管理接口)
7. [健康检查](#7-健康检查)
8. [对接示例代码](#8-对接示例代码)
9. [能力边界与注意事项](#9-能力边界与注意事项)
10. [附录：接口总表](#10-附录接口总表)

---

## 1. 接入准备

| 项目 | 说明 |
|---|---|
| Base URL | 当前部署：`http://118.89.27.233:18090`（启用域名与 HTTPS 后改为 `https://<域名>`） |
| 认证 | 请求头 `X-API-Key: <key>`（也接受 `Authorization: Bearer <key>`） |
| 数据格式 | 请求与响应均为 JSON，UTF-8；上传素材用 `multipart/form-data` |
| 版本 | 路径前缀 `/api/v1`。同一版本内只做向后兼容的变更（例如新增字段），**客户端必须忽略不认识的字段** |

**获取 API Key**：由运维在服务器上执行：

```bash
deploy/deploy.sh tenant <租户名> <产品后端出口IP>/32
```

结果写入服务器上的 `deploy/tenant-<租户名>.json`（仅 root 可读）：其中 `key.key` 是 API Key（**只生成这一次**），`webhook_secret` 是回调验签密钥（仅在首次创建租户时返回）。两者都只能保存在服务端（配置中心或环境变量），**不能下发到浏览器、App 或小程序**。

**第一个请求**：

```bash
export HUB=http://118.89.27.233:18090
export KEY=mh_xxxxxxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

curl -s "$HUB/api/v1/douyin/hot" -H "X-API-Key: $KEY"
```

---

## 2. 通用约定

### 2.1 认证与权限（scope）

每个 API Key 属于一个租户，并带有一组 scope。调用没有授权的接口会返回 `403 FORBIDDEN_SCOPE`。

| scope | 可调用的接口 |
|---|---|
| `douyin:read` | 抖音搜索、热榜、链接解析、作品详情、评论、作者 |
| `media:download` | 抖音作品的服务端下载 |
| `publish:write` | 上传素材、发布视频 |
| `accounts:manage` | 发布账号的扫码登录、列表、删除 |
| `channels:read` | 视频号数据 |

- Key 可以绑定 IP 白名单（CIDR），来自其他地址的请求返回 `403 IP_NOT_ALLOWED`。
- 数据隔离：租户只能看到自己创建的任务、素材和账号，访问其他租户的资源一律返回 `404 NOT_FOUND`。

### 2.2 统一响应信封

除文件下载和 SSE 流以外，所有响应都是这个结构：

```json
{
  "success": true,
  "data": { "...": "业务数据" },
  "error": null,
  "meta": { "request_id": "3b0f5c1e9d2a4b7c8e6f1a2b3c4d5e6f" }
}
```

失败时：

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "INVALID_PARAM",
    "message": "provide exactly one of: url, aweme_id",
    "retryable": false,
    "details": { "given": [] }
  },
  "meta": { "request_id": "3b0f5c1e9d2a4b7c8e6f1a2b3c4d5e6f" }
}
```

- **按 `error.code` 分支**，不要解析 `message`（message 是给人看的英文说明，可能调整）。
- `retryable=true` 表示稍后重试可能成功；有 `retry_after`（秒）时按它等待，响应头也会带 `Retry-After`。
- `details` 为排查辅助信息，字段不固定。

### 2.3 HTTP 状态码与错误码

| error.code | HTTP | 可重试 | 含义与处理建议 |
|---|---|---|---|
| `INVALID_PARAM` | 400 | 否 | 参数缺失或不合法（包括请求体校验失败），检查 `details` |
| `UNAUTHENTICATED` | 401 | 否 | 缺少 Key，或 Key 无效 / 已吊销 / 已过期 |
| `FORBIDDEN_SCOPE` | 403 | 否 | Key 缺少该接口需要的 scope，`details.required` 列出缺少的项 |
| `IP_NOT_ALLOWED` | 403 | 否 | 请求来源 IP 不在 Key 的白名单内 |
| `NOT_FOUND` | 404 | 否 | 资源不存在，或不属于当前租户 |
| `CONTENT_UNAVAILABLE` | 404 | 否 | 作品 / 作者已删除、私密或不可见 |
| `METHOD_NOT_ALLOWED` | 405 | 否 | HTTP 方法错误 |
| `CONFLICT` | 409 | 否 | 同一个 `Idempotency-Key` 被用于内容不同的请求 |
| `PAYLOAD_TOO_LARGE` | 413 | 否 | 上传文件超过大小上限（默认 500 MB） |
| `RATE_LIMITED` | 429 | 是 | 超过本 Key 每分钟的调用上限，按 `retry_after` 等待 |
| `NOT_CONFIGURED` | 501 | 否 | 该能力在服务端尚未开通（例如视频号数据供应商未接入） |
| `UPSTREAM_ERROR` | 502 | 否 | 上游平台返回了无法处理的结果，保留 `request_id` 联系运维 |
| `UPSTREAM_UNAVAILABLE` | 503 | 是 | 上游暂时不可用：平台风控、身份池暂时耗尽、排队已满等，稍后重试 |
| `UPSTREAM_TIMEOUT` | 504 | 是 | 上游超时 |
| `INTERNAL` | 500 | 是 | 服务内部错误，保留 `request_id` 联系运维 |

成功时的状态码：`200` 已完成；`201` 已创建；`202` 已受理、异步处理中（见 [2.6](#26-同步返回与异步任务)）。

### 2.4 请求追踪

- 可以在请求头带 `X-Request-ID`（1–64 位，字符集 `A-Z a-z 0-9 . _ -`），服务端会沿用；不带则自动生成。
- 响应头 `X-Request-ID` 与 `meta.request_id` 一致；`X-Response-Time-Ms` 为服务端耗时。
- 反馈问题时请提供 `request_id`。

### 2.5 限流

- 按 API Key 计数，固定 1 分钟窗口；上限由运维按租户 / Key 配置（默认 120 次/分钟）。
- 限流开启时，每个通过认证的响应都带：

| 响应头 | 含义 |
|---|---|
| `X-RateLimit-Limit` | 当前窗口的上限 |
| `X-RateLimit-Remaining` | 当前窗口剩余次数 |
| `X-RateLimit-Reset` | 窗口重置时间（Unix 秒） |

- 超限返回 `429 RATE_LIMITED`，并带 `Retry-After`。
- 注意：网关限流之外，**抖音平台自身对采集频率很敏感**，高频调用会触发风控并返回 `503 UPSTREAM_UNAVAILABLE`。请在产品侧做缓存与去重（见 [第 9 节](#9-能力边界与注意事项)）。

### 2.6 同步返回与异步任务

| 类型 | 接口 | 行为 |
|---|---|---|
| 数据查询 | 抖音搜索、热榜、解析、详情、评论、作者等 | 服务端最多等待约 20 秒：拿到结果返回 **200 + 数据**；未完成返回 **202 + 任务对象**，之后轮询任务 |
| 服务端下载 | `POST /api/v1/douyin/downloads` | 通常返回 **202 + 任务对象**，轮询下载状态 |
| 视频发布 | `POST /api/v1/publish/videos` | 一律 **202 + 任务对象**（幂等重放时为 200），由后台依次执行 |

因此，调用数据查询接口时要同时处理 200 和 202 两种成功响应：

```text
status == 200  -> data 即业务数据
status == 202  -> data 是任务对象，取 data.task_id 轮询 GET /api/v1/tasks/{task_id}
```

**任务对象**：

```json
{
  "task_id": "9d3c1f0e-5b7a-4c2e-9f1d-8a6b4c2e0f11",
  "kind": "publish_video",
  "status": "succeeded",
  "result": { "platform": "douyin", "account_id": "…", "scheduled": false, "draft": false },
  "error": null,
  "created_at": "2026-09-28T04:00:00.123456+00:00",
  "started_at": "2026-09-28T04:00:01.002000+00:00",
  "finished_at": "2026-09-28T04:02:37.450000+00:00"
}
```

| 字段 | 说明 |
|---|---|
| `kind` | `data_fetch`（数据查询转异步）、`media_download`（服务端下载）、`publish_video`（发布） |
| `status` | `queued` 排队中 → `running` 执行中 → `succeeded` 成功 / `failed` 失败 / `cancelled` 已取消（后三者为终态） |
| `result` | 成功时的结果。`data_fetch` 的结果就是对应查询接口的 `data` |
| `error` | 失败时的错误，结构同信封中的 `error` |

轮询建议：前 30 秒每 2 秒一次，之后每 5–10 秒一次；发布任务通常需要几分钟（上传与平台处理），也可以不轮询，改用回调（[2.9](#29-回调webhook)）。

### 2.7 分页

列表接口返回：

```json
{ "items": [ ], "cursor": "1727400000000", "has_more": true }
```

- 取下一页时把 `cursor` **原样**传回；`has_more=false` 或 `cursor=null` 表示最后一页。
- `cursor` 是不透明字符串，不要解析或自行构造。搜索的 cursor 绑定了本次搜索会话，只能配合**相同的关键词与筛选条件**使用。
- `count` 是期望条数，实际条数可能更少（例如搜索结果中的用户卡片、直播卡片会被过滤掉）。

### 2.8 幂等（发布接口）

`POST /api/v1/publish/videos` 支持请求头 `Idempotency-Key`（≤128 字符，租户内唯一）：

- 同一个 Key + 相同请求体：不会重复发布，直接返回第一次创建的任务（HTTP 200）。
- 同一个 Key + 不同请求体：返回 `409 CONFLICT`，`details.task_id` 是已存在的任务。
- 建议每个业务上的「发布动作」生成一个 UUID 作为 Key，网络超时后用同一个 Key 重试。

### 2.9 回调（Webhook）

发布请求带 `callback_url`（公网 http/https 地址，内网地址会被拒绝）时，任务结束后服务端会 `POST` 通知：

```http
POST <callback_url>
Content-Type: application/json
X-Hub-Event: task.succeeded
X-Hub-Signature: sha256=5d41402abc4b2a76b9719d911017c592…

{"event": "task.succeeded", "task_id": "9d3c1f0e-…", "kind": "publish_video", "status": "succeeded", "sent_at": "2026-09-28T04:02:37.512000+00:00"}
```

失败时 `event` 为 `task.failed`，并附带 `"error": {"code": "...", "message": "..."}`。

- **回调只通知状态**，结果请用 `GET /api/v1/tasks/{task_id}` 获取。
- **验签**：`X-Hub-Signature` = `sha256=` + HMAC-SHA256(密钥 = 租户的 `webhook_secret`，消息 = **原始请求体字节**) 的十六进制。必须对原始字节计算，不要先解析 JSON 再序列化。
- **重试**：收到 2xx 即成功；网络错误、5xx、429 会重试，最多 3 次（间隔约 2 秒、8 秒）；其他 4xx 不重试。回调失败不影响任务本身的状态。
- 接收方应尽快返回 2xx（先落库，再异步处理），并按 `task_id + event` 去重。

验签示例：

```python
import hashlib
import hmac

def verify(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
```

```javascript
// Node.js / Express：用 express.raw({ type: "application/json" }) 拿到原始字节
const crypto = require("crypto");

function verify(rawBody, signature, secret) {
  const expected = "sha256=" + crypto.createHmac("sha256", secret).update(rawBody).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(signature || "");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

```java
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;

static boolean verify(byte[] rawBody, String signature, String secret) throws Exception {
    Mac mac = Mac.getInstance("HmacSHA256");
    mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
    String expected = "sha256=" + HexFormat.of().formatHex(mac.doFinal(rawBody));
    return MessageDigest.isEqual(
        expected.getBytes(StandardCharsets.UTF_8),
        (signature == null ? "" : signature).getBytes(StandardCharsets.UTF_8));
}
```

### 2.10 时间与 ID

- 时间均为 ISO 8601 格式、UTC 时区，例如 `2026-09-28T04:00:00Z` 或 `2026-09-28T04:00:00.123456+00:00`。
- **所有 ID 都是字符串**。抖音作品 ID 是 19 位数字，超过 JavaScript 安全整数范围，不要转成数字类型。
- 网关生成的 ID（任务、素材、账号、租户）为 UUID。

### 2.11 平台能力目录与扩展约定

本服务会持续接入更多平台。为了让产品端**不用为每个新平台改代码**，请遵循三条约定：

1. **平台列表从接口读取，不要写死。** `GET /api/v1/platforms`（任意有效 Key）返回每个平台当前可用的能力：

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "platform": "douyin",
        "name": "抖音",
        "data": { "available": true, "capabilities": ["search", "hot", "parse", "video", "comments", "comment_replies", "user", "user_posts", "download"] },
        "publish": { "available": true, "draft": false, "schedule": true }
      },
      {
        "platform": "channels",
        "name": "视频号",
        "data": { "available": false, "capabilities": ["search", "video", "comments"] },
        "publish": { "available": true, "draft": true, "schedule": true }
      },
      {
        "platform": "kuaishou",
        "name": "快手",
        "data": { "available": false, "capabilities": [] },
        "publish": { "available": false, "draft": false, "schedule": false }
      }
    ]
  },
  "error": null,
  "meta": { "request_id": "…" }
}
```

   - `data.available`：该平台的数据接口已开通；`capabilities` 是它支持的数据能力。
   - `publish.available`：可以扫码绑定账号并发布；`draft` / `schedule` 表示是否支持草稿、定时发布。
   - 例如“绑定账号”页只展示 `publish.available=true` 的平台，“数据查询”页只展示 `data.available=true` 的平台。

2. **路径按平台区分，结构统一。** 数据接口统一为 `/api/v1/{platform}/{能力}`（如 `/api/v1/douyin/search`、`/api/v1/channels/search`）；发布、账号、任务接口通过 `platform` 字段区分平台。新平台接入后沿用同样的路径规则和参数。

3. **数据模型统一。** 所有平台的作品、作者、评论都使用第 3 节的 Content / Author / Comment 模型，平台没有的字段为 `null`。按字段是否为 `null` 做展示判断，不要按平台名写分支。

---

## 3. 抖音数据

### 3.1 数据模型

以下模型在各接口中复用。值缺失时为 `null`（不是 0 或空串）；`raw`（平台原始数据）默认不返回。

**Content（作品）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `platform` | string | 固定 `douyin` |
| `content_id` | string | 作品 ID（aweme_id） |
| `kind` | string | `video` 视频 / `image_album` 图集 |
| `web_url` | string | 作品网页地址 |
| `title` | string | 文案的第一行 |
| `description` | string | 完整文案 |
| `created_at` | string\|null | 发布时间 |
| `duration_ms` | int\|null | 时长（毫秒），图集为 null |
| `is_deleted` / `is_private` | bool | 已删除 / 非公开（此时媒体地址可能为空） |
| `author` | Author | 作者（列表接口中为精简版，`stats` 通常为 null） |
| `stats` | object | `digg_count` 点赞、`comment_count` 评论、`share_count` 分享、`collect_count` 收藏、`play_count` 播放（抖音网页端不提供，恒为 null） |
| `media.video` | object\|null | 无水印视频：`url` 首选地址，`urls` 全部镜像，`width` `height` `bitrate` `format` `size_bytes` |
| `media.streams` | array | 其他码率 |
| `media.covers` | array | 封面图（`url` / `urls` / `width` / `height`） |
| `media.images` | array | 图集图片 |
| `music` | object\|null | `music_id` `title` `author` `duration_ms` `play_url` `cover` |
| `tags` | string[] | 话题（不含 `#`） |
| `location` | string\|null | 位置 |
| `fetched_at` | string | 本条数据的抓取时间 |
| `heat_score` | number | **仅搜索接口按热度排序时返回**，见 [3.2](#32-关键词搜索) |

**Author（作者）**

| 字段 | 说明 |
|---|---|
| `uid` / `sec_uid` | 作者稳定 ID（抖音 sec_user_id，两者相同），作者相关接口的 `sec_user_id` 参数用它 |
| `unique_id` | 抖音号（用户可修改，不要当主键） |
| `nickname` / `signature` | 昵称 / 签名 |
| `avatar` | 头像（`url` / `urls`） |
| `web_url` | 主页地址 |
| `verified` | 是否认证 |
| `stats` | `follower_count` 粉丝、`following_count` 关注、`content_count` 作品数、`total_digg` 获赞总数（资料接口返回，列表中通常为 null） |

**Comment（评论）**

| 字段 | 说明 |
|---|---|
| `comment_id` / `content_id` | 评论 ID / 所属作品 ID |
| `parent_id` | 一级评论为 null；回复指向其一级评论 |
| `text` | 评论内容 |
| `created_at` | 评论时间 |
| `digg_count` / `reply_count` | 点赞数 / 回复数 |
| `author` | 评论者（Author 精简版） |
| `is_author_liked` / `is_pinned` | 作者赞过 / 置顶 |

### 3.2 关键词搜索

`GET /api/v1/douyin/search` · scope `douyin:read`

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `keyword` | 是 | | 关键词，1–100 字 |
| `sort` | 否 | `general` | 平台排序：`general` 综合、`most_liked` 最多点赞、`latest` 最新发布 |
| `publish_time` | 否 | `0` | 发布时间范围（天）：`0` 不限、`1`、`7`、`180` |
| `rank` | 否 | `heat` | `heat` 按热度分对本页重新排序；`upstream` 保持平台返回顺序 |
| `cursor` | 否 | | 上一页返回的 cursor |
| `count` | 否 | `20` | 1–50 |

**热度分**（`rank=heat` 时每条附带 `heat_score`，按它从高到低排序）：

```text
heat_score = (1×点赞 + 3×评论 + 5×分享 + 4×收藏) × 0.5 ^ (发布至今小时数 / 72)
```

权重与半衰期可由运维调整（`HUB_HEAT_*`）。热度排序只作用于**当前页**；想要「全局最热」请用 `sort=most_liked` 结合 `publish_time` 限定时间范围，再逐页获取。

```bash
curl -s -G "$HUB/api/v1/douyin/search" -H "X-API-Key: $KEY" \
  --data-urlencode "keyword=露营装备" -d sort=most_liked -d publish_time=7 -d count=20
```

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "platform": "douyin",
        "content_id": "7420000000000000001",
        "kind": "video",
        "web_url": "https://www.douyin.com/video/7420000000000000001",
        "title": "周末露营装备清单",
        "description": "周末露营装备清单\n#露营 #户外",
        "created_at": "2026-09-26T10:15:00Z",
        "duration_ms": 35200,
        "is_deleted": false,
        "is_private": false,
        "author": {
          "platform": "douyin",
          "uid": "MS4wLjABAAAAexample",
          "sec_uid": "MS4wLjABAAAAexample",
          "unique_id": "camping_lab",
          "nickname": "露营实验室",
          "signature": null,
          "avatar": { "url": "https://p3.douyinpic.com/…", "urls": ["https://p3.douyinpic.com/…"], "width": null, "height": null },
          "web_url": "https://www.douyin.com/user/MS4wLjABAAAAexample",
          "verified": false,
          "stats": null
        },
        "stats": { "play_count": null, "digg_count": 12034, "comment_count": 356, "share_count": 88, "collect_count": 410 },
        "media": {
          "covers": [{ "url": "https://p3.douyinpic.com/…", "urls": ["https://p3.douyinpic.com/…"], "width": 1080, "height": 1920 }],
          "video": {
            "url": "https://v3-web.douyinvod.com/…",
            "urls": ["https://v3-web.douyinvod.com/…", "https://v26-web.douyinvod.com/…"],
            "width": 1080, "height": 1920, "bitrate": 1450000, "format": "mp4", "size_bytes": 6380211, "watermark": false
          },
          "streams": [],
          "images": []
        },
        "music": { "music_id": "7419000000000000000", "title": "原声", "author": "露营实验室", "duration_ms": 35000, "play_url": null, "cover": null },
        "tags": ["露营", "户外"],
        "location": null,
        "fetched_at": "2026-09-28T04:00:00Z",
        "heat_score": 6127.4431
      }
    ],
    "cursor": "20|2026092812000001",
    "has_more": true
  },
  "error": null,
  "meta": { "request_id": "…" }
}
```

### 3.3 热榜

`GET /api/v1/douyin/hot` · scope `douyin:read` · 无参数

```json
{
  "success": true,
  "data": {
    "platform": "douyin",
    "active_time": "2026-09-28 12:00:00",
    "items": [
      {
        "platform": "douyin",
        "word": "某热点事件",
        "position": 1,
        "hot_value": 11813424,
        "label": 3,
        "sentence_id": "1690766",
        "group_id": "7366000000000000001",
        "video_count": 12,
        "event_time": "2026-09-28T08:00:00Z",
        "cover": { "url": "https://p3.douyinpic.com/…", "urls": ["https://p3.douyinpic.com/…"], "width": null, "height": null }
      }
    ],
    "trending": []
  },
  "error": null,
  "meta": { "request_id": "…" }
}
```

| 字段 | 说明 |
|---|---|
| `items` | 热榜，按 `position`（排名，从 1 开始）排列 |
| `hot_value` | 平台给出的热度值，只在同一份榜单内可比 |
| `label` | 平台的角标代码（新、热等），原样透传 |
| `video_count` | 相关作品数 |
| `trending` | 上升热点列表（平台提供时） |
| `active_time` | 平台榜单的更新时间（北京时间字符串） |

热词可直接作为 `keyword` 调用搜索接口获取相关作品。榜单在服务端缓存约 5 分钟。

### 3.4 链接解析

`POST /api/v1/douyin/parse` · scope `douyin:read`

请求体：`{"url": "<链接或整段分享文案>"}`，支持 `https://v.douyin.com/xxxx/` 短链、`https://www.douyin.com/video/<id>` 以及复制出来的整段分享文字。

- 作品链接返回 **Content**（含无水印视频地址 `media.video.urls`）。
- 主页链接返回 **Author**。

```bash
curl -s -X POST "$HUB/api/v1/douyin/parse" -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"url": "7.43 复制打开抖音，看看【露营实验室的作品】周末露营装备清单 https://v.douyin.com/iExample/ Abc:/ 10/28"}'
```

### 3.5 作品详情

`GET /api/v1/douyin/video` · scope `douyin:read`

| 参数 | 说明 |
|---|---|
| `url` | 作品链接（支持分享文案），与 `aweme_id` **二选一** |
| `aweme_id` | 作品 ID |

返回 **Content**。

### 3.6 一级评论

`GET /api/v1/douyin/comments` · scope `douyin:read`

| 参数 | 说明 |
|---|---|
| `url` / `aweme_id` | 二选一 |
| `cursor` | 上一页的 cursor |
| `count` | 1–50，默认 20 |

返回 `{items: [Comment], cursor, has_more}`。

### 3.7 评论回复

`GET /api/v1/douyin/comments/replies` · scope `douyin:read`

| 参数 | 说明 |
|---|---|
| `comment_id` | 必填，一级评论的 `comment_id` |
| `url` / `aweme_id` | 二选一，评论所属作品 |
| `cursor` / `count` | 同上 |

返回 `{items: [Comment], cursor, has_more}`，每条的 `parent_id` 为该一级评论 ID。

### 3.8 作者资料

`GET /api/v1/douyin/user` · scope `douyin:read`

参数：`url`（主页链接）与 `sec_user_id` **二选一**。返回 **Author**（含 `stats`）。

### 3.9 作者作品

`GET /api/v1/douyin/user/posts` · scope `douyin:read`

参数：`url` / `sec_user_id` 二选一，`cursor`，`count`（1–50，默认 20）。返回 `{items: [Content], cursor, has_more}`。

### 3.10 按链接获取视频文件

两种方式，按需选择：

**方式 A：拿直链，由产品自行下载**（轻量，推荐）

调用 [链接解析](#34-链接解析) 或 [作品详情](#35-作品详情)，从 `media.video.urls` 取无水印地址。注意：

- 直链带签名、有有效期（通常几小时），拿到后尽快下载，不要长期保存链接；
- 某个地址失败时依次尝试 `urls` 中的其他镜像；
- 图集作品没有 `media.video`，图片在 `media.images`。

**方式 B：服务端下载后再取文件**（需要 scope `media:download`）

1. 创建下载：`POST /api/v1/douyin/downloads`，请求体 `{"url": "..."}` 或 `{"aweme_id": "..."}`（二选一），返回任务对象（`kind=media_download`）。
2. 查询进度：`GET /api/v1/douyin/downloads/{task_id}`，成功后 `result` 为：

```json
{
  "state": "done",
  "bytes_total": 6380211,
  "files": [
    { "name": "7420000000000000001.mp4", "kind": "video", "state": "done", "bytes": 6380211, "content_type": "video/mp4" }
  ]
}
```

3. 下载文件：`GET /api/v1/douyin/downloads/{task_id}/files/{name}`，`name` 取 `files[].name`，响应为文件二进制流（不是 JSON 信封）。

`state` 为 `partial` 表示部分文件成功（例如图集中个别图片失败），任务仍视为成功，以 `files[].state` 为准。

---

## 4. 视频发布（抖音 / 视频号）

发布通过服务端的浏览器自动化完成（模拟创作者后台操作），需要先用扫码的方式绑定发布账号。

```text
① 绑定账号（一次性）   POST /api/v1/accounts/login-sessions  -> 轮询得到二维码，扫码后得到 account_id
② 上传素材             POST /api/v1/media                    -> media_id   （或直接使用公网 video_url）
③ 提交发布             POST /api/v1/publish/videos           -> task_id    （带 Idempotency-Key）
④ 获取结果             回调 callback_url，或轮询 GET /api/v1/tasks/{task_id}
```

### 4.1 扫码绑定账号（Web 系统推荐：登录会话 + 轮询）

**不需要任何客户端。** 二维码由本服务从抖音 / 视频号的创作者后台取得，你们的网页只负责**显示这张图片**；你们的用户用**自己手机上的抖音 App（视频号则用微信）**扫码并确认，就完成了授权。

```text
用户浏览器              SaaS 后端                       media-hub                     用户手机
   │ 点击“绑定抖音账号”  │                                │                              │
   │ ───────────────────▶│ POST /accounts/login-sessions  │                              │
   │                     │ ──────────────────────────────▶│ 202 {session_id, pending}    │
   │ 每 2 秒轮询 SaaS 后端│ GET /accounts/login-sessions/{id}                             │
   │ ───────────────────▶│ ──────────────────────────────▶│ {status: qrcode_ready, qrcode}│
   │ ◀─── 显示二维码 ─────│                                │                              │
   │                                                       │ ◀── 抖音 App / 微信扫码确认 ──│
   │ 继续轮询 ──────────▶│ ──────────────────────────────▶│ {status: succeeded, account} │
   │ ◀── “绑定成功” ─────│ 保存 account_id 与自己用户的对应关系                            │
```

**创建会话**：`POST /api/v1/accounts/login-sessions` · scope `accounts:manage`

请求体：`{"platform": "douyin", "name": "品牌主号"}`，`platform` 为 `douyin` 或 `channels`，`name` 是账号备注名（1–64 字）。返回 `202`：

```json
{
  "success": true,
  "data": {
    "session_id": "c2b6f0a4-8f1e-4a57-9a0c-3f5d2e1b7c90",
    "platform": "douyin",
    "name": "品牌主号",
    "status": "pending",
    "qrcode": null,
    "account": null,
    "error": null,
    "created_at": "2026-09-28T08:00:00.000000+00:00",
    "expires_at": "2026-09-28T08:04:30.000000+00:00"
  },
  "error": null,
  "meta": { "request_id": "…" }
}
```

**查询会话**：`GET /api/v1/accounts/login-sessions/{session_id}` · scope `accounts:manage`，返回结构同上。

| status | 含义 | 前端表现 |
|---|---|---|
| `pending` | 正在启动浏览器并打开平台登录页（当前服务器实测约 40 秒） | 显示“正在生成二维码…” |
| `qrcode_ready` | 二维码已就绪，`qrcode` 为图片（`data:image/...;base64,...` 或 https 地址），可直接放进 `<img src>` | 显示二维码，提示“请用抖音 App 扫码” |
| `succeeded` | 扫码登录成功，`account` 为绑定的账号（含 `account_id`） | 显示“绑定成功”，保存 `account_id` |
| `failed` | 失败，`error` 为 `{code, message}`；`UPSTREAM_UNAVAILABLE` 表示当前扫码登录的人较多，稍后重试 | 显示失败原因与“重试”按钮 |
| `expired` | 超时未扫码（约 4.5 分钟） | 显示“二维码已过期”与“刷新”按钮 |

对接要点：

- 前端轮询的是**你们自己的后端**，由后端带 API Key 调用本接口，**API Key 不能出现在浏览器里**。
- 轮询间隔 2 秒；`succeeded` / `failed` / `expired` 为终态，结束后会话还能查询 10 分钟。
- “刷新二维码”就是重新创建一个会话。
- 同一台服务器同时进行的扫码登录数量有限（每个会话要启动一个浏览器），超出时会话会在 `pending` 排队。
- 本服务部署在海外时，平台可能在用户手机上提示“异地登录”并要求验证，按提示完成即可；登录态失效后需要重新扫码绑定。

#### 备选：SSE 流式登录

`GET /api/v1/accounts/login?platform=douyin&name=<备注名>` · scope `accounts:manage`

适合能长时间保持连接、并能把事件流转发给浏览器的后端。

| 参数 | 说明 |
|---|---|
| `platform` | `douyin` 或 `channels`（视频号） |
| `name` | 账号备注名，1–64 字 |

响应是 **SSE 事件流**（`Content-Type: text/event-stream`），不是 JSON 信封：

```text
event: qrcode
data: {"qrcode": "data:image/png;base64,iVBORw0KGgo..."}

event: success
data: {"account": {"account_id": "5a1e…", "platform": "douyin", "name": "品牌主号", "status": "active", "created_at": "2026-09-28T04:10:00+00:00"}}
```

| 事件 | 说明 |
|---|---|
| `qrcode` | 二维码图片地址（通常是 `data:image/...;base64,...`，可直接放进 `<img src>`）。可能推送多次（二维码刷新） |
| `success` | 扫码登录成功，返回账号信息，**保存 `account_id` 用于发布** |
| `failed` | 失败或超时（约 4 分钟），`data` 为 `{"code": "...", "message": "..."}` |

收到 `success` 或 `failed` 后流结束。由于 API Key 不能出现在前端，建议由产品后端调用本接口并把事件转发给自己的前端（SSE 或 WebSocket），参考 [8.4](#84-转发扫码登录事件sse备选python)。

### 4.2 账号列表与删除

- `GET /api/v1/accounts?platform=douyin`（`platform` 可选）· scope `accounts:manage`，返回 `{"items": [{account_id, platform, name, status, created_at}]}`。
- `DELETE /api/v1/accounts/{account_id}` · scope `accounts:manage`，同时删除服务端保存的登录态。

账号登录态会过期（平台策略，通常数天到数周），过期后发布会失败，需要重新扫码绑定。

### 4.3 上传素材

`POST /api/v1/media` · scope `publish:write` · `multipart/form-data`，字段名 `file`

```bash
curl -s -X POST "$HUB/api/v1/media" -H "X-API-Key: $KEY" -F "file=@/path/to/video.mp4"
```

```json
{
  "success": true,
  "data": {
    "media_id": "0c7b…",
    "filename": "video.mp4",
    "size_bytes": 52428800,
    "content_type": "video/mp4",
    "created_at": "2026-09-28T04:05:00+00:00"
  },
  "error": null,
  "meta": { "request_id": "…" }
}
```

- 支持的文件类型：`.mp4` `.mov` `.m4v` `.webm`（按文件扩展名判断），其他类型返回 `400 INVALID_PARAM`；
- 单文件上限默认 500 MB，超过返回 `413 PAYLOAD_TOO_LARGE`；
- 建议使用 H.264 编码的 mp4，分辨率与时长需符合目标平台要求。

### 4.4 提交发布

`POST /api/v1/publish/videos` · scope `publish:write`

请求头：`Idempotency-Key: <UUID>`（强烈建议，见 [2.8](#28-幂等发布接口)）

| 字段 | 必填 | 说明 |
|---|---|---|
| `platform` | 是 | `douyin`、`channels`（以 `GET /api/v1/platforms` 中 `publish.available=true` 的平台为准） |
| `account_id` | 是 | 扫码绑定得到的账号 ID，必须与 `platform` 一致 |
| `media_id` | 二选一 | 已上传素材的 ID |
| `video_url` | 二选一 | 公网可下载的视频地址（服务端会先下载） |
| `title` | 是 | 标题 / 文案，1–1000 字。抖音作品标题建议不超过 30 字；视频号会自动生成符合要求的短标题 |
| `tags` | 否 | 话题列表（不带 `#`），最多 20 个 |
| `schedule` | 否 | 定时发布，见下 |
| `draft` | 否 | 仅视频号：`true` 保存为草稿而不发布 |
| `callback_url` | 否 | 任务结束后的回调地址 |

`schedule`：`{"daily_times": [20], "start_days": 0}` 表示在 **（今天 + `start_days` + 1）天的 `daily_times[0]` 点**（北京时间）发布；`daily_times` 取值 0–23，`start_days` 取值 0–30。不传则立即发布。

```bash
curl -s -X POST "$HUB/api/v1/publish/videos" -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" -H "Idempotency-Key: 6f1d2c3b-7a8e-4f90-b1c2-d3e4f5a6b7c8" \
  -d '{
        "platform": "douyin",
        "account_id": "5a1e…",
        "media_id": "0c7b…",
        "title": "周末露营装备清单",
        "tags": ["露营", "户外"],
        "callback_url": "https://api.example.com/hooks/media-hub"
      }'
```

返回 `202` 与任务对象（`kind=publish_video`，`status=queued`）。

### 4.5 发布结果

**提交时的校验**（同步返回，不会创建任务）：

| HTTP / error.code | 原因 |
|---|---|
| 404 `NOT_FOUND` | `account_id` 或 `media_id` 不存在，或不属于当前租户 |
| 400 `INVALID_PARAM` | 账号与 `platform` 不一致；账号需要重新登录；`media_id` 与 `video_url` 同时传或都没传；`draft` 用于非视频号；`video_url` / `callback_url` 不是公网地址 |
| 409 `CONFLICT` | `Idempotency-Key` 已用于内容不同的请求 |

**执行结果**：`GET /api/v1/tasks/{task_id}`

- 成功：`status=succeeded`，`result` 为 `{"platform", "account_id", "scheduled", "draft"}`。
- 失败：`status=failed`，常见 `error.code`：

| error.code | 原因 | 处理 |
|---|---|---|
| `INVALID_PARAM` | `video_url` 下载失败，或文件类型不支持 | 检查地址是否可公网访问 |
| `PAYLOAD_TOO_LARGE` | `video_url` 指向的文件超过大小上限 | 压缩后重新提交 |
| `NOT_FOUND` | 素材文件已被清理 | 重新上传 |
| `UPSTREAM_ERROR` | 平台页面操作失败（登录态失效、平台改版、内容审核拦截等） | 先重新扫码绑定账号，再重试；仍失败请带 `task_id` 联系运维 |
| `UPSTREAM_TIMEOUT` | 上传或发布超时 | 先到平台后台确认是否已发出，再用**新的** `Idempotency-Key` 重新提交 |
| `UPSTREAM_UNAVAILABLE` | 发布服务暂时不可用 | 稍后重新提交 |
| `INTERNAL` | 服务重启导致任务中断（message 含 "not retried"） | 先到平台后台确认是否已发出，再决定是否重新提交 |

发布任务**不会自动重试**：平台侧没有幂等机制，自动重试可能导致同一视频发两次。

---

## 5. 视频号数据

| 接口 | 说明 | scope |
|---|---|---|
| `GET /api/v1/channels/search?keyword=&cursor=&count=` | 关键词搜索 | `channels:read` |
| `GET /api/v1/channels/video?url=` 或 `?object_id=` | 作品详情（含视频地址，用于按链接下载） | `channels:read` |
| `GET /api/v1/channels/comments?url=`/`object_id=`&`cursor=&count=` | 作品评论 | `channels:read` |

返回结构与抖音一致（Content / Comment 模型的子集，分页为 `{items, cursor, has_more}`）。

**当前状态**：视频号内容只在微信内分发，没有可以在服务器上稳定采集的公开接口，需要接入商业数据供应商。供应商确定之前：

- 默认返回 `501 NOT_CONFIGURED`；
- 运维可开启演示模式（`HUB_CHANNELS_PROVIDER=mock`），返回固定的示例数据（每条带 `"mock": true`），供产品先行联调。接入真实供应商后接口和字段不变。

视频号能力一览：

| 能力 | 状态 |
|---|---|
| 发布视频到自己的视频号 | ✅ 可用（[第 4 节](#4-视频发布抖音--视频号)，`platform=channels`） |
| 保存为草稿 | ✅ 可用（`draft=true`） |
| 关键词搜索、他人作品详情与评论、按链接下载 | ⏳ 接口已定义，等待数据供应商接入 |
| 自己作品的评论与数据 | ⏳ 规划中 |

---

## 6. 运维管理接口

供运维或内部管理后台使用，**不要给产品后端的业务代码调用**。认证请求头 `X-Admin-Token: <HUB_ADMIN_TOKEN>`；未配置令牌时这些接口返回 `501`。

| 方法与路径 | 说明 |
|---|---|
| `POST /api/v1/admin/tenants` | 创建租户：`{"name": "acme", "rate_limit_per_min": 120}`，返回租户信息和 `webhook_secret`（仅此一次） |
| `GET /api/v1/admin/tenants` | 租户列表 |
| `POST /api/v1/admin/tenants/{tenant_id}/keys` | 签发 Key：`{"name": "prod", "scopes": ["douyin:read"], "ip_allowlist": ["1.2.3.4/32"], "rate_limit_per_min": 300, "expires_at": null}`，明文 `key` 仅返回一次；`scopes` 不传则授予全部 |
| `GET /api/v1/admin/tenants/{tenant_id}/keys` | Key 列表（不含明文） |
| `POST /api/v1/admin/keys/{key_id}/revoke` | 吊销 Key，立即生效 |
| `GET /api/v1/admin/tenants/{tenant_id}/usage?start=2026-09-01&end=2026-09-30` | 用量：`{"items": [{"day", "endpoint", "count"}]}`，按天（UTC）、按接口统计通过认证且未发生服务端错误（非 5xx）的调用次数，可作为计费依据 |

限流优先级：Key 上的 `rate_limit_per_min` > 租户上的值 > 全局默认值；设为 `0` 表示不限制。

---

## 7. 健康检查

无需认证，不使用信封：

| 路径 | 说明 |
|---|---|
| `GET /healthz` | 进程存活：`{"status": "ok"}` |
| `GET /readyz` | 可以处理请求（数据库、Redis 正常）：`{"status": "ready"}`；否则 `503` 与 `{"status": "not_ready", "failed": [...]}` |

---

## 8. 对接示例代码

### 8.1 调用与轮询（Python）

```python
import time
import uuid

import httpx

HUB = "http://<服务器IP>:8080"
KEY = "mh_…"  # 从配置中心读取，不要写在代码里


class HubError(Exception):
    def __init__(self, status: int, error: dict, request_id: str):
        super().__init__(f"{error['code']}: {error['message']} (request_id={request_id})")
        self.status, self.code, self.error, self.request_id = status, error["code"], error, request_id


client = httpx.Client(base_url=HUB, headers={"X-API-Key": KEY}, timeout=60)


def call(method: str, path: str, **kwargs) -> tuple[int, object]:
    response = client.request(method, path, **kwargs)
    body = response.json()
    if not body["success"]:
        raise HubError(response.status_code, body["error"], body["meta"]["request_id"])
    return response.status_code, body["data"]


def wait_task(task_id: str, timeout: float = 600) -> dict:
    deadline, interval = time.monotonic() + timeout, 2.0
    while time.monotonic() < deadline:
        _, task = call("GET", f"/api/v1/tasks/{task_id}")
        if task["status"] in ("succeeded", "failed", "cancelled"):
            return task
        time.sleep(interval)
        interval = min(interval * 1.5, 10.0)
    raise TimeoutError(task_id)


def fetch(path: str, **params) -> object:
    """数据查询：200 直接返回数据；202 轮询任务后返回结果。"""
    status, data = call("GET", path, params=params)
    if status == 202:
        task = wait_task(data["task_id"])
        if task["status"] != "succeeded":
            raise HubError(502, task["error"], task["task_id"])
        return task["result"]
    return data


page = fetch("/api/v1/douyin/search", keyword="露营装备", sort="most_liked", publish_time=7)
for item in page["items"]:
    print(item["heat_score"], item["title"], item["stats"]["digg_count"])

_, task = call(
    "POST",
    "/api/v1/publish/videos",
    headers={"Idempotency-Key": str(uuid.uuid4())},
    json={"platform": "douyin", "account_id": "…", "media_id": "…", "title": "周末露营装备清单"},
)
print(wait_task(task["task_id"]))
```

### 8.2 调用（Node.js 18+）

```javascript
const HUB = "http://<服务器IP>:8080";
const KEY = process.env.MEDIA_HUB_KEY;

async function call(method, path, { query, body, headers } = {}) {
  const url = new URL(path, HUB);
  if (query) Object.entries(query).forEach(([k, v]) => url.searchParams.set(k, String(v)));
  const res = await fetch(url, {
    method,
    headers: { "X-API-Key": KEY, "Content-Type": "application/json", ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  const envelope = await res.json();
  if (!envelope.success) {
    const err = new Error(`${envelope.error.code}: ${envelope.error.message}`);
    Object.assign(err, { status: res.status, ...envelope.error, requestId: envelope.meta.request_id });
    throw err;
  }
  return { status: res.status, data: envelope.data };
}

const { status, data } = await call("GET", "/api/v1/douyin/comments", {
  query: { url: "https://v.douyin.com/iExample/", count: 20 },
});
// status === 202 时 data.task_id 需要轮询 /api/v1/tasks/{task_id}
```

### 8.3 扫码绑定账号：登录会话 + 轮询（Web SaaS 推荐）

你们的后端提供两个接口给自己的前端，浏览器只和它们打交道（沿用 8.1 中的 `call()`）：

```python
def start_binding(saas_user_id: str, platform: str) -> dict:
    _, session = call("POST", "/api/v1/accounts/login-sessions",
                      json={"platform": platform, "name": f"user-{saas_user_id}"})
    remember_session(saas_user_id, session["session_id"])  # 记录会话归属，查询时校验，防止越权
    return {"session_id": session["session_id"]}


def binding_status(saas_user_id: str, session_id: str) -> dict:
    ensure_owner(saas_user_id, session_id)
    _, session = call("GET", f"/api/v1/accounts/login-sessions/{session_id}")
    if session["status"] == "succeeded":
        save_account(saas_user_id, session["platform"], session["account"]["account_id"])
    return {"status": session["status"], "qrcode": session["qrcode"], "error": session["error"]}
```

前端每 2 秒查询一次，拿到二维码就显示：

```javascript
async function bindAccount(platform) {
  const res = await fetch("/api/publish-accounts/bind", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ platform }),          // "douyin" 或 "channels"
  });
  const { session_id } = await res.json();
  const timer = setInterval(async () => {
    const s = await (await fetch(`/api/publish-accounts/bind/${session_id}`)).json();
    if (s.status === "qrcode_ready") document.querySelector("#qrcode").src = s.qrcode;
    if (["succeeded", "failed", "expired"].includes(s.status)) {
      clearInterval(timer);
      showBindingResult(s);                      // 成功 / 失败原因 / 过期后提供“刷新二维码”
    }
  }, 2000);
}
```

### 8.4 转发扫码登录事件（SSE，备选，Python）

```python
import json

import httpx


def login_events(platform: str, name: str):
    """逐个产出 (event, data)，由产品后端转发给自己的前端。"""
    params = {"platform": platform, "name": name}
    with httpx.stream("GET", f"{HUB}/api/v1/accounts/login", params=params,
                      headers={"X-API-Key": KEY}, timeout=httpx.Timeout(300, connect=10)) as response:
        event = None
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                yield event, json.loads(line[len("data:"):].strip())


for event, data in login_events("douyin", "品牌主号"):
    if event == "qrcode":
        pass  # 推送 data["qrcode"] 给前端展示
    elif event == "success":
        account_id = data["account"]["account_id"]  # 保存
    elif event == "failed":
        pass  # 提示用户重试
```

---

## 9. 能力边界与注意事项

**数据时效与缓存**：服务端对相同请求有缓存——作品详情约 30 分钟，列表（搜索、评论、作品列表、热榜）约 5 分钟；缓存期内的重复请求直接返回、不消耗平台额度。产品侧对热门内容再加一层缓存效果更好。

**采集频率**：数据来自抖音网页端接口，依赖服务端维护的登录态身份池。平台对高频采集（尤其是关键词搜索）会限流或要求验证，表现为 `503 UPSTREAM_UNAVAILABLE`，稍后自动恢复。吞吐量取决于身份池规模，而不是服务器配置；有批量采集需求时请提前与运维沟通扩容。

**发布**：基于浏览器自动化，平台创作者后台改版可能导致短时间发布失败，服务端会跟进适配；请监控 `publish_video` 任务的失败率。

**直链有效期**：`media.video.urls` 等 CDN 地址带签名，几小时后失效，不要长期存储。

**合规**：评论者昵称、头像等属于个人信息，存储和使用需符合《个人信息保护法》；采集与发布需遵守各平台用户协议，并对发布内容负责。

---

## 10. 附录：接口总表

| 方法 | 路径 | scope | 同步/异步 |
|---|---|---|---|
| GET | `/api/v1/platforms` | 任意有效 Key | 200 |
| GET | `/api/v1/douyin/search` | douyin:read | 200 / 202 |
| GET | `/api/v1/douyin/hot` | douyin:read | 200 / 202 |
| POST | `/api/v1/douyin/parse` | douyin:read | 200 / 202 |
| GET | `/api/v1/douyin/video` | douyin:read | 200 / 202 |
| GET | `/api/v1/douyin/comments` | douyin:read | 200 / 202 |
| GET | `/api/v1/douyin/comments/replies` | douyin:read | 200 / 202 |
| GET | `/api/v1/douyin/user` | douyin:read | 200 / 202 |
| GET | `/api/v1/douyin/user/posts` | douyin:read | 200 / 202 |
| POST | `/api/v1/douyin/downloads` | media:download | 202（完成时 200） |
| GET | `/api/v1/douyin/downloads/{task_id}` | media:download | 200 |
| GET | `/api/v1/douyin/downloads/{task_id}/files/{name}` | media:download | 文件流 |
| GET | `/api/v1/tasks/{task_id}` | 任意有效 Key | 200 |
| POST | `/api/v1/media` | publish:write | 201 |
| POST | `/api/v1/publish/videos` | publish:write | 202（幂等重放 200） |
| GET | `/api/v1/accounts` | accounts:manage | 200 |
| POST | `/api/v1/accounts/login-sessions` | accounts:manage | 202（轮询会话） |
| GET | `/api/v1/accounts/login-sessions/{session_id}` | accounts:manage | 200 |
| GET | `/api/v1/accounts/login` | accounts:manage | SSE 流 |
| DELETE | `/api/v1/accounts/{account_id}` | accounts:manage | 200 |
| GET | `/api/v1/channels/search` | channels:read | 200 |
| GET | `/api/v1/channels/video` | channels:read | 200 |
| GET | `/api/v1/channels/comments` | channels:read | 200 |
| * | `/api/v1/admin/*` | X-Admin-Token | 见第 6 节 |
| GET | `/healthz`、`/readyz` | 无 | 200 / 503 |
