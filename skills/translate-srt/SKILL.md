---
name: translate-srt
description: 翻译 SRT 字幕文件到指定语言——加载领域知识库复用已知译名与 ASR 纠错、只补缺口地联网调研、按术语表信达雅翻译并复核,最后把新知识沉淀回库。Use when the user wants to translate an SRT/subtitle file (翻译字幕、字幕翻译、translate subtitles), or to fix a speech-to-text SRT before translating.
---

把一个由语音转文字生成的、不规范的外文 SRT,加工成规范的目标语言 SRT。文本层面的硬规则在 [`references/subtitle-rules.md`](references/subtitle-rules.md)(下称**规范**),译文的「雅」在 [`references/translation-style.md`](references/translation-style.md);派发的每个子代理都必须先 Read 规范。子代理的角色定义(输入、输出、约束)在 [`references/roles.md`](references/roles.md),下文「派发 X 角色」都指它。每条规则背后的实测案例在 [`references/pitfalls.md`](references/pitfalls.md),拿不准时去查。脚本路径以本 skill 目录为基准。

> **给子代理写路径时一律用正斜杠、绝对路径。** 反斜杠里的 `\_` 会被当转义吃掉,子代理会在工作区旁边建错目录还回报"已写入"。派发后 `ls` 一下目标目录。

## 目录约定

为每部字幕建一个独立工作区,所有产出都在工作区内,外部原始字幕保持不动。以翻译 `how-to-code.srt` 为例:

```
<原 srt 同目录>/
  how-to-code.srt                  ← 原始字幕,保持不动
  how-to-code/                     ← 工作区(以原 srt 词干命名)
    how-to-code.srt                ← 原始副本(由脚本复制进来)
    how-to-code_merged.srt         ← merge 产物(输入已是一句一条时无此文件)
    how-to-code_fix.srt            ← 转录修正(原语言,整句一条;含别名替换)
    how-to-code_zh.txt             ← 翻译子代理的原始输出(只有「编号<TAB>译文」,不含时间轴)
    how-to-code_zh.srt             ← 翻译初稿(由 apply 把上面这份贴回时间轴生成)
    how-to-code_zh_fix.srt         ← 复核稿
    how-to-code_zh_split.srt       ← 重切分终稿 ★交付给用户的通常是这一份
    AGENTS.md                      ← 工作区说明(脚本生成)
    _context/                      ← 背景资料区
      hits.json                    ← 知识库命中清单(kb_tools match)
      alias_log.tsv                ← 别名替换日志(含源时间段,整平后 remap-log 更新当前条目号)
      gaps.md                      ← 缺口清单(主代理写)
      brief.md                     ← 背景简报(占位 → 第 2 步填实)
      glossary.md                  ← 术语表(kb_tools glossary 生成「来自知识库」,主代理填「本次新增」)
      research/                    ← 补缺调研产出(01-gaps.md …)
      review_notes.md              ← 复核修改清单
      sediment_proposal.json       ← 沉淀提案
      sediment_result.md           ← 沉淀落库摘要
```

语言后缀用 ISO 639-1(zh、ja、en…)。下文 `<stem>` 指原 srt 词干,`<lang>` 指目标语言代码。

## 知识库

翻译过的人名、节目、术语、ASR 错听和领域约定都沉淀在一个**skill 之外**的纯文本知识库里(格式见 [`references/kb_format.md`](references/kb_format.md)),由 `scripts/kb_tools.py` 读写。开工前跑:

```
python <skill目录>/scripts/kb_tools.py status
```

它打印知识库位置(缺省 `~/.translate-srt/knowledge`,可用环境变量 `TRANSLATE_SRT_HOME` 或 config 改)、各领域条目数、git 状态。**报 MISSING 就先 `kb_tools.py init`**——空库也能跑完整流程,只是没东西可复用。知识库找不到不是跳过后面所有 `kb_tools` 步骤的理由,建一个空的。

## 贯穿全流程:一条一整句

