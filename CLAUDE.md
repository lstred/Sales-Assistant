# CLAUDE.md — Living Agent Context for Sales Assistant

> **Purpose of this file.** Canonical, always-up-to-date context document for
> any Claude / Copilot agent working in this repo. Update this file in the
> same commit as any meaningful change to architecture, data model,
> integrations, conventions, or open questions.
>
> **Rule:** Read this file *and* `NEW_APP_CONTEXT_PROMPT.md` before doing any
> non-trivial work. Update affected sections in the same commit.

---

## 1. Product summary

Sales Assistant is a **native Windows desktop application** that acts as an
AI-powered sales-management assistant for the user (a sales manager). It:

1. Pulls data from `NRF_REPORTS` SQL Server (rep assignments, sales
   activity, account coverage, displays, and historical/old-system sales).
2. Evaluates each sales rep across multiple weighted metrics.
3. Engages reps over email — **manually-approved** at first, **scheduled**
   later — with personalized coaching, follow-up requests, escalation, and
   tone matching the rep's current performance trajectory.
4. **Receives, stores, and replies to** rep responses on email chains the AI
   originated. Maintains durable conversation history and remembers
   commitments (e.g., "I'll do the PK session on Friday") so future emails
   can follow up.
5. Doubles as a **manager-side analytics tool** — actionable, territory-aware
   insights (no generic vanity KPIs).

Form-factor requirements (binding):

- **Native Windows desktop**, packaged as an `.exe` via PyInstaller.
- **No browser / Streamlit / web stack.**
- **Premium UI quality** — clean, modern, professional, polished. Custom QSS,
  consistent spacing, Segoe UI Variable font, accent-driven design.
- **Safe and secure** — secrets in Windows Credential Manager (via `keyring`),
  parameterized SQL only, no plaintext API keys / passwords on disk, AI
  responses constrained to data the rep is authorized to see.

## 2. Source-of-truth documents

| Doc | Contains | When to read |
|---|---|---|
| `NEW_APP_CONTEXT_PROMPT.md` | SQL Server connection setup, every legacy table & field, business rules, unit conversion, gotchas. | Before writing any data-layer or metric code. |
| `CLAUDE.md` (this file) | Architecture, conventions, integrations, open questions, change log. | Every session before non-trivial changes. |
| `README.md` | Human-facing overview & setup. | When updating user-facing instructions. |

If `NEW_APP_CONTEXT_PROMPT.md` and this file disagree, **NEW_APP_CONTEXT_PROMPT.md
wins** for DB/field facts; update CLAUDE.md to match.

## 3. Tech stack (locked-in)

- **Language:** Python 3.11+ (current dev: 3.11.9).
- **UI:** **PySide6** (Qt for Python, LGPL). Custom QSS theme — no
  qt-material / no Streamlit / no Electron.
- **DB (warehouse):** SQLAlchemy + pyodbc + ODBC Driver 18 for SQL Server,
  Windows Trusted Connection.
- **DB (local app state):** SQLite via stdlib `sqlite3`, file lives under
  `%APPDATA%\SalesAssistant\state.sqlite`.
- **Secrets:** `keyring` → Windows Credential Manager. Never persist secrets
  in `config.json` / `config_local.py` / source.
- **Config:** `pydantic` v2 models. JSON file at
  `%APPDATA%\SalesAssistant\config.json` for non-secret settings.
- **AI:** Provider abstraction (`app/ai/base.py`). Default impl: **OpenAI**
  (`gpt-4.1` / `gpt-5` family) via `httpx`. Abstraction allows Anthropic /
  Azure OpenAI later.
- **Email:** **SMTP (send) + IMAP (receive)** via stdlib `smtplib` /
  `imaplib`. `email.message.EmailMessage` for composition. Behind an
  `EmailTransport` interface so Microsoft Graph can be added later.
- **Templating (email):** Jinja2.
- **Scheduling:** APScheduler (BackgroundScheduler) running inside the Qt
  app's event loop. No Windows services in v1.
- **Packaging:** PyInstaller, single-folder build (faster startup than
  one-file). Code-signing TBD.
- **Lint/format/test:** ruff, black, pytest.

## 4. Repository layout

