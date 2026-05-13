# CLAUDE.md — Living Agent Context for Sales Assistant

> **Purpose of this file.** This is the canonical, always-up-to-date context
> document for any Claude / Copilot coding agent working in this repo. Every
> time we make a meaningful change to architecture, data model, integrations,
> conventions, or open questions, we update this file in the same commit.
>
> **Rule for the agent:** Read this file *and* `NEW_APP_CONTEXT_PROMPT.md`
> before doing any non-trivial work. When you make a change that affects
> anything documented here, update the relevant section in the same change.

---

## 1. Project summary

Sales Assistant is an internal tool that:

1. Pulls sales / order / customer-coverage data from the `NRF_REPORTS` SQL
   Server database.
2. Evaluates each sales rep against per-rep and peer-group metrics.
3. Generates insights (e.g., dropping fill rate, dormant accounts, missed
   coverage on assigned accounts, GP erosion).
4. Emails reps:
   - **Event-driven** — when an insight crosses a threshold.
   - **Scheduled** — recurring digest (cadence TBD; likely weekly).

We are **not** building a UI dashboard in this project (the existing Inventory
Dashboard already covers that). This app is a backend service plus an email
delivery layer.

## 2. Source-of-truth documents

| Doc | What's in it | When to read it |
|---|---|---|
| `NEW_APP_CONTEXT_PROMPT.md` | SQL Server connection setup, every table & field used, nicknames/aliases, business rules, unit conversion, gotchas, computed metrics, existing dashboard field definitions. | **Before** writing any data-layer or metric code. |
| `CLAUDE.md` (this file) | Architecture decisions, conventions, integrations, open questions, change log. | Every session, before making non-trivial changes. |
| `README.md` | Human-facing project overview & setup. | When updating user-facing instructions. |

If `NEW_APP_CONTEXT_PROMPT.md` and this file ever disagree, **`NEW_APP_CONTEXT_PROMPT.md` wins** for database/field facts. Update CLAUDE.md to match.

## 3. Tech stack (planned)

Nothing has been installed yet. Planned baseline:

- **Language:** Python 3.11+
- **Data access:** SQLAlchemy + `pyodbc` + ODBC Driver 18 for SQL Server
  (Windows Trusted Connection — no SQL logins).
- **DataFrames / metrics:** pandas.
- **Scheduling:** TBD — candidates: Windows Task Scheduler invoking a CLI
  entry point, or APScheduler running as a long-lived service. Decide before
  implementing the scheduler.
- **Email:** TBD — candidates: SMTP via `smtplib`, Microsoft Graph API
  (preferred if the org is M365), or `win32com` Outlook automation. Decide
  before implementing the email layer.
- **Templating (email body):** Jinja2.
- **Config:** Same resolution order as the existing dashboard
  (env var → `%APPDATA%\PurchaseOrderBot\config.json` → `config_local.py`),
  extended for new keys (SMTP creds, schedule, recipient overrides, etc.).
- **Testing:** pytest.
- **Lint/format:** ruff + black (to be added).

> **Before adding any of the above as dependencies, confirm with the user.**

## 4. Repository layout

Current (scaffolding only):

```
.
├── .gitignore
├── CLAUDE.md
├── NEW_APP_CONTEXT_PROMPT.md
└── README.md
```

Planned (will be created as we build — update this tree when it changes):

```
.
├── app/
│   ├── __init__.py
│   ├── config.py              # AppConfig + connection-string + email config resolution
│   ├── data/
│   │   ├── __init__.py
│   │   ├── db.py              # SQLAlchemy engine, read_dataframe(), validate_connection()
│   │   ├── queries.py         # Raw SQL strings
│   │   └── loaders.py         # Loaders that apply standard filters & normalizations
│   ├── services/
│   │   ├── __init__.py
│   │   ├── rep_metrics.py     # Per-rep KPIs + peer-group comparisons
│   │   └── insights.py        # Insight rules → list of insight objects
│   ├── notifications/
│   │   ├── __init__.py
│   │   ├── email_client.py    # Email transport (TBD provider)
│   │   └── templates/         # Jinja2 .html / .txt templates
│   ├── scheduler/
│   │   └── runner.py          # Schedule entry point
│   └── cli.py                 # CLI entry: run-once, send-test-email, etc.
├── tests/
├── pyproject.toml             # deps + tool config
└── config_local.py            # GITIGNORED — local connection string + secrets
```

