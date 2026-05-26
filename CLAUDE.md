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
- **Future / uninvoiced shipments:** `ORDER_SHIP_DATE` (numeric YYYYMMDD) in
  `dbo._ORDERS`. Filter `INVOICE# = 0` (not yet invoiced) AND
  `ACCOUNT#I > 1` (exclude warehouse POs) to get orders that have
  shipped or are scheduled to ship but haven't been invoiced yet.
  Use `ORDER_SHIP_DATE` for date-range queries about upcoming/pending
  deliveries ("what's shipping next week?", "orders due by end of month").

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

Newest first. Older entries are condensed at the bottom of the list —
read those plus this file's earlier sections for full context.

- **2026-05-19 (latest)** — INNER JOIN → LEFT JOIN on ITEM: all open-order & invoiced revenue now captured:
  - **Root cause**: Both `OPEN_ORDERS_LINES` and `INVOICED_SALES_LINES` used `JOIN dbo.ITEM AS i ON i.[ItemNumber] = o.[ITEM_MFGR_COLOR_PAT]` (an INNER JOIN). Any order line whose item code doesn't exist in `dbo.ITEM` (custom items, direct-ship products, special orders, non-stocked items) was silently dropped from all queries. This caused entire cost centers to go missing from AI responses and totals to be severely understated (observed: 11 CCs / $111k returned vs 23 CCs / $801k actual for Thursday shipping).
  - **Fix 1 — `OPEN_ORDERS_LINES`**: Changed `JOIN dbo.ITEM` → `LEFT JOIN dbo.ITEM`. Used `ISNULL(LTRIM(RTRIM(i.[ICCTR])), '')` and `ISNULL(LTRIM(RTRIM(i.[IPRCCD])), '')` so unmatched rows get empty-string CC/price-class instead of being dropped. Updated `cc_csv` and `code_prefix` filter clauses to use `ISNULL(...)` consistently.
  - **Fix 2 — `INVOICED_SALES_LINES`**: Same change — `JOIN` → `LEFT JOIN`, `ISNULL` wrappers on `i.[ICCTR]` and `i.[IPRCCD]`. Every invoiced revenue line now counted regardless of ITEM-master coverage.
  - **Fix 3 — Invoice cache bumped v3 → v4**: Old cached DataFrames (built from INNER JOIN) are invalidated; next refresh re-fetches correct data from the warehouse.
  - **Fix 4 — `_fetch_open_orders_data` uses `load_all_cost_centers`**: Switched from `load_cost_centers` (XREF-only, misses sample CCs and post-go-live unmapped CCs) to `load_all_cost_centers` (reads from `dbo.ITEM.[ICCTR]` directly — authoritative list). Ensures every CC returned by `OPEN_ORDERS_LINES` has a human-readable name in the prompt.
  - **Fix 5 — UNCLASSIFIED label**: Items with empty cost_center (no ITEM-master match) are shown as `UNCLASSIFIED (no item record)` in the CC table so revenue is never invisible to the AI.
  - **30/30 tests pass.**

