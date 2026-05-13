"""Repository helpers for the local SQLite app-state DB."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
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
