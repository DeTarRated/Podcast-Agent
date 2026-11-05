# 流程测试：Pi Durable 语义验证与 kill -9 崩溃恢复实验

- 日期：2026-11
- 阶段：最小 MVP·流程测试
- 环境：pi-durable 1.5.0（pin）、Node v26.5.0、mock 模型 = pi-ai fauxProvider（固定/脚本化响应）、工具 = runner/src/test-tools.ts 可控慢工具
- 代码位置：`runner/src/semantics.test.ts`（进程内语义）、`runner/src/kill9.test.ts` + `runner/src/crash-child.ts`（崩溃注入）

## 结论先行

框架承诺在本项目用法下全部成立：每步 checkpoint、transcript 跨进程持久化、safe 工具自动重跑、unsafe 工具绝不自动重跑且模型收到 interrupted、restartLimit 熔断、memo 第一次写赢。Runner 需补齐的部分边界已明确（见文末）。

## 实验一：单期 conversation 验证（进程内）

| 验证项 | 方法 | 结果 |
| --- | --- | --- |
| memo 第一次写赢 | 工具内对同名 memo 写两次（first / second） | 第二次写返回 first，不覆盖 ✅ |
| transcript 跨进程持久化 | 进程 A 完成「工具调用 + 收尾」后关闭；进程 B 重开同存储，用 factory 检查模型看到的条目 | 模型看到前一轮 user / assistant / toolResult 条目 ✅ |
| checkpoint 可读 | 重开后 LiveDoc 快照 | rootId 一致、空闲时无 runFailure ✅ |

## 实验二：kill -9 崩溃恢复（四个崩溃点）

| 崩溃点 | 注入方式 | 恢复观察 | 结果 |
| --- | --- | --- | --- |
| 转写调用在途（unsafe 工具替身） | 子进程工具执行中（写标记后睡眠）被 `kill -9` | 重开 + resume：工具执行计数保持 1（不重跑）；模型收到 `isError: true` 的 interrupted toolResult 并续写完成 | ✅ 绝不自动重跑付费调用 |
| 下载在途（safe 工具替身） | 同上，工具声明 `replay: "safe"` | 重开 + resume：工具自动重跑（计数 2）并完成 | ✅ safe 重跑 |
| 翻译中途（模型请求在途） | 子进程慢速流式生成中被 `kill -9` | 重开 + resume：模型请求重发一次并完成，无 runFailure | ✅ 已提交部分不丢、请求重试 |
| 连续崩溃熔断（parked） | `restartLimit: 2`，连续两轮「重跑→被杀」 | 第三轮：工具不再重跑（计数停在 2），以错误结果交给模型，模型收尾 | ✅ restart_limit 熔断生效 |

覆盖范围说明：NEXT_STEPS 列的四个崩溃点中，「转写 POST 在途」「翻译中途」「checkpoint 切换（由模型在途场景部分覆盖）」已验；「子代理完成回写前」依赖子代理接入方式实验，归入翻译与复核阶段（接入方式尚未定）。

## 结论与边界划分

**框架提供（已验）**：每步 checkpoint、transcript 持久化、恢复入口（resume）、safe/unsafe 工具重放契约、effect sandwich 的意图先落盘（unsafe 场景中 interrupted 结果携带已提交输出）、memo 第一次写赢、restartLimit 熔断。

**Runner 仍需补齐（本阶段明确）**：

- 真实付费调用的对账闭环：框架给「interrupted + 已提交输出」，但「查 Scribe 状态 / 重发同幂等键」是模型决策 + 账本 intent→outcome 的配合，需要真实转写阶段接 `state/ledger.py` 验证
- 工具输出与产物清单联动：download 工具落盘文件后，产物清单（`state/manifest.py`）与 Episode 状态推进由主代理按流程写，未纳入工具内部
- 子代理接入（翻译/复核）的崩溃恢复边界待接入方式实验（翻译与复核阶段）
- fork / steer 实际用法未验证（按计划留到有翻译策略调整与驳回场景时验证）

## 复现入口

- 进程内语义：`cd runner && npm test`（semantics 两个用例）
- 崩溃注入：`cd runner && npm test`（kill9 四个用例；每个用例自动 spawn 子进程并 `kill -9`）
- 全部回归：`uv run pytest tests/` + `cd runner && npm run typecheck && npm test` + skill 双 `--self-test`
