# pipeline — 执行层·流水线工具

工具经 bash 被 Runner 调用，本身不依赖 Pi Durable。

| 模块 | 状态 | 职责 |
| --- | --- | --- |
| `download.py` | 完成 | 跟随重定向下载、sha256 固化、时长读取、首版保护（hash 漂移告警） |
| `wordrender.py` | 完成 | Scribe 词级 JSON → 一句一条英文 SRT（`[S01] ` 前缀）+ provenance sidecar |
| `scribe.py` | 待建 | Scribe v2 客户端（直接 HTTP，选型已定） |
| `orchestrate.py` | 待建 | translate-srt 全流程编排 |

`wordrender` 边界规则：句末标点 / 说话人变化 / 停顿超阈值三种边界，均有词级证据；时间戳缺失明确失败；audio_event 不进文本、写入 provenance；cue 的 word_indices 指向原始 token 索引。