```
.
├── .gitignore
├── CLAUDE.md
├── NEW_APP_CONTEXT_PROMPT.md
├── README.md
├── pyproject.toml
├── app/
│   ├── __init__.py
│   ├── __main__.py                  # `python -m app`
│   ├── main.py                      # App entry point
│   ├── app_paths.py                 # %APPDATA% paths
│   ├── config/
│   │   ├── __init__.py
│   │   ├── models.py                # Pydantic config models
│   │   └── store.py                 # Load/save + keyring secrets
│   ├── data/                        # SQL Server (NRF_REPORTS)
│   │   ├── __init__.py
│   │   ├── db.py                    # Engine + read_dataframe + ping
│   │   ├── queries.py               # Raw SQL constants
│   │   └── loaders.py               # Filtered/normalized loaders
│   ├── storage/                     # Local SQLite (conversations, log)
│   │   ├── __init__.py
│   │   ├── db.py
│   │   ├── schema.py
│   │   └── repos.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── rep_metrics.py           # Per-rep metric computations
│   │   └── insights.py              # Insight rules → InsightItem list
│   ├── ai/
│   │   ├── __init__.py
│   │   ├── base.py                  # AIProvider interface
│   │   ├── factory.py
│   │   └── openai_provider.py
│   ├── notifications/
│   │   ├── __init__.py
│   │   ├── email_client.py          # SMTP+IMAP transport
│   │   └── templates/
│   ├── scheduler/
│   │   ├── __init__.py
│   │   └── runner.py
│   └── ui/
│       ├── __init__.py
│       ├── theme.py                 # QSS, palette, fonts
│       ├── main_window.py
│       ├── widgets/
│       │   ├── __init__.py
│       │   ├── sidebar.py
│       │   └── status_bar.py
│       ├── views/
│       │   ├── __init__.py
│       │   ├── dashboard_view.py
│       │   ├── reps_view.py
│       │   ├── conversations_view.py
│       │   └── settings_view.py
│       └── dialogs/
│           ├── __init__.py
│           ├── db_settings_dialog.py
│           ├── email_settings_dialog.py
│           └── ai_settings_dialog.py
└── tests/
    └── test_smoke.py
```

## 5. Database — what *this* app cares about

Full reference in `NEW_APP_CONTEXT_PROMPT.md`. Highlights specific to Sales
Assistant:

### 5a. New-system data (post-2025-08-04, granular)

- **`dbo._ORDERS`** — sales fact rows. Customer sales = `ACCOUNT#I > 1`;
  warehouse POs = `ACCOUNT#I = 1` (must exclude from rep metrics).
- **`dbo.BILLSLMN`** — rep ↔ account ↔ cost center assignment. **Source of
  truth for rep ownership and territory.** Columns: `BSACCT` (account),
  `BSSLMN` (salesman number), `BSCODE` (cost center).
- **`dbo.SALESMAN`** — rep name lookup. Join `BILLSLMN.BSSLMN = SALESMAN.YSLMN#`;
  rep display name is `SALESMAN.YNAME`.
- **`dbo.BILLTO`** — customer master. Columns we use:
  - `BACCT#` — new-system account number (matches `_ORDERS.ACCOUNT#I` and
    `BILLSLMN.BSACCT`).
  - `BBANK2` — old-system account number (joins to
    `ClydeMarketingHistory.CustomerNumber`).
  - **A leading `*` in the account/name field marks the account as CLOSED.**
    Closed accounts:
    - Don't penalize a rep for missing sales there.
    - Still surface as "lost-account context" the rep can be reminded of.
- **`dbo._ORDERS.SALESPERSON_DESC`** — name on the order line (text, may
  drift from `SALESMAN.YNAME`; prefer `BILLSLMN`-driven assignment as truth).
- **Revenue:** `ENTENDED_PRICE_NO_FUNDS` (yes the typo is permanent).
  GP: `LINE_GPD_WITHOUT_FUNDS`. GPP: `LINE_GPP_WITH_FUNDS`.
- **Dates:** `ORDER_ENTRY_DATE_YYYYMMDD` (numeric YYYYMMDD — parse in Python).

