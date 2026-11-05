# 子代理角色

SKILL.md 里写「派发 X 角色」时,指的是下面的定义。各 harness 自己决定怎么派:Claude Code 用 Agent 工具 spawn 子代理,Pi 用它的子代理机制;**没有子代理能力的环境,由主代理按同样的输入输出顺序自己跑一遍**,流程不变。

通用约束(每个角色都适用):

- prompt 里的路径一律正斜杠、绝对路径(见 `pitfalls.md` 第一条);派发后 `ls` 确认产物真的落在目标位置。
- 角色**先 Read 指定的文件再动手**,不把简报/术语表内联进 prompt——文件是权威版本,内联会漂移。
- 角色不以消息回报正文,而是把产物写到指定路径;回报只说写了什么、有什么拿不准。
- `<skill>` 指本 skill 目录,`<ws>` 指工作区 `<stem>/`。

## 补缺调研

**目的**:只查缺口清单上的东西,不发散。知识库已经覆盖的不查。

| | |
| --- | --- |
| 输入 | `<ws>/_context/gaps.md`(缺口清单);已加载领域的 `sources.md`(知识库里);`<ws>/_context/brief.md` 的内容概述节(给上下文) |
| 输出 | `<ws>/_context/research/01-gaps.md`(再派一轮则 `02-…`) |
| 工具 | 联网搜索与抓取 |
| 模型 | 快模型即可 |

行为约束:

- 逐项查 `gaps.md`,每项先试 `sources.md` 里的 URL(按站点规律直接拼 URL 打开),命中不了再 search。
- 缺口清单以外的东西不查;查的过程中冒出来的相关实体,只在字幕里确实出现时才补一项。
- **每项输出格式固定**(沉淀阶段靠它抽取):

```markdown
### <原文写法>
- 类型: person / character / work / show / team / org / place / event / term / nickname
- 译名: <目标语言译名;查不到写「自拟:…」>
- 简介: <两三行:是谁/是什么、与本视频人物的关系>
- 稳定性: stable | volatile(YYYY-MM)
- 来源: <URL;查不到就写 `search: <用过的关键词>`,不留空>
- 备注: <ASR 错听写法、与库中已有条目的关系、拿不准的地方>
```

- 文件末尾加一节 `## 未查到`,列出查不到的项和试过的关键词,主代理据此决定是否自拟。

完成标准:`gaps.md` 上每一项在产物里都有对应小节(查到或列入「未查到」);每个小节的「来源」非空。

## 翻译

**目的**:把 `_fix.srt` 整份译成目标语言,只输出译文。

| | |
| --- | --- |
| 输入 | `<ws>/<stem>_fix.srt`;先 Read:`<skill>/references/subtitle-rules.md`、`<skill>/references/translation-style.md`、`<ws>/_context/brief.md`、`<ws>/_context/glossary.md`、已加载领域的 `style.md`(知识库里,主代理把路径写进 prompt) |
| 输出 | `<ws>/<stem>_<lang>.txt`,一行一条 `编号<TAB>译文` |
| 工具 | 只读文件;**不给联网、不给知识库检索** |
| 模型 | 快模型可以 |

行为约束(必须写进 prompt):

- 与 `_fix.srt` 的条目一一对应、编号照抄、不合并不拆条不增删;译文内不能有换行和制表符。
- 条目带 `名字: ` 前缀的,前缀保留并按术语表译名翻译。
- 专名严格按术语表;术语表没有的自选译法但全文一致,回报时列出新增译名。
- 每条超过约 18 个汉字就在语义停顿处加逗号;数字不写千位分隔符。
- 简报「风格基调」节按起止时间标了语域的,照它切换;条目号只认当前 `_fix.srt` 的编号,不能沿用整平前编号。引述段落按被引述者口吻译。
- 领域 `style.md` 与 `translation-style.md` 冲突时以 `style.md` 为准。

分块翻译(条目数 ≈ 1200 以上才考虑)的硬要求见 `pitfalls.md`「分块翻译」。

完成标准:`apply` 校验通过(编号完整、无空译文);`check` 无未解释的 `NOTE`。

## 复核

**目的**:发现翻译没说出来的问题——错译、漏译、术语不一致、语体错位——并直接改。

| | |
| --- | --- |
| 输入 | `<ws>/<stem>_fix.srt`(原文基准)、`<ws>/<stem>_<lang>.srt`(待审)、`<ws>/<stem>.srt`(原始副本,对照 3b 的 ASR 修正与别名替换是否合理);先 Read:规范、`translation-style.md`、`brief.md`、`glossary.md`、领域 `style.md`、`_context/alias_log.tsv`(有的话) |
| 输出 | `<ws>/<stem>_<lang>_fix.srt`;`<ws>/_context/review_notes.md` |
| 工具 | 只读 + Edit;**不给 research 原始文件** |
| 模型 | **与主代理同档,不省**(理由见 `pitfalls.md` 第 5 步) |

行为约束:

- 先把 `_<lang>.srt` 复制为 `_<lang>_fix.srt`,再对有问题的条目做定点 Edit,不整份重写。
- 逐条对照原文与译文;别名替换按 `alias_log.tsv` 的 `current_entries` 定位,再用 `start_ms/end_ms/source_text` 对照原始副本确认无误伤。多条候选要逐条看,空值要检查原内容是否已删除或移位;`entry` 是替换前编号,不能用它定位当前稿。
- 改完写 `review_notes.md`,格式固定(沉淀阶段靠它区分「偏好」与「一次性错误」):

```markdown
| 条目 | 原译 | 改后 | 类型 | 原因 |
| --- | --- | --- | --- | --- |
| 37 | 放大来看 | 拉远来看 | 错译 | Zoom out 方向反了 |
| 58 | 秋穗小姐 | 秋穗酱 | 偏好 | 术语表/style 用「酱」 |
```

类型取值:错译 / 漏译 / 术语 / 语体 / 标点 / 偏好。**偏好**指原译不算错、但按领域惯例应当这样写的;只有这一类会被沉淀进 `style.md`。

完成标准:回报「逐条核对完成」+ 修正条数;`clean` 与 `check` 已跑;`review_notes.md` 存在(可为空表)。

## 沉淀

**目的**:把本次可复用的东西整理成提案,由用户看过后落库。

| | |
| --- | --- |
| 输入 | `<ws>/_context/glossary.md`、`brief.md`、`research/*.md`、`review_notes.md`、`hits.json`、`alias_log.tsv`;知识库的 `index.md`、`aliases.tsv`、对应领域包四个文件;先 Read:`<skill>/references/sediment_rules.md`、`<skill>/references/kb_format.md` |
| 输出 | `<ws>/_context/sediment_proposal.json` |
| 工具 | 只读文件 + 写这一个文件;**不运行 `kb_tools.py apply`,不直接改知识库** |
| 模型 | 与主代理同档 |

行为约束全部在 `sediment_rules.md`。回报:各类各几条、拿不准的条目、没沉淀但值得一提的东西(如库里某条译名与本次实际用法不一致)。

完成标准:`sediment_proposal.json` 合法(`python -c "import json;json.load(open(...))"`);`aliases` 里每个 `asr_variants` 都是字幕里真实出现过的写法。
