# FinSight AI — local development MVP

Single-user personal finance assistant: FastAPI, LangChain (LCEL + `create_agent` tools), OpenAI, Pinecone, SQLite (SQLAlchemy + Alembic), PDF ingestion, and a plain HTML/CSS/JS dashboard.

## Setup
1. Python 3.11/3.12: `python -m venv .venv`, activate, `pip install -r requirements-dev.txt`.
2. Create `.env` with your OpenAI and Pinecone credentials (see *Environment variables*).
3. In Pinecone, create a **dense cosine** index named `finsight-documents` with **1536 dimensions** (matches `text-embedding-3-small`). No integrated embedding.
4. `python -m uvicorn app.main:app --reload` → http://127.0.0.1:8000 (API docs at `/docs`).
   Database migrations run automatically on startup.
5. Upload an **unlocked, text-based** bank statement PDF. Transactions are extracted, reconciled and shown on the dashboard; ask questions in Agent mode.

## Environment variables
| Name | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | required | LLM, embeddings, fallback categorization |
| `PINECONE_API_KEY` | required | vector store |
| `PINECONE_INDEX` | `finsight-documents` | index name |
| `OPENAI_MODEL` / `EMBEDDING_MODEL` | `gpt-4.1-mini` / `text-embedding-3-small` | |
| `DATABASE_URL` | `sqlite:///<project>/data/finsight.sqlite3` | any SQLAlchemy URL (PostgreSQL later) |
| `LLM_CATEGORIZATION` | `true` | send *only* rule-unmatched transaction descriptions to OpenAI for categorization |
| `DEMO_USER_ID` | `demo-user` | identity used in dev mode (no Firebase configured) |
| `FIREBASE_PROJECT_ID` | empty | enables Firebase sign-in; every `/api/*` call then needs a valid ID token |
| `FIREBASE_WEB_API_KEY`, `FIREBASE_AUTH_DOMAIN` | empty | public Firebase web config used by the browser |
| `ADMIN_EMAILS` | empty | comma-separated emails that may change shared market data (FII/DII CSV import). Sign-in itself is open to any verified Google account |
| `LANGSMITH_API_KEY` (or `LANGSMITH_KEY`) | empty | enables LangSmith tracing of every flow (see *Observability*) |
| `LANGSMITH_PROJECT` / `LANGSMITH_ENDPOINT` | `finsight` / US | project name; EU workspaces use `https://eu.api.smith.langchain.com` |
| `LANGSMITH_TRACING` / `LANGSMITH_TRACE_READS` | `true` / `true` | master switch / also trace read-only GET requests |
| `APP_ENV` | `development` | tag on every trace (`env:production` etc.) |
| `ENABLE_SCHEDULER`, `SMTP_*`, `ALERT_TO` | off | daily cost-basis email demo |

## Architecture: two pipelines from one upload
```
PDF ─┬─ Pipeline A (RAG): plain text → chunks → OpenAI embeddings → Pinecone → LCEL chain → cited answer
     └─ Pipeline B (bank only): layout text → parser → Pydantic validation → categorize → reconcile → SQL
                                                                             ↓
                                       deterministic analytics (Decimal) → API/dashboard + agent tools
```
Pinecone is for *finding text*. SQL is the **source of truth for numbers**; totals are never computed by an LLM from retrieved chunks.

| Module | Role |
|---|---|
| `app/ingest.py` | validation, text extraction (plain + layout), Pinecone indexing, triggers structured import |
| `app/parsers/` | `normalize.py` (amounts, dates, merchants), `generic.py` (table parser), registry in `__init__.py` |
| `app/categorization.py` | merchant overrides → keyword rules → optional schema-constrained LLM fallback |
| `app/reconciliation.py` | balance equation, stated totals, running-balance continuity, duplicate rows |
| `app/statement_import.py` | orchestration + idempotent persistence + cross-statement dedupe |
| `app/analytics.py` | income/spending/savings, monthly cash flow, categories, trends, recurring, transfers |
| `app/tools.py` | read-only LangChain tools (user bound server-side, Pydantic-validated args, no SQL from the LLM) |
| `app/agent.py` | `create_agent` with structured tools + RAG search tool |
| `app/models.py`, `migrations/` | SQLAlchemy models; money stored as integer paise, exposed as `Decimal` |

