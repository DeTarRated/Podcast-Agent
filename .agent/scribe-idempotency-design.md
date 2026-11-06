# 转写调用的防重复计费与崩溃恢复设计

- 日期：2026-11
- 状态：定稿 v1.1（2026-11-06 回填计费口径结案事实；未提交，随转写模块整体完成后一并提交）
- 定位：给出 Scribe 转写调用在「无服务端幂等键」前提下的防重复计费与崩溃恢复设计，供实现 `pipeline/scribe.py` 与修订 `docs/REQUIREMENTS.md` 使用
- 结论来源：`docs/verification/scribe-idempotency-doc-review.md`（文档查证）与 `docs/verification/scribe-idempotency-controlled-test.md`（受控实测）

## 问题定义

- 转写是真实付费调用，按发送音频时长秒级计费、含静音，每次请求独立计费。
- 需求原文要求「转写调用携带幂等键（guid + 音频 hash）、重试不产生二次计费」，但服务端无任何幂等键机制，「同键重发不重复计费」不成立。
- 崩溃/超时/断网后，客户端处于「调用可能已发出并计费、但没拿到结果句柄」的未知窗口；此时若盲目重发，即产生二次计费。
- 设计目标：把「结果未知时的恢复」从「重发同键」降级为「凭服务端 ID 查询 + 对账」，自动路径零重发，只在人工确认后才可能重复计费。

## 已证实事实清单（直接采信，不重新查证）

- 服务端无幂等键机制：官方文档全文检索「idempoten」零命中；`POST /v1/speech-to-text` 的全部请求参数与 header 中无幂等键字段。
- 实测同音频携带相同自定义 `Idempotency-Key` 头重发两次，返回两个不同的 `transcription_id`；该头被接受但无去重语义，每次独立计费。
- 实测中断探针：客户端读超时放弃后，服务端照常完成转写并计费；账单反推 4 次调用全部计费。
- 计费单价（已结案）：免费计划期实测 ≈ `$0.40/h` 系免费 credit 折算口径（43 credits/次），非计费错误；充值 PAYG 后同一 38.4 秒音频重测为 6 credits / `$0.0023469`，反推 `$0.2200/h`，与官方定价零残差吻合（2026-11-06，详见 `docs/verification/scribe-idempotency-controlled-test.md` 结案节）。成本基线按 `$0.22/h` 计。
- 恢复路径：凭 `transcription_id` 调 `GET /v1/speech-to-text/transcripts/{id}` 可取回完整结果（实测 200，含词级时间戳与 `audio_duration_secs`）。
- 端点：`POST /v1/speech-to-text`；旧路径 `/v1/speech-to-text/convert` 已 404。
- 异步路径：`webhook=true` 返回 202 + `request_id` / `transcription_id`；webhook 投递携带 `ElevenLabs-Webhook-Delivery-Id` 供客户端去重，失败重投至多 5 次；webhook 鉴权支持 HMAC 或 OAuth，验签头为 `elevenlabs-signature`（SDK `constructEvent` 可验，服务端不强制）。
- 账本现状：`state/ledger.py` 为 intent→outcome 两段式，`call_ref` 唯一约束，`unknown` 态供对账且不自动重跑。
- 工具现状：`runner/src/conversation.ts` 中 transcribe 为 unsafe 占位工具，崩溃后绝不自动重跑。

## 恢复窗口定义（客户端视角）

| 窗口 | 含义 | 客户端手里有什么 |
| --- | --- | --- |
| 本地重复触发窗口 | 同一期被重复入队、同一 conversation 重放、进程内重复调用 | 有 guid + 音频 hash，尚未发出付费调用 |
| POST 在途窗口 | POST 已发出、响应尚未被收到并落盘（含服务端处理 + 网络往返） | 只有 intent 记录，无 `transcription_id` |
| 已拿 ID 之后 | 已持有 `transcription_id`，但结果尚未消费/落盘或后续崩溃 | 有 `transcription_id`，可凭 ID 查询 |

## 方案对比

| 方案 | 覆盖的窗口 | 面试八股映射 | 代价与局限 |
| --- | --- | --- | --- |
| 本地幂等键 + 去重账本 | 本地重复触发窗口 | 幂等键、去重表/唯一约束 | 只防本地重复触发；不解决「响应丢失后重发」导致的二次计费，也不解决结果恢复 |
| transcription_id 锚定 + GET 恢复 | 已拿 ID 之后 | at-least-once、补偿（用查询替代重发） | 需落盘 ID 与结果；POST 在途窗口仍需对账，不覆盖 |
| 异步 webhook + 投递去重 | 把未知窗口压缩到 POST 在途瞬间；已拿 ID 之后用推送替代轮询 | 回调幂等消费、去重表、at-least-once | 需公网回调地址、签名校验、delivery id 去重、webhook 失败回退 GET；不消除 POST 在途窗口 |
| 账单对账 + 风险量化 | POST 在途窗口的兜底 | 对账/冲正/补偿、unknown 态显式建模 | 人工介入、有滞后（日账单/Request Log），无法实时自动化 |
| 换供应商 | — | — | 选型已定 ElevenLabs，排除 |

