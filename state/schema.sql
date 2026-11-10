-- 跨层数据契约：流程状态、审核修订、人工修改、审批与发布
-- 约定：时间一律 ISO8601 UTC 文本；枚举用 CHECK 约束；金额未知为 NULL（不记零）

-- 发现层·feed 配置；删除采用禁用，保留在途期数的历史快照
CREATE TABLE IF NOT EXISTS feed (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    feed_url        TEXT NOT NULL UNIQUE,
    show_name       TEXT,
    etag            TEXT,
    last_modified   TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    bootstrap_pending INTEGER NOT NULL DEFAULT 1 CHECK (bootstrap_pending IN (0,1)),
    last_checked_at TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_feed_enabled ON feed(enabled);

-- Episode：一期节目
CREATE TABLE IF NOT EXISTS episode (
    id             INTEGER PRIMARY KEY,
    guid           TEXT NOT NULL,
    feed           TEXT NOT NULL,
    feed_snapshot  TEXT,                          -- 发现时的 feed 快照（满足 feed 改动不影响在途期数）
    title          TEXT,
    published_at   TEXT,                          -- ISO8601 UTC
    duration_secs  REAL,                          -- 音频时长（秒）
    enclosure      TEXT,                          -- 音频直链
    audio_sha256   TEXT,                          -- 首次固化版本的 hash
    status         TEXT NOT NULL DEFAULT 'discovered'
                   CHECK (status IN ('discovered','downloaded','transcribed','translated',
                                     'reviewing','rejected','approved','delivered','failed','parked')),
    crash_count    INTEGER NOT NULL DEFAULT 0,    -- 连续崩溃计数（长程熔断用，预留）
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (guid, feed)
);

-- 产物清单：每期每一步的不可变产物，路径、版本、校验、生成步骤、状态
CREATE TABLE IF NOT EXISTS artifact (
    id          INTEGER PRIMARY KEY,
    episode_id  INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    step        TEXT NOT NULL,                    -- download / scribe_raw / en_srt / review / split / review_render ...
    kind        TEXT NOT NULL,                    -- audio / json / srt / md / tsv ...
    path        TEXT NOT NULL,                    -- 相对工作区路径
    version     INTEGER NOT NULL DEFAULT 1,
    sha256      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','done','failed')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 发现候选：包括已入队、跳过和失败，确保未入队项也有去重依据
CREATE TABLE IF NOT EXISTS discovery_candidate (
    id              INTEGER PRIMARY KEY,
    feed_id         INTEGER NOT NULL REFERENCES feed(id),
    guid            TEXT NOT NULL,
    title           TEXT,
    published_at    TEXT,
    enclosure       TEXT,
    summary         TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','enqueued','skipped','failed')),
    reason          TEXT,
    classification_relevant INTEGER CHECK (classification_relevant IS NULL OR classification_relevant IN (0,1)),
    classification_call_ref TEXT,
    episode_id      INTEGER REFERENCES episode(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (feed_id, guid)
);

CREATE INDEX IF NOT EXISTS idx_discovery_candidate_status ON discovery_candidate(status);
CREATE INDEX IF NOT EXISTS idx_discovery_candidate_episode ON discovery_candidate(episode_id);

CREATE INDEX IF NOT EXISTS idx_artifact_episode ON artifact(episode_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_identity
    ON artifact(episode_id, step, path, version);

-- 账本：每次付费调用一行，intent 先落、outcome 后更新（effect sandwich）
CREATE TABLE IF NOT EXISTS ledger (
    id           INTEGER PRIMARY KEY,
    episode_id   INTEGER REFERENCES episode(id),  -- 发现层分类调用可不绑期数
    purpose      TEXT NOT NULL,                   -- transcribe / translate / review / research / highlights / classify ...
    triggered_at TEXT NOT NULL DEFAULT (datetime('now')),
    provider     TEXT,                            -- elevenlabs / anthropic / google ...
    model        TEXT,
    usage_basis  TEXT,                            -- 用量依据（音频秒数、token 数）
    amount_est   REAL,                            -- 金额估算；未知为 NULL，不记零
    call_ref     TEXT NOT NULL UNIQUE,            -- 调用标识（本地防重复键，不代表服务端幂等）
    status       TEXT NOT NULL DEFAULT 'intent'
                 CHECK (status IN ('intent','success','failed','unknown'))
);

-- 审核修订：每次保存、切换版本、重跑和再次编辑都产生新修订
CREATE TABLE IF NOT EXISTS review_revision (
    id                      INTEGER PRIMARY KEY,
    episode_id              INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    base_artifact_id        INTEGER NOT NULL REFERENCES artifact(id),
    rendered_artifact_id    INTEGER NOT NULL REFERENCES artifact(id),
    variant                 TEXT NOT NULL CHECK (variant IN ('review','split')),
    status                  TEXT NOT NULL
                            CHECK (status IN ('reviewing','rejected','approved','delivered','superseded')),
    is_current              INTEGER NOT NULL DEFAULT 1
                            CHECK (is_current IN (0,1) AND (is_current = 0 OR status <> 'superseded')),
    reviewing_since         TEXT NOT NULL,
    auto_approve_at         TEXT,
    auto_approve_paused     INTEGER NOT NULL DEFAULT 0 CHECK (auto_approve_paused IN (0,1)),
    supersedes_revision_id  INTEGER REFERENCES review_revision(id),
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_review_revision_current
    ON review_revision(episode_id) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS idx_review_revision_due
    ON review_revision(status, auto_approve_paused, auto_approve_at);

-- 执行互斥：持久化租约防 cron 重叠；过期租约可接管，不自动重放 unsafe 工具
CREATE TABLE IF NOT EXISTS execution_lock (
    episode_id  INTEGER PRIMARY KEY REFERENCES episode(id) ON DELETE CASCADE,
    owner       TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_execution_lock_expiry ON execution_lock(expires_at);

-- 人工修改：追加历史；有效修改由 supersedes / retracted 修改链计算
CREATE TABLE IF NOT EXISTS override (
    id                      INTEGER PRIMARY KEY,
    episode_id              INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    base_revision_id        INTEGER NOT NULL REFERENCES review_revision(id),
    base_artifact_id        INTEGER NOT NULL REFERENCES artifact(id),
    source_artifact_id      INTEGER NOT NULL REFERENCES artifact(id),
    start_ms                INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms                  INTEGER NOT NULL CHECK (end_ms > start_ms),
    source_text_sha256      TEXT NOT NULL,
    before_text             TEXT NOT NULL,
    after_text              TEXT NOT NULL,
    supersedes_override_id  INTEGER REFERENCES override(id),
    retracted               INTEGER NOT NULL DEFAULT 0 CHECK (retracted IN (0,1)),
    request_id              TEXT NOT NULL UNIQUE,
    result_revision_id      INTEGER REFERENCES review_revision(id),
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_override_episode_anchor
    ON override(episode_id, source_artifact_id, start_ms, end_ms);
CREATE UNIQUE INDEX IF NOT EXISTS idx_override_single_successor
    ON override(supersedes_override_id) WHERE supersedes_override_id IS NOT NULL;

-- override 应用到某个候选稿的结果；冲突持久化并阻断审批
CREATE TABLE IF NOT EXISTS override_application (
    id                      INTEGER PRIMARY KEY,
    override_id             INTEGER NOT NULL REFERENCES override(id) ON DELETE CASCADE,
    candidate_artifact_id   INTEGER NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
    status                  TEXT NOT NULL CHECK (status IN ('applied','conflict')),
    target_start_ms         INTEGER,
    target_end_ms           INTEGER,
    reason                  TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (override_id, candidate_artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_override_application_conflict
    ON override_application(candidate_artifact_id, status);

-- 审批：只授权某个修订的确定 rendered artifact 与 SHA256
CREATE TABLE IF NOT EXISTS approval (
    id                  INTEGER PRIMARY KEY,
    review_revision_id  INTEGER NOT NULL UNIQUE REFERENCES review_revision(id),
    episode_id          INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    artifact_id         INTEGER NOT NULL REFERENCES artifact(id),
    sha256              TEXT NOT NULL,
    method              TEXT NOT NULL CHECK (method IN ('manual','auto_timeout')),
    request_id          TEXT NOT NULL UNIQUE,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 驳回重跑请求：审核层写请求，执行层消费；ranges / override ids 是快照
CREATE TABLE IF NOT EXISTS rerun_request (
    id                  INTEGER PRIMARY KEY,
    episode_id          INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    base_revision_id    INTEGER NOT NULL REFERENCES review_revision(id),
    reason              TEXT NOT NULL,
    ranges_json         TEXT NOT NULL,
    override_ids_json   TEXT NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN ('pending','running','completed','failed','unknown')),
    result_revision_id  INTEGER REFERENCES review_revision(id),
    request_id          TEXT NOT NULL UNIQUE,
    error               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 交付：网络上传在事务外完成；current 只指向完整交付的版本
CREATE TABLE IF NOT EXISTS delivery (
    id                  INTEGER PRIMARY KEY,
    approval_id         INTEGER NOT NULL UNIQUE REFERENCES approval(id),
    episode_id          INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    artifact_id         INTEGER NOT NULL REFERENCES artifact(id),
    status              TEXT NOT NULL
                        CHECK (status IN ('pending','waiting_highlights','uploading','delivered','failed','superseded')),
    is_current          INTEGER NOT NULL DEFAULT 0
                        CHECK (is_current IN (0,1) AND (is_current = 0 OR status = 'delivered')),
    audio_key           TEXT,
    subtitle_key        TEXT,
    highlights_key      TEXT,
    highlights_json     TEXT,
    sha256              TEXT NOT NULL,
    error               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_current
    ON delivery(episode_id) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS idx_delivery_pending ON delivery(status, updated_at);
