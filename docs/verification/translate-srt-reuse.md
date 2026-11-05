# translate-srt 复用边界自检

- 日期：2026-11
- 阶段：最小 MVP·本地代码
- 结论：两个脚本自检通过，可零依赖复用；复用方式为「经 bash 调用脚本子命令」，不 import 为库

## 自检结果

| 脚本 | 命令 | 结果 | 运行环境 |
| --- | --- | --- | --- |
| `skills/translate-srt/scripts/srt_tools.py` | `--self-test` | OK | 项目 venv（Python 3.12，uv run） |
| `skills/translate-srt/scripts/kb_tools.py` | `--self-test` | OK | 项目 venv（Python 3.12，uv run） |

依赖面检查：两脚本仅用 stdlib（srt_tools：argparse/re/shutil/sys/tempfile/unicodedata；kb_tools 另含 csv/datetime/difflib/json/os/pathlib + 兄弟脚本 srt_tools import），无第三方依赖，Python 3.11+ 均可运行。

## 调用约定

- 形态：`python <skill>/scripts/<脚本>.py <子命令> [参数]`，经 subprocess/bash 调用（执行层工具经 bash 调 Python 的既定设计）
- 不 import 为库：脚本以 CLI 为中心，路径解析基于 cwd 与参数，直接 import 会引入隐式耦合
- 产出文件与退出码即接口；`stats` 只打印不写文件
- 回归习惯：每次项目侧改动后跑两个 `--self-test`，确认 skill 未被误改

## 子命令面（复用时用到哪些）

| 脚本 | 本阶段会用 | 后续阶段会用 | 说明 |
| --- | --- | --- | --- |
| srt_tools | `stats`（输入体检）、`init`（工作区）、`merge`、`split`（整平，仅 stats 判定需要时）、`apply`（译文贴回）、`check`（验收对比）、`provenance`（插值统计） | `normalize`、`clean`、`resplit`、`speakers` | 全流程编排见 SKILL.md 第 0–6 步 |
| kb_tools | `init`（最小英超底座）、`status`、`check`、`match`、`glossary`、`replace` | `apply`、`diff`、`remap-log`、`backfill` | 沉淀流程在交付后阶段 |

## 复用边界

- `srt_tools` 管 SRT 文本与时间轴：体检、工作区初始化、整平、译文贴回、检查、重切分、provenance。**不覆盖**：词级 JSON → SRT 重组（本项目 `pipeline/wordrender.py`，自研新组件）
- `kb_tools` 管领域知识库：home 解析、校验、命中、别名替换、术语表、沉淀提案应用。**不覆盖**：流程状态、override、账本、评测集（本项目 `state/` 自研）
- 知识库位置约定：`$TRANSLATE_SRT_HOME` → 缺省 `~/.translate-srt/`（`config.json` + `knowledge/`），知识库在 skill 目录之外且独立 git——本项目沿用，不放入本仓库

## 注意点

- `speakers --map S01=名字` / `--drop`：占位前缀 `[S01] ` 由 wordrender 输出，真名在修正阶段（skill 3b）落实
- `-l <lang>`：原文 en、译文 zh 各传各的；标点风格由 `clean` 按语言切换
- 脚本对 SRT 文件的读取基于调用时 cwd 或显式路径，编排层必须传绝对路径，避免子进程 cwd 漂移
