"""Per-rep performance metrics.

Stubs for now; real implementations land as we wire up insights one at a time.
First metric: sales (new-system + old-system stitched).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RepMetric:
    salesman_number: str
    cost_center: str
    metric: str
    value: float
    note: str = ""


# Future: compute_sales_metrics(db, ...), compute_account_activity(...), etc.
