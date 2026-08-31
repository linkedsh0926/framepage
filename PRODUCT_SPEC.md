# 帧页 FramePage：产品与技术规格（原型评审版）

> 版本：0.1 · 2026-08-30
> 当前阶段：先评审交互与方案，不接入真实下载、模型调用或 PDF 生成。

## 1. 产品定义

帧页把“逐步展开的 PPT/白板式 YouTube 讲解视频”整理为可阅读、可检索的图文 PDF。系统为每一页保留**内容加载完成后的最终画面**，并在其后排入该页出现期间的中文为主、夹杂英文的讲解文本。

首版面向需要复习课程、研究行业讲解和归档知识视频的个人用户。页面的单一核心任务是：粘贴 YouTube 链接，选择适合该类视频的参数 Profile 与输出选项，等待生成并下载 PDF。

### 首版范围

- 输入：公开或可直接访问的单个 YouTube 视频 URL。
- 输出：A4 竖版 PDF，每个视频页包含高清关键帧、页码、视频时间范围和校订后的讲解。
- 支持：普通视频、`youtu.be` 短链、Shorts 链接；中文、英文和中英混讲。
- 暂不支持：播放列表、直播、DRM、需要登录/年龄验证/地区绕过的视频、用户上传本地视频、多人说话人分离、在线逐页编辑。
- 权利提示：用户必须拥有下载、处理和个人使用内容的权利；服务不绕过访问控制。

### 默认限制

- 单视频最长 120 分钟；下载文件最大 2 GB。
- 每个访客同时 1 个任务、每天 5 个任务；后续按账号/套餐扩展。
- 原始视频、中间文件和结果默认在任务完成后 24 小时删除。
- 无账号 MVP 通过不可猜测的任务令牌访问结果；不提供永久历史。原型中的 Profile 先保存在浏览器本地，不跨设备；正式账号体系上线后再迁移到服务端按用户/租户持久化。

## 2. 用户流程

1. 粘贴 YouTube URL。
2. 选择参数 Profile（智能默认或用户保存的多个 Profile）；可展开调整候选阈值、稳定态等待、局部重采样上限和语义复核轮数。
3. 可选设置讲稿样式（校订稿/接近逐字稿）、时间标记、页面密度，以及任务结束后是否给出 Profile 更新建议。
4. 服务读取元数据并显示标题、时长、预计页数范围和预计耗时。
5. 用户确认后创建任务；前端通过 SSE 展示 6 个阶段和可恢复的错误信息。
6. 处理完成后，用户先看页数与抽样预览，再下载 PDF；若产生了高置信参数改进，单独展示原因和差异。
7. 参数改进默认只形成建议，用户确认后才更新当前 Profile 或另存为新 Profile；任务可取消，也可从失败阶段重试。

失败信息必须告诉用户下一步，例如“视频仅登录后可见，请换用公开链接”，不能只显示错误码。

## 3. 处理管线

### 3.1 下载与标准化

- 用 `yt-dlp` 解析受支持域名并下载视频；只允许明确的 YouTube hostname，禁止任意 URL，降低 SSRF 风险。
- `ffprobe` 验证时长、编解码、分辨率和音轨。
- 生成两类派生文件：
  - 分析视频：保持宽高比，高度 360px，4 fps，H.264，固定像素格式。
  - ASR 音频：16 kHz、单声道、24–32 kbps Opus。压缩音频更适合长视频和 OpenRouter 的请求时限。
- 原始视频只用于最终按时间点回取高清帧，输出帧最长边限制为 1920px；因此“360p/4fps”只降低分析成本，不牺牲 PDF 清晰度。

### 3.2 页面边界与最终关键帧

算法针对“同一页逐步加内容，随后快速切到下一页”的模式，而不是通用镜头摘要：

