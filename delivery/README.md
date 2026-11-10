# delivery — 交付层

私人播客库、单期播放与同步字幕、看点模板、R2 上传（boto3）、动态 S3 预签名 URL。

只处理已批准版本；发布后修订期间继续服务旧版，新版完整上传校验后原子切换发布指针。

命令入口：`uv run python -m delivery.cli publish-approved`、`library`、`detail`。`runner/src/drive-delivery.ts` 自动补齐 waiting_highlights 并再次发布；模型 failed／unknown 显式标记，不自动重试。真实交付需配置 `R2_ENDPOINT`、`R2_BUCKET`、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`，可选 `R2_REGION` / `R2_URL_EXPIRES_SECONDS`；缺少看点时 publish 返回供 `runner/src/drive-highlights.ts` 使用的 dispatch 文件。测试使用 fake transport，不冒充真实 R2 验收。
