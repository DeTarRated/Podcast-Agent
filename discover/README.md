# discover — 发现层

feed 配置、轮询、条件请求（ETag/304）、guid + feed 去重、历史基线、LLM 分类接缝、原子入队。

新 feed 首轮默认只记录历史候选为 `initial_feed_baseline`，不自动处理存量；`feed-add --process-existing` 才显式处理首轮存量。

命令：`feed-add`、`feed-list`、`feed-disable`、`poll`。轮询只读取 feed 元数据，不下载音频；`poll --classify` 才启用 Chelsea 分类。

分类使用 `PODCAST_MODEL_CLASSIFY`（默认 `deepseek/deepseek-flash`）和 `DEEPSEEK_API_KEY`，只发送标题／简介。候选先落库，同 call_ref 已有账本时禁止自动重发；超时或响应无法解析记 unknown。