**修正与翻译全程都在"一条一整句"的形态上进行。**半句半句地翻,人称、语序、语气都接不上;十几句挤成一条地翻,时间轴又无从对齐。ASR 的失形有两种,方向相反,都要在翻译前整平:

| 失形 | 成因 | 症状 | 治法 |
| --- | --- | --- | --- |
| **切碎**:一句摊成三四条 | 按显示行宽切(`本日の` / `わびさびワード。`) | 多数条目**不以**句末标点结尾 | 第 3a 步 `merge` |
| **粘连**:十几句挤成一条 | 按静音切(VAD),说话人不停顿时整段打包、只在末尾补一个句号 | 条目**都以**句末标点结尾,但又长又挤(实测过单条 35 秒 / 462 显示列 / 内部零标点) | 第 3b 步补句读 + 3c 步 `split` |

同一份字幕可能两种都有。第 0 步用 `stats` 判定,第 3 步整平,第 4、5 步在整句形态上翻译与复核(中间产物条目少而长,**这是正常的**),第 6 步再用 `resplit` 按目标语言行宽切回观看用分条。

**还有第三种"又长又挤",它不是失形,不要治**:内部已有逗号的长句。`stats` 把它单独报成 `NOTE ... a single, already-punctuated sentence`,原样留给第 6 步折行。**粘连必须在原文层拆开,不能留给 `resplit`**:`resplit` 按译文字宽分配时间轴,跨语言字数比不恒定,音画会漂移数秒;原文字符数≈音节数≈时长,可靠得多。细节见 pitfalls「第 0 步」。

## 第 0 步:输入体检

```
python <skill目录>/scripts/srt_tools.py stats <原 srt 路径> -l <原文语言>
```

`stats` 不写文件,只打印条目形态并给出 `VERDICT`。**照它的判定走**——肉眼判断不出粘连:

- `merge: NEEDED` —— 大量条目不收句,第 3a 步跑 `merge`。
- `split: NEEDED` —— 有超长且像多句粘连的条目。`have sentence punctuation inside` 的那批 `split` 直接能切;`no punctuation inside at all` 的那批由你在 3b 手工补句读。
- `NOTE ... a single, already-punctuated sentence` —— 超长但不是粘连,**一个字都不要动**。
- `punctuation inside: N/M (X%)` —— 比例高(≥50%)说明 ASR 保留标点,零标点的条目多半只是短单句,别硬补句号;比例极低说明 ASR 成片丢标点,补句读才对。
- `speakers:` —— 说话人前缀及各自条目数,`名字: ` 与 `[S01] ` 两种形态都认。没有这一行就跳过所有与说话人有关的动作。

若能拿到上游 ASR 的 word 级时间戳 JSON,可让用户本地重渲染成一句一条再进流程(见 pitfalls「上游 ASR 的 JSON」);多数情况拿不到,直接进第 1 步。

## 第 1 步:建立工作目录

```
python <skill目录>/scripts/srt_tools.py init <原 srt 路径>
```

在原 srt 同目录建 `<stem>/`(已存在则报错退出),复制原 srt,生成 `AGENTS.md` 和 `_context/`(占位 `brief.md`、`glossary.md`、`gaps.md`,空 `research/`)。

完成标准:脚本打印 OK,工作区结构齐全。

## 第 2 步:加载知识库、提问、通读、补缺调研

**2a 加载知识库。**

```
python <skill目录>/scripts/kb_tools.py match <stem>/<stem>.srt -o <stem>/_context/hits.json
```

它拿字幕和整个知识库做别名匹配,打印:命中的名字与出现次数、哪些是 ASR 错听形态、按命中排序的**领域建议**,以及**知识库没覆盖的片假名/拉丁词候选**。然后 Read 知识库的 `index.md`,按命中决定加载哪些领域包(可多个),Read 对应目录的 `entities.md`、`glossary.md`、`style.md`。一个都没命中就当新领域处理,照常往下走,第 8 步沉淀时会建新包。

选定领域后,后续 `glossary` 和 `replace` 都传同一组 `-d <领域>`;多个领域重复传 `-d`。未指定领域时才使用自动路由。

