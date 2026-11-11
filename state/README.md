# state — 跨层数据契约（schema 归执行层维护）

逐表字段、约束、职责与代码入口见 `docs/DATA_MODEL.md`。

- `schema.sql`：当前 SQLite v3 全部表与索引
- `migrations.py`：`PRAGMA user_version` 版本迁移与旧数据保护
- `db.py`：WAL、外键、busy_timeout、默认库路径与迁移入口
- `feed.py` / `discovery.py`：feed 配置、候选去重、分类结果与原子入队
- `episode.py`：Episode 创建、查询、状态推进与音频固化
- `manifest.py`：不可变产物登记、版本、SHA256 与事务内写入
- `ledger.py` / `ledger_cli.py` / `ledger_report.py`：付费账本、Runner CLI 与只读报表
- `execution.py`：执行租约、互斥、续约、过期接管与释放
- `review_state.py` / `delivery_state.py`：当前审核修订与当前发布数据契约