1. 检测并裁去稳定黑边；保留演示主体，不默认裁掉画中画讲师。
2. 对 4 fps 帧计算亮度直方图、感知哈希、SSIM、边缘变化率和清晰度；使用多个相邻阈值宽松提取候选，而不是一次阈值直接定稿。
3. 用“大幅视觉跳变 + 跳变前后各自稳定”识别硬切；对淡入淡出用短窗口聚类确定边界。每个疑似页面区间保留 3–6 个有明确来源的候选：最后稳定帧、切页前帧、内容量最大帧、最清晰帧和相邻阈值候选。
4. 将同一视觉布局中内容逐步增加的候选组织为时间有序的“渐进链”。默认保留链的末端完整态；高亮切换、动画回退等非单调变化不能只用内容量判断。
5. ASR 初稿完成后，把有序候选联系表、单张高清候选、对应讲解和相邻页面上下文交给视觉模型。模型分别判断 `visual_role`（完整页/部分加载/过渡/重复/无意义）与 `narration_relation`（本章/前章/后章/无关），不能用一个总分混合两类证据。
6. 模型只返回受限动作：`keep`、`drop`、`merge`、`search_later`、`search_earlier`、`resample_local`、`low_confidence`。服务端把动作转换为确定性的局部搜索，模型不能直接任意改写全视频阈值。
7. 对“部分加载”只向后搜索当前页面区间；对“漏页”只降低局部窗口阈值；对“过渡污染”搜索最近稳定态；对不确定窗口从原始视频以 8–12 fps 局部重采样。4 fps 仍是常规分析速率，不是所有疑难区间的上限。
8. 最多执行 3 轮复核。连续两轮选择一致且置信度达标、改进幅度不足或达到预算即停止；仍无法确定时保留 2–3 个候选供人工选择，或输出最佳帧并标记低置信度，禁止无限循环。
9. 用 pHash、局部特征和布局相似度合并短暂回闪或完全重复页面；同一页后来新增内容时保留更完整版本。只有难以判定的近似页才补做 OCR 文本比较。
10. 从原始视频在选定时间点重取高清帧。若正好落在转场污染帧，前后各搜索 500ms，选与分析候选最相似且最清晰的帧。

为每页保存：`start_ms`、`end_ms`、`keyframe_ms`、边界置信度、最终态置信度、去重依据和图像对象地址。

### 3.3 参数 Profile 与受控学习

- 系统提供不可覆盖的“智能默认”，用户可以保存多个自定义 Profile，例如“渐进式课件”“屏幕录制”“白板讲解”。Profile 与账号/租户绑定，不保存视频链接、关键帧、音频或讲稿。
- Profile 保存：`name`、`version`、`source_profile_id`、变化敏感度、稳定态等待、局部重采样 fps、复核轮数、ROI/画中画遮罩、适用特征、已验证视频数、最近使用时间和创建/更新时间。
- 运行时采用三层参数：全局安全默认 → 选中的持久 Profile → 当前任务局部覆盖。任务局部迭代不能在处理中直接写回长期 Profile。
- 任务结束后只把高置信改进生成 `profile_update_proposal`，包含旧值、新值、适用范围、证据、算法/VLM 一致性、复核轮数和预计影响。用户可忽略、更新原 Profile 或另存为新 Profile。
- 只有算法与视觉模型一致、跨两轮复核稳定，或经过用户人工确认的参数才能进入建议；低置信模型单判、异常视频和降级路径不得污染长期参数。
- Profile 更新使用乐观锁和版本号；任务始终记录使用的 `profile_version` 与快照，保证后续更新不会改变既有任务的可复现性。删除 Profile 不删除已完成任务。

### 3.4 画面理解与按需 OCR

- 主路径不再对每个关键帧预先运行独立 OCR。高清关键帧直接与该页 ASR 一起发送给视觉校对模型，让模型同时利用文字、图表、箭头和版面层级。
- 发送给模型的分析图最长边限制为 1920px，JPEG quality 90 左右；PDF 仍使用单独保存的高清帧。小字号密集页面可自动补送标题区/正文区裁片。
- 视觉模型必须返回 `visual_evidence[]` 和 `uncertain_terms[]`。当画面模糊、术语冲突、不确定项超过阈值、模型调用失败或页面去重存在歧义时，才调用本地 PaddleOCR 中英模型兜底，保留文本、bbox 和置信度。
- OCR 结果只是补充证据，不能让低置信 OCR 覆盖清晰 ASR。表格、公式和代码块只做视觉理解与文本提示，不承诺首版重建为可编辑结构。

### 3.5 ASR 与页面对齐

- OpenRouter 模型 ID：`qwen/qwen3-asr-1.7b`。
- 不把整段两小时音频作为单请求。页面边界确定后，按页面区间切音频，并在边界两侧保留很短的上下文；超过约 45 秒的页面再按 VAD 静音点切块，块间重叠 800ms。
- 每块调用 `POST /api/v1/audio/transcriptions`，语言不强制为纯中文，以保留英文术语；温度设为 0。
- 如果实际上游支持 `verbose_json` 与时间戳则保存它们；如果不支持，页面级切片本身就是对齐边界，系统仍能工作。这一点要在集成测试中做能力探测，不能假设所有 OpenRouter STT 上游都接受相同扩展字段。
- 重叠块用字符级/词级最长公共序列去重；保留原始分块、用量和 OpenRouter `X-Generation-Id` 便于排障。

