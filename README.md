# 帧页 FramePage

帧页把渐进展开的 YouTube 课件或白板讲解整理成“最终关键帧 + 对齐讲稿”的 A4 PDF。本仓库是可运行的 MVP：默认 `demo` 模式不下载视频、不调用模型、也不需要 API key，适合验证完整任务、SSE 进度、预览和下载流程。

详细产品边界与技术设计见 [PRODUCT_SPEC.md](./PRODUCT_SPEC.md)。

## 最快启动：Docker Compose

需要 Docker Engine 与 Docker Compose v2。默认配置只启动一个 API 容器，数据写入本地 Docker volume；SQLite、本地对象目录和后台任务都在该容器内工作，不要求 Redis、PostgreSQL 或 MinIO。

```bash
cp .env.example .env
docker compose up --build
```

打开 <http://localhost:8000> 使用前端，访问 <http://localhost:8000/docs> 查看 OpenAPI 文档，或检查健康状态：

```bash
curl --fail http://localhost:8000/healthz
```

`.env.example` 已将 `FRAMEPAGE_MODE` 设为 `demo`，`OPENROUTER_API_KEY` 应保持为空。停止服务用 `docker compose down`；这不会删除已生成的数据。

镜像以非 root 用户运行，并安装了 `yt-dlp`、FFmpeg/ffprobe、Poppler、Noto CJK 字体及 WeasyPrint 所需系统库。容器健康检查会调用 `/healthz`。

## 本地开发

需要 Python 3.12，以及用于真实模式的 FFmpeg/ffprobe。要在本机检查生成的 PDF，建议同时安装 Poppler、Pango 和 Noto Sans CJK 字体。macOS 可用 Homebrew 安装常用依赖：

```bash
brew install ffmpeg poppler pango
brew install --cask font-noto-sans-cjk
```

Pango/WeasyPrint 不可用时会自动改用 ReportLab 生成完整 A4 文档；安装上述原生依赖仍能获得更丰富的 HTML/CSS 排版。

创建隔离环境并启动：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
cp .env.example .env
./scripts/dev.sh
```

`scripts/dev.sh` 会读取当前仓库中受信任的 `.env`，以热重载方式启动 `uvicorn app.main:app`。默认数据目录是 `.framepage-data/`。

## 测试

安装 `test` 可选依赖后运行：

```bash
./scripts/test.sh
```

也可直接运行 `FRAMEPAGE_MODE=test python -m pytest`。查看 Compose 展开的最终配置可用 `docker compose config`。

## API 示例

以下示例使用专门的 demo URL；它只在 `demo`/`test` 模式有效：

```text
https://www.youtube.com/watch?v=FramePageDemo
```

FastAPI 的交互文档位于 <http://localhost:8000/docs>。典型流程是先读取元数据，再创建任务；`access_token` 只由任务创建响应（包括同一请求的幂等重放）返回，前端随后只把它保存在 `sessionStorage`。状态、事件、预览和结果请求必须携带它。

读取视频元数据：

```bash
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -d '{"youtube_url":"https://www.youtube.com/watch?v=FramePageDemo"}' \
  http://localhost:8000/api/v1/videos/inspect
```

创建 demo 任务：

```bash
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: readme-demo-001' \
  -d '{
    "youtube_url": "https://www.youtube.com/watch?v=FramePageDemo",
    "profile_id": "profile_smart_default",
    "profile_version": 1,
    "profile_overrides": {},
    "learning_policy": "suggest",
    "transcript_style": "polished",
    "include_timestamps": true,
    "page_density": "comfortable"
  }' \
  http://localhost:8000/api/v1/jobs
```

从响应中复制 `job_id` 与 `access_token`，只放在当前 shell 变量中：

```bash
JOB_ID='job_...'
ACCESS_TOKEN='fp_...'

curl --fail-with-body \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  "http://localhost:8000/api/v1/jobs/${JOB_ID}"

curl --no-buffer --fail-with-body \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  "http://localhost:8000/api/v1/jobs/${JOB_ID}/events"
```

完成后获取预览与下载信息：

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  "http://localhost:8000/api/v1/jobs/${JOB_ID}/pages"

curl --fail-with-body \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  "http://localhost:8000/api/v1/jobs/${JOB_ID}/result"
```

`/result` 返回短期有效的相对 `download_url`；将它拼在 `http://localhost:8000` 后即可下载 PDF。该签名下载 URL 不再需要 Bearer token。页面 `preview_url` 当前仍要求同一个 Bearer token。

创建任务支持 `Idempotency-Key`，客户端重试时应复用同一个 key，避免重复任务和真实模式下的重复费用。不要把 token 或 API key 放进 URL、日志或前端持久存储；浏览器端只保存在 `sessionStorage`。

任务会经过 `queued → downloading → proposing_frames → transcribing → reviewing_frames → correcting → rendering → completed`。进度流使用 SSE；下载事件额外包含已下载字节、总字节、实时速度和阶段 ETA，后续阶段包含完成单元与任务剩余时间。任务完成后先读取页面预览，再请求短期签名下载地址。具体请求体、鉴权 header 与响应字段以 `/docs` 为准。

## 前端

单页前端位于 `index.html`，API 在 `/` 同源提供它，因此推荐直接访问 <http://localhost:8000>，无需 Node.js 或额外静态服务器。这样任务请求、SSE 和下载都保持同源，也不会把服务端密钥暴露给浏览器。