## 推荐组合与理由

- 主链：本地幂等键（guid + 音频 hash）+ transcription_id 锚定（同步 POST、拿到响应即落盘 ID、中断后凭 ID GET 取回）。
- 兜底：unknown 态 + 账单/Request Log 对账，覆盖「ID 未拿死角」。
- 预留演进：webhook 事件驱动，作为独立演进路径写入文档，不承诺 MVP 做。
- 自动重试：始终保持关闭（执行纪律红线）。

取舍论证：

- 本地幂等键是必要非充分。唯一约束挡住本地重复触发（重复入队、重放、进程内重复），但网络层 at-least-once 的「响应丢失」窗口靠本地键挡不住：服务端没有去重，重发就是新任务、新计费。
- transcription_id 锚定把恢复从「重发」变成「查询」。这是服务端唯一可靠、实测可用的恢复手段，且查询不产生新计费。
- unknown 态 + 对账承认服务端无幂等键的事实。ID 未拿窗口内绝不自动重发，宁可进入可对账状态，也不赌「大概率没扣费」。
- webhook 有价值但非 MVP 必需。它压缩窗口、把轮询换成推送，但引入公网回调与签名校验等前置条件；留作演进路径更稳。

## 恢复流程分场景

### 正常路径

- 转写前先 `record_intent(call_ref = guid + 音频 hash)`；`call_ref` 唯一约束挡住本地重复触发。
- 同步 `POST /v1/speech-to-text`（`webhook=false`），携带音频与说话人分离、词级时间戳参数（`diarize` / `timestamps_granularity=word`）。
- 不携带 `Idempotency-Key` 头：实测该头无语义，携带会让实现者误以为有服务端去重；本地幂等键只存在于账本 `call_ref`，不出 HTTP 头。
- 拿到 200：立即落盘 `transcription_id` 与完整原始响应（scribe_raw 产物），再 `record_outcome(status=success)`，推进 Episode 到 transcribed。
- 后续词级重组只读已落盘的 scribe_raw，不再触碰付费接口。

### 已拿 ID 中断

- 崩溃点：POST 已返回、`transcription_id` 已落盘，但 outcome 未落盘或产物未写全。
- 恢复：凭 `transcription_id` 调 GET 取回结果，不重发 POST、不二次计费。
- 把取回结果落盘，补 outcome=success。
- 关键约束：ID 落盘必须先于结果消费，恢复就退化为幂等查询。

### ID 未拿死角

- 崩溃点：POST 已发出、响应未返回，客户端崩溃/超时/断网，本地只有 intent 行、无 `transcription_id`。
- 服务端可能已计费并完成（实测证实），也可能 POST 未到达；客户端无法区分，这是 at-least-once 死角。
- 处理：绝不自动重发；把 intent 推进为 `unknown`，进入人工对账。
- 对账：用 usage/Request Log 导出定位该时间段、该音频时长的转写任务；若能反查 `transcription_id` 则 GET 取回，否则人工决定「放弃本次结果」或「人工确认后重转写（接受二次计费）」。
- 语义边界：自动路径零重发，只有人工明确重发才可能二次计费。

## 风险量化

- 需求成本目标写「典型 $0.30：转写 $0.18 + 翻译 $0.12」；$0.18 ÷ 官方价 $0.22/h 反推对应约 49 分钟音频（需求未直接写明时长）。PAYG 实测单价 $0.22/h（2026-11-06 充值后重测确认），49 分钟单次转写 ≈ $0.18，与需求基线吻合，无需重估。
- 重复计费只在「ID 未拿死角 + 人工决定重转写」时发生；自动重试关闭 + unknown 不自动重跑 + 唯一约束挡本地重复，自动路径重复计费概率为零。
- 期望损失 = 死角概率 × 对账失败需重转写的概率 × 单次转写成本。

| 模式 | 未知窗口量级 | 单次转写成本（49 分钟，实测价） | 重复计费期望损失（参考框架） |
| --- | --- | --- | --- |
| 同步（MVP 主链） | 分钟级（POST 在途 + 服务端处理） | ≈ `$0.18` | ≈ `$0.18 × P × Q` |
| webhook（预留进阶） | 秒级（仅 POST 在途） | ≈ `$0.18` | ≈ `$0.18 × P' × Q`（P' ≪ P） |

- P/P' 为「本期转写落在 ID 未拿窗口且服务端已计费」的概率，需长程故障注入与 usage 数据校准，不臆断。
- Q 为「对账失败、不得不重转写」的概率，取决于 Request Log 能否反查 `transcription_id`（待验证）。
- 结论：自动路径二次计费为零；残差风险集中在同步模式较长的 POST 在途窗口与人工对账能力，属可接受的极小概率重复计费，用对账兜底而非重试兜底。

## 可选进阶：webhook 事件驱动（预留演进路径）

- 定位：不承诺 MVP 做，仅作为预留演进路径，触发条件满足后再启用。

### 触发条件

- 单期时长/耗时上升到同步 POST 的分钟级窗口不可接受。
- 出现多期并行且轮询 GET 造成限流或空转成本。
- 需要把「结果到达」从轮询改为事件驱动，缩短首稿产出时间。

