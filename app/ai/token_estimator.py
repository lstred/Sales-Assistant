"""Tiny token estimator for AI prompts.

Rough rule that works well for OpenAI / Anthropic GPT-style tokenizers:
~1 token per 3.5–4 English characters of content. We use **4 chars / token**
to err on the safer (lower) side for surfacing in UI.

For DataFrames we estimate by summing the length of a CSV-flattened sample
(no header repetition) — this matches what the prompt builder sends.
"""

from __future__ import annotations

import io

import pandas as pd

CHARS_PER_TOKEN = 4.0


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN) + 1


def estimate_df_tokens(df: pd.DataFrame, max_rows: int | None = None) -> int:
    if df is None or df.empty:
        return 0
    sample = df if max_rows is None else df.head(max_rows)
    buf = io.StringIO()
    sample.to_csv(buf, index=False)
    return estimate_text_tokens(buf.getvalue())
