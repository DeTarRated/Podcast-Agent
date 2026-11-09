# review — 审核层

审核箱、静态审核页、Fastify HTTP 适配、Python 审核领域与 CLI、12 小时超时自动审批、保留待审、驳回与 override 历史。

只读「用量与费用」页：账本汇总、筛选、明细与异常状态；网页不改账，R2 费用仍由 Cloudflare 核对。

本轮不实现评测；保留修改上下文供后续补建样本。审核层不直接调用模型或上传 R2。

本地运行需设置 `PODCAST_REVIEW_USER` / `PODCAST_REVIEW_PASSWORD`，然后执行 `cd review && npm start`。默认仅监听 `127.0.0.1:8787`；公网使用必须经 HTTPS 反向代理。
