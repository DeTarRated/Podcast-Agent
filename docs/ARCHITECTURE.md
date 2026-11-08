# 系统架构 — 模块职责划分

> 按系统四层划分四个模块。需求见 `REQUIREMENTS.md`，沟通规则见 `AGENTS.md`。

数据流：发现层入队 → 执行层加工到待审核 → 审核层人工闸门 → 交付层私有链接。

| 分层 | 模块 | 技术归属 |
| --- | --- | --- |
| 发现层 | 调度模块 | 自研（cron + Python discover） |
| 执行层 | Agent 执行模块 | Pi Durable 骨架 + 自研 Runner / 流水线工具 / 账本 |
| 审核层 | 审核与评测模块 | 自研（静态页 + Node 审批服务 + override 表 + 评测集） |
| 交付层 | 交付模块 | 自研（模板生成 + boto3 上传 R2 + 签名链接） |

## 发现层 — 调度模块

**职责**

- 管理 feed 配置（名称、feedUrl、所属节目），支持增删；支持 Apple Podcasts Lookup API 检索 feedUrl
- 每 15 分钟轮询全部 feed，携带 ETag/If-Modified-Since；304 直接结束，不产生任何后续动作
- 以 episode guid 为去重主键，同一 guid 不重复入队
- 可选：用便宜 LLM 对标题/简介做相关性分类（每期 <$0.005），命中才入队
- 新期入队：创建 Episode 记录（状态 discovered），提交执行层

**输入 / 输出**

- 输入：feed 配置、RSS 响应
- 输出：Episode 记录（guid、feed、标题、发布时间、enclosure）

**关键机制**

- 条件请求：ETag / If-Modified-Since / 304
- guid 去重
- 纯元数据操作：不下载音频、不调用付费 API（分类除外）、不调用 LLM（分类除外）
- 单 feed 失败不影响其他 feed

**不负责**：下载、转写、翻译、状态推进、审核、交付

## 执行层 — Agent 执行模块

**职责**

- 每期一个 Pi Durable conversation，驱动 Episode 状态机推进：downloaded → transcribed → translated → reviewing
- 主代理做模型决策：调用哪些工具、翻译策略、新专名调研、驳回后的重跑范围
- 工具（经 bash 调 Python）：
  - 下载与固化：跟随 302 下载 mp3、sha256 固化、失败重试 3 次指数退避、同 guid hash 不一致时告警并保持首版
  - 转写：ElevenLabs Scribe v2（说话人分离 + 词级时间戳）、幂等键 = guid + 音频 hash、重组为一句一条英文 SRT
  - 翻译：translate-srt 流程（输入体检、术语表、翻译、复核、重切分），由子代理执行（接入方式待最小 MVP 验证）
  - 知识库：调研新专名、记录 ASR 错听变体、沉淀写入（人工确认后提交）
- 模型路由：翻译/调研用快模型，复核用强模型
- 账本：记录每次付费调用（金额估算、触发时间、所属期数）
- 崩溃恢复：checkpoint 断点续跑、unsafe 工具绝不自动重跑、safe 工具重跑、memo 防审批重复打扰、连续崩溃熔断 park

**输入 / 输出**

- 输入：Episode 记录 + 音频 mp3
- 输出：英文 SRT、中文译文稿、审核记录、成本记录；Episode 状态推进到 reviewing

**关键机制（Pi Durable 提供）**

- transcript 持久化、每步一个 checkpoint
- safe / unsafe 工具语义 + 幂等键
- effect sandwich（先落 intent 再落 outcome）、memo（第一次写赢）、parked（连续崩溃熔断）

**关键机制（自研）**

- Runner 接口（submit / resume / checkpoint），隔离 experimental API
- 幂等键设计（guid + 音频 hash）
- 账本

**不负责**：调度发现、审核、评测、交付上传

## 审核层 — 审核与评测模块

**职责**

- 审核页：音频播放 + 字幕同步显示、原文/译文逐条 diff、定点修改、问题标注
- 审批服务：通过 → 交付；驳回 → 记录原因与重跑范围，交回执行层重跑
- override 表：人工修改持久化保留，任何重跑中优先于模型产物，不可被覆盖
- 评测：每次人工改动自动生成一条标注样本；指标：专名准确率、术语一致率、漏译率、每期人工修改条数趋势
- 审核期音频由审批服务直接服务本地产物目录；R2 仅在审批后上传

**输入 / 输出**

- 输入：reviewing 状态的期（音频、英文 SRT、中文译文）
- 输出：approved（通过）/ rejected（驳回原因 + 重跑范围）；评测样本

**不负责**：翻译、生成看点页、上传 R2

## 交付层 — 交付模块

**职责**

- 生成看点页：固定模板填充节目信息、看点摘要、看点内容、时间点（可跳转音频）
- 上传 R2：中文 SRT、看点页、音频
- 签发 S3 签名 URL（带有效期与访问范围限制），支持按需重新签发续期
- 交付清单：中文 SRT 链接、看点页链接、原始音频私有链接
- 状态推进：approved → delivered

**输入 / 输出**

- 输入：approved 期的终稿与全部产物
- 输出：交付清单（三类私有链接）

**不负责**：翻译、审核、评测

## 待验证事项

- 子代理（翻译/复核）在 Pi Durable 中的接入方式：**已定为嵌套任务**（2026-11 实验验证）——经 ToolSpec `extras.dispatch` 创建子任务并等待；跨调用去重用 scanTasks + callRef（memo 不跨工具调用）；成本随 result 回传进账本；子任务失败即终态不自动重跑（符合付费红线）。实验记录见 `docs/verification/subagent-dispatch.md`
- fork / steer 在本项目中的实际用法
- Durable 能力边界：已由流程测试验证（checkpoint、transcript 持久化、safe 重跑、unsafe 不重跑、restartLimit 熔断、memo），结论与复现入口见 `docs/verification/flow-test.md`；Runner 仍需补齐的边界（真实付费调用对账闭环、产物清单联动）同见该文档