### 5b. Old-system / pre-go-live data (≤ 2025-08-04, summarized)

- **`dbo.vw_CostCenterCLydeMRKCodeXREF`** — cross-reference between new
  cost centers and old "Clyde Marketing Codes". Columns:
  - `CostCenter` — new system cost center code (e.g. `010`).
  - `CostCenterName` — description (e.g. `CARPET RESIDENTIAL`).
  - `ClydeMarketingCode` — old system marketing code, joins to
    `ClydeMarketingHistory.MarketingCode`.
- **`dbo.ClydeMarketingHistory`** — old summarized sales by customer × cost
  center × month × fiscal year. Columns:
  - `MarketingCode` → join via XREF view.
  - `FiscalYear` — fiscal year. **Fiscal year starts in February**, so FY is
    typically calendar+1. (Today is May 2026 → FY 2027.)
  - `CustomerNumber` → join `BILLTO.BBANK2`.
  - `SalesPeriod1`..`SalesPeriod12` — monthly sales (Period1=Feb, Period12=Jan).
  - `CostsPeriod1`..`CostsPeriod12` — monthly costs.
  - `TotalSales`, `TotalCost`, `Profit` — annual roll-ups.
- Granularity: **NOT by SKU.** Year-over-year comparisons must aggregate
  new-system data to (account × cost center × month) before comparing.

### 5c. Display tracking

- **`dbo.CLASSES`** with `CLCAT='DT'` lists displays. `CLCODE` = 3-char
  display code, `CLDESC` = description.
- **`dbo.BCACCT`** with `BCCAT='DT'` maps displays to accounts.
  - `BCCODE` = display code, `BCACCT` = account number,
  - `DateFormatted` = `YYYY-MM-DD` install date. Treat dates **before
    2025-08-05 as approximate** (system migration cutoff). Sales bumps after
    a post-cutoff display date are reliable signal.

### 5d. App-side configuration the user maintains in the UI (not in DB)

- **Sample CC ↔ product CC mapping** — links sample cost centers to their
  product cost centers.
- **Display ↔ cost center assignment** — which `DT` codes are "core"
  displays for which cost center.
- **Insight weights** — per-metric weights for the composite rep score.
- **Escalation contacts** — per-rep boss/CC email for escalation mode.
- **Tone presets** — per-rep tone bias (carrot ↔ stick scale).

Persisted in `%APPDATA%\SalesAssistant\config.json` (non-secret) and the
local SQLite DB (relational data).

## 6. Conventions for the agent

- Use exact source-of-truth field names (including the `ENTENDED` typo,
  `[D@MFGR]`, `[$DESC]`, `[BSACCT]`, etc.). Alias in Python only.
- **Parameterize all SQL** with `text()` + `:param`. Never f-string user
  values into SQL.
- **All quantities are normalized to SY** in the loader layer.
- **Cost-center conventions**: codes starting with `'0'` are **product**
  cost centers; codes starting with `'1'` are **sample** cost centers.
  Sample CCs are mapped to their sponsoring product CC via
  `AppConfig.sample_to_product_cc` (edited in the *CC Mapping* view).
- **Invoice-driven sales**: anything bucketed by fiscal month/period uses
  `INVOICE_DATE_YYYYMMDD` from `_ORDERS` and **filters to `INVOICE# > 0`**.
  `ORDER_ENTRY_DATE_YYYYMMDD` is order-placed date, not used for
  fiscal-period bucketing.
- **Fiscal calendar**: 4-4-5 weekly pattern, every fiscal month starts on a
  Sunday, anchor = Sunday Feb 1 2026 (FY 2027 P1). January is occasionally
  6 weeks to realign with the calendar year — manage via
  `FiscalCalendarConfig.six_week_january_years` in `AppConfig`. Never
  hard-code month boundaries; call `app.services.fiscal_calendar`.
- **Apply standard filters in loaders** (`IINVEN='Y'`, exclude remnants,
  exclude cost centers starting with `'1'` from product-revenue metrics,
  exclude future-dated rows, drop closed accounts from penalty metrics
  but keep for context).
- **No secrets on disk.** Email passwords, AI API keys → `keyring` only.
  The config JSON only stores non-secret references (host, port, username,
  model name).
