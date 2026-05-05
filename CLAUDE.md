# risk-dashboard — context for future Claude sessions

A small Flask app showing a quick read on market risk: VIX with traffic-light bands,
ICE BofA HY/IG OAS spreads from FRED, and per-asset 1-month drawdown tiles for a
curated cross-asset set. Nightly job emails the user if anything is red.

## Architecture

- **Single file:** `app.py` — Flask + yfinance + Chart.js (CDN) + plain HTML template string. Mirrors the style of `C:/Users/charl/ClaudeProjects/latest-VIX/app.py` deliberately.
- **No DB, no JS framework, no build step.** Page is one rendered template; data via `/api/risk` JSON.
- **Deploy:** Railway, via `Procfile` (`gunicorn app:app`). Production URL: https://risk-dashboard.up.railway.app/
- **Repo:** https://github.com/icarusfall/risk-dashboard (branch `main`)

## Data sources

- **Yahoo Finance** (`yfinance`) — VIX (`^VIX`), ETFs, FX, commodities. End-of-day only.
- **FRED public CSV endpoint** — `https://fred.stlouisfed.org/graph/fredgraph.csv?id=<series>`. No API key. Used for `BAMLH0A0HYM2` (HY OAS) and `BAMLC0A0CM` (IG OAS). Values are %, converted to bps in code.

## Where things live in `app.py`

- `CROSS_ASSETS` — list of tracked tickers. Each has `group`, optional `direction` (`down` default, `up` for assets where rallies are the risk — currently just oil), and optional per-asset `thresholds` override.
- `THRESHOLDS` — group-level defaults (drawdown %).
- `VIX_BANDS` — 20 / 30. (User originally suggested 25/35; we pushed back, they agreed on 20/30.)
- `CREDIT_SPREADS` — FRED series + bps thresholds.
- `compute_extreme_move(closes, direction)` — drawdown OR run-up over rolling ~22 sessions.
- `collect_alerts(...)` / `render_alert_email(...)` — alert evaluation and HTML email.
- `/cron/check-risk?key=<CRON_SECRET>` — alert endpoint (red-only by default).

## Nightly alerts

- **Provider:** Resend. Default sender `Risk Dashboard <onboarding@resend.dev>` works without domain verification *but only delivers to the Resend account owner's email*.
- **Trigger:** GitHub Actions workflow `.github/workflows/risk-alert.yml`, cron `0 22 * * *` UTC. *Not* Railway's cron service (we tried; an empty Railway service has no runtime to execute curl in).
- **Email:** subject `[Risk Alert] N red, M amber`. Status badges, the heading, and a footer link all point at `DASHBOARD_URL`.

## Required environment / secrets

Railway env vars on the web service:

- `RESEND_API_KEY`
- `ALERT_EMAIL` (recipient)
- `CRON_SECRET` (random string; endpoint requires `?key=<secret>` if set)
- Optional: `ALERT_LEVEL` (`red` default; set `amber` for noisier), `ALERT_FROM`, `DASHBOARD_URL`

GitHub repo secrets (for the Actions workflow):

- `DASHBOARD_URL`
- `CRON_SECRET` (must match Railway)

## Decisions / quirks worth remembering

- **Oil (USO) uses `direction: "up"`** with thresholds `+5%` amber / `+10%` red — sharp oil rallies are the risk regime (supply shock / stagflation), not selloffs. The tile and email show "1m UP +X%" and the tile color tone inverts (red = big positive move).
- **MOVE index deliberately omitted** — ICE keeps it paywalled; Yahoo's feed is unreliable.
- **ETF NAV / bid-ask liquidity:** considered, rejected for v1 (scrapeable but fragile).
- **End-of-day only.** Intraday VIX spikes that close lower won't show up. The README notes this.
- **`yfinance` ticker for DXY is `DX-Y.NYB`**, not `^DXY`.
- **The user previously shared the `CRON_SECRET` in chat** — if rotation is ever needed, update both Railway env and the GitHub repo secret in lockstep.

## Local dev

```
pip install -r requirements.txt
python app.py
# http://localhost:5000
```

`.claude/launch.json` is configured for the Claude Preview MCP (port 5000).

To test the alert path without sending an email, in a Python shell:

```python
from app import fetch_vix, fetch_cross_assets, fetch_credit_spreads, collect_alerts
collect_alerts(fetch_vix(), fetch_cross_assets(), fetch_credit_spreads(), 'red')
```

## Likely future asks

- More tickers / asset classes — extend `CROSS_ASSETS`, no other code change needed.
- Tuning thresholds — `THRESHOLDS`, `VIX_BANDS`, `CREDIT_SPREADS` are all top-of-file constants.
- Sparklines on the tiles — `history` already comes back in `/api/risk`; just isn't rendered.
- Domain-verified sender for the email — needs a DNS-verified domain in Resend.
