"""Repository helpers for the local SQLite app-state DB."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from app.storage.db import get_conn


# ============================================================ AI analyses
@dataclass
class AIAnalysis:
    id: int
    title: str
    question: str
    answer: str
    scope_label: str
    cost_centers: str
    date_start: Optional[str]
    date_end: Optional[str]
    rows_in_scope: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str
    question_hash: str
    pinned: bool
    created_at: str


def hash_question(question: str, scope_label: str) -> str:
    return hashlib.sha1(f"{question.strip().lower()}||{scope_label}".encode("utf-8")).hexdigest()


def save_ai_analysis(
    *,
    title: str,
    question: str,
    answer: str,
    scope_label: str,
    cost_centers: Iterable[str],
    date_start: date | None,
    date_end: date | None,
    rows_in_scope: int,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    model: str,
) -> int:
    cc_csv = ",".join(cost_centers or [])
    qhash = hash_question(question, scope_label)
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO ai_analyses (
                title, question, answer, scope_label, cost_centers,
                date_start, date_end, rows_in_scope,
                prompt_tokens, completion_tokens, total_tokens,
                model, question_hash, pinned
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                title, question, answer, scope_label, cc_csv,
                date_start.isoformat() if date_start else None,
                date_end.isoformat() if date_end else None,
                rows_in_scope,
                prompt_tokens, completion_tokens, total_tokens,
                model, qhash,
            ),
        )
        return int(cur.lastrowid or 0)


def list_ai_analyses(limit: int = 200) -> list[AIAnalysis]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM ai_analyses
            ORDER BY pinned DESC, created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_analysis(r) for r in rows]


def find_ai_analysis_by_hash(question_hash: str) -> AIAnalysis | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ai_analyses WHERE question_hash = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (question_hash,),
        ).fetchone()
    return _row_to_analysis(row) if row else None


def set_pinned(analysis_id: int, pinned: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE ai_analyses SET pinned = ? WHERE id = ?",
            (1 if pinned else 0, analysis_id),
        )


def delete_ai_analysis(analysis_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM ai_analyses WHERE id = ?", (analysis_id,))


def _row_to_analysis(row) -> AIAnalysis:
    return AIAnalysis(
        id=row["id"],
        title=row["title"],
        question=row["question"],
        answer=row["answer"],
        scope_label=row["scope_label"],
        cost_centers=row["cost_centers"],
        date_start=row["date_start"],
        date_end=row["date_end"],
        rows_in_scope=row["rows_in_scope"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        total_tokens=row["total_tokens"],
        model=row["model"],
        question_hash=row["question_hash"],
        pinned=bool(row["pinned"]),
        created_at=row["created_at"],
    )


# ============================================================ conversations
@dataclass
class Conversation:
    id: int
    rep_id: str
    cost_center: Optional[str]
    subject: str
    topic: Optional[str]
    status: str          # active | closed | escalated
    tone: int
    thread_key: str
    created_at: str
    last_activity_at: str
    # Populated by join queries:
    rep_name: str = ""
    last_inbound_at: Optional[str] = None
    needs_reply: bool = False


@dataclass
class Message:
    id: int
    conversation_id: int
    direction: str       # inbound | outbound
    message_id: Optional[str]
    in_reply_to: Optional[str]
    from_address: str
    to_address: str
    cc_address: Optional[str]
    subject: str
    body_text: str
    body_html: str
    ai_reasoning: str
    imap_uid: Optional[str]
    sent_at: str


@dataclass
class ActionItem:
    id: int
    conversation_id: int
    rep_id: str
    description: str
    due_at: Optional[str]
    status: str          # open | done | skipped
    created_at: str
    resolved_at: Optional[str]


def list_conversations(status: str | None = None) -> list[Conversation]:
    """Return conversations, newest-last-activity first.

    If ``needs_reply`` joins are done here so callers don't have to query again.
    """
    with get_conn() as conn:
        params: list = []
        where = "WHERE 1=1"
        if status:
            where += " AND c.status = ?"
            params.append(status)
        rows = conn.execute(
            f"""
            SELECT c.*,
                   r.name AS rep_name,
                   MAX(CASE WHEN m.direction='inbound' THEN m.sent_at END) AS last_inbound_at,
                   -- needs_reply: has inbound with no later outbound
                   CASE WHEN EXISTS (
                       SELECT 1 FROM messages mi
                       WHERE mi.conversation_id = c.id
                         AND mi.direction = 'inbound'
                         AND NOT EXISTS (
                             SELECT 1 FROM messages mo
                             WHERE mo.conversation_id = c.id
                               AND mo.direction = 'outbound'
                               AND mo.sent_at > mi.sent_at
                         )
                   ) THEN 1 ELSE 0 END AS needs_reply
            FROM conversations c
            LEFT JOIN reps r ON r.salesman_number = c.rep_id
            LEFT JOIN messages m ON m.conversation_id = c.id
            {where}
            GROUP BY c.id
            ORDER BY c.last_activity_at DESC
            """,
            params,
        ).fetchall()
    return [_row_to_conversation(r) for r in rows]


def get_conversation(conv_id: int) -> Conversation | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT c.*,
                   r.name AS rep_name,
                   MAX(CASE WHEN m.direction='inbound' THEN m.sent_at END) AS last_inbound_at,
                   0 AS needs_reply
            FROM conversations c
            LEFT JOIN reps r ON r.salesman_number = c.rep_id
            LEFT JOIN messages m ON m.conversation_id = c.id
            WHERE c.id = ?
            GROUP BY c.id
            """,
            (conv_id,),
        ).fetchone()
    return _row_to_conversation(row) if row else None


