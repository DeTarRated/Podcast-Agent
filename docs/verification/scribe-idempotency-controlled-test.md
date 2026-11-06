# Scribe 幂等键受控验证（实测）

- 日期：2026-11
- 阶段：最小 MVP·真实转写准备
- 性质：受控实测（真实付费调用，与 `scribe-idempotency-doc-review.md` 文档查证互为印证）
- 结论一句话：实测证实服务端无任何幂等去重——同音频携带相同自定义 `Idempotency-Key` 头调用两次，产生两个不同 `transcription_id`；凭 `transcription_id` 的 GET 取回路径可用；客户端中断窗口内拿不到任何 ID，服务端行为只能靠 usage 面板人工对账；旧路径 `POST /v1/speech-to-text/convert` 已返回 404

## 实验设置

- 素材：`/tmp/scribe_verify/sample.wav`，38.40 秒，macOS `say` 合成语音（可再生；合成命令见脚本所在目录备注）
- 脚本：`/tmp/scribe_verify/verify.py`，文件头注释列出场景清单
- 凭据：`.env` 中 `ELEVENLABS_API_KEY`（不入库、不写日志；脚本不打印 key）
- 端点：`https://api.elevenlabs.io`，`model_id=scribe_v2`

## 调用明细（实测结果）

| 场景 | 路径 | 状态 | transcription_id | 计费时长（秒） | 耗时（秒） | 观察 |
| --- | --- | --- | --- | --- | --- | --- |
| baseline | `/v1/speech-to-text` | 200 | `dg72kHPRogG22tYU3yto` | 38.40 | 3.44 | 正常返回词级 JSON |
| keyed_a | `/v1/speech-to-text` | 200 | `8KzRPsFDM2KQakbTMbNp` | 38.40 | 3.88 | 携带 `Idempotency-Key` 头被接受，无报错 |
| keyed_b | `/v1/speech-to-text` | 200 | `PyLKcrtXySCr3vmZG4mV` | 38.40 | 3.44 | 同键二次调用 → 又一个新的 transcription_id |
| abort_probe | `/v1/speech-to-text` | 无（读超时中止） | 无 | 未知 | 1.80 | 客户端 0.5s 读超时中止；服务端侧是否计费待 usage 面板核对 |
| legacy_path | `/v1/speech-to-text/convert` | 404 | — | 0 | 1.53 | 旧路径已失效 |
| get_by_id | `GET /v1/speech-to-text/transcripts/{id}` | 200 | — | — | — | 凭 baseline 的 ID 成功取回全文与 `audio_duration_secs` |

## 关键观察

- 幂等键不成立：同一音频、同一自定义 `Idempotency-Key` 头（`verify-<sha256 前 24 位>`）重发两次，服务端均返回 200 且各发新 ID。服务端既不报未知头错误，也不按该头去重——该头在 Scribe 上无任何语义。
- 重发即新任务：三次成功调用产出三份独立转写（文本一致但 ID 各异），符合文档「每次请求独立计费」口径。
- 中断窗口：读超时中止后客户端没有可查询句柄（无 ID 落盘）。此窗口内服务端不提供自动对账手段，usage 面板是唯一人工对账渠道。
- 恢复路径：只要客户端曾拿到 `transcription_id`，`GET /transcripts/{id}` 即可取回完整结果（含词级时间戳），恢复路径成立。
- 路径事实：`pipeline/scribe.py` 必须使用 `POST /v1/speech-to-text`；旧文档路径已 404。

## 计费口径（已按面板导出核对）

- 用户导出 ElevenLabs 成本日账单（2026-10-30 ~ 2026-11-06）：仅 2026-11-06 有成本 `$0.0170681`，其余日期为 0。
- 反推：`0.0170681 ÷ (4 × 38.40325 ÷ 3600) = $0.4000/小时`——与「4 次调用全部计费 + 有效单价 $0.40/小时」精确吻合（浮点精度内零残差）；若按文档单价 $0.22/小时反推则等价 279.3 秒，无整数次对应。
- 结论 a：中止探针（客户端读超时）在服务端照常完成并计费——「发出后中断、结果未知」窗口内服务端照计费，被实测证实。
- 结论 b：本账号 Scribe 实际计费单价 ≈ $0.40/小时，与官方定价页口径 $0.22/小时不一致（约 1.8 倍）。归属与单价待进一步确认：Request Log 逐请求导出（按 API key 过滤）+ 账号套餐费率页。

## 计费口径结案（2026-11-06 充值后重测）

- 重测方法：账号充值 PAYG 后，用同一 `sample.wav`（38.40325 秒）再转写一次（transcription_id `mXjoXbn8OT5j3PGbhEZ7`），经 `POST /v1/workspace/analytics/query/usage-by-product-over-time` 读取实际扣费（该接口约 20 秒内聚合出新调用，含 credits 与美元成本两列）。
- 结果：6 credits / $0.0023469，反推 $0.2200/小时——与官方定价零残差吻合。
- 对照：免费计划期同一音频为 43 credits / $0.0042670（$0.40/小时）。
- 定论：$0.40/小时是免费计划的 credit 折算口径，非计费错误；PAYG 余额期扣费口径与官方 $0.22/小时一致。真实转写阶段成本基线按 $0.22/小时计。
- 附带事实：usage 分析接口可被本 key 调用（subscription 接口缺 `user_read` 权限不可读），轮询对账方案可行。
- 对项目影响：真实转写阶段费用基线不能直接引用 $0.22/小时；`pipeline/scribe.py` 落地前需按本账号实际费率重估成本基线。

## 对项目的影响（与文档查证合并后的定稿口径）

- 「转写调用携带幂等键、重试不产生二次计费」服务端层面与实测层面均不成立；建议更新 `docs/REQUIREMENTS.md` 对应条目（需求修订提案，待用户确认）。
- 本地 guid + 音频 hash 键保留，仅防本地重复触发，不作为服务端计费依据。
- 付费调用自动重试保持关闭（执行纪律红线确认）。
- 恢复模式定稿：POST → 拿到 `transcription_id` 即落盘 → 中断后凭 ID GET 取回；「POST 发出到 ID 落盘」窗口内绝不自动重发，只允许人工 usage 对账。
- `pipeline/scribe.py` 落盘策略：`transcription_id` + `audio_duration_secs` + 原始响应，供账本 intent→outcome 对账。

## 边界与未尽项

- abort_probe 未证实服务端必然计费（需 usage 面板读数），也未验证「发一半就断网」的网络层变体。
- 异步 webhook 路径（`webhook=true` 的 202 + `request_id`）未实测：无回调地址配置；文档语义见文档查证记录，接入真实转写阶段时可顺带验证。
- 未探测其他非文档头/参数；供应商可能随时变更行为，`pipeline/scribe.py` 落地前重跑一次本查证。