### 3.6 DeepSeek 视觉校订

- OpenRouter 模型 ID：`deepseek/deepseek-v4-flash-vision-exp`；它能接收图片与文本，适合文档和图表理解。
- 输入包括：本页高清关键帧、本页原始 ASR、上一页末尾与下一页开头的短文本上下文、时间范围；按需附加页面裁片或 OCR blocks。
- 任务只允许：修正中英混讲的错字与术语、恢复标点和分段、去除分块重叠、按页面结构整理；不得补写视频中未出现的知识。
- 使用 JSON mode 输出：`title`、`paragraphs[]`、`visual_evidence[]`、`uncertain_terms[]`、`warnings[]`。Vision Exp 当前支持 JSON 输出但不保证 JSON Schema enforcement，因此服务端必须用 Pydantic 校验；解析失败只做一次 repair retry，仍失败就使用确定性清洗后的 ASR。
- 冲突优先级：清晰 ASR 原话 > 高清画面中的明确文本 > 按需 OCR 的高置信专有名词 > 相邻页上下文 > 模型常识。所有低置信改动记入内部审计字段。
- 页面之间可并发校订，但限制并发数并对 429/5xx 做带抖动指数退避。
- 该模型标记为 experimental，且当前 OpenRouter 模型页只列出一个上游。启动时做图片输入与 JSON mode 能力探测；不可用时任务降级为“按需 OCR + 文本版 `deepseek/deepseek-v4-flash-0731`”，而不是让整条管线失败。

### 3.7 PDF

- 使用 HTML/CSS 分页模板 + WeasyPrint 生成，容器内安装 Noto Sans CJK SC，确保中文不缺字。
- A4 竖版；首页给出标题、来源、生成时间和页数。正文每一页先放 16:9 关键帧，再放时间范围与讲解。
- 讲解过长时自动续页，续页沿用该视频页标题和时间范围，不压缩到难以阅读。
- 关键帧使用 JPEG quality 85 左右；PDF 包含书签、来源链接和生成参数，不嵌入 API 密钥或内部对象地址。

## 4. 系统架构

首版选择 Python 3.12 生态，减少跨语言媒体处理成本：

- API：FastAPI + Pydantic，提供鉴权、任务管理、SSE 和下载签名。
- 异步任务：Celery worker + Redis broker；每个阶段是幂等任务，产物有 checksum，可从失败阶段恢复。
- 数据：PostgreSQL 保存任务、页面、阶段和用量；S3/MinIO 保存视频、音频、帧和 PDF。
- 媒体/视觉兜底：`yt-dlp`、FFmpeg/ffprobe、OpenCV、按需加载的 PaddleOCR。
- PDF：Jinja2 模板 + WeasyPrint。
- 部署：Docker Compose 用于开发；生产拆分 API、CPU 媒体 worker、OCR worker。OpenRouter 调用不需要本地 GPU。

任务状态机：

`queued → downloading → proposing_frames → transcribing → reviewing_frames → correcting → rendering → completed`

任一处理中状态都可进入 `retryable_failed`、`terminal_failed` 或 `cancelled`。进度按已完成工作单元计算，不使用纯时间模拟；页面分析完成后才能显著提高预计准确度。

## 5. API 契约草案

### 创建任务

`POST /api/v1/jobs` → `202 Accepted`

```json
{
  "youtube_url": "https://www.youtube.com/watch?v=...",
  "profile_id": "profile_ppt_progressive",
  "profile_version": 4,
  "profile_overrides": {},
  "learning_policy": "suggest",
  "transcript_style": "polished",
  "include_timestamps": true,
  "page_density": "comfortable"
}
```

返回 `job_id`、`access_token`、`status_url`、`events_url` 和 `expires_at`。`access_token` 只显示一次，Web 客户端存在 sessionStorage，不写进 URL。

### 查询与控制

