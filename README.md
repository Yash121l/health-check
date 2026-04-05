# health-check

[![Health Check](https://github.com/Yash121l/health-check/actions/workflows/health-check.yml/badge.svg)](https://github.com/Yash121l/health-check/actions/workflows/health-check.yml)
[![Status Page](https://img.shields.io/badge/Status%20Page-Live-22c55e?style=flat)](https://Yash121l.github.io/health-check/)

A production-grade website health monitoring system that runs entirely on
GitHub Actions at zero hosting cost. Every 5 minutes it checks 10 services
for HTTP status, latency, SSL certificate validity, keyword presence, redirect
behaviour, and JSON schema correctness. Results are committed back to the repo,
rendered as a static dashboard on GitHub Pages, and sent as Email + Telegram
alerts whenever any service changes state.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    GitHub Actions (every 5 min)             │
│                                                             │
│  ┌─────────────┐   ┌──────────────┐   ┌─────────────────┐  │
│  │  check.py   │──▶│  notify.py   │──▶│build_dashboard  │  │
│  │             │   │              │   │     .py          │  │
│  │ Runs 6      │   │ Detects      │   │ Generates        │  │
│  │ checks per  │   │ state        │   │ static HTML      │  │
│  │ target,     │   │ changes,     │   │ from latest.json │  │
│  │ concurrent  │   │ sends Email  │   │ + history/       │  │
│  │             │   │ + Telegram   │   │                  │  │
│  └──────┬──────┘   └──────────────┘   └────────┬────────┘  │
│         │                                       │           │
│         ▼                                       ▼           │
│  data/latest.json                         docs/index.html   │
│  data/history/YYYY-MM-DD.json                              │
└────────────────────────────┬────────────────────┬──────────┘
                             │                    │
                             │ git push            │ push to main
                             ▼                    ▼
                        main branch        deploy-dashboard.yml
                                                  │
                                                  ▼
                                          GitHub Pages
                                    https://<user>.github.io/health-check/
```

---

## Setup

### a. Fork / Clone the repository

```bash
git clone https://github.com/Yash121l/health-check.git
cd health-check
```

Or click **Fork** on GitHub to create your own copy.

### b. Enable GitHub Pages

1. Go to your repo → **Settings** → **Pages**
2. Under **Source**, select **GitHub Actions**
3. Save

### c. Enable GitHub Actions

Actions are enabled by default on forked repos. If disabled:
**Settings** → **Actions** → **General** → select **Allow all actions**.

### d. Add Required Secrets

Go to **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
and add each of the following:

| Secret Name          | Description                                          |
|----------------------|------------------------------------------------------|
| `GMAIL_USER`         | Gmail address used to send alerts (e.g. `you@gmail.com`) |
| `GMAIL_APP_PASSWORD` | Gmail **App Password** (not your account password). Generate at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) with 2FA enabled. |
| `ALERT_EMAIL_TO`     | Recipient email(s), comma-separated (e.g. `you@gmail.com,ops@company.com`) |
| `TELEGRAM_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) (e.g. `123456:ABC-DEF...`) |
| `TELEGRAM_CHAT_ID`   | Chat or channel ID to send messages to. Get it from [@userinfobot](https://t.me/userinfobot) or use `@channelusername` for public channels. |

> **Note:** Alerts are optional per channel. If Email secrets are missing, email
> alerts are skipped (no crash). Same for Telegram. At least one should be
> configured for useful alerting.

### e. Customize targets

Edit `config/targets.yml` to add, remove, or modify monitored services.
See [Adding a new target](#adding-a-new-target) below.

Edit `config/settings.yml` to adjust global thresholds (latency, SSL days, etc.).

### f. Run the workflow manually

1. Go to **Actions** → **Health Check**
2. Click **Run workflow** → **Run workflow**
3. Watch the logs — results will appear in `data/` and the status page will
   deploy to GitHub Pages within seconds.

---

## Adding a New Target

Add an entry to `config/targets.yml`:

```yaml
- name: "My New Service"
  url: "https://example.com/api/health"
  method: GET
  headers:
    Authorization: "Bearer static-read-only-token"
  expected_status: 200
  expected_keyword: "healthy"
  expected_final_url: ""
  expected_json_keys:
    - "status"
    - "data.version"
  allow_self_signed: false
  tags: ["api", "production"]
```

All fields except `name` and `url` are optional.

| Field               | Default | Description |
|---------------------|---------|-------------|
| `method`            | `GET`   | HTTP method |
| `headers`           | `{}`    | Extra request headers |
| `expected_status`   | any 2xx | Exact status code to require |
| `expected_keyword`  | `""`    | String that must appear in response body |
| `expected_final_url`| `""`    | Final URL after all redirects |
| `expected_json_keys`| `[]`    | Dot-notation key paths that must exist in JSON response |
| `allow_self_signed` | `false` | Skip SSL chain verification |
| `tags`              | `[]`    | Labels displayed on the dashboard |

---

## How Alerts Work

Alerts use **change-detection**, not every-run polling. A notification is sent only when:

- A target transitions **to** `DOWN` or `DEGRADED` (alert email/message)
- A target transitions **back to** `UP` from `DOWN`/`DEGRADED` (recovery email/message)

This prevents alert fatigue — if a service is already down, you won't receive a
flood of messages every 5 minutes.

**Idempotency:** A `data/last_notified_run_id.txt` file tracks the last run for
which notifications were sent. If the script is re-run for the same data (e.g.,
after a crash), it will not double-send.

**Email subjects:**
- `🔴 [Health Check] DOWN: {name} | {timestamp IST}`
- `🟡 [Health Check] DEGRADED: {name} | {timestamp IST}`
- `🟢 [Health Check] RECOVERED: {name} | {timestamp IST}`

**Telegram:** One consolidated MarkdownV2 message per run listing all changes
plus a full status summary.

---

## Reading the Status Dashboard

The dashboard at `https://<user>.github.io/health-check/` shows:

- **Top bar:** Overall system status badge + last-updated timestamp
- **Status summary:** Count of UP / DEGRADED / DOWN targets
- **Service cards:** One per target with:
  - Large status badge (UP / DEGRADED / DOWN)
  - Current latency in ms
  - SSL certificate days remaining + expiry date
  - Per-check result rows (HTTP, Latency, Keyword, SSL, Redirect, JSON Schema)
  - Sparkline latency chart for the last 24 hours
- **30-day uptime table:** Daily uptime % per target (colored dots)
  - Green ● ≥ 99%
  - Amber ● 90–99%
  - Red ● < 90%
  - – no data for that day

The page is **fully static** — all data is baked in at build time. No API calls
are made by the browser.

---

## Troubleshooting

### Workflow never runs
- Confirm the cron schedule is correct in `.github/workflows/health-check.yml`
- GitHub Actions schedules may be delayed by up to 15 minutes under high load
- Make sure the repo has had at least one push to `main` (Actions requires an
  active repo)

### No email alerts received
- Verify `GMAIL_USER` is a valid Gmail address with 2FA enabled
- Verify `GMAIL_APP_PASSWORD` is an App Password, not your account password
- Check the Actions run logs under the **Send notifications** step for error messages
- Check your spam folder

### Telegram bot not sending
- Ensure the bot was started (send `/start` to the bot in your chat)
- For channels: ensure the bot is an admin of the channel
- Verify `TELEGRAM_CHAT_ID` is correct (use negative ID for group chats,
  e.g., `-1001234567890`)

### Dashboard not updating
- Confirm GitHub Pages is set to deploy from **GitHub Actions** source (not branch)
- Check the **Deploy Dashboard to GitHub Pages** workflow in the Actions tab
- The `deploy-dashboard.yml` workflow triggers only on pushes to `docs/**`

### SSL checks failing for valid sites
- Some sites use SNI or non-standard TLS configurations. Set
  `allow_self_signed: true` in `config/targets.yml` for those targets to skip
  chain verification (expiry is still checked)

### Checker exceeds 4-minute timeout
- The checker uses `ThreadPoolExecutor(max_workers=10)` to run all checks
  concurrently. With 10 targets and a 10s timeout each, worst case is ~10s total.
- If adding many more targets, increase `max_workers` in `scripts/check.py`
  proportionally.

---

## Status Page Badge

Embed this badge anywhere to show your live status page:

```markdown
[![Status Page](https://img.shields.io/badge/Status%20Page-Live-22c55e?style=flat)](https://Yash121l.github.io/health-check/)
```

And the Actions workflow badge:

```markdown
[![Health Check](https://github.com/Yash121l/health-check/actions/workflows/health-check.yml/badge.svg)](https://github.com/Yash121l/health-check/actions/workflows/health-check.yml)
```
