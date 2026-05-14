"""Help & documentation view.

Searchable, structured help covering every feature of the app.
No external resources required — all content is inline.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.ui.theme import ACCENT, BORDER, SURFACE, TEXT, TEXT_MUTED
from app.ui.views._header import ViewHeader

# ------------------------------------------------------------------ content
# Each topic: (title, body_html)
HELP_TOPICS: list[tuple[str, str]] = [
    (
        "Getting Started",
        """
<h2>Getting Started</h2>
<p>Welcome to <b>Sales Assistant</b> — an AI-powered sales management tool
built for flooring distributors. Here's how to get up and running in 5 minutes.</p>

<h3>Step 1 — Connect to the database</h3>
<p>Go to <b>Settings → Database</b> and enter your SQL Server connection details.
The app uses Windows Trusted Connection so you don't need a username/password —
just the server name and database name. Click <b>Test connection</b> to verify.</p>

<h3>Step 2 — Configure AI</h3>
<p>Go to <b>Settings → AI</b> and paste your OpenAI API key. The key is stored
securely in Windows Credential Manager — never on disk. Click <b>Test</b> to
verify. Without AI configured, the app still works but uses template-based
email drafts instead of AI-written ones.</p>

<h3>Step 3 — Configure email (optional)</h3>
<p>Go to <b>Settings → Email</b> and enter your SMTP/IMAP credentials. This
enables sending emails directly from the app and receiving rep replies.
Not required for the analytics or AI drafting features.</p>

<h3>Step 4 — Load data</h3>
<p>Click <b>Refresh all data from database</b> on the Dashboard, or navigate
to any data view and press <b>Run</b>. Data is cached locally so subsequent
loads are fast.</p>

<h3>Step 5 — Add rep emails</h3>
<p>Go to <b>Sales Reps &amp; Directory</b> and add each rep's email address,
boss email (for escalations), and tone setting. The weekly email feature
requires rep emails to be set.</p>
""",
    ),
    (
        "Dashboard",
        """
<h2>Dashboard</h2>
<p>The dashboard shows a real-time snapshot of the business. All KPI cards
update automatically when you load data.</p>

<h3>KPI Cards (top row)</h3>
<ul>
<li><b>Last full fiscal month</b> — Total invoiced revenue for the most
    recently completed fiscal period.</li>
<li><b>Fiscal year-to-date</b> — Revenue since the start of the current
    fiscal year (February 1st of the current FY).</li>
<li><b>Selected range</b> — Revenue for whatever date range you've saved as
    your default filters. Set this in the Default Filters card below.</li>
<li><b>Open orders</b> — Un-invoiced pipeline (orders placed but not yet
    shipped/billed).</li>
<li><b>Active reps</b> — Distinct sellers with invoiced activity in the last
    90 days.</li>
</ul>

<h3>Activity Cards (second row)</h3>
<ul>
<li><b>Active conversations</b> — Email threads the AI has opened and is
    tracking. Goes up when you send weekly emails; goes down when you close
    threads in the Conversations view.</li>
<li><b>Open action items</b> — Rep commitments extracted from their replies
    (e.g. "I'll call them Friday") that haven't been marked done yet.</li>
<li><b>Needs review</b> — Rep replies you haven't responded to. This is your
    most important queue — check it every time you open the app.</li>
</ul>

<h3>Default Filters card</h3>
<p>Set your preferred date range and cost centers here. Click
<b>Apply to all pages</b> to push those filters to every data view at once.
Click <b>Save as default</b> to make them persist across app restarts.</p>
""",
    ),
    (
        "Sales by Rep",
        """
<h2>Sales by Rep</h2>
<p>Shows invoiced revenue and GP% for each sales rep within the selected
cost centers and date range.</p>

<h3>How to use it</h3>
<ol>
<li>Select cost centers using the selector on the left.</li>
<li>Set your date range (or use one of the quick presets).</li>
<li>Click <b>Run</b> to load the data.</li>
</ol>

<h3>Column meanings</h3>
<ul>
<li><b>Rep</b> — Rep name from BILLSLMN (the current account owner, not the
    name on the order line — so departed reps like Steve Olink no longer appear).</li>
<li><b>Revenue</b> — Sum of EXTENDED_PRICE_NO_FUNDS for all invoiced lines in
    scope.</li>
<li><b>GP $</b> — Gross profit dollars.</li>
<li><b>GP%</b> — Gross profit percentage.</li>
<li><b>Lines</b> — Number of invoice line items (a proxy for activity level).</li>
</ul>

