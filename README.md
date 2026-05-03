# Market Risk Dashboard

Quick web view of cross-asset risk conditions. Daily-close data only.

## What it shows

- **VIX** — 3 months of history with traffic-light bands (green <20, amber 20–30, red >30).
- **Credit / liquidity** — ICE BofA US High Yield OAS and Investment Grade OAS from FRED.
- **Cross-asset drawdowns** — max drawdown from the rolling 1-month high for a curated set of equity / bond / FX / commodity ETFs and indices.

Thresholds are configured in `app.py` (`THRESHOLDS`, `VIX_BANDS`, `CREDIT_SPREADS`).

## Data sources

- Yahoo Finance via `yfinance` (VIX, ETFs, FX, commodities)
- FRED public CSV endpoint for OAS spreads (no API key required)

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000.

## Deploy

The `Procfile` runs `gunicorn app:app`, suitable for Railway / Heroku-style platforms.

## Email alerts

`GET /cron/check-risk?key=$CRON_SECRET` evaluates current statuses and emails a
summary via Resend if any indicator is at the configured alert level or worse.

Required env vars (set in Railway):

- `RESEND_API_KEY` — Resend API key
- `ALERT_EMAIL` — recipient address
- `CRON_SECRET` — random string; the endpoint requires `?key=<secret>` if set

Optional:

- `ALERT_LEVEL` — `red` (default) or `amber` (more sensitive)
- `ALERT_FROM` — sender address. Defaults to `Risk Dashboard <onboarding@resend.dev>`,
  which works without domain verification. For a custom from-address you'll need
  to verify a domain in Resend.

Schedule it as a Railway Cron service hitting
`https://<your-app>.up.railway.app/cron/check-risk?key=<secret>` once a night
(e.g. `0 22 * * *` UTC, after the US close). The endpoint returns
`{"sent": false, "reason": "no triggers"}` when nothing is elevated.

## Caveats

- End-of-day data only — VIX intraday spikes won't show up if the close was calmer.
- MOVE index is paywalled; not included.
- ETF prices vs. underlying NAV would be a useful liquidity gauge but isn't freely available in a reliable form.
