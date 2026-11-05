# 知识库格式

领域知识库是一组纯文本文件,由 `scripts/kb_tools.py` 读写,git 管理。它**不在 skill 目录里**——skill 是安装拷贝,会被更新覆盖;而且仓库是公开的,个人偏好与听众昵称不该发布出去。

## 位置

```
$TRANSLATE_SRT_HOME/            缺省 ~/.translate-srt/
  config.json                   见下
  knowledge/                    缺省知识库位置(config 的 knowledge_path 可改成绝对路径)
    index.md                    路由用,主代理每次必读
    aliases.tsv                 全局别名表,脚本用
    stopwords.txt               (可选)缺口候选的停用词,一行一个
    <domain>/                   领域包,一个目录一个领域
      entities.md               人物/作品/节目档案
      glossary.md               术语与译法
      style.md                  翻译约定(处理方式,不是术语)
      sources.md                有用过的 URL
```

`python <skill目录>/scripts/kb_tools.py status` 打印解析出来的路径。没有知识库时 `kb_tools.py init` 建骨架并 `git init`。

### config.json

```json
{
  "knowledge_path": "knowledge",
  "default_target_lang": "zh",
  "review_mode": "full",
  "volatile_recheck_months": 3
}
```

- `knowledge_path` 相对 home 目录解析,也可写绝对路径。
- `review_mode`:`full` = 每次沉淀都看完整 diff 再 commit;`conflicts_only` = 没有 CONFLICT 就 `apply --commit` 直接落库。
- `volatile_recheck_months`:`volatile(YYYY-MM)` 条目在视频日期晚于标记多少个月后视为过期。

## index.md

每个领域一段,几行说清楚。主代理只靠它决定加载哪些领域包,所以要精简:

```markdown
## seiyuu
日本女声优相关:广播节目、活动、生放送、访谈。
关键词:声優 / ラジオ / 収録 / アフレコ / イベント / 生放送
常驻人物:涼本あきほ / 幸村恵理 / 永井真里子
```

## aliases.tsv

制表符分隔,首行固定为列名。**这是整个方案的核心资产**:ASR 对日语专名的错听高度稳定,`asr_variants` 攒起来后预替换覆盖率会持续上升。

| 列 | 说明 |
| --- | --- |
| `canonical` | 原文正确写法,全表唯一 |
| `asr_variants` | ASR 常见错听,`;` 分隔,可为空。**不能是 canonical 的子串**(替换会套娃) |
| `translation` | 目标语言译名(缺省 zh) |
| `domain` | 所属领域包目录名 |
| `type` | `person` / `character` / `work` / `show` / `team` / `org` / `place` / `event` / `term` / `nickname` |
| `mode` | `auto`:`replace` 直接把 variants 换成 canonical;`ask`:只报位置,主代理在 3b 定点改 |
| `notes` | 一句备注 |

**`mode` 的判据**:日语子串匹配没有词边界,短别名到处撞。只有 variant 够长(≥3 字)、够独特、且已经在至少一个项目里确认过是错听,才给 `auto`。新沉淀的条目一律 `ask`,下次再遇到、确认无误再升。`kb_tools.py check` 会对短的 auto 别名报 WARN,对 canonical 子串、跨行重复的别名报 ERROR。

`mode` 仍按整行保存,兼容已有 TSV。向 `auto` 行加入新错听写法时,若提案省略 `mode` 或写 `ask`,整行降为 `ask`,包括旧写法;确认该行全部变体满足条件后,再显式提案 `"mode": "auto"`。单独写 `"mode": "ask"` 也可撤销自动替换。

`match` 和 `replace` 共用最长写法占位:长 `ask` 写法及正确写法会保护内部的短 `auto` 片段。替换和待确认位置都从原文计算,不再扫描替换后的文本。

`translation` 只存一种目标语言。目标语言不是它时,别名表仍然用于 ASR 纠错与命中统计,译名由本次翻译另拟。

