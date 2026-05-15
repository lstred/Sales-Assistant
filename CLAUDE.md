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

Newest first. Older entries are condensed at the bottom of the list —
read those plus this file's earlier sections for full context.

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