**2b 向用户提问。** 用 AskUserQuestion 问清:原始语言(可提供"自动检测";发现夹了外语插播段就在选项里写明)、目标语言、**视频日期**(判 volatile 条目是否过期;拿不到就用文件时间)、字幕的主题与热词(用户可留空)、**是否调研**。问之前先把 2a 的命中摘要摆出来——「库里已有 N 个人名/节目,缺口候选有 X、Y、Z」——用户看到缺口很少时通常会选跳过。

若 `stats` 报了说话人前缀,**再问各说话人分别是谁**。占位标签必须拿到真名才能进术语表;只有一个说话人时不必问名字,但要告知前缀会在 3b 去掉。多个说话人但全是匿名素材(纪录片常见)时,要连处置方式一起给成选项(全部 `--drop` / 换成角色名 / 只给非旁白加角色名),见 pitfalls「多个说话人不等于问得到真名」。

完成标准:原始语言、目标语言、视频日期已定;主题要么拿到、要么用户明确跳过;每个 speaker 都有名字或处置方式。

**2c 通读字幕,写简报与缺口清单。** Read 工作区内的原始副本(过长则分段读完),带着 2a 的命中清单读。编辑 `_context/brief.md`:

- 内容概述;
- 出现的全部专名——人名/作品名/组织名,**含说话人前缀里的名字**,也含商品名、店名、活动名、听众投稿昵称;标出哪些库里已有;
- 疑似听录错误的词(别名表没覆盖的,附上下文);
- 引述段落的起止时间(朗读来信、复述他人发言),翻译子代理靠它切换人称与语体;
- **风格基调**:体裁、按起止时间标的语域(旁白/新闻/街访/来信朗读),原文的梗在哪;可附条目号,但须注明所属文件,不能跨 `merge`/`split` 沿用旧编号;
- 外语插播段的位置与逐条语义(见 pitfalls「原文可能不止一种语言」)。

同时写 `_context/gaps.md`——**只列库里没有的**:

- 库里没有的人名 / 作品 / 节目 / 组织;
- 库里有但 `match` 或 2a 阅读时发现标了 `volatile` 且可能过期的;
- 视频里明显在讲"最近的事"需要核实的点;
- 疑似 ASR 错听但别名表没覆盖的词(附上下文)。

每项一行,写清原文写法、出现的条目号、你猜它是什么。gaps 为空或很少时,直接建议用户跳过调研。

**2d 补缺调研(用户跳过则省略)。** 派发 **1 个补缺调研角色**(见 roles.md),输入 `gaps.md` + 已加载领域的 `sources.md` 路径,产出 `_context/research/01-gaps.md`,每项格式固定、**来源 URL 必填**。不再做发散搜索;产物暴露出字幕里确实出现的新实体时,可再派一轮补 `02-…`,总数按需控制。

**2e 汇总落盘。**

```
python <skill目录>/scripts/kb_tools.py glossary <stem>/<stem>.srt -o <stem>/_context/glossary.md --video-date <YYYY-MM-DD> -d <领域>
```

它把命中的别名、档案、术语抽成术语表的「来自知识库」一节,并把过期的 volatile 条目标 ⚠。然后 Read `research/` 下全部文件(**不**合并成单一调研文件,避免信息漂移),把新得的译名、自拟译名、听众昵称填进「本次新增」一节;简报据调研补实。跳过调研时「本次新增」由主代理自己拟。

完成标准:`brief.md` 已填实且含「风格基调」;`glossary.md` 两节齐全,覆盖字幕中每个专名(**说话人名也在其中**),查不到标"自拟";`gaps.md` 上每一项要么在术语表里有了着落,要么标了"未查到、自拟"。

## 第 3 步:整平成一条一整句 + 修正转录错误 → `_fix.srt`

**3a 合并被切碎的条目(`stats` 报 `merge: NEEDED` 才做)。**

```
python <skill目录>/scripts/srt_tools.py merge <stem>/<stem>.srt -o <stem>/<stem>_merged.srt
```

脚本打印 `merged: 783 -> 570 entries` 和合并后最长的 5 条条目号——**必须去看这几条**,它们最可能是误合。判据与陷阱见 pitfalls「merge」。

