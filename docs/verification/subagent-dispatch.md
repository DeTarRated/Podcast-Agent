# 子代理接入方式实验（嵌套任务形态）

- 日期：2026-11
- 阶段：最小 MVP·翻译与复核（前置实验）
- 性质：进程内实验（模拟子代理执行体，faux 模型驱动主 conversation；零真实付费调用）
- 结论一句话：**嵌套任务形态成立**——主 conversation 经工具 `dispatch` 创建子任务并等待结果，产物落盘、usage 回传、父子关联、跨调用去重均已验证；崩溃恢复语义为「子任务失败即终态，不自动重跑」，与防重复计费红线一致

## 实验设置

- 形态选择：嵌套任务（`api.createTask` + `api.waitForTask`），由主 conversation 的工具经 `extras.dispatch` 派发。另两种候选（扩展任务、独立 conversation）未展开：嵌套任务已满足全部验收项，且与现有 ToolSpec 契约兼容，无需另起 conversation 管理
- 代码：`runner/src/experiment-subagent.ts`（子任务定义，模拟执行体 + 可脚本化行为表）、`runner/src/experiment-subagent.test.ts`（4 个场景）、`runner/src/durable-adapter.ts`（dispatch extras 注入）
- 模型：faux mock 模型驱动主 conversation 调用派发工具；子任务执行体为模拟（写文件 + 回传模拟 usage），不真实调 DeepSeek

## 验证结果（4 场景全过）

| 场景 | 结果 | 证据 |
| --- | --- | --- |
| 派发翻译子任务 | ✅ 产物 `translate.srt` 落盘；usage（100+50 tokens）随结果回传主 conversation；taskId 父子关联 | `experiment-subagent.test.ts` |
| 同 callRef 重复派发 | ✅ 第二次命中去重（reused=true），复用同一 taskId，不新建任务 | 同上 |
| 复核角色子任务 | ✅ 同路径跑通，自定义 usage（800+200）正确回传 | 同上 |
| 子任务 init 崩溃后重开存储 resume | ✅ 框架语义确认：init 抛错 → 子任务记为失败终态，resume 不自动重跑、产物未写 | 同上（attempts=1，产物=false） |

## 关键机制事实（实现时踩过的坑，供后续接线参考）

- **memo 的作用域是工具任务**：`api.memo` 存在当前工具调用的任务记录上，每次工具调用都是新任务，memo **不跨工具调用**。跨调用去重不能用 memo，要用 `tx.scanTasks({conversationId, kind})` 扫描既有任务的 `input.callRef` 匹配复用
- **memo 读写签名**：读 = `memo(name, ctx)`；写 = `memo(name, candidate, ctx)`，写是「第一次写赢」返回 durable winner
- **`waitForTask` 返回结构**：`settled.state.outcome`（`{status:"completed", result}`），不是顶层 `outcome`
- **waitForTask 的竞态**：在 resume 场景可能返回 `state.status="running"` 的快照（子任务尚未 terminal）。dispatch 内需防御性轮询至 terminal（实验中以 50ms 间隔重等，上限 100 次）
- **子任务注册**：经 `registry.install(defineExtension({tasks:[...]}))` 注册任务定义；`api.createTask` 的 `ownership` 必填（`{kind:"conversation"}`）
- **子任务失败语义**：phase handler 抛错 → 任务记为失败终态，不自动重试。这对付费子代理是**正确语义**（不自动重跑 = 不重复计费），恢复决策交给主代理/人工

## 对四件验收事的回答

| 验收项 | 结论 |
| --- | --- |
| 崩溃恢复 | 子任务失败即终态不自动重跑（符合付费红线）；父 conversation 的 transcript 持久化已由流程测试覆盖，恢复后由主代理决定是否重新派发 |
| 成本归属 | 子任务 result 携带 usage 回传主 conversation；账本记录由主 conversation 侧（或 orchestrate）在 dispatch 返回后写 `state/ledger.py`，call_ref 与子任务 input.callRef 一致 |
| 产物落盘 | 子任务写文件用 create-only（`wx` 旗标），已存在则校验不覆盖；产物路径由 input 指定，与 manifest 登记在编排层完成 |
| 父子完成关联与去重 | taskId 由 createTask 返回并随 dispatch 结果回传；同 callRef 扫描去重已验证 |

## 对 ARCHITECTURE 待验证项的回填

「子代理（翻译/复核）在 Pi Durable 中的接入方式（扩展任务、嵌套任务或独立 conversation）」→ **定为嵌套任务**：经 ToolSpec 的 `extras.dispatch` 创建子任务并等待，去重靠 scanTasks + callRef，成本随 result 回传进账本。实验代码即接入骨架。

## 未覆盖 / 留待真实接入

- 真实 DeepSeek 调用（本实验执行体为模拟）；真实 usage 字段映射（pi-ai usage → 账本 amount_est）
- 子任务执行体的耗时与超时策略（真实翻译 37 分钟 SRT 的 token 量与限流）
- 复核修改异常少时主代理补查的编排逻辑（属 orchestrate.py）