- **2026-05-26 (latest)** — Weekly email "Generate AI drafts" / "Generate master leaderboard" buttons fixed (both buttons did nothing on any day):
  - **Root cause 1 — `KeyError: 'rep_key'`**: `_ensure_scorecards()` filtered `self._df` on column `"rep_key"` which does NOT exist on the raw DataFrame returned by `load_blended_sales`. The column `rep_key` is only an internal alias created inside `manager_analytics._normalise_sales()` (derived from `salesperson_desc.strip()`). The raw df uses `salesperson_desc`. Because the filter raised `KeyError`, both `_generate_all` and `_generate_master` failed silently (Qt's signal/slot mechanism swallows unhandled exceptions in slot functions when running under `pythonw.exe`).
  - **Fix 1**: `_ensure_scorecards` now detects which column is present — prefers `salesperson_desc` (always present), falls back to `rep_key` if somehow that's the column name. Uses `.fillna("").astype(str)` for safety. Applied to both `sc_df` and `sc_prior` filters.
  - **Root cause 2 — Wrong week range on Mon–Thu**: `_generate_master` and `_weekly_lines_for` passed `anchor` (= latest invoice date, which might be a Friday) to `previous_week_range()` and `current_week_range()`. On Monday May 26 with anchor = Friday May 23, `previous_week_range(May 23)` returned May 11–17 instead of the correct May 18–24. The week-window selection must always use `today` as the reference; `anchor` should only be used to cap `wk_end` (so we never show future dates before invoices are posted).
  - **Fix 2**: Both `_generate_master` and `_weekly_lines_for` now call `previous_week_range(today)` / `current_week_range(today)` for week-boundary logic, then cap `wk_end = min(anchor, wk_end)`. This guarantees "last full week (Sun–Sat)" is always the correct calendar week regardless of when invoices were last posted.
  - **Fix 3 — Silent failure → visible error**: `_generate_all` and `_generate_master` now wrap `_ensure_scorecards()` in a try/except and show a human-readable ❌ error message in the preview panel if it fails, instead of doing nothing. Helps debugging future issues.
  - **30/30 tests pass.**

- **2026-05-22 (latest)** — Non-standard ICCTR normalization + total pipeline vs confirmed-date context fix:
  - **Root cause 1 — Non-standard ICCTR values displayed as CC names**: The `dbo.ITEM` table has some items where `ICCTR` contains non-standard strings like `"TRAY"`, `"K.SHOWER"`, `"KS T TRAY"`, `"TT TRAY"` — these are not valid 3-digit cost-center codes. After the LEFT JOIN fix (commit 1b4c586), these items matched `dbo.ITEM` and their `ICCTR` values became the `cost_center` column. The `cc_name_map.get(cc, cc)` fallback then displayed the raw ICCTR string ("TRAY") as the CC name in the AI prompt, which propagated into the reply.
  - **Root cause 2 — cc_name_map polluted with "None"/"nan"**: When `load_all_cost_centers` returns `NULL` for `cost_center_name` (items with no XREF entry), the old code did `str(r.get("cost_center_name", "")).strip()`. If the pandas cell is `None`, `str(None)` = `"None"` (truthy), causing `cc_name_map["TRAY"] = "None"` to be added. If NaN float, `str(nan)` = `"nan"`. Either way the map was polluted.
  - **Root cause 3 — AI reported only confirmed-date orders, not full pipeline**: The per-day breakdown (added in a prior commit) correctly showed Thursday = $48k (orders with `ORDER_SHIP_DATE` confirmed for that day). However, many open orders may have `ORDER_SHIP_DATE = 0` (no confirmed ship date set in ERP). The AI was instructed to use DAY-BY-DAY for specific-date questions, so it reported only the $48k slice — missing the larger open pipeline. The user expected to see the full open pipeline by CC.
  - **Fix 1 — CC normalization**: Added `_cc_norm` column to `df` after date parsing. `_norm_cc(val)` returns the CC code only if it matches `^\d{3}$` (standard 3-digit numeric), otherwise returns `""`. All BY COST CENTER groupbys now use `_cc_norm` instead of raw `cost_center`. Non-standard ICCTR values ("TRAY", "K.SHOWER", etc.) and items with no ITEM record both map to `""` → displayed as `"UNCLASSIFIED"` (one merged row). Revenue is preserved in the total.
  - **Fix 2 — cc_name_map None/NaN guard**: Added `import math as _math` inside try block. Name-raw values are now checked: `if name_raw is None → name=""`, `elif isinstance(name_raw, float) and math.isnan(name_raw) → name=""`, else check `name.lower() in ("nan","none","na","<na>","null","")`. Only valid non-empty strings are added to `cc_name_map`.
  - **Fix 3 — Total pipeline vs day-specific context**: Renamed BY COST CENTER section header to `"BY COST CENTER — TOTAL OPEN PIPELINE (all dates, including orders with no confirmed ship date)"`. Updated management system prompt: (a) explains the two sections — TOTAL PIPELINE (all dates) vs DAY-BY-DAY (confirmed ship-date only); (b) instructs AI that for "shipping [day] by CC" questions, show the TOTAL PIPELINE breakdown alongside the day-specific confirmed amount, and add a note if day-specific is small; (c) NEXT 7 DAYS BY COST CENTER header now labeled "confirmed ship-date orders only"; (d) added message if no orders have confirmed dates in next 7 days. Updated rep system prompt similarly. COST CENTER NAMES CRITICAL section updated to include "UNCLASSIFIED" as a valid label.
  - **30/30 tests pass.**

- **2026-05-22 (latest, earlier)** — Per-day shipping schedule + anti-hallucination hardening (round 2):
  - **Root cause still surfacing**: even with the BY COST CENTER summary added earlier today, the AI was still inventing generic flooring-industry CC names ("Carpet Residential", "Tile & Stone", "Wood Flooring", "Adhesives & Accessories", "Other") when asked about a specific date ("shipping tomorrow"). The bucket data only covered "Next 7 days" — no per-day granularity — so the AI had no exact source for "tomorrow" and fabricated the entire breakdown.
  - **Fix 1 — Per-day breakdown for next 15 days**: `_fetch_open_orders_data` now includes a `SHIPPING SCHEDULE — DAY-BY-DAY` table covering today + next 14 days (each row = one calendar date with total $ and line count). For every specific-date question ("tomorrow", "Friday", "May 22") the AI has exact source rows. Labels include `Today`, `Tomorrow`, and `Wed May 21, 2026` style dates.
  - **Fix 2 — Per-day × cost-center table (management only)**: For each of the next 8 days that has any shipments, the source block lists every cost center with non-zero revenue for that day. So `"by cost center for tomorrow"` has the exact breakdown — AI can no longer invent it.
  - **Fix 3 — Strict CC-name rule in both system prompts**: New `COST CENTER NAMES — CRITICAL` section instructs the AI that the ONLY valid CC names are those listed verbatim in the `BY COST CENTER` table. Explicitly forbids generic category names like 'Carpet Residential', 'Tile & Stone', 'Wood Flooring', 'Adhesives & Accessories', 'Other'. Lists the actual CC name pattern (CARPET RESIDENTIAL, CUSHION, COMMERCIAL RESILIENT, VCT, RESILIENT LVT, HARDWOOD, etc.). Forbids collapsing small CCs into 'Other'.
  - **Fix 4 — Validator hardened**: `_validate_draft` now checks NAMED ENTITIES (cost-center names, product names, rep names) in addition to numbers. New rule 5 explicitly rejects fabricated CC names like 'Carpet Residential' / 'Tile & Stone' / 'Wood Flooring' / 'Other' unless they appear verbatim in the source. New rule 6 enforces that the TOTAL in the draft must equal the TOTAL row in the source data exactly.
  - **Fix 5 — Management temperature lowered**: 0.2 → 0.1 for management replies (rep replies stay at 0.2 for slight tone warmth). Lower temperature reduces creative paraphrasing of category names.
  - **Cleanup**: removed unused `import pandas as pd` and `timedelta as _td` imports inside `_fetch_open_orders_data` (re-added `_td` since it's now used by the per-day loop).
  - **30/30 tests pass.**

- **2026-05-22** — Open orders CC breakdown + validator AI pass (hallucination prevention):
  - **Root cause of $169k vs $500k hallucination**: `_fetch_open_orders_data` called `load_open_orders(db, code_prefix="0")` which filtered to product CCs only. Sample and other CC orders were excluded, making the real total lower. More critically, the function provided only per-rep breakdowns for management — **no cost-center grouping at all**. When the manager asked "what's shipping tomorrow by cost center", the AI had zero CC data in the source block, so it fabricated an entire table with invented flooring industry category names ("Carpet Residential", "Tile", "Wood", etc.) and invented dollar values that summed to $169k vs the real $500k+.
  - **Fix 1 — Load all CCs**: `_fetch_open_orders_data` now calls `load_open_orders(db)` with no `code_prefix` filter. This captures ALL open orders (product + sample + other) so the grand total in the prompt matches the actual warehouse total. The function docstring and comment note this explicitly.
  - **Fix 2 — BY COST CENTER summary table**: After the bucket summary, a new `BY COST CENTER — total open pipeline:` table is always included. Loads CC names via `load_cost_centers(db)` (which calls `COST_CENTER_XREF`). Shows every CC with its human-readable name, total open revenue, and line count, plus a `TOTAL` row that matches the grand total exactly. The grand total line is annotated `← USE THIS AS THE GRAND TOTAL` so the AI doesn't deviate.
  - **Fix 3 — Per-bucket CC breakdown in management mode**: Each bucket's detail section now shows BOTH a rep breakdown AND a cost-center breakdown (using CC names from the lookup) — so if the manager asks "by cost center for next 7 days" the AI has exact per-bucket CC figures, not just company-wide CC totals.
  - **`_validate_draft(draft, warehouse_block) -> str`** added to `_AiReplyWorker`. A second AI call (temperature 0.0) that receives the generated draft + the warehouse data block and is instructed to: (1) extract every number from the draft; (2) verify each one against the source data (exact match or provable sum); (3) if any number is unverifiable → it is FABRICATED → remove or correct it using only what the source data shows; (4) return the corrected text verbatim if accurate. Falls back to the original draft on any error — never blocks the send flow. Uses first 7,000 chars of warehouse_block (covers the hallucination-prone open orders and sales summary sections).
  - **`_generate()` updated**: after the first AI call, calls `self._validate_draft(draft, warehouse_block)` before returning. Only runs when `warehouse_block` is non-empty (no point validating when there's no source data).
  - **30/30 tests pass.**

- **2026-05-18 (latest)** — Open orders / future shipments in AI conversation replies:
  - **Root cause of "doesn't know about future shipments"**: `_AiReplyWorker` only loaded invoiced FY-YTD data via `load_invoiced_sales()`. The `OPEN_ORDERS_LINES` query existed but was never called in the AI reply flow. Any question about pending orders, upcoming shipments, or backlog returned no data, forcing the AI to say it had no information.
  - **`OPEN_ORDERS_LINES` query updated** in `app/data/queries.py`: added `TRY_CONVERT(int, o.[ORDER_SHIP_DATE]) AS order_ship_yyyymmdd` and `LTRIM(RTRIM(i.[IPRCCD])) AS price_class` to the SELECT. The ITEM join was already present; no additional SQL join needed. Column schema change is additive — no cache bump needed (open orders are not cached).
  - **`_fetch_open_orders_data(is_management: bool) -> str`** added to `_AiReplyWorker` in `conversations_view.py`. Loads all open (uninvoiced) orders from `load_open_orders(db, code_prefix="0")`. Rep mode: filters to the rep's BILLSLMN-assigned accounts (same salesman_number resolution as `_fetch_rep_data`). Management mode: all accounts. Buckets orders by ship date: Overdue (past ship date, not yet invoiced), Next 7 days, 8–30 days, 31–90 days, 90+ days, No ship date. Per-bucket detail: rep mode shows per-account + per-product breakdown; management mode shows per-rep + per-product breakdown. Grand total and bucket summary table always shown. Returns `""` on any error so AI generation never crashes.
  - **`_generate()` updated**: calls `_fetch_open_orders_data(is_management=is_mgmt)` for both management and rep paths. Appended to `warehouse_block` alongside invoiced sales and account detail.
  - **Both system prompts updated**: added `OPEN ORDERS / FUTURE SHIPMENTS` section describing bucket meanings and instructing the AI to use this section for any question about pending orders, upcoming shipments, backlog, what's shipping this week/month, or overdue orders.
  - **30/30 tests pass.**

- **2026-05-21** — BILLSLMN deleted-record filter + professional HTML auto-reply:
  - **Root cause of Steve Olink**: `dbo.BILLSLMN` has a `BSDEL` column. `BSDEL = 'D'` marks a record as deleted. Steve Olink's one remaining BILLSLMN entry has `BSDEL = 'D'`. The `REP_ASSIGNMENTS` query was not filtering on this column, so the deleted record was included in the rep_map, causing his account's sales to be attributed to him.
  - **Fix**: Added `AND ISNULL(LTRIM(RTRIM(b.[BSDEL])), '') <> 'D'` to the `WHERE` clause of `REP_ASSIGNMENTS` in `app/data/queries.py`. This removes ALL deleted BILLSLMN entries from the rep attribution map — not just Steve Olink's, but any departed/reassigned rep's stale records.
  - **SALESMAN whitelist** (from previous commit) remains as defense-in-depth for any case where a record is not flagged `BSDEL='D'` but the rep is no longer in SALESMAN.
  - **HTML auto-reply formatting**: Added `_ai_text_to_html(raw, is_management)` module-level function in `conversations_view.py`. Converts the AI's plain-text output (with ASCII tables) to a professional HTML email matching the leaderboard aesthetic — dark navy header, aligned HTML tables with zebra rows and a TOTAL row, clean typography, footer note. ASCII table detection: any blank-line-separated block containing a separator line (5+ dashes/em-dashes) is parsed into `<table>` HTML; column numeric-ness is inferred from cell content; the TOTAL row is styled with navy background + white text. Non-table sections render as styled `<p>` elements; ALL-CAPS lines or lines ending in `:` become small uppercase section labels.
  - **Both send paths updated**: `_AutoReplyWorker.run()` and `_ReplyComposeDialog._send()` now call `_ai_text_to_html(draft/body)` and pass the result as `body_html` to `client.send()` and `save_message()`. Outlook/Gmail render the HTML; plain-text fallback still included.
  - **30/30 tests pass.**
  - **Root cause of Steve Olink**: `dbo.BILLSLMN` has a `BSDEL` column. `BSDEL = 'D'` marks a record as deleted. Steve Olink's one remaining BILLSLMN entry has `BSDEL = 'D'`. The `REP_ASSIGNMENTS` query was not filtering on this column, so the deleted record was included in the rep_map, causing his account's sales to be attributed to him.
  - **Fix**: Added `AND ISNULL(LTRIM(RTRIM(b.[BSDEL])), '') <> 'D'` to the `WHERE` clause of `REP_ASSIGNMENTS` in `app/data/queries.py`. This removes ALL deleted BILLSLMN entries from the rep attribution map — not just Steve Olink's, but any departed/reassigned rep's stale records.
  - **SALESMAN whitelist** (from previous commit) remains as defense-in-depth for any case where a record is not flagged `BSDEL='D'` but the rep is no longer in SALESMAN.
  - **HTML auto-reply formatting**: Added `_ai_text_to_html(raw, is_management)` module-level function in `conversations_view.py`. Converts the AI's plain-text output (with ASCII tables) to a professional HTML email matching the leaderboard aesthetic — dark navy header, aligned HTML tables with zebra rows and a TOTAL row, clean typography, footer note. ASCII table detection: any blank-line-separated block containing a separator line (5+ dashes/em-dashes) is parsed into `<table>` HTML; column numeric-ness is inferred from cell content; the TOTAL row is styled with navy background + white text. Non-table sections render as styled `<p>` elements; ALL-CAPS lines or lines ending in `:` become small uppercase section labels.
  - **Both send paths updated**: `_AutoReplyWorker.run()` and `_ReplyComposeDialog._send()` now call `_ai_text_to_html(draft/body)` and pass the result as `body_html` to `client.send()` and `save_message()`. Outlook/Gmail render the HTML; plain-text fallback still included.
  - **30/30 tests pass.**

- **2026-05-21** — Departed-rep filter: SALESMAN whitelist across all views:
  - **Root cause**: `dbo.SALESMAN` has NO active/terminated flag. A rep who leaves the company may still have stray BILLSLMN account entries. Because BILLSLMN is the source of truth for attribution, their sales appear under their name in every per-rep report (e.g. Steve Olink with 1 remaining account showing $513 of Win Win sales in the management auto-reply).
  - **Fix**: At every report generation point, load the current `dbo.SALESMAN` roster via `load_reps(db)` and build a `valid_rep_names: set[str]` whitelist (upper-cased). Any `salesperson_desc` not in the whitelist is filtered out before grouping. Also enforce `_EXCLUDED_REPS = {"", "house account", "(legacy / pre-aug 2025)"}` consistently.
  - **`_fetch_management_data()` in `conversations_view.py`**: Added `load_reps` to the import block. After loading `df_c` / `df_p`, applies `_is_valid_rep()` row-filter to both DataFrames before any groupby — so departed reps never appear in SALES BY REP, QUERY-MATCHED PRODUCTS, PRODUCT × REP BREAKDOWN, or COMPLETE CATALOG totals. Filter fails open (shows all reps) if the SALESMAN query errors.
  - **`_generate_master()` in `weekly_email_view.py`**: Replaced `_EXCLUDED_REPS`-only check with a `_rep_is_active()` helper that enforces both `_EXCLUDED_REPS` AND the SALESMAN whitelist built from `self._assignments_df.salesman_name`. The leaderboard `active_reps` set no longer includes departed reps.
  - **`_ensure_scorecards()` in `weekly_email_view.py`**: Same SALESMAN whitelist applied before calling `compute_rep_scorecards()` — per-rep email drafts and scorecard analytics only cover current SALESMAN table entries.
  - **Long-term DB fix**: The proper cleanup is to remove departed reps from `dbo.SALESMAN` **and** reassign their remaining accounts in `dbo.BILLSLMN` to a current rep. Until that DB update is made, this app-side SALESMAN whitelist prevents departed reps from surfacing anywhere in the app.
  - **30/30 tests pass.**

- **2026-05-21** — Fuzzy product matching, full product catalog, professional formatting in auto-reply:
  - **Root cause of "Win Win" failure**: `_fetch_management_data()` and `_fetch_rep_data()` both used `pc_cur.head(8)` for the PRODUCT × REP/ACCOUNT cross-tab. Any product outside the top 8 by revenue was completely invisible to the AI. The system prompt also instructed the AI to look in "PRODUCT × REP BREAKDOWN" — a section that only covered top-8 products — for named products, making the failure deterministic.
  - **`_extract_query_keywords(messages) -> list[str]`** — new static method on `_AiReplyWorker`. Extracts content words (3+ chars, non-stopword) from all inbound messages using `re.findall(r'\b[a-zA-Z]{3,}\b', ...)`. Returns up to 40 deduplicated candidate keywords for product name matching. Filters ~80 English stopwords + business email boilerplate terms to avoid false matches.
  - **`_find_matched_products(pc_all_codes, pc_lookup, keywords) -> list[str]`** — new static method. Scores all price class codes by how many keywords appear as substrings in their description (case-insensitive). Returns codes ordered by match score (most keywords first). For "win win", keywords ["win"] match "WIN WIN BROADLOOM COMMERCIAL", "WIN WIN RESIDENTIAL", etc.
  - **`_fetch_management_data()` overhauled**: (1) Computes all price class codes (not just top N). (2) Pre-computes **QUERY-MATCHED PRODUCTS** section — full per-rep revenue breakdown for every product matching any keyword from the conversation. This section appears FIRST and is clearly labeled as the priority source. (3) Adds **COMPLETE PRODUCT CATALOG** section listing every active product with YTD revenue and GP% for AI-side fuzzy search. (4) Expands PRODUCT × REP cross-tab from `head(8)` → `head(20)` products, top 5 reps each.
  - **`_fetch_rep_data()` overhauled**: Same pattern — QUERY-MATCHED PRODUCTS section (per-account breakdown for matched products), COMPLETE PRODUCT CATALOG, top products expanded to 15 shown + catalog for full coverage, PRODUCT × ACCOUNT cross-tab expanded from `head(8)` → `head(20)`.
  - **Management system prompt rewritten**: Added TERMINOLOGY section (product/rep/account/YTD/CC/GP definitions + "Win Win" example), PRODUCT SEARCH ORDER (check QUERY-MATCHED first → COMPLETE CATALOG partial match → report not found), ABSOLUTE RULES (NEVER fabricate, NEVER say CLARIFICATION NEEDED for product name lookup — only for genuinely ambiguous dates/metrics), PROFESSIONAL FORMATTING RULES (ASCII tables, right-align numbers, totals row, 300 word limit, plain text only).
  - **Root cause**: `dbo.SALESMAN` has NO active/terminated flag. A rep who leaves the company may still have stray BILLSLMN account entries. Because BILLSLMN is the source of truth for attribution, their sales appear under their name in every per-rep report (e.g. Steve Olink with 1 remaining account showing $513 of Win Win sales in the management auto-reply).
  - **Fix**: At every report generation point, load the current `dbo.SALESMAN` roster via `load_reps(db)` and build a `valid_rep_names: set[str]` whitelist (upper-cased). Any `salesperson_desc` not in the whitelist is filtered out before grouping. Also enforce `_EXCLUDED_REPS = {"", "house account", "(legacy / pre-aug 2025)"}` consistently.
  - **`_fetch_management_data()` in `conversations_view.py`**: Added `load_reps` to the import block. After loading `df_c` / `df_p`, applies `_is_valid_rep()` row-filter to both DataFrames before any groupby — so departed reps never appear in SALES BY REP, QUERY-MATCHED PRODUCTS, PRODUCT × REP BREAKDOWN, or COMPLETE CATALOG totals. Filter fails open (shows all reps) if the SALESMAN query errors.
  - **`_generate_master()` in `weekly_email_view.py`**: Replaced `_EXCLUDED_REPS`-only check with a `_rep_is_active()` helper that enforces both `_EXCLUDED_REPS` AND the SALESMAN whitelist built from `self._assignments_df.salesman_name`. The leaderboard `active_reps` set no longer includes departed reps.
  - **`_ensure_scorecards()` in `weekly_email_view.py`**: Same SALESMAN whitelist applied before calling `compute_rep_scorecards()` — per-rep email drafts and scorecard analytics only cover current SALESMAN table entries.
  - **Long-term DB fix**: The proper cleanup is to remove departed reps from `dbo.SALESMAN` **and** reassign their remaining accounts in `dbo.BILLSLMN` to a current rep. Until that DB update is made, this app-side SALESMAN whitelist prevents departed reps from surfacing anywhere in the app.
  - **30/30 tests pass.**

- **2026-05-21** — Fuzzy product matching, full product catalog, professional formatting in auto-reply:
  - **Root cause of "Win Win" failure**: `_fetch_management_data()` and `_fetch_rep_data()` both used `pc_cur.head(8)` for the PRODUCT × REP/ACCOUNT cross-tab. Any product outside the top 8 by revenue was completely invisible to the AI. The system prompt also instructed the AI to look in "PRODUCT × REP BREAKDOWN" — a section that only covered top-8 products — for named products, making the failure deterministic.
  - **`_extract_query_keywords(messages) -> list[str]`** — new static method on `_AiReplyWorker`. Extracts content words (3+ chars, non-stopword) from all inbound messages using `re.findall(r'\b[a-zA-Z]{3,}\b', ...)`. Returns up to 40 deduplicated candidate keywords for product name matching. Filters ~80 English stopwords + business email boilerplate terms to avoid false matches.
  - **`_find_matched_products(pc_all_codes, pc_lookup, keywords) -> list[str]`** — new static method. Scores all price class codes by how many keywords appear as substrings in their description (case-insensitive). Returns codes ordered by match score (most keywords first). For "win win", keywords ["win"] match "WIN WIN BROADLOOM COMMERCIAL", "WIN WIN RESIDENTIAL", etc.
  - **`_fetch_management_data()` overhauled**: (1) Computes all price class codes (not just top N). (2) Pre-computes **QUERY-MATCHED PRODUCTS** section — full per-rep revenue breakdown for every product matching any keyword from the conversation. This section appears FIRST and is clearly labeled as the priority source. (3) Adds **COMPLETE PRODUCT CATALOG** section listing every active product with YTD revenue and GP% for AI-side fuzzy search. (4) Expands PRODUCT × REP cross-tab from `head(8)` → `head(20)` products, top 5 reps each.
  - **`_fetch_rep_data()` overhauled**: Same pattern — QUERY-MATCHED PRODUCTS section (per-account breakdown for matched products), COMPLETE PRODUCT CATALOG, top products expanded to 15 shown + catalog for full coverage, PRODUCT × ACCOUNT cross-tab expanded from `head(8)` → `head(20)`.
  - **Management system prompt rewritten**: Added TERMINOLOGY section (product/rep/account/YTD/CC/GP definitions + "Win Win" example), PRODUCT SEARCH ORDER (check QUERY-MATCHED first → COMPLETE CATALOG partial match → report not found), ABSOLUTE RULES (NEVER fabricate, NEVER say CLARIFICATION NEEDED for product name lookup — only for genuinely ambiguous dates/metrics), PROFESSIONAL FORMATTING RULES (ASCII tables, right-align numbers, totals row, 300 word limit, plain text only).
  - **Rep system prompt rewritten**: Same structure — terminology, product search priority, CLARIFICATION NEEDED restricted to genuinely ambiguous requests, professional ASCII table formatting, 100-220 word limit.
  - **30/30 tests pass.**

- **2026-05-20 (latest)** — Budget view: 4-digit CC filter + rep total budget upload with proportional scaling:
  - **4-digit CC codes eliminated**: `_on_loaded` in `budget_view.py` now filters `prior_df` to rows matching `^0\d{2}$` (exactly 3-char product CCs) before storing `self._prior_df`. The `all_product_ccs` set is also filtered with `len(str(cc)) == 3`. Codes like `0122` no longer appear in the CC growth table or downloads.
  - **`parse_rep_budget_upload(path)`** added to `app/services/budget_service.py`: reads CSV/Excel with `salesman_number` + `full_budget` columns; normalizes rep numbers (strip leading zeros); handles `$`/`,` formatting in budget values; rejects negatives; returns `({rep_number: float}, errors)`.
  - **`apply_rep_budget_targets(rows_by_rep, rows_by_cc, rows_by_acct, rep_budget_targets)`** added to `app/services/budget_service.py`: scales all three row lists in-place so each rep's budget totals match the uploaded target. Algorithm: sum current `budget_full_year` per rep → compute `scale = target / current` → multiply rep and account rows; rebuild CC rows by summing scaled rep rows per CC. No-op when target dict is empty or rep has zero current budget.
  - **`BudgetConfig.rep_budget_targets_saved`** field added to `AppConfig`: persists uploaded targets across sessions (JSON-serializable `dict[str, float]`, key = rep_number).
  - **"Rep Total Budget (Upload)" card** added to `_SettingsPanel` in `budget_view.py`: green spec box, Upload / Download Template / Clear buttons, preview table, status label. Targets persist to config on upload and are restored on app launch via `_load_saved_overrides()`.
  - **`_recompute()` updated**: calls `apply_rep_budget_targets()` after computing base rows but before `add_ytd_actuals()` — so YTD vs-budget calculations use the scaled numbers.
  - **`budget_targets_applied` signal** added to `_SettingsPanel`; connected to `_on_budget_targets_applied()` in `BudgetView` which auto-recomputes when targets are uploaded/cleared.
  - **30/30 tests pass** (4 new: `test_parse_rep_budget_upload_basic`, `test_parse_rep_budget_upload_normalises_rep_and_skips_bad`, `test_apply_rep_budget_targets_scales_all_three_levels`, `test_apply_rep_budget_targets_zero_current_is_noop`).

- **2026-05-19 (latest)** — Core display coverage made CC-specific; AI/footer conditional:
  - **Root cause**: `compute_rep_scorecards` flattened ALL configured core displays from ALL CCs into one `flat_core` set, so choosing CCs 031/032 (which have no core displays configured) still used CC 010's display codes and produced spurious coverage numbers and coaching text.
  - **`app/services/manager_analytics.py`**:
    - `RepScorecard` gains `core_display_configured: bool = True` — set to `False` when the selected CCs have no core-display configuration.
    - `compute_rep_scorecards` gains `selected_ccs: list[str] | None = None`. When provided, `core_displays_by_cc` is filtered to only those CCs before computing `flat_core`. If the selected CCs have entries in `core_displays_by_cc` → normal coverage computation. If the selected CCs have **no** entries → `core_configured_for_scope = False`, coverage left at 0, and a note is appended ("Core-display coverage is not configured for the selected product lines — this metric is omitted from the email."). If `selected_ccs=None` (all-scope) and nothing configured → the existing "any display" fallback is preserved.
  - **`app/ui/views/weekly_email_view.py`**:
    - `_ensure_scorecards` passes `selected_ccs=self.filter_bar.selected_codes() or None` to `compute_rep_scorecards`.
    - `_scorecard_footer_html`: core-display `<li>` is only rendered when `sc.core_display_configured` is `True`; otherwise the line is omitted entirely.
    - `_build_rep_prompt` user_msg: `accounts_with_core_displays` and `core_display_coverage_pct` lines are only included when `sc.core_display_configured`. Sys_msg coaching insight examples and GOOD EXAMPLES bullet for core displays are also gated on `scorecard.core_display_configured`.
  - **26/26 tests pass.**

- **2026-05-18 (latest)** — Management flag for auto-reply whitelist (full company data access):
  - **`EmailConfig.auto_reply_management_emails: list[str]`** added to `app/config/models.py`. Management senders receive auto-reply with FULL company-wide data (all reps, all territories). They do NOT need to be in the regular `auto_reply_whitelist` — management addresses are implicitly whitelisted.
  - **Email Settings → Auto-Reply tab extended**: Below the existing "Sales Reps" whitelist, a new green-tinted "Management — Full Data Access" section allows adding executive/manager email addresses. Has its own Add / Remove buttons and input field. `_collect()` now persists `auto_reply_management_emails` alongside the existing whitelist.
  - **`_AiReplyWorker._is_management_sender()`** — checks `conv.rep_id` and all inbound `from_address` values against `cfg.email.auto_reply_management_emails`. Returns `True` if the thread was initiated by a management address.
  - **`_AiReplyWorker._fetch_management_data()`** — loads ALL invoiced sales across ALL reps and territories (no rep filter). Returns: company-wide YTD vs prior YTD totals, SALES BY REP table (all reps sorted by revenue with YoY + GP%), TOP PRODUCTS COMPANY-WIDE (top 15 with YoY + GP%), PRODUCT × REP BREAKDOWN (top 5 reps per top 8 product). All computed from same cached DataFrames, no extra SQL beyond what rep mode loads.
  - **`_generate()` branched on `is_mgmt`**: Management senders get `_fetch_management_data()` + a separate system prompt that (a) allows comparing reps by name, (b) uses PRODUCT × REP BREAKDOWN for product-specific questions, (c) targets up to 300 words. Rep senders get the existing per-territory flow.
  - **Whitelist gates updated**: Both `_ImapPollWorker.run()` and `_on_new_conv_ids()` now build `all_eligible = whitelist | management_set` and check against that combined set. Management emails can initiate new conversations without being in the rep whitelist.
  - **Status bar updated**: Shows e.g. "Auto-reply active for 3 rep(s), 1 management — checking every 2 min." when both lists have entries.
  - **30/30 tests pass.**
  - **Root cause**: `_AiReplyWorker._generate()` only fetched data for account numbers explicitly mentioned in the conversation text. For any fresh rep email with no account numbers (e.g. "what are my top products YTD?"), `_fetch_account_data()` returned empty and the AI invented plausible-looking but entirely fake tables (e.g. "Residential Carpet A … $1,250,000").
  - **`_fetch_rep_data()` added** (new method on `_AiReplyWorker`): loads the rep's full FY YTD warehouse dashboard unconditionally — before considering account numbers. Steps: (1) resolve `conv.rep_id` to a salesman_number via `load_rep_assignments()` or the `cfg.rep_emails` reverse map; (2) load `load_invoiced_sales()` for current FY YTD and mirror period in prior FY; (3) filter to the rep's BILLSLMN-assigned accounts; (4) compute YTD revenue vs prior YTD, top 15 products by revenue with prior-year comparison and GP%, top 20 accounts with prior-year comparison, and a **product × account cross-tab** (top 5 accounts per top 8 product — so the AI can answer "show me Win Win by account" questions directly); (5) load `load_price_class_lookup()` for human-readable product names. Returns structured plain-text table block or `""` on any error (never crashes).
  - **`_fetch_account_data()` renamed → `_fetch_account_detail()`**: still supplements the rep dashboard with period-by-period detail when specific account numbers appear in the conversation thread.
  - **`_generate()` updated**: calls `_fetch_rep_data()` first (always), then `_fetch_account_detail()` for any mentioned accounts, combines into `warehouse_block` and passes to prompt. When no data can be loaded, an explicit warning tells the AI to report the failure rather than guess.
  - **System prompt hardened** with CRITICAL RULES: "NEVER invent, estimate, or fabricate any number." Product-specific questions: AI is instructed to use the PRODUCT × ACCOUNT BREAKDOWN section. "By rep" questions: AI explains it only has the current rep's territory data and immediately pivots to the product × account table — never just refuses. Temperature lowered from 0.35 → 0.2 for factual consistency.
  - **30/30 tests pass.**

- **2026-05-18 (latest)** — Auto-reply whitelist (per-address pass-through control):
  - **`EmailConfig.auto_reply_whitelist: list[str]`** added to `app/config/models.py`. An empty list means auto-reply is **inactive for everyone** — you must explicitly add an email address before any rep gets an auto-reply. Persisted to `config.json`.
  - **Email Settings → Auto-Reply tab** added to `EmailSettingsDialog`: polished `QListWidget` of whitelisted addresses (alternating rows, rounded border, blue selection), inline `QLineEdit` + Add / Remove Selected buttons, Return-key shortcut on the input field, automatic lowercase normalization, deduplication guard, and a descriptive hint about checking the From: header. `_collect()` now includes `auto_reply_whitelist=self._collect_whitelist()` so the list is saved alongside SMTP/IMAP settings.
  - **`_on_new_conv_ids` in `conversations_view.py`** now builds a case-insensitive whitelist set from `self._cfg.email.auto_reply_whitelist` and skips `_fire_auto_reply` for any rep whose email is not in the set. Log messages distinguish between "whitelist is empty" and "address not in whitelist" for easy debugging.
  - **Status bar message** updated: when IMAP + SMTP + AI are all configured but whitelist is empty, shows "Auto-reply paused — add rep email addresses in Email Settings → Auto-Reply tab to activate." When addresses are present, shows "Auto-reply active for N whitelisted address(es) — checking every 2 min."
  - **`ORDER_SHIP_DATE` documented**: `dbo._ORDERS.ORDER_SHIP_DATE` (numeric YYYYMMDD) is the ship date for uninvoiced/future orders. Filter `INVOICE# = 0 AND ACCOUNT#I > 1` to query pending shipments by date range. Added to CLAUDE.md section 5a.
  - **Orphan inbound emails from whitelisted reps now captured**: Previously only emails that were replies to AI-originated threads were matched; any email sent directly to the inbox (new thread, not a reply) was silently dropped. Fixed in `_ImapPollWorker.run()`: when `find_conversation_for_reply()` returns `None`, the sender's address is parsed and checked against the whitelist. If whitelisted, `create_conversation_for_inbound()` (new function in `repos.py`) creates a `topic='rep_initiated'` conversation with `thread_key=message_id` and records the inbound. The auto-reply then fires normally. Non-whitelisted orphans are still dropped.
  - **`create_conversation_for_inbound()`** added to `app/storage/repos.py`: idempotent (deduplicates on `message_id`), upserts the rep row, creates the conversation, saves the inbound message, returns a `Conversation`. `thread_key = message_id` ensures future AI replies are matched back via `In-Reply-To`.
  - **Rep email fallback chain** in `_AutoReplyWorker.run()` and `_on_new_conv_ids`: `cfg.rep_emails.get(rep_id)` → `rep_id` if it contains `@` (stored email-as-rep_id) → `from_address` of last inbound message.
  - **Poll interval reduced to 2 minutes** (was 5).
  - **30/30 tests pass.**

- **2026-05-18 (latest)** — L3M partial-window fix + auto-reply in Conversations:
  - **L3M prior-window detection tightened**: The previous fix only flagged `prior_3mo` data when `prior_3mo_end < filter_start` (the entire window is before the filter). In practice the prior window often starts before the filter but ends inside it — e.g. filter starts Feb 1 and prior_3mo covers Nov 17–Feb 16, giving only 16 days of overlap out of 91. This caused AI to cite "+523% surge" by comparing ~90 days vs ~16 days. Fix: condition changed to `if _p3m_s < start:` (prior_3mo **start** before filter_start), which correctly catches partial overlaps. The dollar figure is now completely suppressed (replaced with `DATA NOT LOADED — window predates the filter; use yoy_3mo instead`) so the AI cannot compute a percentage. NEVER WRITE prohibition tightened: flag is now `DATA NOT LOADED`, not `⚠ OUTSIDE filter window`.
  - **`_AutoReplyWorker(QThread)`** added to `conversations_view.py`: subclass of `_AiReplyWorker` that overrides `run()` to generate the AI reply, then immediately send it via SMTP and record it in SQLite — no compose dialog, no button click needed. Emits `replied(str)` on success, `send_error(str, str)` on failure. Skips send if SMTP is not configured or rep has no email on file.
  - **AI system prompt updated for auto-reply**: Added `CLARIFICATION NEEDED:` protocol — if the rep's request is ambiguous or context is insufficient, the AI must start its reply with exactly `"CLARIFICATION NEEDED:"` and ask ONE clarifying question instead of attempting an answer with insufficient data.
  - **`_ImapPollWorker` upgraded**: Now also emits `new_conv_ids = Signal(object)` (list of `int` conversation IDs that received new inbound messages this cycle), in addition to the existing `found(int)` count signal.
  - **Background auto-poll timer** added to `ConversationsView.__init__`: A `QTimer` fires every 5 minutes when IMAP + SMTP + AI are all configured (`self._auto_reply_enabled`). Uses `_auto_poll_cycle()` which skips if a poll is already running.
  - **`_on_new_conv_ids` handler**: After each poll, for every conversation ID that received a new inbound message, checks if the conversation `needs_reply`, looks up the rep's email, and fires `_fire_auto_reply(conv, messages)`. Logs a warning and skips if no email is on file.
  - **Status bar updated**: Shows "Auto-reply active — checking every 5 min." when fully configured; "Auto-reply disabled (configure SMTP, AI provider to enable)." otherwise. After each poll cycle updates to "✓ Auto-replied to {rep} at HH:MM" or "Auto-reply active — last checked HH:MM" when no new replies.
  - **30/30 tests pass.**

- **2026-05-18 (latest)** — L3M bug fix, explicit dates, per-rep trend chart:
  - **Root cause fixed: spurious "453% L3M surge"** — `prior_3mo_by_rep` was sliced from the current-year `sales` DataFrame, but when the default fiscal YTD filter covers Feb–May the "prior 3 months" window (Nov–Feb) falls entirely outside that range, returning $0 for every rep. The 453% figure was noise (any revenue vs $0). Fix: added `last_3mo_start`, `last_3mo_end`, `prior_3mo_start`, `prior_3mo_end` fields to `RepScorecard`; the user_msg L3M block now labels each window with exact dates and flags the prior_3mo with `⚠ OUTSIDE filter window — treat as unreliable; use yoy_3mo instead` when `prior_3mo_end < filter_start`. The AI also receives an explicit instruction to ignore flagged L3M values.
  - **Scorecard footer updated**: `last_3mo_vs_prior_3mo_pct` (unreliable) replaced with `last_3mo_yoy_pct` (same 90 days last year), labeled "90-day momentum (Feb 17–May 16) vs prior year".
  - **"This period" eliminated**: stale_lines and new_lines format strings now use explicit `({start_label}–{end_label})` and `({prior_start_label}–{prior_end_label})`. Added to NEVER WRITE: `"this period"`, `"current period"`, `"previous period"`, `"prior period"`, `"recent months"`, `"recent period"`. AI is instructed to always write exact date ranges.
  - **Per-rep trend chart added** (`_generate_trend_chart_html`): new module-level function generates a matplotlib chart embedded as a base64 PNG `<img>` tag inside each per-rep email. Two lines on one chart: (1) dashed gray — all reps combined cumulative YTD % vs prior year; (2) blue solid — this rep's cumulative YTD % vs prior year. X-axis: fiscal weeks in the filter window, labeled by month. Y-axis: `+/- %`. When prior-year data is unavailable, falls back to a bar chart of raw weekly revenue. Chart rendered in non-interactive `Agg` backend; errors are silently caught and return `""` so email generation never blocks. `matplotlib` installed to venv.
  - **`_wrap_ai_body`** gains `chart_html: str = ""` parameter; chart is injected between the AI body text and the scorecard footer.
  - **`_apply_draft_text`** calls `_generate_trend_chart_html(self._df, self._prior_df, rep_key, fb_start, fb_end)` and passes `chart_html` to `_wrap_ai_body`.
  - **30/30 tests pass.**

- **2026-05-18** — Analytical AI framework for weekly emails (per-rep + master BI report):

  - **`_AiReplyWorker(QThread)`** new class: drafts a data-rich 100–220 word reply using fresh warehouse data, fulfilling the rep's specific request. Temperature 0.35. `get_db` passed from `main_window.py`.
  - **`_ReplyComposeDialog(QDialog)`** new class: polished compose window with readonly To/Subject, collapsible thread history, editable draft body, Send Reply button (dispatches via SMTP with `In-Reply-To` header, then calls `save_message()` on the **existing** conversation — not `record_send()` — so `needs_reply` is cleared correctly).
  - **AI system prompt** restricted to data-only service offers (no scheduling calls, meetings, or check-ins).
  - **Needs Review tab redesigned** with `QSplitter` + "✨ Draft AI Reply" + "Mark as replied (manual)" buttons.
  - **`request_timeout_seconds`** fix: `_AiReplyWorker._generate()` was referencing the non-existent `cfg.ai.timeout_seconds`; corrected to `cfg.ai.request_timeout_seconds`.
  - **Orphaned ~480 lines** of old `ConversationsView` body removed from `conversations_view.py`.
  - **26/26 tests pass.**

- **2026-05-15 (latest)** — Conversations view fully wired; IMAP reply detection:
  - **Root cause fixed**: Emails sent via `_SendWorker` / `_SendReviewDialog` were never written to SQLite — the Conversations view showed "No conversations yet" even after real emails had been sent. Fix: `_SendWorker.run()` now calls `record_send()` after every successful SMTP send, creating the conversation row (idempotent via `INSERT OR IGNORE`) and recording the outbound message.
  - **`app/storage/repos.py`** — four new helper functions:
    - `upsert_rep(salesman_number, name, tone)` — idempotent rep upsert that does NOT overwrite user-configured email/boss_email/tone.
    - `record_send(...)` — combines `upsert_rep` + `INSERT OR IGNORE` conversation + `save_message(outbound)` in one call. Returns the conversation id.
    - `find_conversation_for_reply(in_reply_to, references)` — matches an inbound email to an existing conversation by checking `messages.message_id` and `conversations.thread_key` against each Message-ID in the headers.
    - `record_inbound(...)` — deduplication-safe inbound message save (checks `message_id` and `imap_uid` before inserting).
  - **`app/notifications/email_client.py`** — `fetch_new_replies()` method: connects to IMAP via UID SEARCH UNSEEN, fetches messages with `BODY.PEEK[]` (preserves unread status), decodes RFC 2047 subjects, extracts text + HTML bodies, returns list of dicts. `_extract_body()` static helper handles multipart and single-part messages.
  - **`app/ui/views/weekly_email_view.py`** — `_SendWorker` updated:
    - Constructor gains `from_address: str = ""` parameter.
    - `run()` calls `record_send()` for each successfully sent per-rep email (skips the master leaderboard which has no `salesman_number`).
    - `_SendReviewDialog._send()` passes `from_address=cfg.email.smtp_from_address`.
  - **`app/ui/views/conversations_view.py`** — complete overhaul:
    - `ConversationsView.__init__` now accepts `cfg: AppConfig`; `main_window.py` updated accordingly.
    - `_ImapPollWorker(QThread)` class: background IMAP poll → `find_conversation_for_reply` → `record_inbound` per matched message; emits `found(count)` / `error(str)` / `done`.
    - **"🔄 Check for new replies" button** added above the tab widget. Disabled with tooltip when IMAP is not configured; shows "Checking inbox…" while running; updates to "✓ N new replies saved." on completion and auto-refreshes the list.
    - **Thread view** now prefers `body_html` over `body_text` so emails render richly; falls back to escaped plain text. Added subtle colour-coded border per message direction.
    - **`_mark_replied`** now uses `cfg.rep_emails.get(conv.rep_id)` for `to_address` instead of the salesman number; uses `smtp_from_address` for `from_address`.
    - **Tab badge colour** uses `QColor("#DC2626")` (red) / `QColor()` (theme default) instead of the hardcoded `Qt.GlobalColor.black` which rendered incorrectly on dark backgrounds.
  - **26/26 tests pass.**

- **2026-05-18 (latest)** — AI Reply in Conversations view; Needs Review tab redesign:
  - **`_AiReplyWorker(QThread)`** new class in `conversations_view.py`:
    Signals `draft_ready(str)` / `error(str)` / `done()`. Pulls the full
    conversation history, extracts account numbers via regex from all messages,
    fetches fresh month-by-month warehouse data (Jan prior yr → today) from
    `load_invoiced_sales` + `load_rep_assignments` for those accounts, then
    calls the configured AI provider to draft a 100–220 word data-rich reply
    fulfilling the rep's specific request. Temperature 0.35 for factual
    consistency. `get_db` is passed in from `main_window.py` (same pattern
    as all other views).
  - **`_ReplyComposeDialog(QDialog)`** new class: polished compose window
    with readonly To/Subject header, collapsible thread history (`QTextBrowser`
    170 px, toggleable via ▼/▶ button), editable draft body (`QTextEdit`),
    status label, Cancel + "✉ Send Reply" buttons. `Send Reply` dispatches
    via `EmailClient.send()` with proper `In-Reply-To` header (last inbound
    message-id) for email-client threading, then calls `record_send()` to
    log the outbound message. Emits `sent` signal → triggers `refresh()`.
    Send button disabled with tooltip when SMTP not configured.
  - **`_render_thread_html(messages)`** module-level shared helper (replaces
    inline duplicated HTML building in both tabs and the old `_load_thread`
    method). Inbound messages: blue (`#EFF6FF` bg / `#BFDBFE` border); outbound:
    green (`#F0FDF4` bg / `#BBF7D0` border). Prefers `body_html`, falls back
    to escaped `body_text`.
  - **Needs Review tab completely redesigned** — old layout (list + small
    detail panel below + single "Mark as replied" button) replaced with a
    `QSplitter` (list left, full thread right) mirroring the All Conversations
    tab. Two action buttons: primary blue **"✨ Draft AI Reply"** (launches
    worker + compose dialog) and secondary **"Mark as replied (manual)"**.
    Thread auto-scrolls to bottom so the most recent reply is immediately
    visible. Both buttons are disabled until a conversation is selected; AI
    button further disabled if AI provider not configured.
  - **Old orphaned ConversationsView body removed** — previous session left
    ~480 lines of dead old `__init__` code and all old methods trailing after
    the new class. Cleaned up.
  - **`main_window.py`** updated: `ConversationsView(self._cfg)` →
    `ConversationsView(self._cfg, get_db=lambda: self._cfg.database)`.
  - **Help view updated**: Conversations section rewritten to document AI
    Reply workflow (4-step numbered list), compose dialog, manual fallback,
    and IMAP poll button.
  - **26/26 tests pass.**

- **2026-05-17 (latest)** — Leaderboard exclusions, HTML clipboard, email send, shoutout polish:
  - **`_EXCLUDED_REPS` constant**: `frozenset({"", "house account", "(legacy / pre-aug 2025)"})` — blank rep names, HOUSE ACCOUNT, and the legacy pre-Aug-2025 synthetic rep are excluded from `active_reps` before the leaderboard is built, so they never appear in the standings table, shoutout sections, or improvement calculations.
  - **"Copy leaderboard" now copies rich HTML** via `QMimeData.setHtml()`. Outlook and Gmail accept `text/html` clipboard data and render the table with proper proportional-font alignment — no more misaligned columns. Plain text is still set as a fallback via `QMimeData.setText()`.
  - **"📧 Email leaderboard" button** added to the actions row (enabled only when master leaderboard is selected). Opens a `QInputDialog` asking for a To: address, then sends the leaderboard HTML directly via `EmailClient.send()`. Requires SMTP to be configured and `enable_outbound_send = True`.
  - **Shoutout italic quotes removed**: The italic call-out sentences beneath each rep's sales figures in the HTML shoutout boxes have been removed. Shoutouts now show only the medal + name + value — clean and scannable. Plain-text shoutout blocks similarly have no quote lines.
  - **Shoutout prompts cleaned up**: Percentages removed from AI shoutout context (`l3mo %` removed from weekly bullets; `%` strings forbidden in sys_msg). Prompts now instruct the AI to mention account names and dollar amounts. "Most Improved" fallback text says "building solid momentum" without a % sign.
  - **Inline `from PySide6.QtCore import QTimer`** removed from `_copy_leaderboard` (was orphaned; `QTimer` now imported at module top).
  - **`QTimer` and `QMimeData` added to top-level imports**; `QInputDialog` and `QLineEdit` added for the email dialog.
  - **26/26 tests pass.**

- **2026-05-17** — Email sending, structured leaderboard, budget persistence:
  - **Send Review dialog** — `_queue()` now opens `_SendReviewDialog` instead of a static summary. The dialog shows all drafts in a scrollable checklist (pre-checked for reps with email addresses on file), with a live preview panel on the right. Supports Select All / Deselect All. "Send Selected (N)" button dispatches via `EmailClient.send()` in a background `_SendWorker` QThread. Per-row status shows ✓ Sent or ✗ Failed (with error tooltip). Works for per-rep drafts and the master leaderboard item. Disabled (amber warning) if SMTP is not configured or `enable_outbound_send` is False.
  - **Leaderboard clipboard format redesigned**: Shoutout sections now each use a mini-table (rank | rep | sales, then quotes on wrapped indented lines below). "Most Improved" section shows a three-column mini-table (Now/Wk | Prev/Wk | +/-/Wk). Main standings table uses dynamic column widths, `═` heavy rules for section headers, `─` light rule below header row. All sections clearly delineated. Renders cleanly when pasted into Outlook/Gmail with proportional fonts.
  - **Budget upload persistence** — `BudgetConfig.rep_cc_growth_pct_saved` field added (JSON-serializable nested dict: rep_number → cc → pct). When a CSV/Excel upload is applied in the budget settings panel, overrides are saved to `config.json` via `save_config()`. On next launch, `_SettingsPanel._load_saved_overrides()` restores them automatically — no re-upload needed.
  - **Help view updated**: Weekly Email section revised to document the Send Review dialog and the new "Copy leaderboard" plain-text format. Budget section notes that uploads are saved and restored automatically.
  - **26/26 tests pass.**

- **2026-05-17** — Outbound status fix + leaderboard clipboard format:
  - **"Outbound disabled" message fixed**: The `ViewHeader` subtitle and `_queue()` body now read `cfg.email.enable_outbound_send` (was incorrectly `cfg.enable_outbound_send`, causing an AttributeError crash on launch). When the flag is `True`, the header says "Outbound sending is enabled" and the queue pane shows a green confirmation note.
  - **Dynamic "Most Improved" shoutout section**: Replaces the static "Fiscal YTD Avg" comparison with a mode that adapts to the fiscal calendar position:
    - **First week of new period** → Fiscal YTD Avg/Wk vs prior YTD avg (unchanged from before).
    - **Mid-month weeks** → Fiscal MTD Avg/Wk vs prior year same MTD avg.
    - **Last week of a non-quarter period** → Completed fiscal month avg vs same month prior year.
    - **Last week of a quarter-end period (P3, P6, P9, P12)** → Completed fiscal quarter avg vs same quarter prior year.
    - All comparisons use weekly averages (total / weeks) so old-system monthly data and new-system daily data are treated identically.
    - `_compute_improvement_metrics()` standalone function encapsulates the logic. Tested by 4 new smoke tests (first_week, month_end, quarter_end, mtd_avg). 
  - **Plain-text leaderboard restructured**: Shoutouts now appear FIRST (before the table), each with the rep name, dollar value, and AI quote on separate indented lines. The table uses dynamic name-column width, `═` top rule, `─` body rules, clean labeling ("This Week | YTD Avg/Wk | Prev YTD Avg"), and a simple footer note. Date formatting uses `%b %#d, %Y` (Windows-compatible). Result pastes cleanly into Outlook/Gmail with proportional fonts.
  - **26/26 tests pass.**

- **2026-05-17** — Master leaderboard overhaul:
  - **Three columns** — "Weekly Sales", "Fiscal YTD Avg/Wk", "Prev FY YTD Avg/Wk" (replaces old "Last week" + "Week to date").
  - **Weekly column logic** — if today is Friday (4) or Saturday (5): use the current in-progress week; Monday–Thursday: use the last full week (Sun–Sat). Anchored to `_anchor_date()` as before.
  - **Fiscal YTD Avg/Wk** — total revenue for the rep in `_df` divided by `weeks_elapsed = max(1.0, (fb_end - fb_start).days / 7)` where `fb_start, fb_end = filter_bar.date_range()`.
  - **Prev FY YTD Avg/Wk** — same calculation against `_prior_df` for `(fb_start.year-1, fb_end.year-1)`.
  - **Exclusion rule** — reps where BOTH ytd avg ≤ 0 AND prior ytd avg ≤ 0 are omitted from the table.
  - **Totals row** at the bottom of the table for all three numeric columns.
  - **Shoutout sections** (before the table):
    - "⭐ Top 3 This Week" — top 3 by weekly revenue, AI-generated (or fallback) one-liner each.
    - "📈 Most Improved vs Prior FY YTD (Avg/Week)" — top 3 by (current_ytd_avg − prior_ytd_avg), AI-generated (or fallback) one-liner each.
  - **`_ai_shoutouts` rewritten** — now accepts `category="weekly_top"|"ytd_improvement"` and optional `ytd_avg`/`prior_ytd_avg` dicts for context. Returns `dict[str, str]` (rep name → shout-out sentence).
  - **`_render_master_html` now returns `(html, plain_text)`** — plain text is a formatted ASCII table plus shoutout sections, ready to paste into an email.
  - **"📋 Copy leaderboard" button** added to actions row. Enabled only when master leaderboard is selected; copies `plain_text` from the draft dict to the clipboard. Shows "✓ Copied!" for 2 seconds then resets.
  - **`_show_selected`** updated to enable/disable the copy button based on whether the master leaderboard item is selected.
  - **22/22 tests pass.**

- **2026-05-17** — Per-page filter defaults + relative date options:
  - **`PageFilterDefault` model** added to `AppConfig` in `app/config/models.py`:
    `start_relative`, `end_relative` (relative-date tokens), `start_iso`, `end_iso` (ISO fallbacks), `cost_centers`, `vs_prior_year`. Persisted under `page_defaults: dict[str, PageFilterDefault]` keyed by page_id.
  - **`SalesFilterBar` new `page_id` param**: When provided, a "⭐ Save as default" button appears. Clicking it persists the current filter state (relative tokens when applicable, ISO dates otherwise) via `save_config(cfg)`. A confirmation label shows the saved range.
  - **Relative date picker (▾ button)** added next to each `QDateEdit`. Clicking shows a `QMenu` with 11 practical options: Today, Yesterday, 1 week ago, Start of this month, 1 month/3 months/6 months ago, Start of calendar year, Start of fiscal year, Start/End of last full fiscal month.
  - **`resolve_relative_date(token, six_week_january_years) → date`** module-level helper in `sales_filter_bar.py`. Tokens re-evaluate fresh on every app load so "yesterday" always means yesterday.
  - **Auto-load on init**: If `page_id` is set and a `PageFilterDefault` exists in config, it is applied immediately after widgets are built — resolving relative tokens fresh.
  - **`_run()` re-resolves** any active relative tokens each time Run fires.
  - **Presets and `apply_filters()`** clear relative tokens (they set absolute dates).
  - **`page_id` wired in all 4 views**: `ask_ai`, `sales_by_cc`, `sales_by_rep`, `weekly_email`.
  - **22/22 tests pass.**

- **2026-05-16 (latest)** — Ask AI deep-dive quality + weekly email period clarity:
  - **Ask AI output token limit raised**: `_AskWorker` now uses `max(4096, cfg.ai.max_output_tokens)` for Ask AI requests. Weekly email drafts continue using the config value. `_AI_CHAT_MIN_OUTPUT_TOKENS = 4096` constant.
  - **SYSTEM_PROMPT rewritten** for highest-quality analysis: explicit rules to weight toward large sample sizes (not one-off outliers), require time periods on ALL sales figures (`'$25,239 (Feb–Apr 2025) → $12,548 (Feb–Apr 2026)'`), use account names + bank numbers (`ABC FLOORING (#50342)`), use price class descriptions not codes, lead with highest-impact findings, find correlations, no fluff.
  - **Account names in aggregates**: `_format_aggregates` now accepts `acct_lookup: dict[str, dict]`. Loaded lazily in `_ask()` via `load_rep_assignments`. `by_account` table now shows `Account Name (#old) [new_acct]` labels.
  - **Price class descriptions in CSV**: `_ask()` adds a `price_class_desc` column to the enriched DataFrame copy sent to the AI. Also includes a `PRICE CLASS REFERENCE` section (code → description) in the user_msg.
  - **Account info + price class lookup cached** on `AIChatView` (`self._pc_lookup`, `self._acct_lookup`) — loaded once on first ask, reused thereafter.
  - **`get_db` stored on AIChatView** (`self._get_db`) so lookups can be loaded on demand.
  - **Weekly email sys_msg**: added explicit rule: "When citing a sales figure for an account, ALWAYS show BOTH periods: '$25,239 (Feb–Apr 2025) → $12,548 (Feb–Apr 2026)'. Never mention just one dollar amount without its time period." Also added: "Use PRODUCT DESCRIPTIONS (e.g. 'Carpet Residential') NOT 6-character price class codes."
  - **22/22 tests pass.**

- **2026-05-16** — Weekly email: new 5-section AI prompt structure; account name enforcement; fallback body rewrite:
  - **`_build_rep_prompt` sys_msg completely rewritten** with a new 5-section structure that replaces the old HIGHLIGHT/LOWLIGHT format:
    1. **QUICK SCOREBOARD** (3–5 short bullets): weekly sales vs prior week, period YoY, top product line, ranking movement.
    2. **BIGGEST WIN** (2–3 sentences): one specific success story with account name + number and a dollar figure or trend.
    3. **BIGGEST OPPORTUNITY** (2–3 sentences): one actionable opportunity — stale account, display gap, product gap — always names the account.
    4. **COACHING INSIGHT** (1–2 sentences): one intelligent correlation or behavioral pattern from the data (e.g. display accounts outperforming non-display accounts).
    5. **THIS WEEK'S FOCUS**: ONE simple action. Struggling reps get a firm expectation; performing reps get an opportunity/momentum play. Tier logic preserved via `is_struggling`.
    - Optional **SERVICE OFFER** (1 line) if a specific data question warrants a deeper pull.
  - **Account label enforcement**: sys_msg now contains a hard rule: "ALWAYS pair account numbers with the account name when mentioning accounts (#1234 · ABC FLOORING or ABC FLOORING (#1234)). Never cite a number alone." `format_account_label` already produces this format — the new instruction ensures the AI respects it.
  - **Stale accounts block**: fixed "prior period" to use explicit date labels (`prior_start_label`–`prior_end_label`), consistent with the no-vague-dates rule.
  - **`_fallback_body` rewritten** to mirror the new 5-section structure (QUICK SCOREBOARD → BIGGEST WIN → BIGGEST OPPORTUNITY → COACHING INSIGHT → THIS WEEK'S FOCUS) so the no-AI fallback is structurally consistent with AI-generated emails.
  - **Orphaned code removed**: `closing_instruction` variable (formerly used to separate struggling vs performing closing sections) removed; tier logic is now embedded inline inside the `section5_instruction` block within the new sys_msg.
  - **22/22 tests pass.**

- **2026-05-15 (latest)** — Ask AI full-dataset + blunt tone; weekly email HIGHLIGHT/LOWLIGHT structure; dashboard KPIs wired; conversations view with reply queue; help section:
  - **Ask the AI**: Removed 1500-row cap — full DataFrame CSV is now sent to the AI. `estimate_df_tokens` updated accordingly. New KPI card shows estimated token cost at gpt-4.1 pricing ($2/1M input tokens). System prompt rewritten to be blunt and direct: "Call out underperformers by name. Say clearly when a trend is bad, not just 'there is room for improvement'."
  - **Weekly email new structure**: Every email now starts with `HIGHLIGHT:` (best result, real numbers) then `LOWLIGHT:` (biggest concern), followed by FOCUS AREAS (2-3 bullets), closing action items or opportunities, and a `SERVICE OFFER` section where the AI offers a deeper data pull (e.g. "Want a month-by-month breakdown of carpet sales at #1234 since Jan 2026? Reply YES"). Target length 150-250 words (was 200-350). All date references are explicit (e.g. "February 2026–April 2026") — "previous period" is forbidden.
  - **"Price class" → "product" everywhere reps can see**: In the AI prompt and rendered email HTML, "TOP PRICE CLASSES" renamed to "TOP PRODUCTS BY REVENUE". Product descriptions only shown (internal code hidden). "declining price class" in service-offer text → "declining product line". A new **Product lines** badge (📊) now appears at the top of every email showing the cost center names (e.g. "CARPET RESIDENTIAL · CARPET COMMERCIAL"), not raw codes. Human-readable CC label built in `_generate_all` from the CC selector's loaded DataFrame. `cc_label` stored in draft dict and piped through `_apply_draft_text` → `_wrap_ai_body` as a new `cc_label` keyword arg.
  - **Dashboard KPIs wired**: `_DashboardLoader` now calls `dashboard_counts()` from `app.storage.repos` and wires the result to the Active Conversations, Open Action Items, and Needs Review KPI cards. All three show real data from SQLite on every refresh.
  - **`app/storage/repos.py` expanded**: `Conversation`, `Message`, `ActionItem` dataclasses added. Full CRUD: `list_conversations` (with `needs_reply` computed via SQL subquery), `get_conversation`, `list_messages`, `list_action_items`, `resolve_action_item`, `save_conversation`, `save_message`, `save_action_item`. `dashboard_counts()` returns `{active_conversations, open_action_items, needs_review}`.
  - **ConversationsView fully implemented** (was a placeholder): 3-tab QTabWidget — (0) All Conversations with filter buttons (All/Active/Needs reply) + message thread pane; (1) Needs Review — unanswered rep replies with "Mark as replied (manual)" button that logs an outbound message to clear the queue; (2) Action Items with mark-done/skip buttons. `needs_review_changed` signal updates sidebar badge and dashboard. Auto-loads via `QTimer.singleShot` on init.
  - **Help view** (`app/ui/views/help_view.py`) added: searchable, full-content help documentation covering all 14 topics (Getting Started, Dashboard, Sales by Rep, Budget, Weekly Email, Ask the AI, Conversations, CC Mapping, Core Displays, Fiscal Calendar, Settings, Reps, Troubleshooting, Data & Privacy). Added as "Help" to sidebar.
  - **22/22 tests pass.**

- **2026-05-14 (latest)** — BILLSLMN attribution fix + price class insights + weekly email tier differentiation:
  - **BILLSLMN is now the source of truth for ALL sales attribution** (new and
    legacy). Previously, new-system sales used `SALESPERSON_DESC` from
    `_ORDERS`, so departed reps (e.g. Steve Olink, rep 205, who has 1 account
    in BILLSLMN) were credited with $1.9M in sales that belong to their
    successors. Fixed in `load_blended_sales`: `rep_map` is now always built
    from `load_rep_assignments()` (regardless of whether the date range
    includes pre-cutoff legacy data), and applied to new-system rows via
    a **vectorized merge** — any `(account_number, cost_center)` in BILLSLMN
    has its `salesperson_desc` overridden with the current owner's name. Rows
    for accounts with no BILLSLMN entry keep their original `salesperson_desc`.
  - **Critical index bug fixed**: `load_invoiced_sales` returns a
    boolean-filtered slice of the per-month cache, giving a non-sequential
    index (e.g. rows 0, 5, 12…). The vectorized merge produces a fresh
    RangeIndex. `override.where(override != "", orig)` was aligning by pandas
    label and returning NaN for every fallback → ~90% of revenue appearing as
    "(unassigned)". Fixed by `reset_index(drop=True)` before the merge and
    using `merged["salesperson_desc"]` (same rows, same order, clean index) as
    the fallback source. Result: unassigned is now <0.1% (genuinely unmapped).
  - **Performance**: attribution is O(n log n) merge, not O(n) row-by-row
    `apply`. Full fiscal YTD (95k rows, all CCs) loads in ~2.3 seconds.
  - **Price class added to sales data and rep scorecards.**
    - `INVOICED_SALES_LINES` query now returns `price_class` (`ITEM.[IPRCCD]`);
      ITEM was already joined so no extra join needed.
    - New query `PRICE_CLASS_LOOKUP` returns `{price_class: description}` from
      `dbo.PRICE` (`[$PRCCD]` / `[$DESC]`).
    - Invoice cache bumped from `v2` → `v3` so old pickled DataFrames (without
      `price_class`) are not served.
    - `load_price_class_lookup(db) → dict[str, str]` added to `app/data/loaders.py`.
    - `RepScorecard.price_class_top: list[dict]` field added (top 8 price
      classes by revenue, with GP%). Computed in `compute_rep_scorecards` when
      `price_class_lookup` is passed.
    - `compute_rep_scorecards` accepts `price_class_lookup: dict[str, str] | None`.
    - `WeeklyEmailView._ensure_scorecards` lazily loads price class lookup and
      passes it through.
    - `_build_rep_prompt` includes a `TOP PRICE CLASSES` block in the user
      message so the AI knows what product types each rep is actually selling.
  - **Weekly email: tier-differentiated closing section.**
    - `_build_rep_prompt` now classifies each rep as STRUGGLING (bottom 40%
      by revenue rank AND declining YoY >5% or active-account rate <50%) or
      PERFORMING.
    - Struggling reps: closing section is "SPECIFIC ASSIGNED ACTION ITEMS" —
      concrete tasks with account labels and dollar context, framed as
      expectations.
    - Performing reps: closing section is "OPPORTUNITIES" — insight-framed
      patterns from the data (product line momentum, display correlation,
      territory upside), not directives.
    - Rep TIER label included in the AI user message for transparency.
    - The system prompt hard-rule added: "Do NOT give everyone things to do."
  - **22/22 tests pass.** All existing tests continue to pass; no new tests
    needed (functional changes verified via live DB + import check).
  - **Root cause of upload not applying**: CC codes in the CSV used no leading
    zeros (`10`, `27`, `40` etc.) while the DB stores 3-char zero-padded codes
    (`010`, `027`, `040`). Dict key `("212","10")` never matched `("212","010")`.
    Fixed in `parse_rep_cc_upload`: numeric CC codes shorter than 3 chars are
    now zero-padded with `cc.zfill(3)` before building the key. Rep numbers are
    normalized by stripping leading zeros via `str(int(rep))` so both `"4"` and
    `"004"` map to the same key. `_effective_growth` applies the same
    normalization when looking up, so matching is always consistent.
    Template download updated with realistic examples (two-digit CCs, note
    that leading zeros are optional). Format-spec label in UI updated likewise.
  - **Weekly email — high-impact account drops now flagged persistently**:
    AI system prompt updated to instruct the model to flag top-declining and
    stale accounts with large drops (>$5k decline, or regular buyer now at $0)
    as the top-priority action item in every email until resolved — not just
    the first week. These high-impact signals warrant persistent follow-up.
  - **22/22 tests pass.** Test updated to use 2-digit CC codes to reflect
    real upload format and verify zero-padding is applied correctly.
  - **Default filter date range** changed to **fiscal YTD**: from the start
    of the current fiscal year through the end of the last fully-completed
    fiscal period.  Prior-year comparison covers the same fiscal YTD range
    one year earlier.  The "YTD" quick-preset in `SalesFilterBar` was also
    updated to use this fiscal definition (was calendar Jan 1 → today).
    `fy_start_date` added to imports in `sales_filter_bar.py`.
  - **Fiscal YTD is now always the on-launch default**, regardless of any
    previously saved `start_iso`/`end_iso` in `config.json`.  Saved dates
    are only applied when the user explicitly clicks "Apply to all pages" on
    the Dashboard — so the dates shown on first open are always current and
    clean, not stale from a prior session.
  - **Upload → auto-recompute wired up**.  `_SettingsPanel` now emits an
    `upload_applied` signal immediately after a valid file is loaded.
    `BudgetView` connects this signal to `_on_upload_applied`, which calls
    `_recompute()` if prior-year data is already loaded, or shows an
    instructional status label if Compute hasn't been run yet.  Previously
    the overrides were stored but the table was never refreshed.
  - **Rep-level growth upload** added to the Budget & Forecast view's
    settings panel (`app/ui/views/budget_view.py`):
    - New card **"Rep-Level Growth Override (Upload)"** appears above the
      CC growth table with a clear column-format spec, a **Download
      Template** button, and a **Upload CSV / Excel** button.
    - Upload accepts `.csv`, `.xlsx`, `.xls`.  Exact required columns
      (case-insensitive): `rep_number`, `cost_center`, `growth_pct`.
    - After parsing, a compact preview table shows the loaded overrides;
      the upload-status label turns green on success.  Warning dialog lets
      the manager decide whether to keep partial results when some rows are
      invalid.
    - **`parse_rep_cc_upload(path)`** helper in `budget_service.py`:
      reads the file, normalises columns, handles NaN empty cells, validates
      numeric growth %, returns `({(rep_num, cc): pct}, errors)`.
    - **`_SettingsPanel.rep_cc_growth_pct()`** method exposes the loaded
      override map to `BudgetView`.
    - **Budget service** (`app/services/budget_service.py`) extended:
      - `compute_budget_by_cc`, `compute_budget_by_rep`,
        `compute_budget_by_account` each accept
        `rep_cc_growth_pct: dict[tuple[str,str], float] | None = None`.
      - When a `(rep_num, cc)` key is present: rep budget = rep prior ×
        (1 + override/100) — direct, not via CC proportional share.
      - CC budget = *sum of its reps' budgets* when overrides are present;
        unassigned lines use the CC-level fallback.
      - `_effective_growth(rep_num, cc, rep_overrides, cc_defaults)` helper.
      - `_cc_aggregates` recalculated to handle blended growth rates.
    - `compute_budget_by_cc` now also accepts `assignments_df` so it can
      attribute prior sales to reps when computing CC totals from overrides.
    - **22/22 tests pass** (4 new: `test_parse_rep_cc_upload_from_csv`,
      `test_parse_rep_cc_upload_skips_bad_rows`,
      `test_budget_rep_cc_override_takes_priority`,
      `test_budget_cc_level_fallback_when_no_override`).

- **2026-05-14** — Default date range, leaderboard cleanup, Budget & Forecast view:
  - **Default filter date range** in `SalesFilterBar` set to **12 months
    ending yesterday** (superseded by fiscal YTD in the entry above).
  - **Master leaderboard shoutouts removed.** Per-rep AI shout-out column
    eliminated; table now shows rank, rep, last-week $, and week-to-date $ only.
  - **Budget & Forecast view** (`app/ui/views/budget_view.py`) added as a new sidebar entry:
    - Settings panel (left): budget fiscal year spinner, CC growth % table (editable per CC), monthly seasonality % table (P1=Feb…P12=Jan, must sum to 100), Save Settings.
    - Results panel (right): toggle by Cost Center / Sales Rep / Customer; Download CSV and Download Excel buttons.
    - **Three-level cascade**: CC budget = prior year × (1 + growth%). Rep and account budgets distributed proportionally by prior-year sales share within each CC. Monthly budgets = full-year budget × seasonality %.
    - Default YTD display: Prior Year Full, Growth %, $ Change, Budget Full Year + (current FY only) Prior YTD, Budget YTD, Actual YTD, Vs Budget.
    - Downloads: full fiscal year with 12 monthly columns (Feb Budget…Jan Budget). Export mode picker lets user choose CC/Rep/Customer at download time. Excel export uses openpyxl with dark header, auto-width, currency format.
    - **`BudgetConfig`** added to `AppConfig`: `budget_fiscal_year`, `cc_growth_pct`, `monthly_seasonality_pct`.
    - **`app/services/budget_service.py`** (new): `BudgetRow` dataclass, `compute_budget_by_cc/rep/account`, `add_ytd_actuals`, `rows_to_dataframe`.
    - `openpyxl` added to venv.
  - **18/18 tests pass.**

- **2026-05-14 (late PM)** \u2014 Sample-attribution + display-table fix:
  - **`dbo.BCACCT` does not exist**. The display-placement query was
    pointing at the wrong table. The actual customer-display table is
    `dbo.BILL_CD` (with `BCACCT`/`BCCODE`/`BCCAT` columns). Fixed in
    `app/data/queries.py::DISPLAY_PLACEMENTS`. Loader now returns
    20,516 placements and core-display coverage is no longer zero.
  - **Samples were credited to nobody** because almost every sample
    line in `_ORDERS` has a blank `SALESPERSON_DESC` (samples are
    pulled by inside-sales, not the rep). New attribution logic in
    `compute_rep_scorecards` looks each sample row up by
    *(account, sample_cc \u2192 product_cc)* in `BILLSLMN` to find the rep
    who owns that account on the sponsoring product CC, with a
    fallback to "any product CC owner" for samples whose CC has no
    explicit mapping. Live test on rep MARK LOMONACO: 0 \u2192 2,138
    sample lines.
  - **CC-mapping direction-of-entry is now forgiving**. New helper
    `normalise_sample_product_pairs(mapping)` returns
    `{sample_cc: product_cc}` regardless of which side the user typed
    it on, by inspecting the leading digit (`'1'` = sample,
    `'0'` = product). The `CCMappingView` header copy was rewritten to
    explain this. `AIChatView` now uses the same normaliser when
    expanding scope to "samples that feed the selected product CC".
  - **Tests**. New `test_normalise_sample_product_pairs_either_direction`
    and `test_samples_attributed_via_account_ownership`. **18/18 pass.**

- **2026-05-14 (PM)** — Bug-fix + polish round (master leaderboard,
  outliers, samples/displays, account labels):
  - **Master leaderboard "No invoiced sales last week" fix**. The
    button anchored the previous-week window to `date.today()`, but
    when the manager's filter range ended *before* today (e.g. data
    loaded through May 2 and "last week" was May 3-9), every per-rep
    weekly query returned $0 and the email rendered empty. New
    `_anchor_date()` returns `min(today, max(invoice_date in scope))`,
    which `_generate_master` and `_weekly_lines_for` both use for the
    weekly windows. The rendered HTML adds a yellow "Anchored to last
    invoice date in scope (YYYY-MM-DD) — widen the date range to
    include this calendar week." banner whenever the anchor < today,
    so the user knows why and can fix it in one click.
  - **Outlier YoY exclusion** (`OUTLIER_YOY_PCT_THRESHOLD = 500.0`).
    A rep whose `|yoy_pct|` exceeds the threshold (e.g. Matthew Keenan
    at +6410% after a territory transfer) is now flagged
    `RepScorecard.is_yoy_outlier = True`, **excluded** from the
    `peer_avg_yoy_pct` rollup so it can't skew everyone else's
    comparative numbers, but the rep still gets a scorecard. The
    AI weekly-email prompt receives an extra system instruction telling
    it not to frame the email around YoY for outliers and to lean on
    absolute revenue, GP%, last-3-months momentum, top movers, and
    active-account ratio instead. The shoutout AI prompt converts the
    YoY field to "YoY=outlier (territory transfer; ignore)" for those
    reps and adds a `3mo=±X%` field so the model has a stable
    alternative metric. The scorecard footer appends "(outlier — likely
    territory transfer)" next to the YoY line.
  - **Samples + core-display "all zeros" fix**. Two bugs stacked:
    (a) `_ContextLoader` was loading sample sales via
    `load_blended_sales(..., cost_centers=self._ccs, code_prefix="1")`
    — but `self._ccs` contained the user's *product* CC selection,
    which is mutually exclusive with `code_prefix='1'` in the SQL
    filter, so the loader returned 0 rows every time. Fixed by passing
    `cost_centers=None` (and `ccs_key=""` for the singleflight key) on
    the samples loader call. (b) Core-display coverage was 0 for every
    rep because `AppConfig.core_displays_by_cc` is empty by default
    (the manager hadn't tagged any displays as "core" yet), so
    `flat_core` was empty and no placement could match. New fallback:
    when `core_displays_by_cc` is empty/None, **any** display
    placement on a rep's account counts toward coverage, and a
    `notes` line ("Core-display coverage uses ANY display because no
    core displays are configured…") is added to the scorecard so the
    manager knows to configure them in the Core Displays view for a
    stricter measure.
  - **Account labels everywhere use legacy BBANK2 + name**. Plumbed
    `account_info` (`{new_acct: {old: BBANK2, name: BNAME stripped}}`)
    from `load_rep_assignments` through `compute_rep_scorecards` so
    every account dict in `top_growing_accounts` /
    `top_declining_accounts` / `stale_accounts` / `new_accounts` now
    carries `old_account` + `account_name` (closed-account leading
    `*` stripped). New `format_account_label(rec, style='short'|'long')`
    helper renders `"50285 (#1234 · ABC FLOORING)"` (short) or
    `"50285 · ABC FLOORING (#1234)"` (long). The weekly email AI
    prompt, fallback body, fallback shoutout, AI shoutout bullets,
    and the scorecard footer growing/declining lists all use the
    short form — reps now see the legacy `#-number` they recognise
    next to the new account number.
  - **Polish**. Master leaderboard HTML upgraded: dark navy header
    (`#0F172A`/`#F8FAFC`), zebra-striped rows, `font-variant-numeric:
    tabular-nums` on currency cells, rounded `1px solid #E2E8F0`
    border, "Week to date covers …" caption, and the new anchor
    warning banner. Scorecard footer typography tightened
    (`font-weight:600`, `line-height:1.55`, `color:#0F172A`).
  - **Tests**. New `tests/test_manager_analytics.py` covers (a) outlier
    YoY excluded from `peer_avg_yoy_pct` while still flagged on the
    scorecard, and (b) `format_account_label` short and long styles
    plus the no-old-account fallback. **16/16 tests pass.**

- **2026-05-14** — Sales-manager analytics + AI-coached weekly emails +
  rep directory + master leaderboard:
  - **New service `app/services/manager_analytics.py`**. Two dataclasses
    (`RepScorecard`, `PeriodOverview`) and pure functions that turn the
    blended-sales DataFrame + rep assignments + display placements +
    sample sales into deterministic, manager-grade analytics:
    - Per-rep YoY revenue, **peer-average YoY** (peers defined by the
      currently selected scope; min 5 active accounts and $1k revenue
      to be peer-eligible), **vs-peers delta**, GP / GP%, line count.
    - **Active-account ratio** (% of a rep's assigned accounts that
      had any invoiced revenue in the window).
    - **Core-display coverage** (% of the rep's accounts that have at
      least one of the cost-center's "core" displays installed, per
      `AppConfig.core_displays_by_cc`).
    - **Samples-per-account** (sample-CC lines normalised to account
      count) — a leading indicator of pipeline activity.
    - **Last 3 months vs prior 3 months** and **last 3 months YoY** so
      the email always has a current-trend talking point regardless of
      the filter window.
    - Top growing / top declining / **stale** (had revenue last yr,
      zero now) / **new** (zero last yr, revenue now) accounts — each
      with current, prior, delta, and pct.
    - Within the loaded scope: `rank_revenue` and `rank_yoy`.
    - `compute_period_overview(label, start, end, df, prior)` returns
      total revenue / GP / YoY / active reps / active accounts /
      top-rep & top-CC contributors for the period — used as the
      preamble in monthly / quarterly / yearly emails.
    - `current_week_range` / `previous_week_range` (Sun→Sat).
    - `revenue_in_window(df, start, end, by='rep'|'account')` — used
      to compute "last week" and "week-to-date" rep totals from the
      already-loaded DataFrame without going back to SQL.
    - `aggregate_for_ai(df)` returns `{by_rep, by_cc, by_account[top
      200], by_period}` — the full-dataset truth tables fed to the AI.
  - **`Sales Reps & Directory` view (`reps_view.py`) is now editable.**
    Three new in-grid columns: **email**, **boss email** (Cc on
    escalations), and **tone** (-3 firm … +3 extra-encouraging). Edits
    persist to `AppConfig.rep_emails / rep_boss_emails / rep_tone`
    keyed by `salesman_number` and write through `save_config(cfg)`.
    Empty values clear the entry. Status bar reports how many reps
    have an email on file. Answers the user's "is there a section to
    add emails" question — yes, this view.
  - **Weekly Email view rewritten end-to-end (`weekly_email_view.py`)**
    to behave like a real sales manager:
    1. Filter bar loads blended sales (current + prior year).
    2. `_ContextLoader` background-loads rep assignments, display
       placements, and sample-CC sales (sample sales reuse the
       `sales_singleflight` cache with `code_prefix='1'`).
    3. `compute_rep_scorecards(...)` + `compute_period_overview(...)`
       are auto-run; the period preamble is detected from the
       window's end date via `find_period(...)`.
    4. **`_AIDraftWorker` (QThread)** sequentially asks the configured
       AI provider to draft one email per rep using a tone-aware
       system prompt (200–350 words, opens with a real positive,
       2–3 focus areas with concrete numbers, 1–2 specific action
       items, never invents figures). Tone ladder reads `rep_tone`:
       ≥+2 warm; ≥0 supportive-candid; ≥-1 direct; else firm.
    5. Each draft body is wrapped with a **company period overview
       banner** ("FY27 P3 (April): $X · YoY +Y%"), a **"Last week /
       This week to date" box** (per-rep weekly cadence), the AI
       body, and a **scorecard footer** (revenue, YoY, peer delta,
       L3M vs prior, active-account %, core-display coverage,
       samples/account, top growing/declining accounts).
    6. **Fallback path** when AI is not configured: a deterministic
       5-paragraph human draft is rendered synchronously so the
       workflow never blocks.
  - **Master leaderboard email**. New "Generate master leaderboard"
    button produces a single email recapping **last full week** for
    every rep, sorted descending. Each row includes "Last week" and
    "Week to date" totals + a one-line **AI-generated positive
    shout-out** (separate AI call with a strict "always find
    something honest and positive — never insult" system prompt).
    Falls back to a hand-written shout-out template per rep
    (top growing account / new account / YoY) when AI is off.
  - **Per-rep recipient resolution**. The view builds a
    `salesman_name → salesman_number` map from the assignments
    DataFrame and looks each rep's `email` / `boss_email` /
    `tone` up in `AppConfig`. The list label clearly flags
    "no email on file (set in Sales Reps)" so the manager can fix
    it in one click.
  - **AI Chat view (`ai_chat_view.py`) now sends the full filtered
    dataset truth.** Every question now includes a
    `PRE-AGGREGATED TABLES` block built by `aggregate_for_ai(self._df)`
    with TOTALS + by-rep (top 100) + by-cost-center (top 50) + top
    accounts (100) + by-fiscal-period, *before* the existing capped
    CSV sample. The system prompt explicitly tells the model to use
    the aggregates for ranking/totals questions and the CSV only for
    line-level detail — so "top 5 reps" answers are now correct even
    when there are 200k underlying rows. The CSV cap stays at 1500
    for token sanity.
  - **Crash-safety pattern preserved**: `_ContextLoader` and
    `_AIDraftWorker` use the `self._context_loaders: list[...]` /
    `self._ai_workers: list[...]` pattern with
    `finished.connect(...remove)` so concurrent generations never GC
    a running QThread.
  - 14/14 tests still pass.

- **2026-05-14** \u2014 Earlier same-day rounds (condensed):
  - **Crash fix on Refresh** \u2014 every QThread-owning view now holds a
    `self._loaders: list[...]` and connects each thread's `finished` to
    self-removal, so concurrent loaders never get GC'd mid-run.
  - **Per-screen sidebar status glyphs** (`\u27f3`/`\u2713`/`!`) via
    `Sidebar.set_status(key, state)` driven by `busy_state_changed`.
  - **Fiscal YTD KPI** uses `fy_start_date(fiscal_year_for(today))`.
  - **Dashboard KPIs honor `cfg.defaults.cost_centers`** + new
    *Selected range* KPI for `cfg.defaults.start_iso\u2192end_iso`.
  - **Singleflight loader dedup** (`app/services/singleflight.py`) keyed
    `(start, end, ccs, code_prefix)` collapses concurrent identical
    queries across all views \u2014 ~3-4x refresh speedup.
  - **Global default filters** (`AppConfig.defaults: GlobalFiltersConfig`)
    + Dashboard *Default filters* card with *Apply to all pages* and
    *Save as default*. `SalesFilterBar.apply_filters(start, end, ccs)`
    + `refresh_data()` are the public APIs.
  - **Removed `N_NOT_INVENTORY='Y'` and `IINVEN='Y'` filters** from
    `INVOICED_SALES_LINES`/`OPEN_ORDERS_LINES` \u2014 they were silently
    dropping $21.65M / 38,894 valid invoiced lines (freight, services,
    custom, non-stock) in one rolling year. Sales now reconcile to the
    warehouse aggregate.
  - **Single global Refresh** \u2014 only the Dashboard hosts the refresh
    button; `MainWindow._refresh_all_views` fans out to every
    `filter_bar.refresh_data()`. Cache schema bumped to `v2|`.

- **2026-05-13** \u2014 Earlier rounds (condensed):
  - **OpenAI key newline crash** \u2014 strip whitespace in
    `OpenAIProvider.__init__`, `AISettingsDialog.commit_secrets/_on_test`,
    and `factory.build_provider`.
  - **Legacy revenue gap fix** \u2014 `OLD_SYSTEM_SALES` now `LEFT JOIN`s
    `vw_CostCenterCLydeMRKCodeXREF` and synthesises `cost_center =
    '0' + marketing_code` for unmapped codes.
  - **Per-month invoice cache** (`app/storage/invoice_cache.py`,
    `invoice_month_cache_v2`). Closed historical months come from
    SQLite; only the current calendar month hits the warehouse.
  - **CC selector autoload** \u2014 `QTimer.singleShot(0, self.reload)`
    on construction; signals blocked during populate.
  - **`code_prefix` plumbed end-to-end** through queries, loaders,
    `SalesFilterBar`, and the cache key (`\u2026|p=0`). Sample CCs (`'1xx'`)
    can never leak into product views, even when "all" is selected.
  - **Premium polish round** \u2014 `Select all` + `Deselect all` only on
    the CC selector; legacy sales now attributed via current
    `(account, cost_center)` ownership in `BILLSLMN`/`SALESMAN`;
    persistent `sales_cache` (SQLite, pickled DataFrame, keyed
    `start|end|sorted-CCs`) with startup *Use cached / Refresh from DB*
    prompt.
  - **Empty-Dashboard fix** \u2014 new `load_blended_sales(...)` blends
    new-system `_ORDERS` rows (\u2265 `NEW_SYSTEM_CUTOFF` 2025-08-04) with
    pre-cutoff `ClydeMarketingHistory` rows unpivoted to monthly
    granularity. Loaders return `data_source` \u2208 `{"new","legacy"}`.
    Dashboard now runs `_DashboardLoader` and shows real KPIs.
  - **Invisible-window / pythonw fix** \u2014 app data path moved to
    `~\\Documents\\SalesAssistant` (managed-IT was wiping
    `%APPDATA%\\SalesAssistant`); `_configure_logging` skips
    `StreamHandler(sys.stderr)` when `sys.stderr is None`;
    `MainWindow.show()` calls `raise_()` + `activateWindow()`.
  - **DB error fix** \u2014 dropped non-existent `o.[SALESPERSON]` from
    `INVOICED_SALES_LINES` and `OPEN_ORDERS_LINES`; rep number lives in
    `BILLSLMN.BSSLMN` only.
  - **CC duplicates fix** \u2014 `COST_CENTER_XREF` now `GROUP BY
    cost_center` (one row per CC).
  - **CC Mapping view + Core Displays view** added.
  - **Auto-load on launch** for every data-driven view.
  - **Smart default date range** \u2014 last 12 fully-completed fiscal
    periods; presets (Last full FM / 3 / 6 / Rolling year / YTD / 30d).
  - **AI analysis history** \u2014 `ai_analyses` table + Saved Analyses
    pane in Ask the AI (search, restore, pin, dedupe banner). Schema
    bumped to v2.
  - **Major UX expansion** \u2014 reusable `CostCenterSelector`,
    `SalesFilterBar` (CC + date range + presets), new views
    (`SalesByRepView`, `SalesByCostCenterView`, `CCMappingView`,
    `WeeklyEmailView`, `AIChatView`, `FiscalCalendarView`).
  - **Fiscal calendar service** \u2014 4-4-5 weekly pattern, anchor
    Sunday Feb 1 2026 = FY 2027 P1. Six-week-January override via
    `FiscalCalendarConfig.six_week_january_years`.
  - **Initial scaffold + scope lock-in** \u2014 PySide6 desktop, custom
    QSS, SQLite local state, SMTP+IMAP, OpenAI default + provider
    abstraction, keyring for secrets. Tables surfaced:
    `vw_CostCenterCLydeMRKCodeXREF`, `ClydeMarketingHistory`, `BILLTO`
    (with `BBANK2` + leading-`*` closed-account flag), `SALESMAN`,
    display tracking via `CLASSES`/`BILL_CD` (`CLCAT='DT'`). Repo:
  https://github.com/lstred/Sales-Assistant.