### Financial definitions (`app/analytics.py`)
- **income** = credits excluding investment/internal transfers and refunds
- **spending** = debits excluding investment/internal transfers, minus refunds
- **net invested** = debits to investment platforms − credits back from them
- **savings rate** = (income − spending) / income; rates are `null` (not 0) when income is 0
- **recurring** = same merchant, ≥3 distinct months, amounts within ±10% of median, median gap 20–40 days

### Reconciliation
Every import records: `opening + credits − debits = closing`, extracted vs printed totals, per-row running balance (catches missing/misread rows), and repeated rows. Status is `reconciled`, `mismatch` or `unverifiable`, and is shown on the dashboard and returned by agent tools. Ambiguous rows become warnings / `needs_review`, never guesses.

## Investment imports (Phase 3)
Upload with type **Investment report**. Supported (structure verified on real exports; test fixtures are fictional):

| Document | Format | Parser | Stored as | Checks |
|---|---|---|---|---|
| Depository holding statement (INDmoney/INDstocks, CDSL style) | PDF | `dp_holding_statement` | holdings snapshot (ISIN, qty, reported rate/value, as-of) | qty components, qty × rate = value, rows = printed total |
| Groww "Statement of Accounts of Funds" | .xlsx / .csv | `groww_funds_ledger` | broker ledger entries (deposit / withdrawal / buy / sell settlement / charges) | running balance |
| Groww mutual fund order history | .xlsx / .csv | `groww_mf_orders` | investment transactions | per-row validation; units × NAV ≈ amount (row format unverified: sample had no orders) |

Rules: only the latest snapshot per broker account counts; positions are combined across platforms by ISIN; values are labelled *reported* (statement, as of its date) — there are no live prices, cost basis, P&L or XIRR yet. Spreadsheets are never embedded or sent to an LLM. Holder name, PAN, address and full account IDs are never stored (last 4 digits only).

Endpoints: `GET /api/investments/imports|holdings|holdings/history|cash-flows|fund-orders`. Agent tools: `get_investment_holdings`, `get_holding_history`, `get_broker_cash_flows`, `get_mutual_fund_orders`.

Also supported: Groww **Stocks Holdings Statement** (with average buy price → statement cost basis) and **Stocks P&L Report** (realised/open lots, charges → realised P&L and XIRR), and the **INDmoney US stocks holdings report** (USD, fractional shares, tickers instead of ISINs). Foreign holdings keep native USD values and are converted to INR at the live rate (`USDINR=X`); INR P&L on them uses today's rate, not the purchase-date rate. Quantities are stored to 8 decimals and prices to 6.

Not yet supported: Zerodha exports, Groww holdings / stock order history, tradebooks, capital-gains reports, corporate actions.