- `GET /api/v1/jobs/{job_id}`：状态、真实进度、当前阶段、页数、预估剩余时间、警告。
- `GET /api/v1/jobs/{job_id}/events`：SSE，事件类型 `stage`、`progress`、`review_round_started`、`review_round_failed`、`review_action`、`review_round_passed`、`review_stopped`、`warning`、`completed`、`failed`。复核事件包含轮次、问题区间数、失败原因、局部动作与停止条件，供前端逐轮展示而不是只显示最终结果。
- `DELETE /api/v1/jobs/{job_id}`：取消任务并排队清理产物。
- `POST /api/v1/jobs/{job_id}/retry`：仅重试可恢复失败阶段。
- `GET /api/v1/jobs/{job_id}/pages?cursor=...`：预览页与校订文本。
- `GET /api/v1/jobs/{job_id}/result`：完成后返回短期签名下载地址。
- `GET /api/v1/jobs/{job_id}/profile-proposal`：返回本次高置信参数改进及证据；没有建议时返回 `204`。
- `GET /api/v1/profiles`、`POST /api/v1/profiles`：列出与创建用户的参数 Profile。
- `PATCH /api/v1/profiles/{profile_id}`：带 `If-Match`/版本号更新 Profile；版本冲突返回 `409`。
- `POST /api/v1/profiles/{profile_id}/duplicate`、`DELETE /api/v1/profiles/{profile_id}`：复制或删除 Profile。系统默认 Profile 不可删除。
- `POST /api/v1/profile-proposals/{proposal_id}/apply`：用户确认后更新目标 Profile 或创建新 Profile；请求必须幂等。

所有响应含 `request_id`；创建任务支持 `Idempotency-Key`，避免重复点击产生双重费用。

## 6. 数据、安全与运行策略

- OpenRouter 密钥只放服务端 secret/env `OPENROUTER_API_KEY`，日志、错误响应、HTML 和 Git 永不出现。
- 严格校验 YouTube hostname、重定向目的地、视频 ID 和媒体大小；下载进程使用无特权用户、CPU/内存/磁盘/时长限制。
- 对 API 做 IP/令牌速率限制、并发限制、请求体限制和 CORS allowlist。
- 对象存储默认私有，下载 URL 短期有效；数据库只存对象 key。
- Profile 仅保存参数、适用特征和聚合验证统计；不得把原始帧、ASR 文本或可还原用户视频的信息写入 Profile。所有查询按用户/租户隔离。
- 记录每次 OpenRouter 的模型 ID、generation ID、耗时、重试与 `usage.cost`，但不记录完整音频或图片 base64。
- 定时清理任务必须可观测且可重试；用户取消后立即撤销结果 URL，并异步删除文件。
- 用户在聊天中提供过一枚完整 API key。正式开发前应在 OpenRouter 撤销并重新生成；原密钥视为已经暴露。

## 7. 质量门槛与验收

建立至少 20 条、覆盖中文夹英文、不同动画速度、淡入淡出、画中画和长停顿的视频标注集：

- 页面边界 F1 ≥ 0.95。
- “最终内容态”选中率 ≥ 0.95。
- 重复页率 ≤ 0.03，漏页率 ≤ 0.03。
- 无意义过渡页进入 PDF 的比例 ≤ 0.01；需要局部重采样的区间中，最终帧正确率 ≥ 0.95。
- 复核循环 100% 在配置轮数/预算内停止；低置信样本不会自动写入 Profile。固定任务快照在 Profile 更新后仍能重现相同参数。
- 讲解分配到正确页面的句子比例 ≥ 0.92。
- 中英术语校订后人工错误率低于原 ASR，图表关系描述与画面一致，且无事实性增写。
- 30 分钟目标视频在标准生产 worker 上 P95 处理时间 ≤ 12 分钟；失败阶段可恢复。
- 375、768、1024、1440px 前端无横向溢出；键盘可完成创建、查看状态与下载。

当前 OpenRouter 标价显示 Qwen3-ASR-1.7B 为每秒 $0.000008，即一小时音频约 $0.0288；DeepSeek V4 Flash Vision Exp 当前为每百万输入/输出 token $0.22/$0.66，图片还会转成输入 token，基础设施成本另计。产品计费不得硬编码网页价格，以每次响应的实际 `usage.cost` 为准。

## 8. 原型评审重点

请重点确认：四步向导的顺序、Profile 选择/展开参数/管理多 Profile 是否易懂、参数学习是否坚持“先建议再确认”、多轮复核阶段是否清楚，以及结果页的 PDF 预览与改进建议优先级。确认后再进入 API/worker 与真实模型集成。

## 9. 已核验的外部接口资料

- [OpenRouter Speech-to-Text 文档](https://openrouter.ai/docs/guides/overview/multimodal/stt)
- [OpenRouter 上的 Qwen3-ASR-1.7B](https://openrouter.ai/qwen/qwen3-asr-1.7b)
- [OpenRouter 上的 DeepSeek V4 Flash Vision Exp](https://openrouter.ai/deepseek/deepseek-v4-flash-vision-exp)
- [OpenRouter Structured Outputs](https://openrouter.ai/docs/guides/features/structured-outputs)
- [OpenRouter Image Inputs](https://openrouter.ai/docs/guides/overview/multimodal/image-understanding)
