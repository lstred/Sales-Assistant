# Sales Assistant

An internal application that uses sales data from the `NRF_REPORTS` SQL Server
database to evaluate sales reps and reach out to them via email — either
event-driven (insight triggers) or on a schedule — with personalized insights
and recommendations.

> **Status:** Project scaffolding only. Application code has not been written yet.
> See [CLAUDE.md](CLAUDE.md) for the running design / context document used by
> the Claude / Copilot coding agent.

---

## Goals

1. Pull rep-level activity, sales, fill rate, returns, and coverage data from
   SQL Server (`NRF_REPORTS`).
2. Compute per-rep performance metrics and surface actionable insights
   (under-performing accounts, slipping fill rates, dormant customers, etc.).
3. Email reps with their insights — ad-hoc when a threshold is tripped, and on
   a recurring schedule (e.g., weekly summary).
4. Keep the database connection layer, metric definitions, and field mappings
   aligned with the existing Inventory Dashboard so the same source-of-truth
   semantics are preserved.

## Source-of-truth references

- [NEW_APP_CONTEXT_PROMPT.md](NEW_APP_CONTEXT_PROMPT.md) — full SQL Server
  connection details, table/field dictionary, nicknames, business rules,
  unit-conversion logic, and existing dashboard field definitions. **Read this
  before touching any data layer code.**
- [CLAUDE.md](CLAUDE.md) — living design notes for the agent (kept in sync as
  decisions are made).

## High-level prerequisites (for when build begins)

- Windows host on the NRF corporate network (or VPN) — SQL Server uses
  Windows Trusted Connection only.
- Python 3.11+
- ODBC Driver 18 for SQL Server installed at the OS level.
- A local `config_local.py` containing `SQLSERVER_ODBC` (gitignored).

## Repository layout (current)

```
.
├── .gitignore
├── CLAUDE.md                      # Living agent context / design notes
├── NEW_APP_CONTEXT_PROMPT.md      # SQL schema, fields, business rules (source of truth)
└── README.md
```

The `app/` package, tests, and tooling will be added in subsequent commits.
