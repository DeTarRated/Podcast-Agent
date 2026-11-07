# pipeline — 执行层·流水线工具

工具经 bash 被 Runner 调用，本身不依赖 Pi Durable。

| 模块 | 状态 | 职责 |
| --- | --- | --- |
| `download.py` | 完成 | 跟随重定向下载、sha256 固化、时长读取、首版保护（hash 漂移告警） |
| `wordrender.py` | 完成 | Scribe 词级 JSON → 一句一条英文 SRT（`[S01] ` 前缀）+ provenance sidecar |
| `scribe.py` | 完成 | Scribe v2 客户端（直接 HTTP）：本地幂等键 call_ref + transcription_id 锚定恢复；付费 POST 绝不自动重试，超时/断连记 unknown 转人工对账 |
| `orchestrate.py` | 完成 | translate-srt 确定性步骤编排（prepare / fix / fix --manual-done / apply / finalize），主代理驱动；不直接调模型，翻译/复核/调研经 Runner dispatch |

`wordrender` 边界规则：句末标点 / 说话人变化 / 停顿超阈值三种边界，均有词级证据；时间戳缺失明确失败；audio_event 不进文本、写入 provenance；cue 的 word_indices 指向原始 token 索引。

## orchestrate 子命令与主代理交接

orchestrate 采用「确定性步骤封装 + 主代理驱动」：只封装确定性步骤为 CLI，主代理（Runner conversation）在步骤之间执行判断性工作（通读、转录修正、子代理派发、复核补查）。

| 子命令 | 确定性动作 | 主代理随后做 |
| --- | --- | --- |
| `prepare` | 建工作区骨架、stats 体检、kb match、生成 glossary 骨架 | 通读 en.srt、填 brief/gaps/glossary、定 speakers 真名、决定是否调研 |
| `fix` | merge（若需）、复制 en_fix、speakers --map/--drop、kb replace | 亲自定点修正 en_fix.srt（phase=transcript_manual_review 保护人工修改） |
| `fix --manual-done` | split/normalize、remap-log、最终体检 | 经 dispatch 派发翻译（role=translate） |
| `apply` | 账本 success 闸门、译文贴回时间轴、check、推进 translated | 经 dispatch 派发复核（role=review，同档强模型） |
| `finalize` | 复核账本闸门、clean/check/resplit/provenance、推进 reviewing | 抽查候选、报告插值、等人工审核（不上传、不选交付版本） |

产物路径约定：单期工作区 `var/episodes/{id}/`，`_context/` 存 brief/gaps/glossary/alias_log/review_notes 与 `orchestrate_state.json`（phase 与幂等控制），`_context/logs/` 存各命令完整输出。时间轴只由 `apply` 从 en_fix.srt 复制，不经模型。

账本：orchestrate 不写翻译/复核/调研账本（由 Runner dispatch 在 createTask 前后经 `state/ledger_cli.py` 窄 CLI 写 intent/outcome）；apply/finalize 只读取账本 success 作闸门。call_ref 形如 `{role}:{episode_id}:v{version}`。