**3b 复制,落实说话人前缀,别名替换,再定点修改。** 把上一步的产物(`_merged.srt`,或没合并时的原始副本)**复制**为 `<stem>/<stem>_fix.srt`。

`stats` 报了说话人前缀的话,先用脚本一次性落实(**不要用 Edit 逐条改**):

```
python <skill目录>/scripts/srt_tools.py speakers <stem>/<stem>_fix.srt --map S01=関根瞳 --map S02=丸岡和佳奈
python <skill目录>/scripts/srt_tools.py speakers <stem>/<stem>_fix.srt --drop
```

多个说话人用 `--map` 换成**原语言**真名(译名留到第 4 步);只有一个说话人 `--drop`;匿名素材整体 `--drop` 前先把「标签 → 身份 → 起止时间」写进 `brief.md`。前缀必须在 `merge` **之后**处理。

然后跑别名替换:

```
python <skill目录>/scripts/kb_tools.py replace <stem>/<stem>_fix.srt --log <stem>/_context/alias_log.tsv -d <领域>
```

`mode=auto` 的错听直接换成正确写法;`mode=ask` 的列位置,**由你对照上下文定点改**。两者共用最长匹配,长 `ask` 词内部的短 `auto` 不会被替换。日志保存替换前的条目号、时间段和原文;第 3d 步更新当前条目号后,随 `_fix.srt` 一起交给复核角色。

然后 Read `_fix.srt`、用 **Edit 做定点修改**——不要整份重写。主代理亲自做(需要对照简报做同音/近音纠错,不外包)。对照 `brief.md`、`glossary.md`,按规范执行:纠正听录错误(优先怀疑与术语表读音相近的词)、修正 3a 的误合与漏合、把归属错位的句首/句尾词移到相邻条目、删除纯口语废句、精简句内重复口语词、`[]`→`()`。保持原语言,不翻译。

**给 `stats` 在 `split: NEEDED` 下点名为 `no punctuation inside at all` 的条目补上句读**(原语言的句末标点),这是 3c 能拆开它们的前提。补之前先把这几条读一遍,只有确实是好几句黏在一起才补;`NOTE ... already-punctuated` 的那批一个字都不要动。若 3a 跑过 `merge`,条目号已变,先对 `_fix.srt` 重跑 `stats` 拿本文件里的条目号。

**3c 拆开粘连的条目(`stats` 报 `split: NEEDED` 才做)。**

```
python <skill目录>/scripts/srt_tools.py split <stem>/<stem>_fix.srt -l <原文语言>
```

原地写回,按句末标点把超长条目拆成一句一条,时间轴按各句显示宽度比例分配。仍然超限的分两档报:`WARN ... no punctuation inside at all` 逐条读一遍再决定是否回 3b 补句读;`NOTE ... already-punctuated` 留着。两档都不是必须清零的指标,**不要为了消掉它们硬插句号**。`split` 已包含 `normalize` 的工作。

**3d 收尾。** 没跑 `split` 的话运行:

```
python <skill目录>/scripts/srt_tools.py normalize <stem>/<stem>_fix.srt
```

整平与定点修改完成后,无论有没有拆分,都运行:

```
python <skill目录>/scripts/kb_tools.py remap-log <stem>/<stem>_fix.srt --log <stem>/_context/alias_log.tsv
```

它按源时间段更新 `current_entries`,可能对应多条;空值表示原内容可能已删除或移位,需对照 `source_text` 解释。日志的 `entry` 始终是替换前编号,不再用于定位当前稿。`brief.md` 继续以起止时间定位,若附了条目号则在此更新为当前 `_fix.srt` 的编号。

完成标准:`split`(或 `normalize`)输出 OK;`stats` 重跑后 `merge`/`split` 均不再 `NEEDED`,或每一处残留都能说明原因;`merge` 最长的几条已抽查;说话人前缀已落实(不留 `[S01]` 占位);`replace` 列出的 `ask` 项都已处置;原 srt 每一条都已处理(修正、合并、拆分、删除四者之一,多数原样保留也算)。