- **AI access scope:** the AI may only read/answer about a rep's own
  accounts and metrics in per-rep flows. The Ask-the-AI view is for the
  manager and may see all data. The prompt-builder enforces per-rep scoping.
- **AI responds only on chains it originated.** Verify thread ownership via
  the local `conversations` table before generating any reply.
- **Territory-aware comparisons:** any cross-rep comparison must normalize
  for territory size and account mix. No raw "rep A sold $X vs rep B sold
  $Y" framing.
- **Don't over-engineer.** Add abstractions only after second use.
- **Premium UI bar:** if a UI change makes the app look more like a stock Qt
  demo, don't ship it. Spacing, alignment, typography, and motion matter.
- Update this file in the same commit as any change that affects it.

## 7. Local-state schema (SQLite, `%APPDATA%\SalesAssistant\state.sqlite`)

CREATE-IF-NOT-EXISTS at startup, defined in `app/storage/schema.py`:

- `reps` — cached rep roster (`salesman_number`, `name`, `email`, `tone`,
  `boss_email`, `active`).
- `conversations` — one row per email thread the AI initiated
  (`rep_id`, `subject`, `topic`, `status`, `tone`, timestamps, `thread_key`).
- `messages` — full audit log of every email in/out (direction, headers,
  body html+text, raw IMAP UID, AI reasoning summary if any).
- `action_items` — extracted commitments from rep replies, with `due_at`
  and follow-up status.
- `send_log` — SMTP send attempts, message-id, deliverability.
- `metric_snapshots` — periodic metric values per rep for trend analysis.
- `settings_kv` — small misc key/value (last sync timestamps, etc.).

## 8. Open questions (decide before building the dependent piece)

| # | Question | Needed before |
|---|---|---|
| 1 | Recipient email-address resolution: hard-coded, AD lookup, or manual entry per rep in UI? | First real send |
| 2 | Default scheduled cadence (weekly Mon 7am? bi-weekly?) and quiet hours | Enabling scheduler |
| 3 | Manager review queue UX: in-app preview-and-send (current plan) vs Outlook draft | Done — going with in-app |
| 4 | Escalation policy: when does AI auto-suggest CC'ing the boss vs only on user trigger? | Building escalation feature |
| 5 | How long to retain message bodies (forever / N years)? | Before production |
| 6 | Reps' own read-only portal? | Not v1 |
| 7 | Code-signing certificate for the .exe? | Before distribution |

## 9. Change log

Newest first.

- **2026-05-13** — Empty-Dashboard / partial-sales / sample-CC fixes:
  - **Blended sales loader**: new `load_blended_sales(db, start, end, ccs,
    six_week_jan_years)` in `app/data/loaders.py`. For dates ≥
    `NEW_SYSTEM_CUTOFF` (`2025-08-04`) it pulls line-level invoiced sales
    from `_ORDERS`; for dates before the cutoff it falls back to the
    summarized `dbo.ClydeMarketingHistory` table, unpivoting
    `SalesPeriod1..12` / `CostsPeriod1..12` into one row per (account ×
    cost center × fiscal period). Legacy rows have no rep attribution and
    are tagged `salesperson_desc = "(legacy / pre-Aug 2025)"`. The new
    `data_source` column is either `"new"` or `"legacy"`. Fixes the
    "all sales views show ~$100M instead of ~$180M" complaint —
    historical pre-cutoff portion of the rolling-year window is now
    included.
  - **`SalesFilterBar` switched to blended loader**: every screen that
    uses it (Sales by Rep, Sales by CC, Weekly Email, Ask the AI)
    now sees blended totals. **"Also load prior year"** is on by default
    and now shifts the range exactly one calendar year back (handles
    leap-day) so YoY columns make sense even when the prior range
    crosses the cutoff.
  - **All cost centers in selectors**: new SQL `ALL_COST_CENTERS` (from
    `dbo.ITEM.[ICCTR]`, left-joined to the XREF view for friendly names)
    and loader `load_all_cost_centers`. Both `CostCenterSelector` (used
    by `SalesFilterBar`) and `CCMappingView` now use this — sample CCs
    that start with `'1'` are surfaced and the *Maps to (Parent CC)*
    dropdown contains every code, not just the 23 from the XREF view.
    Empty-state copy in CC Mapping updated to mention `dbo.ITEM`.
  - **Vs prior year columns**: `SalesByRepView` and
    `SalesByCostCenterView` now subscribe to
    `sales_loaded_with_prior(current, prior)` instead of the legacy
    `sales_loaded(current)`. Each surfaces a `prior_revenue` column and a
    `yoy_pct` column (Δ% vs prior year). When prior data is missing the
    columns are blank.
  - **Dashboard populated**: `DashboardView` now accepts `cfg` + `get_db`,
    runs a `_DashboardLoader` `QThread` on first show, and shows real
    KPIs — Last full fiscal month revenue (blended), YTD revenue
    (blended), Open orders ($) + line count, and Active reps (distinct
    `salesperson_desc` over the last 90 days). A *Refresh* button reruns
    the load. Conversation / action-item / needs-review cards remain
    `0` until those features land. `KpiCard.set_caption()` added so the
    card sub-text can be updated post-load (e.g., "FY27 P3 · April").

