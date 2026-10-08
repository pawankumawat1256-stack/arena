# Ebook Business Agent

An AI agent system that runs an ebook business: it researches a topic, drafts the book, proposes cover concepts, computes prices and royalties, writes marketing, sells through Gumroad, Stripe, and KDP, delivers downloads, sends email, publishes to Instagram, and reports on sales.

- **Language and stack:** Python 3.11+, LangChain (OpenAI chat models in JSON mode), FastAPI, SQLAlchemy 2 (SQLite by default, PostgreSQL supported), Streamlit, APScheduler 3.x, Docker Compose.
- **Design rule:** models write text and proposals. Code does the money math (`core/pricing.py`), the access decisions, and the publishing quota checks. Models never compute fees or royalties.

## Contents

- [Project tree](#project-tree)
- [Requirements](#requirements)
- [Environment variables](#environment-variables)
- [Setup](#setup)
- [First ebook, end to end](#first-ebook-end-to-end)
- [API endpoints](#api-endpoints)
- [Scheduled jobs](#scheduled-jobs)
- [Database schema](#database-schema)
- [How the business rules work](#how-the-business-rules-work)
- [Verified behaviour and where the code differs from the brief](#verified-behaviour-and-where-the-code-differs-from-the-brief)
- [Security notes](#security-notes)
- [Testing](#testing)
- [Known limits](#known-limits)

## Project tree

```
ebook-agent/
├── main.py                  FastAPI app: agent endpoints, webhooks, downloads, unsubscribe
├── scheduler.py             APScheduler jobs (separate process)
├── dashboard.py             Streamlit dashboard (reads the DB, calls the API)
├── config.py                Settings from environment (.env), utcnow()
├── requirements.txt         Runtime dependencies
├── requirements-dev.txt     Adds pytest for development
├── pytest.ini
├── .env.example             Every setting, grouped by service
├── Dockerfile               python:3.11-slim, non-root user, uvicorn on :8000
├── docker-compose.yml       api, scheduler, dashboard services
├── .dockerignore / .gitignore
├── core/
│   ├── llm.py               ChatOpenAI factory (fails clearly without OPENAI_API_KEY)
│   ├── agent.py             ResearchAgent, WriterAgent, DesignerAgent, PricingAgent,
│   │                        MarketingAgent, AnalyticsAgent (schema-checked outputs)
│   ├── pricing.py           KDP, GST, withholding, Gumroad, Payhip math (no LLM)
│   └── pipeline.py          Runs the agents in order; records every run; CLI entry point
├── db/
│   ├── database.py          Engine (SQLite pragmas or PostgreSQL), sessions, init_db()
│   └── models.py            Ebook, Product, Sale, Click, MarketingPost, EmailLog,
│                            SequenceEnrollment, BuyerAccess, AgentRun, WebhookEvent, User
├── integrations/
│   ├── net.py               HTTP helper: timeouts, retries, Retry-After, error parsing
│   ├── gumroad.py           Sales API, resource subscriptions, Ping parsing, CLI
│   ├── instagram.py         Graph API publishing (container, status, publish), live quota
│   ├── gmail_client.py      Gmail: create a draft, then send it
│   ├── google_auth.py       OAuth for Gmail and Drive (one token), CLI
│   ├── drive_client.py      Drive upload for product files
│   ├── notion_client.py     Manuscript pages (batched children, text limits)
│   └── stripe_client.py     Checkout sessions, webhook signature check, fee lookup
├── services/
│   ├── security.py          Token hashing, HMAC, API-key and webhook-token checks
│   ├── sales.py             Webhook intake and processing, Gumroad reconciliation,
│   │                        Stripe events, manual sales, analytics snapshot
│   ├── buyers.py            Buyer accounts, download links, access email, sequences,
│   │                        unsubscribe, lead capture
│   └── marketing.py         Posts and email sequences, weekly slots, quota-aware publishing
└── tests/                   92 offline tests (fake LLM, Gmail, Gumroad, Instagram, Stripe)
```

## Requirements

**Software**

- Python 3.11 or newer (tested here on 3.11.2).
- Docker and Docker Compose v2 for the containerized setup.

**Python packages** (`requirements.txt`, ranges as pinned):

| Package | Range | Purpose |
|---|---|---|
| fastapi | >=0.115,<1 | API |
| uvicorn[standard] | >=0.30,<1 | ASGI server |
| pydantic / pydantic-settings | >=2.7 / >=2.4 | Validation, `.env` loading |
| email-validator, python-multipart | | Email fields, form parsing |
| sqlalchemy | >=2.0.30,<3 | ORM (SQLite and PostgreSQL) |
| psycopg[binary] | >=3.2,<4 | PostgreSQL driver |
| httpx | >=0.27,<1 | HTTP client |
| langchain-core, langchain-openai | <2 | Agents and the OpenAI chat model |
| google-api-python-client, google-auth, google-auth-oauthlib, google-auth-httplib2 | | Gmail and Drive |
| stripe | >=11,<14 | Checkout and webhook verification |
| APScheduler | >=3.10,<4 | Background jobs |
| streamlit | >=1.40,<2 | Dashboard |
| pandas | >=2.2,<3 | Dashboard tables |
| tzdata | >=2024.1 | Asia/Kolkata for the scheduler, even on slim images |

**Accounts you need** (the code assumes you have the keys ready):

| Service | What to create | Used for |
|---|---|---|
| OpenAI | API key with access to your chosen model | All agents |
| Gumroad | Access token with the `view_sales` scope | Reading sales; registering webhooks |
| Instagram | Business or Creator account linked to a Facebook Page, and a Meta app with a long-lived token | Publishing posts |
| Google Cloud | OAuth client (Desktop app) with the Gmail and Drive APIs enabled | Sending access emails; Drive uploads |
| Notion | Internal integration, with the target page shared to it | Manuscript pages |
| Stripe (optional) | Secret key and a webhook endpoint | Card checkout |
| A public HTTPS host | For webhooks and download links | Gumroad, Stripe, and buyers must reach your app |

## Environment variables

Copy `.env.example` to `.env` and fill in the values. The file lists every setting with a comment. The most important ones:

| Variable | Required for | Notes |
|---|---|---|
| `APP_SECRET_KEY` | Unsubscribe links | 32+ random characters. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `API_KEY` | Every protected endpoint | Sent as `X-API-Key`. The API refuses all protected calls while this is empty |
| `PUBLIC_BASE_URL` | Download, unsubscribe, webhook, and checkout links | Your public HTTPS address |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | Agents | Default model `gpt-4o`; set any chat model you have access to |
| `GUMROAD_ACCESS_TOKEN` | Gumroad sales reads and webhook registration | Needs `view_sales` |
| `GUMROAD_WEBHOOK_TOKEN` | Gumroad webhooks | Becomes `?token=` on the webhook URL |
| `GOOGLE_CLIENT_SECRETS_FILE`, `GOOGLE_TOKEN_FILE` | Gmail, Drive | Files under `secrets/` |
| `BUSINESS_POSTAL_ADDRESS` | Marketing email | Marketing email is blocked until this is set (required for CAN-SPAM-style footers) |
| `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_USER_ID` | Instagram publishing | Facebook Login path |
| `GRAPH_API_VERSION` | Instagram | Default `v21.0` on `graph.facebook.com` |
| `INSTAGRAM_DAILY_POST_LIMIT` | Fallback only | Used when the live quota endpoint cannot be read. Default 100 |
| `NOTION_API_KEY`, `NOTION_PARENT_PAGE_ID` | Manuscript pages | Optional. If unset, the manuscript is saved locally only |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Stripe checkout | Optional |
| `DATABASE_URL` | Storage | SQLite by default. PostgreSQL: `postgresql+psycopg://user:pass@host:5432/ebook_agent` |
| `DASHBOARD_PASSWORD` | Dashboard login | Required when `APP_ENV=production` |
| `KDP_LIST_PRICE_USD`, `KDP_FILE_SIZE_MB`, `TOOLKIT_PRICE_USD`, `BUNDLE_PRICE_USD` | Pricing math | Defaults 9.99, 3.0, 19.00, 24.00 |
| `AMAZON_IN_WITHHOLDING_RATE` | Amazon.in royalty view | Default 0.10 |
| `SCHEDULER_TIMEZONE`, `ANALYTICS_CRON_HOUR`, `MARKETING_CRON_WEEKDAY`, `MARKETING_CRON_HOUR` | Scheduler | Defaults Asia/Kolkata, 07:00 daily, Monday 09:00 |

## Setup

For a step-by-step launch on a server (non-developer guide, with where to get each API key), see [GO-LIVE-CHECKLIST.md](GO-LIVE-CHECKLIST.md).

### Option A: Docker Compose

```bash
cd ebook-agent
cp .env.example .env            # then fill in the values
mkdir -p data secrets && sudo chown -R 1000:1000 data secrets   # the container runs as UID 1000
docker compose up -d --build
```

Services: `api` on port 8000 (OpenAPI docs at `/docs`), `scheduler`, and `dashboard` on port 8501. All three share `./data` (the SQLite file and generated manuscripts).

### Option B: Local Python

```bash
cd ebook-agent
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then fill in the values
uvicorn main:app --host 0.0.0.0 --port 8000          # terminal 1: API
python scheduler.py                                  # terminal 2: background jobs
streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501   # terminal 3: dashboard
```

### One-time service setup

1. **Google (Gmail and Drive).** In Google Cloud, create a Desktop OAuth client, download the JSON, and save it as `secrets/google_client_secret.json`. Then run `python -m integrations.google_auth` on a machine with a browser. It writes `secrets/google_token.json`. Scopes: `gmail.compose` (create and send drafts) and `drive.file` (files this app creates). Leave `GOOGLE_DRIVE_FOLDER_ID` empty unless the folder was created by this app.
2. **Gumroad webhooks.** Once `PUBLIC_BASE_URL` is live, run:
   ```bash
   python -m integrations.gumroad register    # subscribes the webhook URL to all 8 resource names
   python -m integrations.gumroad list        # check
   ```
   This calls `PUT /v2/resource_subscriptions` with Bearer auth for `sale`, `refund`, `cancellation`, `subscription_ended`, `subscription_restarted`, `subscription_updated`, `dispute`, and `dispute_won`. It is safe to run again.
3. **Map products.** Create each sellable product with the Gumroad product ID as `external_product_id`:
   ```bash
   curl -X POST https://YOUR_HOST/products -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
     -d '{"ebook_id":1,"tier":"basic","name":"Book (PDF)","price_usd":"9.99","platform":"gumroad","external_product_id":"GUMROAD_PRODUCT_ID","file_path":"/app/data/ebooks/book.pdf"}'
   ```
   A sale for an unmapped product is recorded as `failed` with a clear message, and it is retried after you add the product.
4. **Instagram.** Set the token and user ID. Posts need a public HTTPS JPEG URL (the app does not host images). Attach it with `POST /posts/{id}/image`.
5. **Stripe (optional).** Add a webhook endpoint at `https://YOUR_HOST/webhook/stripe` for `checkout.session.completed` and `charge.refunded`. For a card-checkout link, create a product with `platform: "stripe"`; the buyer link is `/buy/{product_id}`.
6. **Notion (optional).** Share the parent page with your integration and set `NOTION_PARENT_PAGE_ID`.

## First ebook, end to end

```bash
H='X-API-Key: YOUR_KEY'; B=https://YOUR_HOST
curl -X POST $B/research -H "$H" -H 'Content-Type: application/json' \
  -d '{"topic":"AI tools for solopreneurs","audience":"Solo founders in India","sources":[]}'   # returns run_id
curl $B/runs/1 -H "$H"                                   # poll until status is "succeeded"
curl -X POST $B/write  -H "$H" -H 'Content-Type: application/json' -d '{"ebook_id":1}'
curl -X POST $B/design -H "$H" -H 'Content-Type: application/json' -d '{"ebook_id":1}'
curl -X POST $B/price  -H "$H" -H 'Content-Type: application/json' -d '{"ebook_id":1}'
curl -X POST $B/market -H "$H" -H 'Content-Type: application/json' -d '{"ebook_id":1}'
```

Then:

1. Finish the book file (you edit the generated Markdown and produce the PDF). Place it under `DATA_DIR/ebooks/`.
2. Create products (`POST /products`), with `file_path` pointing at that file.
3. Attach an image to each post (`POST /posts/{id}/image`), then either schedule a time (`POST /posts/{id}/schedule`) or let the Monday job schedule the next drafts that have images.
4. Check the numbers on the dashboard's Overview and Analyst pages. Enter KDP royalties by hand on the Sales page (or with `POST /sales/manual`), because KDP has no public royalty API.

### Dashboard pages

- **Overview:** paid units, gross, net after fees, refund rate, clicks, and sales per 100 clicks (shown once there are at least 30 clicks), plus net sales by day. Totals are in USD. Test sales and non-USD sales are left out.
- **Ebooks:** the outline, the computed pricing with fees and net on each platform, the three cover concepts with prompts you can copy, the research claims to verify, and the files and products for each ebook.
- **Run agents:** start research for a new ebook, or run research, writing, design, pricing, or marketing for an existing one.
- **Analyst:** the latest analyst report for one ebook or for all ebooks: what is working, what is failing, how to improve sales, and the snapshot of numbers the analyst used.
- **Sales:** manual entry for KDP royalty lines and other platforms, the recent sales list, and resending an access link.
- **Marketing:** products, posts, and the image URL and publish time for each post. Times are entered in IST.
- **Runs:** every agent run with its status and error. The input and output are shown for finished runs.

## API endpoints

Protected endpoints need `X-API-Key`. Agent work returns `202` with a `run_id`; poll `GET /runs/{id}`.

| Method and path | Auth | What it does |
|---|---|---|
| `GET /health` | none | Liveness and database check |
| `POST /research` | key | Titles, audience, pain points, competitor gaps, keywords, and an outline. Creates the ebook when `ebook_id` is absent |
| `POST /write` | key | Writes every chapter in full, one model call per chapter, saves Markdown, and mirrors to Notion when configured |
| `POST /design` | key | Three cover concepts with hex colors, Google Fonts, Midjourney and DALL-E prompts (both say "no text"), and Canva steps |
| `POST /price` | key | Deterministic fee plan (in code) plus model-written tier copy |
| `POST /market` | key | Posts (caption, hashtags, image brief), 5-email launch and 3-email onboarding sequences |
| `POST /analytics` | key | Analyst report: what is working, what is failing, how to improve sales. Only sees the metrics snapshot |
| `POST /pipeline` | key | Research through market in one run |
| `GET /analytics/snapshot` | key | The metrics the analyst sees (USD sales, refunds, clicks, products, emails) |
| `GET /runs`, `GET /runs/{id}` | key | Run status, error, and output |
| `POST /ebooks`, `GET /ebooks`, `GET /ebooks/{id}` | key | Ebook records |
| `POST /products`, `GET /products` | key | Products and their platform IDs; optional Drive upload |
| `POST /leads` | key | Adds a lead to the launch sequence (for Tally or Zapier) |
| `POST /sales/manual` | key | Manual sale or KDP royalty line |
| `POST /access/{sale_id}/resend` | key | Issues a new link (the old one stops working) |
| `GET /posts`, `POST /posts/{id}/image`, `POST /posts/{id}/schedule` | key | Instagram queue |
| `POST /admin/gumroad/reconcile` | key | Re-reads recent Gumroad sales now |
| `POST /webhook/gumroad?token=…` | URL token | Gumroad Ping and resource-subscription receiver; answers at once |
| `POST /webhook/stripe` | Stripe signature | `checkout.session.completed`, `charge.refunded` |
| `GET /buy/{product_id}` | none | Redirects to Stripe Checkout for a Stripe product |
| `GET /download/{token}` | secret link | Serves the file while the link is valid |
| `GET`/`POST /unsubscribe?email=…&sig=…` | HMAC signature | Unsubscribes; also handles the one-click `List-Unsubscribe-Post` request |

## Scheduled jobs

Run by `scheduler.py` in Asia/Kolkata:

| Job | When | What |
|---|---|---|
| `email-sequences` | every 15 min | Sends the next due onboarding or launch email (one per person per run). Blocked when `BUSINESS_POSTAL_ADDRESS` is empty |
| `publish-posts` | every 15 min | Publishes due Instagram posts, checking the live quota first |
| `retry-webhooks` | every 30 min | Re-processes failed webhook events (up to 8 attempts) |
| `gumroad-reconcile` | every 6 h | Re-reads Gumroad sales from the last 3 days. Recovers any ping that never arrived |
| `reap-stale-runs` | every 30 min | Marks runs stuck in `queued` or `running` for over 2 h as failed (after a restart) |
| `daily-analytics` | 07:00 daily | Analyst report for each ebook and for the whole business |
| `weekly-post-slots` | Monday 09:00 | Schedules the next `MARKETING_POSTS_PER_WEEK` draft posts that have images, two days apart |

## Database schema

SQLAlchemy models in `db/models.py`. Money is stored as integer cents. Datetimes are naive UTC.

| Table | Key columns and constraints |
|---|---|
| `ebooks` | slug (unique), title, subtitle, topic, audience, stage, list_price_cents, research/outline/cover/pricing/email JSON, manuscript_path, notion_page_url |
| `products` | ebook_id, tier (unique with ebook_id), name, price_cents, platform (kdp / gumroad / stripe / manual), external_product_id (unique), permalink, checkout_url, file_path, drive_file_id, active |
| `sales` | platform + external_id (unique), ebook_id, product_id, user_id, buyer_email, amount/fee/net cents, currency, country, is_refunded, is_test, sold_at, raw_json |
| `users` | email (unique), full_name, country, source, unsubscribed |
| `buyer_access` | token_hash (unique; SHA-256 only), sale_id (unique), expires_at, max_downloads, download_count, revoked |
| `email_logs` | sequence (access / onboarding / launch), step, subject, body, status (sent / failed), provider message id, error |
| `sequence_enrollments` | user_id + ebook_id + sequence (unique), next_step, next_send_at, status |
| `marketing_posts` | caption, hashtags, image_url, status (draft / scheduled / publishing / published / failed), scheduled_for, published_at, platform_post_id, error |
| `clicks` | ebook_id, product_id, channel, campaign, referrer, ip_hash |
| `agent_runs` | task, ebook_id, status (queued / running / succeeded / failed), input_json, output_json, error |
| `webhook_events` | source, dedupe_key (unique), payload_json, status (received / processed / ignored / failed), attempts, last_error |

## How the business rules work

**Sales and access (Gumroad and Stripe).** A ping is only a trigger. The app reads the sale back from `GET /v2/sales/:id` and decides from its current state:

- Paid, not test, not refunded, no open dispute, subscription not ended: access is granted once. A download link is emailed as a Gmail draft that is sent immediately. Renewals of an active subscription do not resend it.
- Refunded, charged back, disputed (open), or ended subscription: access is revoked. Dispute won or subscription restarted: access is restored, if the sale is not refunded.
- A refund that arrives before its sale creates a refunded record with no access. Duplicate and out-of-order pings give the same result.

**Download links.** Valid for `BUYER_LINK_TTL_HOURS` (default 72) and `BUYER_MAX_DOWNLOADS` (default 5). Only the SHA-256 hash is stored. Each download is counted with an atomic update. Files are served only from inside `DATA_DIR`. `POST /access/{sale_id}/resend` replaces the link.

**Email.** Onboarding (3 emails, days 0, 1, 3) follows a purchase. Launch (5 emails, days 0, 2, 4, 7, 10) follows a lead. Marketing emails carry the postal address and an unsubscribe link, and include `List-Unsubscribe` headers. A send failure is logged and retried in 6 hours. After 3 failures on the same step, the enrollment is paused.

**Pricing.** `core/pricing.py` implements:

- Distribution strategy: **wide distribution**. The book is not enrolled in KDP Select. The Kindle edition is sold through Amazon KDP, and the PDF is sold directly on Gumroad and Payhip.
- KDP US: 70% of (list price − delivery), where delivery is US$0.15 per MB. This applies in the US$2.99–12.99 band. Otherwise 35% of the list price.
- KDP India: 18% GST is removed first. 70% applies only with KDP Select and the ₹99–599 band, with delivery at ₹7 per MB. Because the book is wide (not KDP Select), Indian sales earn 35%.
- Gumroad: 10% + US$0.50 direct, or 30% with Discover.
- Payhip: Free 5%, Plus 2% at US$29/month, Pro 0% at US$99/month, before processor fees.
- Break-even file size for the 70% royalty against 35%: about 33.3 MB at $9.99.

The price stage fails only for hard errors (a price outside the KDP band). A bundle that is not cheaper than Basic plus Pro, or a Basic price other than the approved $9.99, produces a warning in the output.

**Instagram.** Before each post, the scheduler reads `GET /{ig-user-id}/content_publishing_limit`. If that fails, it falls back to `INSTAGRAM_DAILY_POST_LIMIT` minus the posts it published in the last 24 hours. A quota error defers the rest of the batch.

## Verified behaviour and where the code differs from the brief

The brief supplied some Gumroad and Instagram details. Official sources were checked for each. Where they differ, the code follows the official behaviour and this section says so.

| Topic | Brief said | Official source says | What the code does |
|---|---|---|---|
| Ping format | JSON body | Form-encoded (`application/x-www-form-urlencoded`) | Accepts both |
| Ping signature | `X-Gumroad-Signature` with `GUMROAD_SECRET` | Pings are unsigned. No signature header is documented | Does not verify a signature. Uses the URL token, then reads the sale back through the API |
| Delivery | — | At-least-once, unordered, retried only on 499/500/502/503/504 at 1, 3, 10, 60 min; 5 s to respond | Stores and acknowledges at once; dedupe key `gumroad:{resource_name}:{sale_id}`; retry job and 6-hourly reconcile |
| Sales read-back | `GET /v2/sales/:id` | Sales object includes `refunded`, `chargedback`, `disputed`, `dispute_won`, `ended`, `price`, `gumroad_fee` | Uses those fields. The `sale` wrapper key is handled with a fallback |
| Instagram quota | 100 per 24 h | Meta's publishing guide says 100; the `content_publishing_limit` reference says 50 | Reads the live value. Falls back to the configured 100 |
| Graph version | v21.0 | v21.0 is listed as available until 21 Jan 2027 (per a 2026 changelog summary). Newer versions are live | Defaults to `v21.0`; change with `GRAPH_API_VERSION` |
| Rate limit | 200 calls/hour/app | Not confirmed. A 2026 guide cites a formula based on impressions | Retries 429 and rate-limit codes (4, 17, 32, 613) with backoff. Does not hard-code the 200 figure |
| Permissions | `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` | Not verified here | Your token must be generated with these. The code does not check them; a missing permission shows up as an API error on the run |

Other points that depend on external services:

- **Gmail:** `gmail.compose` covers creating and sending drafts. Google treats it as a restricted scope, so a public app needs Google's verification. A private app used by its owner does not.
- **Gumroad resource subscriptions:** the list response key (`resource_subscriptions`) is parsed defensively and was not checked against a live response.
- **Stripe:** fees come from the balance transaction when Stripe returns it. Otherwise the fee is recorded as 0 and the net equals the gross.

## Security notes

- Protected endpoints fail closed when `API_KEY` is unset. Keys are compared in constant time.
- The Gumroad webhook URL contains a secret token. Web-server access logs record query strings. Keep the logs private, and rotate `GUMROAD_WEBHOOK_TOKEN` and re-run `register` if they leak.
- Stripe webhooks are checked with `STRIPE_WEBHOOK_SECRET` and a timestamp tolerance.
- Download and unsubscribe tokens are random. Only hashes or HMACs are stored or checked.
- Webhook payloads are stored for audit and contain buyer emails. Apply your retention policy.
- The dashboard refuses to start in production without `DASHBOARD_PASSWORD`.
- Put the API behind HTTPS. Keep `.env` and `secrets/` out of Git (they are in `.gitignore`).

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q
```

122 tests, all offline. They use a fake chat model, and fake Gmail, Gumroad, Instagram, Stripe, and Notion calls. They cover:

- Royalty and fee math against worked examples
- Each agent's output validation (hex colors, "no text" in image prompts, sequence lengths and delays)
- A full pipeline run through the database
- Webhook handling: duplicates, refund-before-sale, retries, unmapped products
- Access rules, link expiry and limits, path safety, re-issue
- Email sequences: timing, unsubscribe, blocked without a postal address, failures and retry
- Instagram: quota (live and fallback), deferral, transient and permanent failures, publish flow
- HTTP retries, Stripe signature and checkout, Notion batching, scheduler job registration
- The Streamlit dashboard (`tests/test_dashboard.py`): login, every page with and without data, the analyst report, the manual-sale and scheduling guards, readable API error messages, and escaping of model output in the cover colours

Also run during development: `compileall` passes, and `ruff check` (E, F, W, I, B rules, 130-column lines) passes for `dashboard.py` and `tests/test_dashboard.py`. Ruff still reports long lines (E501) and FastAPI `Depends` defaults (B008) in other modules.

**Not run here:** the Docker image build (Docker was not available in the build environment), a live call to OpenAI, Gumroad, Instagram, Gmail, Drive, Notion, or Stripe (the build sandbox only reaches package registries and GitHub), and PostgreSQL. The code paths for those are covered by fakes, not by live calls.

Smoke-tested by starting the processes: the API (health, 401 without a key, 401 for a wrong webhook token, 400 for a malformed ping, 200 with a failed event recorded when Gumroad is not configured, failed agent run recorded with its error), the Streamlit dashboard (all seven pages and the Ebooks tabs load with no exceptions in a headless browser, against a throwaway database with sample data), and the scheduler (seven jobs registered, clean shutdown on SIGTERM).

## Known limits

- **Single-host storage.** SQLite with WAL is the default. Three containers sharing `./data` work on one host. For multiple hosts or heavy write load, set `DATABASE_URL` to PostgreSQL. That path is supported by the code but was not run here.
- **Background runs live in the API process.** A restart mid-run marks the run failed after 2 hours (the `reap-stale-runs` job). Run it again.
- **Images.** The app does not host or resize images. Instagram needs a public HTTPS JPEG. Convert and host it yourself, then attach the URL.
- **KDP.** No public royalty API exists, so KDP lines are entered manually.
- **Payhip and Canva.** Payhip is a fee calculator only. Canva is a manual step in the cover instructions.
- **Tax.** GST and withholding rates are settings, not tax advice. Confirm them with your accountant.
- **Scope.** This is a working service with tests. It is not a managed product: there is no user management for the dashboard beyond one shared password, and no rate limiting on public routes beyond what the platforms enforce.
