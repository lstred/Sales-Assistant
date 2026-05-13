# Sales Assistant

A native **Windows desktop application** that uses sales data from `NRF_REPORTS`
SQL Server to evaluate sales reps and engage them through AI-drafted email —
either manually approved or scheduled — and tracks every conversation,
commitment, and follow-up over time.

> Built with PySide6 (Qt for Python). No browser, no Streamlit. Designed to be
> packaged as a single `.exe` via PyInstaller.

See [CLAUDE.md](CLAUDE.md) for the running design / context document and
[NEW_APP_CONTEXT_PROMPT.md](NEW_APP_CONTEXT_PROMPT.md) for full SQL Server
schema, field dictionary, and business rules.

---

## Status

Initial scaffold is in place:

- Premium QSS theme (Segoe UI Variable, accent-driven, 8-px grid).
- Sidebar + Dashboard / Reps / Conversations / Settings views.
- Background liveness checks for **Database**, **Email**, and **AI** in the
  status bar.
- Settings dialogs for **Database** (Trusted Connection), **Email**
  (SMTP+IMAP, with safe-by-default outbound switch and dry-run redirect),
  and **AI provider** (OpenAI today, abstraction for Anthropic / Azure later).
- All secrets stored in **Windows Credential Manager** via `keyring` —
  never on disk.
- SQL data layer with the new tables you asked for: `BILLSLMN`, `SALESMAN`,
  `BILLTO` (incl. `BBANK2` and the leading-`*` closed-account flag),
  `vw_CostCenterCLydeMRKCodeXREF`, `ClydeMarketingHistory`, and
  `CLASSES`/`BCACCT` for displays.
- Local SQLite app-state DB for conversations, messages, action items,
  send log, metric snapshots.
- Reps view loads the live `SALESMAN` roster on demand via a worker thread.

Not yet built (next iterations): per-rep metric computation, insight rules,
email draft composer, AI prompt builder, schedule + IMAP polling.

---

## Prerequisites

- Windows 10/11 on the NRF corporate network or VPN.
- **Python 3.11+** (3.11.9 confirmed working).
- **ODBC Driver 18 for SQL Server** installed at the OS level
  ([download](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)).

## Setup

```powershell
cd "C:\Users\lukass\Desktop\Sales Manager"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## Run the app

```powershell
.\.venv\Scripts\Activate.ps1
python -m app
```

On first launch, open **Settings** to confirm the database (NRFVMSSQL04 /
NRF_REPORTS, Trusted Connection) and configure your email + AI provider.
Outbound email stays disabled until you explicitly enable it — review every
draft in-app first.

## Run tests

```powershell
pytest
```

## Build a standalone .exe (later)

```powershell
pyinstaller --noconsole --name "SalesAssistant" -w app/main.py
```
(Final packaging recipe lives in CLAUDE.md once we lock it in.)

---

## Application data location

Everything user-specific lives under `%APPDATA%\SalesAssistant\`:

- `config.json` — non-secret settings.
- `state.sqlite` — conversations, messages, action items, send log.
- `logs/app.log` — runtime log.

Credentials (SMTP password, IMAP password, AI API key) live in **Windows
Credential Manager** under the service prefix `SalesAssistant/…`.
