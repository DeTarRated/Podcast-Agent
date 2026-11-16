# discover — 发现层

feed 配置、轮询、条件请求（ETag/304）、guid + feed 去重、历史基线、LLM 分类接缝、原子入队。

新 feed 首轮默认只记录历史候选为 `initial_feed_baseline`，不自动处理存量；`feed-add --process-existing` 才显式处理首轮存量。

命令：`feed-add`、`feed-list`、`feed-disable`、`poll`。轮询只读取 feed 元数据，不下载音频；`poll --classify` 才启用 Chelsea 分类。

关注对象由 `PODCAST_CLASSIFY_TOPICS` 配置（逗号分隔，默认 `Chelsea 男子足球队`，最多 8 项）；配置非法时失败关闭、零请求。提示词限定男子一线队，其他球队、其他运动与女足内容判 false。已分类候选不会因为改动该配置而重新判定。

业务白名单启用时（`PODCAST_ALLOWLIST_FILE` 指向的文件存在）：`poll_all` 只轮询 `feeds` 列出的 feed；未列入 `guids` 的候选不入队，标记 `skipped/not_in_allowlist`。基线期间绝不发送 ETag/Last-Modified 条件头，基线 304 不算成功，中断重做保留既有候选记录。

分类使用 `PODCAST_MODEL_CLASSIFY`（默认 `deepseek/deepseek-flash`）和 `DEEPSEEK_API_KEY`，只发送标题／简介。候选先落库，同 call_ref 已有账本时禁止自动重发；超时或响应无法解析记 unknown。