<h3>Attribution note</h3>
<p>Attribution uses the current BILLSLMN assignment, not the SALESPERSON_DESC
on the order line. This means all of a departing rep's accounts are
re-attributed to whoever owns them now — which is the correct view for
coaching the current team.</p>
""",
    ),
    (
        "Sales by Cost Center",
        """
<h2>Sales by Cost Center</h2>
<p>Aggregates invoiced revenue by product line (cost center). Use this to
identify which product lines are growing or declining across the territory.</p>

<h3>Cost center codes</h3>
<p>Codes starting with <b>0</b> (e.g. 010, 027) are product cost centers.
Codes starting with <b>1</b> are sample cost centers. The Sales by Cost Center
view only shows product CCs. Sample activity is tracked separately in the
rep scorecards.</p>
""",
    ),
    (
        "Budget & Forecast",
        """
<h2>Budget &amp; Forecast</h2>
<p>Projects next-year budgets based on prior year actuals and growth assumptions.</p>

<h3>How it works</h3>
<ol>
<li>Set the <b>budget fiscal year</b> (the year you're budgeting for).</li>
<li>Set a <b>growth %</b> per cost center, or accept the default (0%).</li>
<li>Optionally upload a <b>rep-level growth override</b> CSV to set different
    growth rates for specific rep × CC combinations.</li>
<li>Click <b>Compute budget</b> to see results by CC, rep, or customer.</li>
</ol>

<h3>Rep-level override CSV format</h3>
<p>Required columns (case-insensitive): <code>rep_number</code>,
<code>cost_center</code>, <code>growth_pct</code>. Leading zeros in CC codes
are optional (both <code>10</code> and <code>010</code> work). Download the
template from the Settings panel for an example.</p>

<h3>Seasonality</h3>
<p>The 12 seasonality % values in the table must sum to 100%. Period 1 = February,
Period 12 = January. These control how the annual budget is spread across the
fiscal year for monthly reporting.</p>
""",
    ),
    (
        "Weekly Email",
        """
<h2>Weekly Email</h2>
<p>The most powerful feature in the app. Generates personalized, AI-coached
weekly coaching emails for every sales rep based on their current performance data.</p>

<h3>Workflow</h3>
<ol>
<li>Load sales data using the filter bar on the left.</li>
<li>Click <b>Generate AI drafts</b> — one email per rep is drafted in the background.</li>
<li>Select each rep from the list on the right to review the draft.</li>
<li>Click <b>Queue for review</b> when you're ready to send.</li>
</ol>

<h3>Email structure</h3>
<p>Every email follows the same format:</p>
<ul>
<li><b>HIGHLIGHT</b> — One sentence on their best result this week (real numbers, specific account).</li>
<li><b>LOWLIGHT</b> — One sentence on the biggest concern. Large account drops (&gt;$5k) are flagged
    every week until resolved.</li>
<li><b>FOCUS AREAS</b> — 2-3 bullets with concrete, numbered-backed action items or opportunities.
    All date references are explicit (e.g. "February–April 2026", never "previous period").</li>
<li><b>Closing</b> — Struggling reps get assigned action items; performing reps get
    insight-framed opportunities.</li>
<li><b>Service offer</b> — If the AI sees a data question worth a deeper dive, it offers
    to pull a breakdown. Reps can reply "YES" to request it.</li>
<li><b>Scorecard footer</b> — Revenue, GP%, YoY vs peers, 3-month trend, active accounts,
    display coverage, top growing/declining accounts.</li>
</ul>

<h3>Rep tier classification</h3>
<p><b>Struggling</b>: Bottom 40% by revenue AND (YoY &lt; -5% OR active account rate &lt; 50%).
These reps get assigned action items with explicit expectations.</p>
<p><b>Performing</b>: Everyone else. These reps get opportunities and insights, not directives.</p>

<h3>Tone settings</h3>
<p>Each rep has a tone setting in Sales Reps &amp; Directory:</p>
<ul>
<li>+2 or +3: Extra-encouraging and warm</li>
<li>0 or +1: Supportive but candid</li>
<li>-1: Direct, results-focused, no fluff</li>
<li>-2 or -3: Firm and clear about underperformance</li>
</ul>

<h3>Master leaderboard</h3>
<p>Click <b>Generate master leaderboard</b> to create a single team-wide email
showing last week's revenue ranking for all reps. Useful for a team-wide
Monday morning email.</p>

<h3>Rep service requests</h3>
<p>When a rep replies "YES" to a service offer in an email, that reply will
appear in the <b>Conversations → Needs Review</b> tab. You can then pull the
requested data and reply manually. Future versions will automate this.</p>
""",
    ),
    (
        "Ask the AI",
        """
<h2>Ask the AI</h2>
<p>Ask any natural-language question about the currently loaded sales data.
The AI has access to the <i>full dataset</i> — no row limit — and is pre-fed
aggregated summaries for fast ranking/totals questions.</p>

<h3>How to use it</h3>
<ol>
<li>Select cost centers and date range using the filter bar.</li>
<li>Click <b>Run</b> to load the data.</li>
<li>Type your question and click <b>Ask</b>.</li>
</ol>

<h3>Good questions to ask</h3>
<ul>
<li>"Which 5 reps grew the most vs last year?"</li>
<li>"Which accounts bought carpet in February 2026 but not in March 2026?"</li>
<li>"What's our GP% by cost center for the last 3 months?"</li>
<li>"Which reps have the most stale accounts?"</li>
<li>"Compare Mark Lomonaco and Chris Thomas YoY."</li>
</ul>

<h3>The AI is blunt</h3>
<p>The AI is instructed to be direct. It will name underperformers, call out
declining trends clearly, and tell you when something is bad — not just that
"there is room for improvement".</p>

<h3>Token usage</h3>
<p>The KPI cards at the top show estimated token usage and cost before you
ask. The full dataset is sent to the AI, so costs scale with data size.
A typical fiscal YTD query for one cost center costs ~$0.01–0.05.</p>

<h3>Saved analyses</h3>
<p>Every Q&amp;A is automatically saved in the <b>Saved analyses</b> panel on
the left. You can search, pin important ones, and reopen them without re-asking.
If you ask the same question again with the same data scope, a banner will
alert you so you don't waste an API call.</p>
""",
    ),
    (
        "Conversations & Reply Queue",
        """
<h2>Conversations &amp; Reply Queue</h2>
<p>Tracks every email thread the app has started with a rep, plus their replies.</p>

<h3>All Conversations tab</h3>
<p>Shows all threads sorted by most recent activity. Filter by All / Active /
Needs reply using the buttons at the top. Click a conversation to see the
full message thread on the right.</p>

<h3>Needs Review tab</h3>
<p><b>This is your most important queue.</b> Any time a rep replies to an
email and you haven't responded, it appears here — whether the app was open
or not. Check this tab every time you open the app.</p>
<p>When the count is &gt; 0, the Dashboard "Needs review" KPI card turns red.
The sidebar badge also updates.</p>
<p>To clear an item: select it, review the rep's reply in the bottom panel,
then click <b>Mark as replied (manual)</b> after you've sent your response.
This logs the reply so the thread no longer shows as needing attention.</p>

<h3>Action Items tab</h3>
<p>Rep commitments extracted from their replies (e.g. "I'll call them Friday",
"will do a PK session next week"). These are created automatically when the AI
parses inbound messages. You can mark them as <b>Done</b> or <b>Skip</b>.
Active action items are counted on the Dashboard.</p>
""",
    ),
    (
        "CC Mapping (Samples)",
        """
<h2>CC Mapping (Sample Cost Centers)</h2>
<p>Maps sample cost centers (codes starting with 1) to their sponsoring
product cost centers (codes starting with 0).</p>

<h3>Why this matters</h3>
<p>When a rep pulls samples, the order goes against a sample CC (e.g. 110),
not the product CC (e.g. 010). Without this mapping, sample activity can't
be attributed to the right product line or rep scorecard.</p>

<h3>How to enter mappings</h3>
<p>The direction of entry doesn't matter — you can enter "010 → 110" or
"110 → 010" and the system normalizes it. Just pair the sample CC with the
product CC it feeds.</p>
""",
    ),
    (
        "Core Displays",
        """
<h2>Core Displays</h2>
<p>Defines which display codes are "core" for each product cost center.
This powers the <b>core-display coverage %</b> metric in rep scorecards.</p>

<h3>What is core-display coverage?</h3>
<p>Coverage = the % of a rep's accounts that have at least one of the cost
center's core displays installed. High coverage correlates with higher revenue
per account — the emails call this out when the data supports it.</p>

<h3>Default behavior</h3>
<p>If no core displays are configured for a CC, the scorecard falls back to
counting <i>any</i> display as coverage and notes this in the footer.
Configure core displays here for a stricter, more meaningful metric.</p>
""",
    ),
    (
        "Fiscal Calendar",
        """
<h2>Fiscal Calendar</h2>
<p>Displays and configures the fiscal calendar used throughout the app.</p>

<h3>Fiscal year definition</h3>
<p>The fiscal year starts in <b>February</b>, so FY2027 runs from February 2026
through January 2027. The calendar uses a 4-4-5 weekly pattern with the anchor
date of Sunday February 1, 2026 = FY2027 Period 1.</p>

<h3>January length</h3>
<p>Some years use a 6-week January to realign with the calendar year.
Configure those years in Settings → Fiscal Calendar. This affects period
boundaries and date filters throughout the app.</p>
""",
    ),
    (
        "Settings",
        """
<h2>Settings</h2>

<h3>Database</h3>
<p>SQL Server connection: server name, database name. Uses Windows Trusted
Connection (no password required). Click <b>Test</b> to verify connectivity.</p>

<h3>AI Provider</h3>
<p>OpenAI API key (stored in Windows Credential Manager), model name, and
token/timeout limits. The default model is gpt-4.1. You can also set
max output tokens (controls email length) and temperature (0 = deterministic,
1 = creative — default 0.7 works well for emails).</p>

<h3>Email (SMTP/IMAP)</h3>
<p>SMTP host/port for sending, IMAP host/port for receiving. Credentials
stored in Windows Credential Manager. Flip the <b>Outbound enabled</b> toggle
to start actually sending emails from the app.</p>

<h3>Fiscal Calendar</h3>
<p>Configure which years use a 6-week January period.</p>

<h3>Rep Emails &amp; Tone</h3>
<p>These are set directly in the <b>Sales Reps &amp; Directory</b> view, not here.
Click any rep row to edit their email, boss email, and tone.</p>
""",
    ),
    (
        "Sales Reps & Directory",
        """
<h2>Sales Reps &amp; Directory</h2>
<p>Shows all active sales reps pulled from BILLSLMN + SALESMAN. Editable columns:</p>

<ul>
<li><b>Email</b> — The rep's email address. Required for sending weekly coaching emails.
    Reps without an email are flagged in the Weekly Email view.</li>
<li><b>Boss email</b> — CC'd on escalation emails (future feature).</li>
<li><b>Tone</b> — Scale from -3 (firm) to +3 (extra-encouraging). Controls the writing
    style of weekly emails for this specific rep.</li>
</ul>

<p>Click any cell to edit. Changes save immediately.</p>
""",
    ),
    (
        "Troubleshooting",
        """
<h2>Troubleshooting</h2>

<h3>App won't connect to the database</h3>
<p>Make sure you're on the network/VPN that can reach NRF_REPORTS. Check the
server name in Settings → Database. The app uses Windows Trusted Connection,
so your Windows account must have read access to the database.</p>

<h3>Data takes too long to load</h3>
<p>The first load of a date range queries SQL Server; subsequent loads use the
local cache. Click <b>Refresh all data from database</b> to force a fresh
pull. For very large date ranges, consider narrowing the cost center selection.</p>

<h3>AI emails are blank or generic</h3>
<p>Check that: (1) AI is configured in Settings → AI, (2) your API key is
valid (test it in Settings), (3) data is loaded before clicking Generate.
If AI fails, the app falls back to template-based drafts automatically.</p>

<h3>All revenue showing as "(unassigned)"</h3>
<p>This means accounts aren't found in BILLSLMN for the selected cost centers.
Try selecting "All" cost centers first to check if data loads at all. If it
does, check that the cost center codes in the filter match what's in BILLSLMN.</p>

<h3>Fiscal year or period numbers look wrong</h3>
<p>Remember: fiscal year starts in February. May 2026 is in FY2027 (Period 4).
If period boundaries are off, check Settings → Fiscal Calendar for any
6-week January year configurations.</p>

<h3>App window doesn't appear on startup</h3>
<p>Check the task bar or try Alt+Tab. If you see "pythonw" in Task Manager
but no window, check the log file at
<code>%APPDATA%\\SalesAssistant\\app.log</code> for errors.</p>

<h3>Rep emails from before I set up the app — where do they go?</h3>
<p>Replies to emails sent before email transport was configured won't be
automatically captured. Once you configure SMTP/IMAP in Settings → Email
and enable outbound, all future rep replies will be tracked in Conversations.</p>
""",
    ),
    (
        "Data & Privacy",
        """
<h2>Data &amp; Privacy</h2>

<h3>What data is sent to the AI?</h3>
<p>When you use Ask the AI or generate weekly emails, invoiced sales data
(revenue, GP, accounts, rep names, cost centers, dates) is sent to the
OpenAI API. No personally identifiable information about customers beyond
account numbers and account names is sent. Rep names are included.</p>
<p>For weekly emails: only the individual rep's own data is sent to the AI
for that rep's draft. The AI does not see other reps' data when drafting
a single rep's email.</p>

<h3>Secrets storage</h3>
<p>API keys and email passwords are stored in Windows Credential Manager.
They are never written to disk, to config files, or to the local SQLite database.
The config.json file contains only non-secret settings (host names, model names).</p>

<h3>Local database</h3>
<p>The local SQLite database stores conversation history, message bodies,
action items, and saved AI analyses. It is located at
<code>~\\Documents\\SalesAssistant\\state.sqlite</code>. Back it up if you
want to preserve conversation history.</p>

<h3>SQL security</h3>
<p>All queries to SQL Server use parameterized statements. No user-provided
values are ever interpolated into SQL strings.</p>
""",
    ),
]


# ------------------------------------------------------------------ view
class HelpView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        root.addWidget(
            ViewHeader(
                "Help & Documentation",
                "Everything you need to know about using Sales Assistant.",
            )
        )

        # Search bar
        search_row = QHBoxLayout()
        search_lbl = QLabel("Search:")
        search_lbl.setStyleSheet(f"color: {TEXT_MUTED};")
        search_row.addWidget(search_lbl)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter topics… (e.g. 'email', 'token', 'fiscal')")
        self.search.textChanged.connect(self._filter_topics)
        search_row.addWidget(self.search, 1)
        root.addLayout(search_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: topic list
        self.topic_list = QListWidget()
        self.topic_list.setMinimumWidth(220)
        self.topic_list.setMaximumWidth(280)
        self.topic_list.setAlternatingRowColors(True)
        for title, _ in HELP_TOPICS:
            self.topic_list.addItem(QListWidgetItem(title))
        self.topic_list.itemSelectionChanged.connect(self._show_topic)
        splitter.addWidget(self.topic_list)

        # Right: content pane
        self.content = QTextBrowser()
        self.content.setOpenExternalLinks(False)
        self.content.setStyleSheet(
            f"QTextBrowser {{ background: {SURFACE}; border: 1px solid {BORDER};"
            f" border-radius: 8px; padding: 20px 28px; color: {TEXT}; font-size: 13px; }}"
        )
        splitter.addWidget(self.content)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 800])
        root.addWidget(splitter, 1)

        # Select first topic
        self.topic_list.setCurrentRow(0)

    def _show_topic(self) -> None:
        items = self.topic_list.selectedItems()
        if not items:
            return
        idx = self.topic_list.row(items[0])
        # Find in full list by matching title
        title = items[0].text()
        for t, body in HELP_TOPICS:
            if t == title:
                self.content.setHtml(_styled_html(body))
                return

    def _filter_topics(self, text: str) -> None:
        needle = text.strip().lower()
        self.topic_list.clear()
        for title, body in HELP_TOPICS:
            hay = (title + " " + body).lower()
            if not needle or needle in hay:
                self.topic_list.addItem(QListWidgetItem(title))
        if self.topic_list.count():
            self.topic_list.setCurrentRow(0)
        else:
            self.content.setHtml(
                f"<p style='color:{TEXT_MUTED}'>No topics match your search.</p>"
            )


def _styled_html(body: str) -> str:
    return f"""
<html><head><style>
body {{ font-family: 'Segoe UI Variable', 'Segoe UI', sans-serif;
       font-size: 13px; color: #0F172A; line-height: 1.65; }}
h2 {{ font-size: 18px; font-weight: 700; color: #0F172A; margin: 0 0 12px 0; }}
h3 {{ font-size: 14px; font-weight: 600; color: #1E40AF; margin: 16px 0 6px 0; }}
p  {{ margin: 0 0 10px 0; }}
ul, ol {{ margin: 6px 0 12px 0; padding-left: 22px; }}
li {{ margin: 4px 0; }}
code {{ background: #F1F5F9; border: 1px solid #E2E8F0; border-radius: 3px;
       padding: 1px 4px; font-family: Consolas, monospace; font-size: 12px; }}
b {{ color: #0F172A; }}
</style></head><body>{body}</body></html>
"""