## 第 4 步:翻译 → `_<lang>.srt`

**派发一个翻译角色处理整份 `_fix.srt`**,不要拆分:字幕是连续体,人称、指代、语气跨条目绵延,整份交给同一子代理上下文最完整、术语最一致。分块是降级手段,只在条目数 ≈ 1200 以上才考虑,硬要求见 pitfalls「分块翻译」。

**子代理不输出 SRT,只输出译文**:写 `<stem>/<stem>_<lang>.txt`,一行一条 `编号<TAB>译文`。时间轴由 `apply` 从 `_fix.srt` 搬过来,不经过 LLM(理由见 pitfalls「时间轴不经过子代理」)。

prompt 必须包含:`_fix.srt` 路径、输出路径与格式;要求**先 Read 规范、`translation-style.md`、`brief.md`、`glossary.md`、已加载领域的 `style.md`**(把知识库里的绝对路径写进去)再动手;以及 roles.md「翻译」节的全部行为约束(一一对应、不换行、前缀按术语表译、超 18 字加逗号、数字无千位分隔符、语域按简报「风格基调」切换)。不给联网,不给 research 文件。

```
python <skill目录>/scripts/srt_tools.py apply <stem>/<stem>_fix.srt <stem>/<stem>_<lang>.txt -o <stem>/<stem>_<lang>.srt -l <lang>
python <skill目录>/scripts/srt_tools.py check <stem>/<stem>_fix.srt <stem>/<stem>_<lang>.srt
```

`apply` 已包含 `clean`;校验通过才写文件,报错时照它点名的条目号让子代理补齐再跑。`check` 在这一步主要查漏译(`NOTE` 报译文与原文逐字相同的条目;纯汉字专名合法相同,自行判断)。

完成标准:`apply` 输出 OK;`check` 打印 `OK: aligned`,或每一处 `DIFF`/`NOTE` 都能说明原因。

## 第 5 步:复核 → `_<lang>_fix.srt`

派发一个复核角色(见 roles.md),给它 `_fix.srt`、`_<lang>.srt`、原始副本、`alias_log.tsv`,要求先 Read 规范、`translation-style.md`、`brief.md`、`glossary.md`、领域 `style.md`。**不给 research 原始文件。**

任务:先复制为 `_<lang>_fix.srt`,再对有问题的条目定点 Edit;检出并修正漏译、错译、术语不一致、说话人前缀丢失或译名不一致、语体错位、不符合规范的条目;别名替换按日志 `current_entries` 定位当前稿,再用源时间段和 `source_text` 对照原始副本确认无误伤。改完写 `_context/review_notes.md`(固定表格,类型列区分「偏好」与一次性错误,格式在 roles.md)。然后跑:

```
python <skill目录>/scripts/srt_tools.py clean -l <lang> <stem>/<stem>_<lang>_fix.srt
python <skill目录>/scripts/srt_tools.py check <stem>/<stem>_<lang>.srt <stem>/<stem>_<lang>_fix.srt
```

`check` 报 `DIFF timeline mismatch` 时直接用 base 覆盖时间轴:

```
python <skill目录>/scripts/srt_tools.py check <stem>/<stem>_fix.srt <stem>/<stem>_<lang>_fix.srt --fix-timeline
```

> **复核角色别省模型,主代理也别全信它。** 快模型实测只改 prompt 点名的那一处就回报"逐条核对完成";同档模型在同一份稿子上主动改了 37/110 条。复核清单为空或很短时,**主代理必须自己读一遍译文**——尤其术语表里的词、引述段落的人称、拟声拟态词密集的条目。两者叠加,不是替代。案例见 pitfalls「第 5 步」。

`-l <lang>` 决定 clean 的标点风格:zh/ja/ko 走 cjk(句中非成对标点转空格、句尾不留句号),其余走 western(保留 ASCII 标点,只清残留的全角标点)。说话人前缀在两种风格下都原样透传。

完成标准:子代理回报"逐条核对完成"+ 修正条数;`review_notes.md` 存在;`check` 无未解释的 `DIFF`。