## entities.md

`### 原文正确写法` 分节,字段 `- 键: 值`(冒号全半角都认)。字段之外的行原样保留。

```markdown
### 涼本あきほ
- 类型: person
- 译名: 凉本秋穗
- 别名: あきちゃん; 鈴本あきほ(ASR)
- 简介: VIMS 所属声优。《偶像大师 闪耀色彩》有栖川夏葉、《蔚蓝档案》鷲見セリナ。与永井真里子、幸村恵理私交深。
- 稳定性: stable
- 来源: akiho-30 (2026-09-05)
```

- `别名` 里的写法也参与 `match` 的命中统计(但不参与 `replace`——替换只认 aliases.tsv)。括号注记会被剥掉再匹配。
- 同一实体后续追加信息时,`apply` 会加 `- 补充(项目 (日期)): …` 行,不重写已有内容。

## glossary.md

同样 `### ` 分节。多种写法用 ` / ` 分隔在标题里。

同一领域中,canonical / 标题写法相同的条目,在 `aliases.tsv`、`entities.md`、`glossary.md` 中必须使用同一译名。`check` 检查这个约束,`apply` 在跨文件及同一提案内也会判冲突。不同领域的同形词可以有不同译法;`glossary -d alpha -d beta` 分别匹配并保留两个领域的定义。昵称可能重名,不用于推断条目身份。

```markdown
### ふつおた
- 译法: 普通来信
- 备注: 广播节目环节名,指听众普通投稿;不要译成「普通邮件」
- 稳定性: stable
- 来源: 领域底座 (2026-09-05)
```

## 稳定性

每个 entities / glossary 条目带 `稳定性`:

- `stable` —— 译名、作品名、节目环节名。长期信任。
- `volatile(YYYY-MM)` —— 比赛结果、当前所属、近期活动、在售商品。`kb_tools.py glossary --video-date` 发现视频日期晚于标记 + `volatile_recheck_months` 时,在术语表里打 ⚠ 并列入「需重查」,主代理把它写进缺口清单。

声优领域大部分是 stable;CS 电竞领域 volatile 占比高,这个标记主要为它准备。

## style.md

不是术语,是**处理方式**:敬称怎么译、语气词保不保留、梗和内部笑话的惯例、口癖怎么对应。按 `## ` 小节组织(敬称与称呼 / 语气与口语 / 节目惯例 / 其他),条目是 `- ` 行,末尾括注来源。

这是复用率最高的文件,也是用户个人偏好沉淀的地方。**只收用户确认过、或在多个项目里反复出现的偏好**,一次性的润色不进来。翻译与复核子代理都要读它。

## sources.md

一行一条 `- URL — 用途`。补缺调研子代理先 fetch 这里的,命中不了再 search。

## 项目侧产物

`kb_tools.py` 在项目工作区 `_context/` 下产生或读取:

| 文件 | 谁写 | 说明 |
| --- | --- | --- |
| `hits.json` | `match` | 命中清单、领域建议、未覆盖的片假名/拉丁词候选 |
| `alias_log.tsv` | `replace --log` + `remap-log` | 每处替换与待确认项的源时间段、原文及当前条目号 |
| `glossary.md` | `glossary` 生成「来自知识库」一节,主代理填「本次新增」 | 本次专用术语表 |
| `sediment_proposal.json` | 沉淀子代理 | 见下 |
| `sediment_result.md` | `apply --summary` | 新增/更新/冲突/跳过的一行摘要 |

### alias_log.tsv 与条目定位

列为 `entry / variant / canonical / status / start_ms / end_ms / source_text / current_entries`,用 TSV 的引号规则保存原文中的制表符等字符:

- `entry`:替换前编号,仅供追溯,不能当成拆分后的编号。
- `start_ms / end_ms / source_text`:替换前的时间段与原文,后续重映射不会改变它们。
- `current_entries`:与源时间段重叠的当前条目号,多条用 `;` 分隔。它是候选范围,复核时还须结合原文判断具体片段。

