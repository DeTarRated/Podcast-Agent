# 交接文档 — override 机制设计讨论

- 日期：2026-11
- 定位：给「override 机制设计」专题讨论会话的交接。聚焦这一个机制的设计决策，不是全项目交接（全项目交接见 `docs/HANDOFF.md` 与 `docs/MVP_HANDOFF.md`）
- 当前阶段：最小 MVP 已完成，进入「审核与交付」阶段；override 是该阶段的核心机制之一

## 本次讨论的任务

设计 override 机制：人工修改的持久化保留与「任何重跑中人工修改优先于模型产物、不可被覆盖」的落地语义。设计已定稿，总体实施按 `.agent/tmp/review-library-mvp-execution.md`，代码施工按 `.agent/tmp/review-library-mvp-implementation-spec.md` 推进。

## 必读（按序）

- `AGENTS.md`：沟通规则（禁编号、文档管理、测试规范、Git 规范含 commit 书写）
- `docs/REQUIREMENTS.md`：「人工审核与审核箱」节 + 状态机驳回与发布后修订边 +「评测」节的后置边界
- `docs/ARCHITECTURE.md`：审核层职责（审核箱、人工／超时审批 + override 表）、四层边界
- `docs/IMPLEMENTATION_PLAN.md`：贯穿约束里「人工修改、审批、交付均绑定具体版本」「修正稿/译文稿/复核稿/终稿按版本关联」
- `docs/ROADMAP.md`：支线表「override 机制」行的当前状态
- 代码现状：`pipeline/orchestrate.py`（phase 人工修改保护）、`state/schema.sql`（三表现状）、`state/manifest.py`（版本关联）

## 需求里已定的纲领（不重议）

- 人工修改必须持久化保留，任何重跑中人工修改优先于模型产物，不可被覆盖
- 状态机：`reviewing → rejected（记录原因与重跑范围）→ translated（按范围重跑，override 存活）`
- 审核层含「override 表」；人工修改、审批、交付均绑定具体版本
- 驳回可指定范围重跑（如仅重新翻译指定条目）
- 评测不在本轮实现；override 保留修改上下文，后续可补建标注样本

## 定稿结论

| 决策点 | 结论 |
| --- | --- |
| 存储 | SQLite override 表，修改历史追加保留；不使用工作区 JSON 作为权威来源 |
| 粒度与定位 | 单条人工修改，绑定被修改产物版本，以源稿版本 + 开始／结束时间 + 原文 hash 定位；SRT 条目号仅用于展示 |
| 重跑范围 | 驳回按时间范围局部重跑，只重新生成受影响的下游产物 |
| 合并 | 模型写新候选稿，完成后重新应用有效 override；同位置人工内容优先 |
| 冲突 | 无法唯一映射时保留修改并标记冲突，阻止人工与自动发布；不模糊猜测、不静默丢弃 |
| 审批 | 绑定最终产物版本与 SHA256；新修改或重跑使旧审批失效；保存接口幂等，旧页面版本请求拒绝 |
| 评测 | 不进入审核与私人播客库 MVP；override 保留源文、修改前后文本、时间与版本，后续可补建样本 |
| 审核窗口 | 新待审版本默认 12 小时后 `auto_timeout` 审批；人工可提前通过、驳回或保留待审，打开与播放不重置计时 |
| 发布形态 | 审批并交付后进入私人播客库，不是向用户倾倒中间文档 |
| 发布后修订 | 已批准／已发布内容可再次编辑；旧发布版本在新修订完整交付前继续在线 |

## 现状技术基础（可直接复用）

- `pipeline/orchestrate.py`：phase=transcript_manual_review 的人工修改保护（重跑不覆盖）；manifest 版本关联
- `state/manifest.py`：产物按 step+path+version 登记，sha256 校验
- `state/ledger.py`：call_ref 唯一约束、intent→outcome、reconcile
- skill 的 `kb_tools.py remap-log`：按源时间段把别名日志的条目号重映射到当前稿——override 的条目对齐可复用同思路
- 样本期真实产物：`var/episodes/1/`（en_fix.srt 709 条 / en_zh.srt / en_zh_fix.srt 679 条 / en_zh_split.srt 898 条），可作为 override 场景的测试素材

## 实施边界

- 本专题设计已结束；实现按 `.agent/tmp/review-library-mvp-execution.md` 与 `.agent/tmp/review-library-mvp-implementation-spec.md` 另起，不在本交接文档继续扩写施工细节
- 权威需求、架构、实施计划与技术栈已同步自动审批、私人播客库、发布后修订和评测后置口径
- 遵循 AGENTS.md：禁编号、文档不轻易新增、测试场景注释与 Git 提交批准规则