## 5. Database — quick orientation

Full details in `NEW_APP_CONTEXT_PROMPT.md`. Highlights for *this* app:

- **Server:** `NRFVMSSQL04`, **DB:** `NRF_REPORTS`, **Auth:** Windows Trusted.
- **Sales fact table:** `dbo._ORDERS`. Customer sales = `ACCOUNT#I > 1`;
  warehouse POs = `ACCOUNT#I = 1` (must exclude from rep metrics).
- **Sales rep name on the order line:** `_ORDERS.SALESPERSON_DESC`.
- **Rep ↔ account assignment:** `dbo.BILLSLMN`
  (`BSACCT` = account, `BSSLMN` = salesman, `BSCODE` = cost center). This is
  the canonical source for "who owns which accounts" and for peer grouping
  (BSCODE-overlap Jaccard ≥ 0.60 = peers).
- **Customer name:** `_ORDERS.BANK_NAME2`.
- **Revenue field:** `ENTENDED_PRICE_NO_FUNDS` *(yes, the typo is intentional
  and permanent in the schema — never "fix" it)*.
- **GP fields:** `LINE_GPD_WITHOUT_FUNDS` (GP) and `LINE_GPP_WITH_FUNDS` (GPP).
- **Dates:** `ORDER_ENTRY_DATE_YYYYMMDD` and `INVOICE_DATE_YYYYMMDD` are
  numeric YYYYMMDD — must be parsed in Python, not compared as SQL dates.
- **Backorders:** `DETAIL_LINE_STATUS in ('B','R')`; quantity-level metrics
  use `'B'` only.
- **CCA program accounts:** `dbo.BILL_CD` filter
  (`BCCAT='MP'` AND `BCCODE IN ('ACA','ACP','AC1')`).
- **Always parameterize SQL** with `text()` + `:param`. No f-strings around
  user-supplied values.

## 6. Conventions for the agent

- **Don't break the source-of-truth field names.** Use the column names exactly
  as documented (including `ENTENDED_PRICE_NO_FUNDS`, `[D@MFGR]`, `[$DESC]`,
  etc.). When tempted to "clean up" a name, alias it in Python instead.
- **No hard-coded credentials, server names, or paths.** Resolve via the
  config layer.
- **All quantities are normalized to SY** at the loader layer (see
  `NEW_APP_CONTEXT_PROMPT.md` §4). Downstream code assumes SY.
- **Apply standard filters in loaders, not ad hoc** (`IINVEN='Y'`, exclude
  remnants, exclude cost centers starting with `'1'`, etc.).
- **Don't over-engineer.** Add abstractions only when they're used twice.
- **Don't commit `config_local.py`, `.env`, or anything in
  `%APPDATA%\PurchaseOrderBot\`.** The `.gitignore` already covers these — do
  not weaken it.
- **Update this file** when you change architecture, dependencies, schema
  assumptions, integrations, or conventions.

## 7. Open questions (decide before building the relevant piece)

| # | Question | Needed before |
|---|---|---|
| 1 | Which email transport? (SMTP / Microsoft Graph / Outlook COM) | Building `app/notifications/email_client.py` |
| 2 | Where does the app run? (laptop / always-on Windows VM / scheduled task) | Building `app/scheduler/` |
| 3 | What is the recipient list source? (hard list / `BILLSLMN` lookup → AD email lookup / config) | Building rep email resolution |
| 4 | Which insights go in v1 vs later? (fill rate drop, dormant account, GP erosion, missed-coverage, backorder spike, …) | Building `app/services/insights.py` |
| 5 | Send cadence + quiet hours? | Building scheduler |
| 6 | Dry-run / preview mode for emails before live send? | Building email layer (strongly recommended yes) |
| 7 | Persistence for "last sent" / dedup of insights? (SQLite file vs JSON vs none) | Building insights pipeline |

## 8. Change log

Update this list (newest first) every time CLAUDE.md is meaningfully changed.

- **2026-05-13** — Initial scaffold: `.gitignore`, `README.md`, `CLAUDE.md`,
  and the existing `NEW_APP_CONTEXT_PROMPT.md`. No application code yet.
  Repository pushed to `https://github.com/lstred/Sales-Assistant`.