def list_messages(conv_id: int) -> list[Message]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY sent_at ASC",
            (conv_id,),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def list_action_items(conv_id: int | None = None, status: str | None = None) -> list[ActionItem]:
    with get_conn() as conn:
        params: list = []
        where = "WHERE 1=1"
        if conv_id is not None:
            where += " AND conversation_id = ?"
            params.append(conv_id)
        if status:
            where += " AND status = ?"
            params.append(status)
        rows = conn.execute(
            f"SELECT * FROM action_items {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
    return [_row_to_action(r) for r in rows]


def resolve_action_item(item_id: int, new_status: str = "done") -> None:
    from datetime import datetime
    with get_conn() as conn:
        conn.execute(
            "UPDATE action_items SET status=?, resolved_at=? WHERE id=?",
            (new_status, datetime.utcnow().isoformat(), item_id),
        )


def save_conversation(
    *,
    rep_id: str,
    subject: str,
    thread_key: str,
    cost_center: str = "",
    topic: str = "",
    status: str = "active",
    tone: int = 0,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO conversations
              (rep_id, cost_center, subject, topic, status, tone, thread_key)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (rep_id, cost_center, subject, topic, status, tone, thread_key),
        )
        return int(cur.lastrowid or 0)


def save_message(
    *,
    conversation_id: int,
    direction: str,
    from_address: str,
    to_address: str,
    subject: str,
    body_text: str = "",
    body_html: str = "",
    message_id: str = "",
    in_reply_to: str = "",
    cc_address: str = "",
    ai_reasoning: str = "",
    imap_uid: str = "",
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages
              (conversation_id, direction, message_id, in_reply_to,
               from_address, to_address, cc_address, subject,
               body_text, body_html, ai_reasoning, imap_uid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id, direction, message_id, in_reply_to,
                from_address, to_address, cc_address, subject,
                body_text, body_html, ai_reasoning, imap_uid,
            ),
        )
        conv_id = conversation_id
        conn.execute(
            "UPDATE conversations SET last_activity_at=datetime('now') WHERE id=?",
            (conv_id,),
        )
        return int(cur.lastrowid or 0)


def save_action_item(
    *,
    conversation_id: int,
    rep_id: str,
    description: str,
    due_at: str = "",
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO action_items (conversation_id, rep_id, description, due_at)
            VALUES (?, ?, ?, ?)
            """,
            (conversation_id, rep_id, description, due_at or None),
        )
        return int(cur.lastrowid or 0)


def _row_to_conversation(row) -> Conversation:
    return Conversation(
        id=row["id"],
        rep_id=row["rep_id"],
        cost_center=row["cost_center"],
        subject=row["subject"],
        topic=row["topic"],
        status=row["status"],
        tone=row["tone"],
        thread_key=row["thread_key"],
        created_at=row["created_at"],
        last_activity_at=row["last_activity_at"],
        rep_name=row["rep_name"] or "",
        last_inbound_at=row["last_inbound_at"],
        needs_reply=bool(row["needs_reply"]),
    )


def _row_to_message(row) -> Message:
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        direction=row["direction"],
        message_id=row["message_id"],
        in_reply_to=row["in_reply_to"],
        from_address=row["from_address"],
        to_address=row["to_address"],
        cc_address=row["cc_address"],
        subject=row["subject"],
        body_text=row["body_text"],
        body_html=row["body_html"],
        ai_reasoning=row["ai_reasoning"],
        imap_uid=row["imap_uid"],
        sent_at=row["sent_at"],
    )


def _row_to_action(row) -> ActionItem:
    return ActionItem(
        id=row["id"],
        conversation_id=row["conversation_id"],
        rep_id=row["rep_id"],
        description=row["description"],
        due_at=row["due_at"],
        status=row["status"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


# ============================================================ dashboard counts
def dashboard_counts() -> dict:
    """Return live counts for the three dashboard KPI cards.

    Returns:
        dict with keys:
        - active_conversations: int  — conversations with status='active'
        - open_action_items: int     — action_items with status='open'
        - needs_review: int          — inbound messages not yet replied to
                                       (conversation has an inbound message
                                       with no subsequent outbound message)
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE status = 'active'"
        ).fetchone()
        active_convos = int(row[0]) if row else 0

        row = conn.execute(
            "SELECT COUNT(*) FROM action_items WHERE status = 'open'"
        ).fetchone()
        open_actions = int(row[0]) if row else 0

        # "Needs review": conversations that have at least one inbound message
        # that is more recent than the last outbound message (or have no
        # outbound message at all). These are rep replies we haven't answered.
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT c.id)
            FROM conversations c
            JOIN messages m ON m.conversation_id = c.id
                AND m.direction = 'inbound'
            LEFT JOIN messages m2 ON m2.conversation_id = c.id
                AND m2.direction = 'outbound'
                AND m2.sent_at > m.sent_at
            WHERE m2.id IS NULL
            """
        ).fetchone()
        needs_review = int(row[0]) if row else 0

    return {
        "active_conversations": active_convos,
        "open_action_items": open_actions,
        "needs_review": needs_review,
    }


# ============================================================ conversation tracking helpers

def upsert_rep(*, salesman_number: str, name: str, tone: int = 0) -> None:
    """Insert a rep if new; update name/active if already exists.

    Does NOT overwrite user-configured email, boss_email, or tone — those are
    managed in the Reps view.
    """
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO reps (salesman_number, name, tone, active, updated_at)
            VALUES (?, ?, ?, 1, datetime('now'))
            ON CONFLICT(salesman_number) DO UPDATE SET
                name       = excluded.name,
                active     = 1,
                updated_at = datetime('now')
            """,
            (salesman_number, name, tone),
        )


