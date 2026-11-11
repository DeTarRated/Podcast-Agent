# review — 审核层

审核箱、静态审核页、Fastify HTTP 适配、Python 审核领域与 CLI、12 小时超时自动审批、保留待审、驳回与 override 历史。

只读「用量与费用」页：账本汇总、筛选、明细与异常状态；网页不改账，R2 费用仍由 Cloudflare 核对。

本轮不实现评测；保留修改上下文供后续补建样本。审核层不直接调用模型或上传 R2。

本地运行需设置 `PODCAST_REVIEW_USER` / `PODCAST_REVIEW_PASSWORD`，然后执行 `cd review && npm start`。默认仅监听 `127.0.0.1:8787`；公网使用必须经 HTTPS 反向代理。

长驻领域服务模式：先设置 `PODCAST_DOMAIN_TOKEN`，在 `127.0.0.1:8788` 运行 `uv run python -m uvicorn review.api:app`，再为 Fastify 设置 `PODCAST_DOMAIN_URL=http://127.0.0.1:8788` 和同一令牌。领域服务不开放 API 文档，RPC 仅限内部调用。

多用户模式：使用 `state.postgres.user_cli create-user` 创建账号，并用 `grant` 授予 Episode 的 read 权限；随后以 `PODCAST_MULTIUSER=1` 启动 Fastify。登录页为 `/login.html`，会话存于 `podcast_session` Cookie。生产 HTTPS 时设置 `PODCAST_AUTH_SECURE_COOKIE=1`。
