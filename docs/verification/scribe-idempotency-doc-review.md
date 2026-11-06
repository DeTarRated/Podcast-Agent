# Scribe 幂等键服务端语义文档查证

- 日期：2026-11
- 阶段：最小 MVP·真实转写准备
- 性质：文档查证（供应商声称语义层面）；结论经用户审阅后方可作为依据，不替代受控验证
- 结论一句话：官方文档不支持任何幂等键机制，「同键重试不重复计费」在文档层面不成立；安全恢复只能依赖服务端生成的 `transcription_id` + GET 查询 + usage 面板对账

## 查证项与结论

| 查证项 | 结论 | 证据 |
| --- | --- | --- |
| 请求键是否支持 | 不支持。Create transcript（`POST /v1/speech-to-text`）的全部请求参数（query / multipart body）与 header 中均无幂等键字段，唯一认证 header 为 `xi-api-key` | Create transcript 页参数清单；llms-full.txt 全文检索「idempoten」零命中 |
| 参数名 | 不适用（机制不存在） | 同上 |
| 作用域 | 不适用（机制不存在） | 同上 |
| 有效期 | 不适用（机制不存在） | 同上 |
| 超时后查询 / 对账手段 | 有，但不是幂等键续查：同步 200 或异步 202 响应携带 `transcription_id`（异步另带 `request_id`），凭 `GET /v1/speech-to-text/transcripts/{transcription_id}` 取回结果；webhook 投递失败后官方建议按此路径取回。`enable_logging=false`（zero retention）请求不存储转写、不可查询 | Get transcript 页；异步 STT webhook 指南「If a delivery still fails, fetch the result with Get transcript using the transcription_id」 |
| 计费语义 | 按发送音频时长计费（Scribe v2 当前 $0.22/小时），秒级计量、含静音，无任何去重或免重复计费声明；每次请求是独立计费调用。`seed` 参数仅「尽力确定性采样」且明确不保证确定性，不是去重/计费机制 | Transcription 能力页 FAQ「metered on every second of audio you send, including pauses and silent stretches」；API 定价页；Create transcript 页 `seed` 参数说明 |
| 其他计费注意 | 2026-10-26 changelog「Minimum charge」：TTS 与 STT 请求不足 1 credit 按 1 credit 计（Creative 套餐口径） | changelog 2026/10/26 |

## 证据位置（2026-11 快照）

- Create transcript 参数全集：`https://elevenlabs.io/docs/api-reference/speech-to-text/convert`（端点 `POST https://api.elevenlabs.io/v1/speech-to-text`）
- Get transcript：`https://elevenlabs.io/docs/api-reference/speech-to-text/get`（`GET /v1/speech-to-text/transcripts/{transcription_id}`）
- 异步与 webhook 语义：`https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/batch/webhooks`（webhook 失败后凭 `transcription_id` 取回；同一事件重投带相同 `ElevenLabs-Webhook-Delivery-Id` 供客户端去重）
- 计费 FAQ 与模型能力：`https://elevenlabs.io/docs/overview/capabilities/speech-to-text`
- 定价：`https://elevenlabs.io/pricing/api?price.section=speech_to_text`（Scribe v2 $0.22/小时，2026-11 当前；30 秒 ≈ $0.002）
- 全量文档检索：`https://elevenlabs.io/docs/llms-full.txt`（「idempoten」零命中）
- 近期 changelog（2026/10/19、10/26、11/2）STT 相关条目无幂等键公告

## 端点路径事实更新

- 现行文档端点为 `POST /v1/speech-to-text`（文档页 URL 仍为 `/docs/api-reference/speech-to-text/convert`），查询为 `GET /v1/speech-to-text/transcripts/{transcription_id}`。
- `docs/TECH_STACK.md` 选型决策记录中写的「POST /speech-to-text/convert + GET /transcripts/{id}」已不是当前文档口径，已随本查证回填修正。
- 受控验证时应以现行文档路径为准（如顺带探测旧路径兼容性则作为附加记录）。

## 对项目的影响（待用户审阅后生效）

- `docs/REQUIREMENTS.md`「转写调用携带幂等键、重试不产生二次计费」在服务端层面不成立。本地 guid + 音频 hash 键仍有价值：防止本地重复触发同一期转写；但不能作为「服务端不重复计费」的证明。
- 付费调用自动重试保持关闭（执行纪律红线）。恢复语义降级为「拿回 `transcription_id` → GET 查询」模式。
- 结果未知窗口 = POST 发出到 `transcription_id` 落盘之间。该窗口内服务端不提供自动对账手段，只有 usage 面板人工核对；此窗口内绝不自动重发。
- `pipeline/scribe.py` 落盘策略建议：异步（`webhook=true`）模式拿到 202 响应即落盘 `transcription_id` 与 `request_id`；同步 200 落盘完整响应与 `transcription_id`。

## 受控验证设计（如做，待用户确认后执行）

- 目标：实测证实「每次 Create transcript 独立计费」与「异步模式下凭 ID 取回」两条，并观察在途中断窗口行为。
- 素材：30 秒级短音频即可（成本约 $0.002/次，可忽略；注意 Creative 套餐 1 credit 最低计费单位）。
- 步骤草案：
  - 基线：调用一次，记录 usage 面板读数与响应 `transcription_id`
  - 重发同音频（无任何去重键可用）：观察 usage 是否按音频时长增加；增加即证实每次请求独立计费
  - 异步路径：`webhook=true` 拿 `transcription_id` 后不等待 webhook，直接 `GET /transcripts/{id}` 验证可凭 ID 取回
  - 在途窗口：以极短客户端超时构造「请求已发出、响应未收到」，观察 usage 是否已入账、事后能否对账
  - 每次调用记录 intent/outcome 与 usage 读数，写入查证记录
- 通过判据沿用交接文档：能证明结果未知时可安全恢复（凭 ID 查询、不重复计费）则付费调用自动重试语义成立；否则关闭自动重试并记录阻塞。

## 边界

- 本文档结论是供应商当前声称的语义（2026-11 快照），不能替代受控验证；文档未记载的服务端内部行为（如隐式去重）无从排除。
- 供应商文档可能随时变更；`pipeline/scribe.py` 落地前重跑一次本查证。