- **2026-05-13** — Invisible-window / pythonw fix:
  - **App data path moved** to `~\Documents\SalesAssistant` because a
    managed-IT policy was silently deleting any new file under
    `%APPDATA%\SalesAssistant` ~1s after process exit (verified by
    writing test files to several alternate paths — only the original
    `%APPDATA%\SalesAssistant` lost them). `app/app_paths.py` now uses
    `Path.home() / "Documents" / "SalesAssistant"`.
  - **pythonw-safe logging**: `_configure_logging` skips the
    `StreamHandler(sys.stderr)` when `sys.stderr is None` (the
    pythonw.exe case), uses `logging.basicConfig(force=True)`, and
    installs a `sys.excepthook` so unhandled exceptions land in the
    log. The original silent crash was traceable to
    `StreamHandler(sys.stderr)` raising on `None` before `MainWindow.show()`.
  - `MainWindow.show()` now also calls `raise_()` and
    `activateWindow()`, and logs the post-show
    `isVisible()` state for diagnosis.

- **2026-05-13** — Bugfix + feature round (running-app feedback):
  - **DB error fix**: removed non-existent `o.[SALESPERSON]` column from
    `INVOICED_SALES_LINES` and `OPEN_ORDERS_LINES`. The warehouse only
    exposes `o.[SALESPERSON_DESC]`; the rep number lives in
    `BILLSLMN.BSSLMN` and is joined separately when needed. Updated
    `sales_by_rep_view` and `weekly_email_view` to group by
    `salesperson_desc` only.
  - **CC duplicates fix**: `vw_CostCenterCLydeMRKCodeXREF` returns one
    row per (CC × ClydeMarketingCode) — previously surfaced 137 rows for
    23 CCs. `COST_CENTER_XREF` now `GROUP BY cost_center` so each CC
    appears exactly once. The CC selector and CC Mapping view also dedupe
    defensively before rendering.
  - **CC Mapping reworked**: now lists *all* cost centers (not just
    "starts-with-1"), with a search box, *Show only unmapped* toggle,
    parent-name preview column, save/load round-trip into
    `AppConfig.sample_to_product_cc`. Empty-state card explains where the
    data comes from when the warehouse returns nothing.
  - **New view: Core Displays**: assign one or more cost centers to each
    display (`CLASSES.CLCAT='DT'`). Persists in
    `AppConfig.core_displays_by_cc`. Master/detail layout — pick a
    display row, tick the cost centers that consider it core. Auto-loads
    on first show.

