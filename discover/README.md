# discover — 发现层

feed 配置、轮询、条件请求（ETag/304）、guid + feed 去重、历史基线、LLM 分类接缝、原子入队。

新 feed 首轮默认只记录历史候选为 `initial_feed_baseline`，不自动处理存量；`feed-add --process-existing` 才显式处理首轮存量。

命令：`feed-add`、`feed-list`、`feed-disable`、`poll`。轮询只读取 feed 元数据，不下载音频；`poll --classify` 才启用 Chelsea 分类。

关注对象由一个 JSON 策略文件集中配置（`PODCAST_CLASSIFY_POLICY`，默认 `var/interest-policy.json`；样例见 `config/interest-policy.example.json`）：

```json
{
  "version": 1,
  "scope": "男子一线队",
  "unknown_is_relevant": true,
  "topics": {
    "chelsea": {"label": "Chelsea 男子足球队", "aliases": ["切尔西", "Blues"]},
    "man-utd": {"label": "Manchester United 男子足球队", "aliases": ["曼联", "Man Utd"]}
  },
  "default_topics": ["chelsea", "man-utd"],
  "feeds": {"1": ["chelsea"], "3": ["chelsea", "man-utd"], "9": []},
  "prefilter": {"exclude_keywords": ["rugby", "cricket"]}
}
```

- `topics` 定义可复用的关注对象（label + 别名），`default_topics` 是默认组合
- `feeds` 按 feed id 覆盖组合；空数组表示该 feed 不分类
- `prefilter.exclude_keywords` 命中标题或简介即跳过，不调用付费分类
- 文件不存在时退回 `PODCAST_CLASSIFY_TOPICS`（逗号分隔，历史行为）
- 未知字段、引用不存在的 topic、类型错误一律失败关闭（零请求）

业务白名单启用时（`PODCAST_ALLOWLIST_FILE` 指向的文件存在）：`poll_all` 只轮询 `feeds` 列出的 feed；未列入 `guids` 的候选不入队，标记 `skipped/not_in_allowlist`。基线期间绝不发送 ETag/Last-Modified 条件头，基线 304 不算成功，中断重做保留既有候选记录。

分类使用 `PODCAST_MODEL_CLASSIFY`（默认 `deepseek/deepseek-flash`）和 `DEEPSEEK_API_KEY`，只发送标题／简介。候选先落库，同 call_ref 已有账本时禁止自动重发；超时或响应无法解析记 unknown。
