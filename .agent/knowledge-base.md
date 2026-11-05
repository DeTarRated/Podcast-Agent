# 知识库讨论记录

> 本文件记录本会话对「英超领域知识库」的讨论结论与未决问题，供后续会话在此基础上继续深入。
> 相关文件：`REQUIREMENTS.md`（需求）、`ARCHITECTURE.md`（模块职责）、translate-srt skill（`/Users/xxx/xxx/changzy/projects/tmp/sallyn-skill/skills/translate-srt/`，权威实现参考：SKILL.md、references/kb_format.md、references/sediment_rules.md）。

## 已达成的理解

### 需求文档中的知识库相关内容

- 散落在多处：项目概述（译名/术语/错听变体）、背景与现状（translate-srt 流程含术语表与沉淀）、目标（知识库沉淀、调研项数递减）、场景·聚焦球队（跨期译名一致）、翻译流水线（术语表生成）、领域知识库一节、数据模型（领域包 + aliases.tsv）
- 「沉淀词汇表」没有独立成条的需求条目，隐含在「每期沉淀：新专名、新错听变体、复核偏好」中；待补

### 术语表与沉淀的关系（translate-srt 的读/写闭环）

- 术语表（翻译前，第 2 步收尾）：从知识库命中 + 本期调研/自拟，汇编成 glossary.md（「来自知识库」与「本次新增」两节），覆盖字幕中每个专名；翻译与复核子代理必读。方向：知识库 → 本期
- 沉淀（交付后，第 8 步）：沉淀角色读本期全部上下文写 sediment_proposal.json（只写提案不碰库），kb_tools.py apply 合并校验，人工看 diff 确认后 git commit，不确认则回滚。方向：本期 → 知识库
- 闭环：术语表让本期复用上次沉淀的知识；沉淀让本期新发现变成下次可复用的知识

### 沉淀的本义

- 受益者是**下一次翻译流程**（主代理 + 子代理 + 脚本），不是人
- 人是**审核闸门**：错误一旦进库会被当作「已知」永久复用，所以宁缺毋滥
- 沉淀判据全是未来导向：「下次还会用到」「换一期还会出现」的才进库

### 统计字段问题

- 知识库持久化字段**不含**出现次数等统计
- 出现次数只在单期临时产物 hits.json 里计算（match 生成），不落库
- mode=auto 的判据用到「本次字幕出现 ≥ 3 次」，用一次不存
- 影响：本项目评测指标（调研项数递减、术语一致率）需要跨期聚合，知识库不提供，由自研评测模块负责

### 物理形态：不是数据库

- 知识库 = 一组纯文本文件（index.md、aliases.tsv、领域包目录含 entities.md/glossary.md/style.md/sources.md）+ git 管理
- 字段 = markdown 里的 `- 键: 值` 行或 TSV 列，靠约定与 kb_tools.py check 校验，无数据库 schema
- 本项目双存储分工：SQLite 存流程状态（Episode、override、账本、评测集——程序读写、需精确查询）；文本知识库存译名/术语/错听变体（LLM 直接读、人工 git diff 审核）
- 为什么知识不用数据库：LLM 读文本最友好；人工审核靠可读 diff；数据量几百到几千条；个人偏好独立 git 仓不入公开仓库

### 与 RAG 的关系

- 广义 RAG 定义满足：生成时检索外部知识并注入
- 但形态特殊：精确字符串匹配（无向量/embedding）、结构化条目（非文档片段）、部分在进入模型前直接替换原文（别名替换）、写入带人工审核闸门
- 刻意不做向量检索的理由：译名要求确定性一致，语义相似召回反而制造不一致；数据量小；要可解释可 diff
- 更贴切的定位：agent 长期记忆 + 词典驱动的实体归一化（ASR 纠错）+ 偏好规则注入
- 面试表述：「外部知识库做术语一致性与 ASR 纠错，广义检索增强，但刻意精确匹配而非向量检索，因为译名一致性要求确定性而不是相关性」

## 现状与缺口

### 已继承的字段（translate-srt kb_format）

- aliases.tsv：canonical、asr_variants、translation、domain、type、mode、notes
- entities.md：类型、译名、别名、简介、稳定性、来源（+ 补充行）
- glossary.md：译法、备注、稳定性、来源
- style.md：处理方式 + 来源；sources.md：URL — 用途
- 稳定性：stable / volatile(YYYY-MM)

### 未定（字段级设计 gap）

- type 枚举的英超映射（skill 原生 person/character/work/show/team/org/place/event/term/nickname → 英超 player/coach/club/stadium/competition/media-term）
- volatile 细分与领域逻辑（转会状态、首发、伤病；skill 只有月份过期重查一种机制）
- 领域包粒度（一个英超大包 vs 按球队/节目分包）
- 说话人与外号进库规则（主持人/常驻嘉宾；球员外号与 ASR 缩写怎么挂）
- 评测统计方案（沿用 hits.json 聚合 vs 扩展知识库字段）
- config 继承（review_mode、volatile_recheck_months 默认 3 个月是否沿用）

## 待继续深入的话题

- 英超知识库字段级 schema 定稿（type 枚举、实体键、volatile 规则）
- 知识库与自研模块的交互：评测指标的数据来源与聚合、账本是否需要关联知识库命中
- 知识库沉淀流程与审核 override 的关系（人工改的译名如何回流沉淀）
- 面试叙事中知识库部分的定位与表述打磨
