# Pi Durable vs LangGraph 选型讨论记录

- 日期：2026-11
- 状态：讨论记录，未整理回权威文档
- 起因：质疑旧文档（HANDOFF）对比依据质量，要求独立重新评估执行层骨架选型

## 结论先行

重新核实两边 2026-11 现状后，**Pi Durable 的选择依然成立，但旧文档的理由需要替换**。旧论断「LangGraph = 状态图但恢复语义弱」过时且不准；准确的差别是恢复粒度与防重责任归属，不是能力有无。

## 职责划分的澄清

「进程默认关闭、按需唤醒」与「拉起后从断点恢复」是两块独立的需求，容易混为一谈：

| 需求 | 负责方 | 备注 |
| --- | --- | --- |
| 进程开关：按需唤醒、空闲退出 | cron + 自研 Runner | 两套方案下都要自建 |
| 拉起后从断点继续、付费调用不重复 | Pi Durable（open + resume + safe/unsafe 契约） | 选型的核心依据 |

Pi Durable 不提供进程管理能力；LangGraph 也不提供（其生产形态反而是常驻 worker/server，与「无常驻进程」目标相反，官方 cron 能力在平台侧）。

## 两边现状核实（2026-11，以官方文档为准）

**Pi Durable**

- npm 包 2026-09-19 创建，1.0.0（10-01）→ 1.5.0（11-02）一个月五个 minor，README 仍标注 Experimental：「The API changes without notice between releases」
- 持久化单元是对话：模型请求、工具调用、compaction 各自是任务，先落盘再执行，每步一个 checkpoint
- 工具级 safe/unsafe 契约：恢复时只有存储的 intent 与当前声明都为 safe 才重跑；unsafe 绝不自动重跑，模型收到 interrupted 结果自行决策（spec 原文：孤儿副作用产生 interrupted 结果并附带 durable partial output）
- 模型请求崩溃：已提交部分标记 aborted，请求重发（计一次失败尝试）
- 配套机制：effect sandwich（先 intent 后 outcome）、memo（第一次写赢）、restartLimit（默认 5 次连续崩溃 park）
- 官方 Limitations 第一条：无内置审批
- 单进程独占存储；SQLite 单文件后端

**LangGraph**

- durability 三档：exit / async（默认，有极小崩溃窗口）/ sync（每步同步落盘）
- checkpoint 在 super-step（node）边界：「A node either completes and gets checkpointed, or it did not run. There is no checkpoint for half a node」
- 恢复时 in-flight node 从函数开头重跑；官方文档原话：「Design node logic so re-execution does not corrupt state. Use idempotency keys」
- `@task` 包装的副作用：恢复时从 checkpointer 按**位置**取缓存结果不重执行；恢复点之前 task 代码顺序变化会导致缓存错位（官方 backward-compatibility 文档明确警告）
- SQLite saver 官方定位「experimentation and local workflows」，生产推荐 Postgres
- `interrupt()`/`Command(resume)` 内建人工审核闸门；retry_policy / error_handler / timeout 组合实现重试与熔断
- 自由 agent loop（如 create_react_agent）装进一个 node 时，checkpoint 只在 loop 整体前后，循环内部单个工具调用不在 checkpoint 粒度内

## 恢复粒度对比：具体场景

场景：一期 49 分钟节目，两个崩溃点——转写 POST 在途、翻译中途（术语表与翻译子代理已完成、复核在途）。

**崩溃点·转写 POST 在途，进程死**

- Pi Durable：工具任务已落盘，replay=unsafe 不重跑；模型收到 interrupted，拿落盘参数里的幂等键（guid+hash）查 Scribe 状态或重发同键；POST 永远不会被框架盲目重发
- LangGraph：node 未完成即无 checkpoint；恢复后 node 从头重跑，POST 再发一次，靠应用层幂等键去重
- 两者结果都能安全，差别在保证位置：Pi Durable 是协议，LangGraph 是纪律

**崩溃点·翻译中途**

- Pi Durable：术语表脚本、翻译子代理轮次各自已 committed；只有复核在途的模型请求重发；前面全部不动
- LangGraph 单 node 写法：整个 node 重跑，所有 LLM 调用重发，且模型输出非确定 → 翻译稿漂移 + 成本翻倍
- LangGraph 五小 node 写法：只有复核 node 重跑，粒度追平，代价是流程固化成 DAG、node 内部再有步骤就再拆

**本项目执行层是自由 agent loop**（主代理现场决定工具调用、翻译策略、新专名调研、重跑范围），Pi Durable 的持久化单元天然就是对话、粒度不退化；LangGraph 表达自由 loop 时粒度退化为整个 loop 重跑。

## 恢复速度分析

- 通常 Pi Durable 更快：重做量小（in-flight 单元 vs 整个 node），快在翻译阶段这种多模型调用串行的场景，省的是分钟级时间和真金白银
- 限定一：unsafe 工具场景 Pi Durable 反而可能更慢——多一个模型决策回合；LangGraph 确定性重放 + 幂等键直达服务端没有中间回合。Pi Durable 是用时间换「绝不自动重跑」的安全。对本项目影响小：Scribe 是服务端异步任务，两边恢复后都是重新挂到同一个 in-flight 任务等结果，时间大头在服务端
- 限定二：LangGraph 可用「小 node + @task + durability=sync」三件套逼近相同重做量，代价是设计纪律与代码结构

## 选型结论与依据（建议回填 TECH_STACK 选型决策记录）

维持 Pi Durable，依据按本项目需求逐条映射：

- 需求「进程重启不得导致重复付费调用」对应 unsafe 契约，是框架保证而非应用纪律
- 需求「VPS 单机、SQLite 单文件、无常驻进程」对应 Pi Durable 原生形态；LangGraph 生产路径是 Postgres + 可选 server/queue，运维面大一圈
- effect sandwich / memo / parked 与自研账本、审批防打扰、熔断需求一一对应
- 参考项目（赛博飞升）同构流水线已验证，是强工程去风险依据

LangGraph 的真实优势（若未来重开决策，作为参照）：成熟度与主流生态、Python 单语言栈、内建审核闸门、面试叙事更通用。

## 风险与缓解（维持原有决策，但表述更具体）

- 包龄短（六周）、API 可无通知变动 → pin 小版本 + Runner 接口隔离 + 自研降级预案，是必须项不是可选项
- 单进程独占存储 → 多期并行必须在同一 harness 内调度，最小 MVP 实测
- 子代理（翻译/复核）接入方式未验证 → 最大不确定性，见 TECH_STACK 待验证项
- 旧文档 Temporal 论断同样偏粗糙：否定 Temporal 的更好理由是运维重量级（需 server/Postgres）与单机轻量目标不符，而非「LLM 非确定重放无意义」

## 信息来源

- Pi Durable：npm（@earendil-works/pi-durable）、GitHub earendil-works/pi packages/durable（README + spec.md）、earendil.com/posts/pi-durable/
- LangGraph：docs.langchain.com/oss/python/langgraph（checkpointers、graph-api、backward-compatibility、functional-api、fault-tolerance 页）
- 旧文档：tmp/sky-podcast-agent/HANDOFF.md（第 2、4 节）