## 第 6 步:重切分 → `_<lang>_split.srt`

```
python <skill目录>/scripts/srt_tools.py resplit -l <lang> <stem>/<stem>_<lang>_fix.srt -o <stem>/<stem>_<lang>_split.srt
```

切点按「空格(clean 留下的读点)> 词边界近似 > 兜底硬切」择优,遵守换行禁则;时间轴在原条目区间内按各段宽度比例分配。行宽缺省 cjk 38 列 / 其余 42 列,`--max-line-width` 可调。**抽查终稿时专门看有没有被切开的词**,尤其拉丁专名两侧;发现了回第 5 步给那几条补逗号或调语序,再重跑。原理与案例见 pitfalls「第 6 步」。这一步会改变条目数,**不要**对它跑 `check`。

### 交付哪一份,由用户定,不由你定

`resplit` 切出来的时间点是按译文字宽插值的,没有音频依据;第 3 步的 `split` 同样在插值,只是依据强一些。对每份候选产物跑:

```
python <skill目录>/scripts/srt_tools.py provenance <原 srt 路径> <stem>/<stem>_<lang>_fix.srt
python <skill目录>/scripts/srt_tools.py provenance <原 srt 路径> <stem>/<stem>_<lang>_split.srt
```

它打印每份有多少时间点来自原始 ASR、多少是插值的,并验证插值点都落在原条目区间内(越界 = bug,必须查)。**拿这两个数字去问用户要哪一份**:`_split` 观看体验最好但插值最多;`_fix` 只含第 3 步的插值,代价是单条可能十几秒;要求零插值只能合并回原始条目边界;真正的解法是换能出词级时间戳的 ASR 重转。

完成标准:resplit 输出 OK;`provenance` 无越界;抽查若干条,单条不超行宽、时间轴单调、前缀完整、没切开词;**已把插值数字告知用户并拿到他的选择**。

## 第 7 步:总结

主代理抽查终稿的开头、中间、结尾各若干条,然后向用户报告:工作区路径;产出文件路径并**指明用户选定的那一份是最终交付物**;`_context/` 内的背景资料;修正要点(别名替换了什么、转录纠错了什么、复核改了什么);知识库这次省了什么(命中 N 个名字、调研只查了 M 个缺口);以及**时间轴里有多少个插值点**——这件事必须主动说。

## 第 8 步:沉淀

交付后派发一个沉淀角色(见 roles.md),它读本次的 `glossary.md`、`brief.md`、`research/`、`review_notes.md`、`hits.json`、`alias_log.tsv` 和知识库现状,按 [`references/sediment_rules.md`](references/sediment_rules.md) 写 `_context/sediment_proposal.json`——**只写提案,不碰知识库**。它回报后运行:

```
python <skill目录>/scripts/kb_tools.py apply <stem>/_context/sediment_proposal.json --summary <stem>/_context/sediment_result.md
```

脚本先在临时副本合并并校验,通过后才更新知识库;同一领域内跨 `aliases.tsv`、`entities.md`、`glossary.md` 的译名冲突也标 `CONFLICT`。返回 2 表示校验或执行失败,先处理错误;校验失败不会改库或提交。成功合并后再按 config 的 `review_mode`:

- `full`(缺省):把摘要给用户看,附上 `kb_tools.py diff` 的输出;用户确认后 `git -C <知识库> add -A && git -C <知识库> commit -m "kb: sediment from <stem>"`,不确认就 `git -C <知识库> checkout -- . && git -C <知识库> clean -fd`。
- `conflicts_only`:没有 CONFLICT 就直接 `apply --commit`;有则只把冲突项给用户裁决。

CONFLICT 项需要用户裁决后手工改库(或改提案重跑 `apply`)。沉淀是本 skill 降低下次成本的唯一途径,**不要因为用户没提就跳过**,但落库前必须让用户看到摘要。

完成标准:`sediment_result.md` 存在;知识库要么已 commit、要么已回滚,不留未确认的改动;`kb_tools.py check` 通过。