如果单独托管前端，需要把该 origin 加入 `FRAMEPAGE_CORS_ORIGINS`。不要把 `OPENROUTER_API_KEY` 注入 HTML、JavaScript、构建变量或浏览器请求。

## Demo 与真实模式

| 模式 | 用途 | 外部依赖 |
| --- | --- | --- |
| `demo` | 默认；完整跑通任务和 PDF 下载 | 无 API key；不访问 YouTube/OpenRouter |
| `test` | 自动化测试、临时数据库 | 无外部网络 |
| `real` | 处理公开 YouTube 视频 | `yt-dlp`、FFmpeg/ffprobe、`OPENROUTER_API_KEY` |

启用真实模式前编辑本地 `.env`：

```dotenv
FRAMEPAGE_MODE=real
FRAMEPAGE_TOKEN_SECRET=请替换为至少32字符的全新随机值
```

可用 `openssl rand -hex 32` 生成签名密钥。同时在复制出的 `.env` 中为 `OPENROUTER_API_KEY` 填写新生成的服务端密钥，再运行 `chmod 600 .env` 和 `docker compose up --build`。真实模式缺少模型密钥或仍使用占位签名密钥时会拒绝启动。真实模式只接受明确的 `youtube.com`/`youtu.be` HTTPS 链接，不支持播放列表、登录后可见内容、DRM、直播、年龄或地区绕过；默认最长 120 分钟、最大下载 2 GiB。

真实管线使用 Qwen3 ASR 与 DeepSeek 视觉模型，可能产生 OpenRouter 费用。模型 ID、generation ID、重试和实际 `usage.cost` 应用于审计，但密钥和完整媒体 base64 不应写日志。MVP 的 SQLite + 本地文件 + API 进程内任务适合单机开发，不是生产级多租户部署；生产环境应拆分 worker，并接入持久数据库、私有对象存储、队列、TLS、认证与集中限流。

## 配置

所有配置均由服务端环境变量读取：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FRAMEPAGE_MODE` | `demo` | `demo`、`test` 或 `real` |
| `FRAMEPAGE_DATA_DIR` | `.framepage-data` | SQLite 与任务产物根目录；Compose 固定为 `/data` |
| `FRAMEPAGE_DATABASE_PATH` | `<data_dir>/framepage.sqlite3` | 可选 SQLite 文件位置 |
| `FRAMEPAGE_TOKEN_SECRET` | 本地开发占位值 | 任务 token 与下载签名密钥；部署时必须替换 |
| `FRAMEPAGE_CORS_ORIGINS` | `localhost:3000,localhost:5173` | 逗号分隔的精确 origin allowlist |
| `FRAMEPAGE_MAX_VIDEO_SECONDS` | `7200` | 最大视频时长（秒） |
| `FRAMEPAGE_MAX_DOWNLOAD_BYTES` | `2147483648` | 最大媒体文件大小 |
| `FRAMEPAGE_ARTIFACT_TTL_HOURS` | `24` | 任务产物默认保留时长 |
| `FRAMEPAGE_SIGNED_URL_SECONDS` | `300` | 结果下载签名有效期 |
| `FRAMEPAGE_MAX_REQUEST_BYTES` | `1048576` | API 请求体上限 |
| `FRAMEPAGE_RATE_LIMIT` | `180` | 单 IP 每分钟请求上限 |
| `OPENROUTER_API_KEY` | 空 | 仅 `real` 模式需要，必须只存在于服务端 |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter API 根地址 |

## 数据与清理

本地开发的数据在 `.framepage-data/`；Compose 使用 `ytb2pdf_framepage_data` volume。目录内包含 SQLite、任务媒体、中间帧和 PDF，不应提交 Git 或公开共享。

- 取消单个任务：调用该任务的 `DELETE /api/v1/jobs/{job_id}`，结果访问会立即失效。当前单进程 MVP 不内置定时清理调度器，磁盘文件仍需由下面的完整重置或部署侧清理任务删除。
- 普通停机：`docker compose down`，保留数据以便下次启动。
- 完全重置 Compose：先确认没有需要保留的结果，再运行 `docker compose down -v`。该操作永久删除该 Compose 项目的数据库和全部任务产物。
- 完全重置本地开发：停止 API、确认路径确实为仓库内的 `.framepage-data/`，备份需要的 PDF 后再删除该目录。

默认保留期为 24 小时。部署环境还应安排可观测、可重试的定时清理任务，并监控失败；仅设置 TTL 变量不能替代运维侧的清理调度。

## 安全提示

- 聊天或历史开发过程中出现过的旧 OpenRouter key 必须视为已泄露：先在 OpenRouter 撤销并轮换，再把新 key 写入本机 `.env` 或正式 secret manager。不要继续使用旧 key。
- `.env` 已被 Git 忽略，`.env.example` 中的 `OPENROUTER_API_KEY` 必须始终留空。提交前可用 `git diff -- . ':!.env.example'` 检查意外密钥。
- 不要向浏览器返回 OpenRouter key；不要把任务 `access_token` 放在 URL。下载地址中的短期签名也不应长期保存或转发。
- 只处理你有权下载和用于个人用途的公开内容；本服务不绕过访问控制。
- 面向公网部署前必须配置高熵 `FRAMEPAGE_TOKEN_SECRET`、TLS、严格 CORS、反向代理请求限制、隔离 worker、私有对象存储以及数据库/对象级租户隔离。