def record_send(
    *,
    salesman_number: str,
    rep_name: str,
    subject: str,
    thread_key: str,
    from_address: str,
    to_address: str,
    cc_address: str = "",
    body_html: str = "",
    cost_center: str = "",
    tone: int = 0,
) -> int:
    """Ensure the rep and conversation exist, then record the outbound message.

    Uses INSERT OR IGNORE on both reps and conversations so re-sends (same
    thread_key) only create one conversation row.  Returns the conversation id.
    """
    upsert_rep(salesman_number=salesman_number, name=rep_name, tone=tone)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO conversations
              (rep_id, cost_center, subject, topic, status, tone, thread_key)
            VALUES (?, ?, ?, 'weekly_email', 'active', ?, ?)
            """,
            (salesman_number, cost_center, subject, tone, thread_key),
        )
        row = conn.execute(
            "SELECT id FROM conversations WHERE thread_key = ?",
            (thread_key,),
        ).fetchone()
        conv_id = int(row["id"])

    save_message(
        conversation_id=conv_id,
        direction="outbound",
        message_id=thread_key,
        from_address=from_address,
        to_address=to_address,
        cc_address=cc_address,
        subject=subject,
        body_html=body_html,
        ai_reasoning="Weekly email sent via Sales Assistant.",
    )
    return conv_id


def find_conversation_for_reply(
    in_reply_to: str,
    references: str = "",
) -> Optional["Conversation"]:
    """Return the conversation a reply belongs to, or None if unrecognised.

    Checks each Message-ID in *in_reply_to* and *references* against:
    1. ``messages.message_id``  — catches mid-thread replies.
    2. ``conversations.thread_key`` — catches direct replies to the seed.
    """
    candidates = [
        mid.strip()
        for mid in (in_reply_to + " " + references).split()
        if mid.strip()
    ]
    if not candidates:
        return None
    with get_conn() as conn:
        for mid in candidates:
            row = conn.execute(
                """
                SELECT c.*, r.name AS rep_name,
                       NULL AS last_inbound_at, 0 AS needs_reply
                FROM conversations c
                JOIN messages m ON m.conversation_id = c.id
                LEFT JOIN reps r ON r.salesman_number = c.rep_id
                WHERE m.message_id = ?
                LIMIT 1
                """,
                (mid,),
            ).fetchone()
            if row:
                return _row_to_conversation(row)
            row = conn.execute(
                """
                SELECT c.*, r.name AS rep_name,
                       NULL AS last_inbound_at, 0 AS needs_reply
                FROM conversations c
                LEFT JOIN reps r ON r.salesman_number = c.rep_id
                WHERE c.thread_key = ?
                LIMIT 1
                """,
                (mid,),
            ).fetchone()
            if row:
                return _row_to_conversation(row)
    return None


def record_inbound(
    *,
    conversation_id: int,
    message_id: str,
    in_reply_to: str = "",
    from_address: str,
    subject: str,
    body_text: str = "",
    body_html: str = "",
    imap_uid: str = "",
) -> Optional[int]:
    """Save an inbound message, skipping duplicates.

    Deduplication is performed by *message_id* (if non-empty) and by
    *imap_uid* + *conversation_id*.  Returns the new message id, or None if
    the message was already present.
    """
    with get_conn() as conn:
        if message_id:
            existing = conn.execute(
                "SELECT id FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            if existing:
                return None
        if imap_uid:
            existing = conn.execute(
                "SELECT id FROM messages WHERE imap_uid = ? AND conversation_id = ?",
                (imap_uid, conversation_id),
            ).fetchone()
            if existing:
                return None

    return save_message(
        conversation_id=conversation_id,
        direction="inbound",
        message_id=message_id,
        in_reply_to=in_reply_to,
        from_address=from_address,
        to_address="",
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        imap_uid=imap_uid,
    )