### 收益

- 202 受理即返回 `request_id` / `transcription_id`，把「拿到 ID」提前到受理时刻，未知窗口压缩到 POST 在途瞬间。
- 结果由服务端推送，免去轮询；失败重投至多 5 次。
- 客户端按 `ElevenLabs-Webhook-Delivery-Id` 去重，回调幂等消费成立。

### 代价

- 新增公网回调地址与鉴权/签名校验链路。
- 新增 delivery id 去重表与事件入队/出队机制。
- webhook 失败或超时后仍需回退到 `transcription_id` + GET，不能只依赖推送。

### 前置条件

- 回调地址公网可达；验签实现待落（机制已确认：webhook 鉴权 HMAC/OAuth，验签头 `elevenlabs-signature`，SDK `constructEvent` 可验；服务端不强制，客户端必须自行校验）。
- delivery id 去重表与幂等消费的状态迁移已实现。
- 与同步主链共用同一「intent 先落、outcome 后更新、unknown 对账」账本语义。

## 对 REQUIREMENTS.md 的修订建议措辞

原文：

> 幂等：转写调用携带幂等键（由 episode guid + 音频 hash 构造）；重试不产生二次计费

建议改为：

> 防重复计费：转写调用以本地幂等键（episode guid + 音频 hash）为账本 `call_ref`，账本唯一约束防止本地重复触发；服务端不提供幂等键（已查证 + 实测），故不以本地键承诺服务端不重复计费。恢复采用 transcription_id 锚定 + GET 查询：拿到响应即落盘 transcription_id，中断后凭 ID 取回结果、不重发；ID 未拿到的未知窗口不自动重发，转入 unknown 态并走账单/Request Log 对账。付费调用自动重试始终保持关闭。

配套建议（另起，非本条目）：

- 「成本与计费透明」中的单期成本基线维持需求原文，无需重估：PAYG 实测单价 `$0.22/h`（2026-11-06 充值后重测确认），49 分钟转写 ≈ `$0.18`。免费计划期的 `$0.40/h` 系免费 credit 折算口径，不适用于 PAYG 余额期。

## 面试叙事小结

- 为什么本地幂等键不够：本地键只能挡「本地重复触发」，挡不住网络层 at-least-once 的「响应丢失后重发」；服务端无幂等键，重发即新任务、新计费。本地去重是必要不充分。
- 为什么拒绝自动重试：超时重试本质是 at-least-once，在服务端无幂等键时等于二次计费；安全姿态是「未知窗口内绝不自动重发」，改用 transcription_id 查询与对账。
- 两段式账本与 outbox 的关系：intent 先落、outcome 后更新是 effect sandwich 的持久化版本，等价于 outbox 模式的「先写待投递意图、执行副作用、再标记完成」；unknown 态就是「未确认投递结果的 outbox 条目」，触发对账而不是重放。
- at-least-once 对账：外部调用天然是 at-least-once（可能已送达并计费但客户端未见响应）；在无服务端幂等键时，用对账/冲正/补偿替代重试，把「不确定是否重复」收敛到「可查证」。
- 回调幂等消费：webhook 重投至多 5 次，客户端按 delivery id 去重 + 幂等状态迁移，保证多次投递只消费一次。
- unknown 态显式建模：不把未知折叠成 failed（会诱发重试）或 success（会丢结果）；unknown 是一等状态，专责对账、永不自动重跑。
- exactly-once 幻觉：声称「重试不重复计费」而服务端无幂等键，正是 exactly-once 幻觉；无服务端幂等键时只能做到「effectively-once 靠查询恢复 + 残差靠人工对账补偿」。

## 开放项（定稿后保留，供实现阶段处理）

| 开放项 | 影响 | 推荐方向 |
| --- | --- | --- |
| 账本字段 | `transcription_id` 锚定需要落盘服务端 ID；现 schema 仅有 `call_ref` | 真实转写阶段给 ledger 加 `server_ref`（transcription_id / request_id）与 `reconciled_at` 列（schema 归执行层维护），原始响应仍落每期工作区 |
| 同步 POST 客户端超时上限 | 决定 ID 未拿窗口大小，影响风险量化的 P | 读超时与音频时长挂钩（如 `max(120s, 时长×3)`），超时即按 unknown 处理，不重发 |
| Request Log / usage 导出能否反查 transcription_id | 决定死角对账是半自动还是纯人工 | 真实转写阶段做一次受控验证补查；已确认 `usage-by-product-over-time` 接口本 key 可读、约 20 秒聚合、含 credits 与美元成本（2026-11-06），`requests` 接口可访问但能否反查 transcription_id 未验证 |
| 单价差异（已解决，2026-11-06） | 实测 $0.40/h 系免费计划 credit 折算口径；PAYG 重测 $0.22/h 零残差吻合 | 成本基线按 $0.22/h 计，结案节见 `docs/verification/scribe-idempotency-controlled-test.md` |
| webhook 验签实现 | 预留进阶启用时需先落地 | 用 SDK `constructEvent` + webhook secret，启用前先自测重投去重 |