第 3d 步及以后重新整平原文时,运行 `kb_tools.py remap-log <当前_fix.srt> --log <alias_log.tsv>`。找不到重叠条目时该列为空、返回 1 并提示检查删除/移位,不会猜一个编号。缺少时间戳的旧版日志返回 2 且不覆盖;旧项目需对照生成日志时的原文处理,不能直接套当前编号。简报中的语域和引述区间也以起止时间定位。

## sediment_proposal.json

沉淀子代理**只写这个文件,不直接改知识库**。`kb_tools.py apply` 负责合并、查重、判冲突。

```json
{
  "project": "akiho-30",
  "date": "2026-09-05",
  "index": [
    {"domain": "cs", "description": "Counter-Strike 赛事相关。", "keywords": ["CS2", "Major"], "regulars": []}
  ],
  "aliases": [
    {"canonical": "涼本あきほ", "asr_variants": ["鈴本あきほ"], "translation": "凉本秋穗",
     "domain": "seiyuu", "type": "person", "mode": "ask", "notes": "VIMS"}
  ],
  "entities": [
    {"domain": "seiyuu", "name": "涼本あきほ", "type": "person", "translation": "凉本秋穗",
     "aliases": ["あきちゃん"], "summary": "两三行简介", "stability": "stable"}
  ],
  "glossary": [
    {"domain": "seiyuu", "term": "ふつおた", "translation": "普通来信", "notes": "…", "stability": "stable"}
  ],
  "sources": [{"domain": "seiyuu", "url": "https://…", "note": "官方 profile"}],
  "style": [{"domain": "seiyuu", "section": "敬称与称呼", "text": "ちゃん 译作「酱」,さん 视语境译「桑」或省略"}]
}
```

`index` 只在需要新领域时给。所有数组都可省略。`domain` 必须是单层目录名,不能使用绝对路径、`..` 或目录分隔符。

### apply 的合并规则

- 先在临时副本合并并运行 `check`,通过后才替换发生变化的文件。校验失败返回 2,原知识库、Git 暂存区与提交均不变;修正提案后重试。
- **新条目**追加,`来源` 字段自动写成 `项目 (日期)`。
- **已有条目**:译名/译法不同 → 不覆盖,记 `CONFLICT`,由用户裁决;别名、asr_variants 取并集;`summary`/`notes` 作为 `- 补充(来源): …` 行追加。
- **跨文件或同一提案内译名不同**:同样记 `CONFLICT`,不加入冲突的新记录;已有记录保留原译名。空译名或 `(未定)` 可以补全。
- **variant 已属于别的 canonical** → 不加,记 CONFLICT。
- 新变体没有显式确认 `auto` 时整行降为 `ask`;再次升级需要显式写 `"mode": "auto"`。状态变化在摘要里单列。
- 有 CONFLICT 时 `apply` 返回 1,`--commit` 也照常提交其余部分——冲突项本来就没写入。
- 无冲突且成功返回 0。提交失败返回 2 并保留已校验的未提交改动,不会回报成功。

`kb_tools.py diff` 分别显示暂存区、未暂存的修改和未跟踪文件的正文,包括新领域文件,且不修改 Git 暂存区。完整审查应使用这个命令,单独 `git diff` 看不到全部三类内容。

## 命名一致性

方案文档里的名字与本仓库的对应:`init_project.py` = `srt_tools.py init`;`normalize_srt.py` = `srt_tools.py clean`(规则在 `subtitle-rules.md`);`match_aliases.py` = `kb_tools.py match` + `replace`;`build_glossary.py` = `kb_tools.py glossary`;`backfill.py` = `kb_tools.py backfill`;`kb_diff.py` = `kb_tools.py apply` 的摘要 + `diff`。