- **2026-05-13** — Polish round (UX feedback from running app):
  - **SQL data quality**: `INVOICED_SALES_LINES` now also requires
    `TRY_CONVERT(int, [ORDER#]) > 0` (orders with blank/zero `ORDER#` are
    discarded entirely). Added new query `OPEN_ORDERS_LINES` and loader
    `load_open_orders` for un-invoiced orders (`ORDER# > 0` and
    `INVOICE# = 0/NULL`) — used for pipeline insights only, never for
    salesman credit.
  - **Auto-load on launch**: every data-driven view now populates itself
    on first show. `RepsView` reloads via `QTimer.singleShot(0)`,
    `CCMappingView` auto-reloads and shows an instructional empty-state
    card when no sample CCs are present, and `SalesFilterBar` auto-loads
    its CC list, defaults selection to *All*, and auto-fires *Run* once
    the CCs arrive.
  - **Smart default date range**: `SalesFilterBar` defaults to the last
    12 fully-completed fiscal periods (rolling year ending at the last
    closed fiscal month). Added `last_full_period` and
    `last_n_full_periods_range` to `app.services.fiscal_calendar`. New
    presets row exposes Last full FM, Last 3/6 FM, Rolling year, YTD,
    Last 30d. Added a *vs prior year* checkbox that simultaneously loads
    a parallel range one year back for comparison.
  - **CC selector layout**: action buttons are now split across two rows
    so labels never clip in the narrow filter card; live "X selected"
    count next to "loaded".
  - **AI analysis history (persistence)**: new `ai_analyses` SQLite table
    + `app.storage.repos.save_ai_analysis` / `list_ai_analyses` /
    `find_ai_analysis_by_hash` / `set_pinned` / `delete_ai_analysis`. The
    *Ask the AI* view now has a left-side **Saved analyses** pane with
    search, click-to-restore, right-click pin/delete, and an inline
    "you already asked this" banner when a question matches a previously
    saved Q&A for the same scope. Schema bumped to version 2.

- **2026-05-13** — Major UX expansion based on user feedback:
  - Reusable cost-center **multi-select widget** (`app/ui/widgets/cc_selector.py`)
    with All/None/Products-only/Samples-only shortcuts and a search filter.
  - Reusable **`SalesFilterBar`** that combines the CC selector with a
    date-range picker (with 7d / 30d / 90d / YTD presets) and runs the
    `load_invoiced_sales` worker in the background.
  - **New views**: `SalesByRepView`, `SalesByCostCenterView`,
    `CCMappingView` (Sample CC starts with `'1'` → Product CC starts with
    `'0'`), `WeeklyEmailView` (one draft per rep for the selected CCs and
    period), `AIChatView` (manager-side ad-hoc Q&A over the loaded data with
    a live token estimate), `FiscalCalendarView` (browse any FY, flag
    6-week January overrides).
  - **Sales source switched to invoice-driven**: `INVOICED_SALES_LINES` now
    requires `INVOICE# > 0` and uses `INVOICE_DATE_YYYYMMDD` for fiscal-period
    bucketing. The old monthly aggregator was removed; loaders provide
    derived `fiscal_year`, `fiscal_period`, `fiscal_period_name` columns
    via the new fiscal-calendar service.
  - **Fiscal calendar service** (`app/services/fiscal_calendar.py`):
    4-4-5 weekly pattern, every month starts on a Sunday, anchor =
    Sunday Feb 1 2026 = FY 2027 P1. Supports rare 6-week-January overrides
    via `FiscalCalendarConfig.six_week_january_years` in `AppConfig`.
  - **Token estimator** (`app/ai/token_estimator.py`) — used by the AI Chat
    view to show data/system/total token estimates before sending.

- **2026-05-13** — Major scope expansion. Locked stack: PySide6 desktop +
  custom QSS theme; SQLite local state; SMTP+IMAP email; OpenAI default
  with provider abstraction; keyring for secrets. Added new tables
  introduced by user: `vw_CostCenterCLydeMRKCodeXREF`, `ClydeMarketingHistory`,
  `BILLTO` (esp. `BBANK2` & leading-`*` closed-account flag), `SALESMAN`,
  display tracking via `CLASSES`/`BCACCT` with `CLCAT='DT'`. Added §5b
  (old-system semantics + fiscal-year-starts-Feb), §5c (displays), §5d
  (app-side config), §7 (local SQLite schema), and §1/§3/§6 form-factor
  + UI-quality requirements. Initial app skeleton committed.
- **2026-05-13** — Initial scaffold: `.gitignore`, `README.md`, `CLAUDE.md`,
  and existing `NEW_APP_CONTEXT_PROMPT.md`. Repo:
  https://github.com/lstred/Sales-Assistant.
