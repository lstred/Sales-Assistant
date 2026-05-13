"""Insight rules: turn metrics into actionable observations for an email."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class InsightItem:
    salesman_number: str
    cost_center: str
    severity: str  # "info" | "warn" | "critical"
    headline: str
    detail: str
    action_suggestion: str = ""
