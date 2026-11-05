# state — 跨层数据契约（schema 归执行层维护）

- `schema.sql`：episode / artifact / ledger 三表（最小 MVP 最小版；override、评测集在审核与交付阶段补）
- `db.py`：连接约定（WAL、外键开、busy_timeout、schema 幂等初始化）、默认库路径 `var/podcast_agent.db`
- `episode.py`：创建、guid+feed 唯一查询、状态推进、音频固化
- `manifest.py`：产物清单增查、完成/失败标记
- `ledger.py`：intent → outcome 两段式记账（call_ref 唯一；unknown 态供对账，不自动重跑）
