-- 流程状态存储：垂直切片最小 schema
-- 约定：时间一律 ISO8601 UTC 文本；枚举用 CHECK 约束；金额未知为 NULL（不记零）

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
                                     'reviewing','approved','delivered','failed','parked')),
    crash_count    INTEGER NOT NULL DEFAULT 0,    -- 连续崩溃计数（长程熔断用，预留）
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (guid, feed)
);

-- 产物清单：每期每一步的产物，路径、版本、校验、生成步骤、状态
CREATE TABLE IF NOT EXISTS artifact (
    id          INTEGER PRIMARY KEY,
    episode_id  INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    step        TEXT NOT NULL,                    -- download / scribe_raw / en_srt / glossary / translate / review / split ...
    kind        TEXT NOT NULL,                    -- audio / json / srt / md / tsv ...
    path        TEXT NOT NULL,                    -- 相对工作区路径
    version     INTEGER NOT NULL DEFAULT 1,
    sha256      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','done','failed')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_artifact_episode ON artifact(episode_id);

-- 账本：每次付费调用一行，intent 先落、outcome 后更新（effect sandwich）
CREATE TABLE IF NOT EXISTS ledger (
    id           INTEGER PRIMARY KEY,
    episode_id   INTEGER REFERENCES episode(id),  -- 发现层分类调用可不绑期数
    purpose      TEXT NOT NULL,                   -- transcribe / translate / review / research / classify ...
    triggered_at TEXT NOT NULL DEFAULT (datetime('now')),
    provider     TEXT,                            -- elevenlabs / anthropic / google ...
    model        TEXT,
    usage_basis  TEXT,                            -- 用量依据（音频秒数、token 数）
    amount_est   REAL,                            -- 金额估算；未知为 NULL，不记零
    call_ref     TEXT NOT NULL UNIQUE,            -- 调用标识（幂等键 / request id）
    status       TEXT NOT NULL DEFAULT 'intent'
                 CHECK (status IN ('intent','success','failed','unknown'))
);
