# 系统架构 — 模块职责划分

> 按系统四层划分四个模块。需求见 `REQUIREMENTS.md`，沟通规则见 `AGENTS.md`。

数据流：发现层入队 → 执行层加工到待审核 → 审核层人工干预／超时自动审批 → 交付层发布到私人播客库。

| 分层 | 模块 | 技术归属 |
| --- | --- | --- |
| 发现层 | 调度模块 | 自研（cron + Python discover） |
| 执行层 | Agent 执行模块 | Pi Durable 骨架 + 自研 Runner / 流水线工具 / 账本 |
| 审核层 | 审核模块 | 自研（审核箱静态页 + Node 审批服务 + 超时自动审批 + override 表；评测后置） |
| 交付层 | 私人播客库与交付模块 | 自研（节目库与播放页 + 模板生成 + boto3 上传 R2 + 动态签名 URL） |

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

## 审核层 — 审核模块

**职责**

- 审核箱：展示所有待审核节目，支持多期积压、抽检、直接通过、保留待审与按范围驳回
- 审核页：音频播放 + 字幕同步显示、原文／译文逐条 diff、定点修改、分条版本选择
- 审批服务：人工通过立即进入交付；每个新待审版本默认 12 小时后由定时任务自动审批；打开与播放不重置计时，内容版本变化重新计时
- 自动审批在短事务内复核状态、版本、hash 与冲突；审批记录区分 `manual` / `auto_timeout`，绑定最终产物版本与 SHA256
- override 表：以源稿版本、时间范围与原文 hash 持久化人工修改；局部重跑后人工优先，无法唯一映射时阻止发布
- 再次编辑：已批准或已发布版本可创建新修订；审核层不直接改当前发布指针
- 审核期音频由审批服务直接服务本地产物目录；R2 仅在人工或自动审批后上传
- 用量与费用页：只读汇总、筛选和查看账本明细，区分估算与未对账；对账核销仍走 CLI，不在网页改账
- 评测不在本轮范围；保留 override 历史供后续补建样本

**输入 / 输出**

- 输入：reviewing 状态的期（音频、英文 SRT、中文译文）及再次编辑请求
- 输出：approved（人工或超时自动审批，绑定确定版本）/ rejected（驳回原因 + 时间范围）/ 保留待审

**不负责**：翻译与模型调用、生成看点内容、上传 R2、当前发布版本切换、本轮评测

## 交付层 — 私人播客库与交付模块

**职责**

- 生成看点内容：固定模板填充节目信息、看点摘要、看点内容和音频跳转时间点
- 上传 R2：已批准版本的中文 SRT、看点页与首次固化音频，完成校验后才推进 delivered
- 私人播客库：展示全部已发布节目，提供单期音频播放、字幕同步与点击跳转、双语对照、看点和 SRT 下载
- 提供稳定私有节目地址，按需签发有限期、限定对象范围的 S3 URL；过期重新签发，不改变节目地址
- 发布后修订期间继续服务旧发布版本；新版重新审批、上传并校验后原子切换当前发布指针
- 状态推进：approved → delivered；上传失败保留 approved 与旧发布版本，不暴露半套新产物

**输入 / 输出**

- 输入：approved 期的确定终稿、override 合并结果与交付选择
- 输出：私人播客库中的稳定节目页面及其私有音频、字幕与看点资源

**不负责**：翻译、审核与自动审批、override 写入、评测

## 待验证事项

- 子代理（翻译/复核）在 Pi Durable 中的接入方式：**已定为嵌套任务**（2026-11 实验验证）——经 ToolSpec `extras.dispatch` 创建子任务并等待；跨调用去重用 scanTasks + callRef（memo 不跨工具调用）；成本随 result 回传进账本；子任务失败即终态不自动重跑（符合付费红线）。实验记录见 `docs/verification/subagent-dispatch.md`
- fork / steer 在本项目中的实际用法
- Durable 能力边界：已由流程测试验证（checkpoint、transcript 持久化、safe 重跑、unsafe 不重跑、restartLimit 熔断、memo），结论与复现入口见 `docs/verification/flow-test.md`；Runner 仍需补齐的边界（真实付费调用对账闭环、产物清单联动）同见该文档
