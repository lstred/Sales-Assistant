"""DDL for the local SQLite app-state database."""

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reps (
        salesman_number TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        email           TEXT,
        boss_email      TEXT,
        tone            INTEGER NOT NULL DEFAULT 0,
        active          INTEGER NOT NULL DEFAULT 1,
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        rep_id          TEXT NOT NULL,
        cost_center     TEXT,
        subject         TEXT NOT NULL,
        topic           TEXT,
        status          TEXT NOT NULL DEFAULT 'active',  -- active|closed|escalated
        tone            INTEGER NOT NULL DEFAULT 0,
        thread_key      TEXT NOT NULL UNIQUE,            -- Message-ID of the seed
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        last_activity_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (rep_id) REFERENCES reps(salesman_number)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_conversations_rep ON conversations(rep_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id  INTEGER NOT NULL,
        direction        TEXT NOT NULL,        -- outbound|inbound
        message_id       TEXT,                 -- RFC822 Message-ID
        in_reply_to      TEXT,
        from_address     TEXT NOT NULL,
        to_address       TEXT NOT NULL,
        cc_address       TEXT,
        subject          TEXT NOT NULL,
        body_text        TEXT NOT NULL DEFAULT '',
        body_html        TEXT NOT NULL DEFAULT '',
        ai_reasoning     TEXT NOT NULL DEFAULT '',
        imap_uid         TEXT,
        sent_at          TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (conversation_id) REFERENCES conversations(id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS action_items (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL,
        rep_id          TEXT NOT NULL,
        description     TEXT NOT NULL,
        due_at          TEXT,                  -- ISO date
        status          TEXT NOT NULL DEFAULT 'open',  -- open|done|skipped
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        resolved_at     TEXT,
        FOREIGN KEY (conversation_id) REFERENCES conversations(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS send_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id   TEXT,
        to_address   TEXT NOT NULL,
        subject      TEXT NOT NULL,
        ok           INTEGER NOT NULL,
        error        TEXT,
        attempted_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metric_snapshots (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        rep_id      TEXT NOT NULL,
        cost_center TEXT,
        metric      TEXT NOT NULL,
        value       REAL NOT NULL,
        captured_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_metric_rep ON metric_snapshots(rep_id, metric, captured_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS settings_kv (
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_analyses (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT NOT NULL,
        question        TEXT NOT NULL,
        answer          TEXT NOT NULL,
        scope_label     TEXT NOT NULL DEFAULT '',  -- e.g. "all CCs · 4/14/2026 → 5/13/2026"
        cost_centers    TEXT NOT NULL DEFAULT '',  -- comma-separated CC codes
        date_start      TEXT,                      -- ISO date
        date_end        TEXT,                      -- ISO date
        rows_in_scope   INTEGER NOT NULL DEFAULT 0,
        prompt_tokens   INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens    INTEGER NOT NULL DEFAULT 0,
        model           TEXT NOT NULL DEFAULT '',
        question_hash   TEXT NOT NULL DEFAULT '',  -- sha1 of (question + scope_label)
        pinned          INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ai_analyses_created ON ai_analyses(created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ai_analyses_hash ON ai_analyses(question_hash)
    """,
)

CURRENT_SCHEMA_VERSION = 2