## Live market data, weekly AI review, alerts (Phase 5/6)
- **Market data** (`app/market_data.py`): pluggable `MarketDataProvider`; default `YahooProvider` uses Yahoo Finance's public chart/search endpoints — free, no key, **unofficial**, NSE/BSE prices typically ~15 min delayed. Results are cached (prices 5 min, symbol lookups 7 days) and always carry source + timestamp. Statistics (returns, 50/200-day averages, trend, volatility, max drawdown, 52-week range) are computed deterministically. For an official feed implement the provider interface (e.g. Upstox / Angel One SmartAPI) and call `set_provider()`.
- **Live portfolio** (`app/advisor.py`): latest statement holdings (authoritative quantity) merged by ticker with manual holdings (cost basis) → market value (Decimal), weights, day change, concentration, unrealized P&L where cost is known.
- **Weekly review**: deterministic inputs (portfolio, NIFTY 50, 90-day bank cash flow, headlines marked untrusted) → LLM writes a schema-constrained report (stances, bull/bear cases, actions, bull/base/bear scenarios, data limitations). Inputs are stored with each review. No price targets or guarantees; a fixed disclaimer is always attached.
- **Notifications** (`app/notifications.py`, `app/jobs.py`): idempotent per dedupe key; email deliveries are tracked, retried (max 5) and only sent to the user's verified sign-in email (or `ALERT_TO` in dev mode).
- **Schedule** (needs `ENABLE_SCHEDULER=true`; runs in the web process — use one worker): weekly review `REVIEW_DAY`/`REVIEW_HOUR` (default Sat 09:00 IST), price-move check Mon–Fri 16:15 IST (threshold per user, default 5%), email retry every 30 min. "Run review now" in Settings/Overview (10-minute cooldown).
- **Email via Gmail**: enable 2-step verification, create an App Password, then set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USERNAME=<gmail>`, `SMTP_PASSWORD=<app password>`, `SMTP_FROM=<gmail>`.

Endpoints: `GET /api/market/quote?q=`, `GET /api/investments/live`, `POST /api/reviews/run`, `GET /api/reviews/latest|{id}`, `GET /api/notifications`, `POST /api/notifications/{id}/read|read-all`, `GET|PUT /api/settings`. Agent tools added: `get_stock_quote`, `get_market_news`, `get_live_portfolio`, `get_latest_portfolio_review`.

## Markets tab
- **FII/DII flows**: NSE's public `fiidiiTradeReact` JSON (cookie-primed session; unofficial; latest trading day only, INR crore, all exchanges). Each day is stored in `market_flows` (weekday job 19:30 IST + on-demand when the tab opens, max once per 30 min), so history accumulates from the first day collected; monthly totals sum stored days. Admins (`ADMIN_EMAILS`) backfill with `POST /api/markets/fii-dii/import` (CSV, long `date,category,buy,sell,net` or wide `Date,FII Net,DII Net[,FII Buy,...]`).
- **NIFTY 50**: daily closes via the market data provider (`GET /api/markets/index?range=1mo|6mo|1y|5y`).

## Authentication (Firebase)
Without `FIREBASE_PROJECT_ID` the app runs in **dev mode** as `DEMO_USER_ID` (no login). With it, the browser signs in with Google through the Firebase JS SDK and sends `Authorization: Bearer <ID token>`; `app/auth.py` verifies the signature against Google's rotating public certs, the audience (project id), issuer and expiry, then requires a verified email. **Sign-in is open to any Google account.** The token's uid becomes `user_id`, so every query, tool and Pinecone filter is scoped to that user, and the verified Google email is the only address that user's review/alert emails go to. No Firebase service-account key is used.

Because anyone can sign in: chat (40/h), uploads (60/h), quote lookups (120/h) and feedback are rate-limited per user (`app/ratelimit.py`, in memory: one process only), manual reviews have a 10-minute cooldown, and writes to data shared by all users (FII/DII CSV import) need an `ADMIN_EMAILS` address. Weekly reviews run for every user with holdings, so OpenAI cost grows with users. Before going public, add your production domain under Firebase → Authentication → Settings → Authorized domains.

Data uploaded in dev mode belongs to `demo-user`; after enabling sign-in, re-upload statements under your account.

## Observability (LangSmith)
Set `LANGSMITH_API_KEY` and every flow is traced to the `LANGSMITH_PROJECT` project (`app/observability.py`):

| Trace | Contents |
|---|---|
| Each API request (`GET /api/investments/live`, `POST /api/upload`, …) | root run: user id, status code, latency, route; child runs nest under it. Notification polling, static files and `/health` are skipped; `LANGSMITH_TRACE_READS=false` skips all GETs |
| `assistant_chat` / `document_search` | full agent run: prompts, every tool call and result, LLM calls, tokens. Grouped per conversation as a LangSmith **thread** |
| `portfolio_review`, `review_writer`, `llm_categorize` | review inputs build, the LLM call and its structured output; categorisation batches |
| `ingest_pdf`, `ingest_spreadsheet`, `extract_pdf_text`, `import_*`, `index_in_pinecone` | metadata only: file type/size, pages, parser, row counts, status, reconciliation check names, warning codes |
| `yahoo_finance`, `nse_fii_dii`, `refresh_fii_dii` | market data calls (cache misses), latency, failures |
| `notify`, `deliver_emails`, `send_email`, `job.*` | notifications, email delivery (recipient domain + subject only), scheduled jobs as their own traces |

Privacy: a client-side anonymizer masks emails/UPI ids, PAN, Aadhaar, phone, card and long account numbers in everything sent. Upload pipelines never send document text, rows or amounts. AI-flow traces contain what was already sent to OpenAI. Users rate answers with 👍/👎 (+ optional comment); `POST /api/feedback` stores it as `user_rating` feedback on that exact run. Filter on it in LangSmith to find bad answers. Tests never contact LangSmith (`tests/test_observability.py` records requests in memory).

## Privacy: "Local only" uploads
Ticking **Local only** on upload (`local_only=true`) extracts and reconciles transactions into the local database but sends nothing to OpenAI or Pinecone: no embeddings, no LLM categorization (unmatched rows become `Other`, flagged for review). The document is then not searchable in chat. Re-uploading the same file without the flag indexes it once.

## Sharing a statement layout safely
```powershell
python -m app.redact path\to\statement.pdf -o redacted.txt
```
Writes the first 3 pages as a layout skeleton (unknown words → `XXXX`, digits outside dates scrambled, column spacing kept) plus a parser diagnosis. Read the file before sharing it.

## HTTP endpoints
- `GET /api/auth/config` (public), `GET /api/me`
- `POST /api/upload` multipart `file`, `category=bank|investment`, optional `local_only=true` → includes `structured` import result for bank statements
- `POST /api/chat` `{"question": "...", "mode": "agent"|"rag"}` → `answer`, `tools_used`
- `GET /api/statements` imports with reconciliation checks and warnings
- `GET /api/analytics/summary|monthly|categories?start=YYYY-MM-DD&end=YYYY-MM-DD`
- `GET /api/analytics/recurring`, `GET /api/analytics/trends?month=YYYY-MM`
- `GET /api/transactions?start&end&category&type&search&needs_review&limit&offset`
- `PATCH /api/transactions/{id}` `{"category": "Shopping", "apply_to_merchant": true}` manual correction (+ remembered merchant rule)
- `GET /api/categories`, `POST /api/holdings`, `GET /api/portfolio`, `GET /health`

## Agent tools
`get_spending_summary`, `get_monthly_expenses`, `get_category_spending`, `get_spending_trends`, `get_recurring_payments`, `get_bank_transactions`, `get_largest_transactions`, `get_investment_transfers`, `get_statement_import_status`, `search_uploaded_documents` (RAG), `get_portfolio_cost_basis`.

## Database migrations
Applied automatically at startup (`app.db.init_db`). Manually: `alembic upgrade head`. New migration after changing `app/models.py`: `alembic revision --autogenerate -m "describe change"` then review the generated file.

## Tests
`python -m pytest` — uses a temporary database per test, fake API keys, mocked Pinecone, and a scripted fake chat model (no network). Fixtures are synthetic (`tests/fixtures/demo_statement_2026.pdf`, plus PDFs generated by `tests/pdf_factory.py`).

## Known limitations
- Parser coverage: one generic parser for selectable-text `Date | Narration | Debit | Credit | Balance` tables, tested on synthetic layouts only. No bank-specific parsers yet (add them to `app/parsers/` with redacted samples + tests). No OCR, no password-protected PDFs.
- Pinecone indexing and SQL writes are not one transaction; a Pinecone failure aborts the upload before anything is saved.
- Investment PDFs are indexed for search only (structured import is Phase 3). Holdings are manual, float-based legacy data.
- Firebase sign-in protects the API, but there is still no encryption at rest, audit log, rate limiting, or secure-delete flow. Do not expose the server publicly yet.
- Scheduler runs in the web process (unsafe with multiple workers). Market data is from an unofficial, delayed source; reviews are educational analysis, not registered investment advice.
