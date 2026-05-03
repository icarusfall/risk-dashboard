import os
import io
import csv
import logging
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template_string, request, abort
import yfinance as yf
import requests

app = Flask(__name__)
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


CROSS_ASSETS = [
    {"ticker": "SPY",    "label": "US Large Cap (SPY)",      "group": "Equity"},
    {"ticker": "QQQ",    "label": "US Tech (QQQ)",           "group": "Equity"},
    {"ticker": "IWM",    "label": "US Small Cap (IWM)",      "group": "Equity"},
    {"ticker": "EEM",    "label": "Emerging Markets (EEM)",  "group": "Equity"},
    {"ticker": "TLT",    "label": "Long Treasuries (TLT)",   "group": "Bonds"},
    {"ticker": "HYG",    "label": "High Yield Credit (HYG)", "group": "Bonds"},
    {"ticker": "DX-Y.NYB","label": "US Dollar Index (DXY)",  "group": "FX"},
    {"ticker": "JPY=X",  "label": "USDJPY",                  "group": "FX"},
    {"ticker": "GLD",    "label": "Gold (GLD)",              "group": "Commodity"},
    {"ticker": "USO",    "label": "Oil (USO)",               "group": "Commodity"},
    {"ticker": "DBC",    "label": "Broad Commodities (DBC)", "group": "Commodity"},
]

# Per-asset-class drawdown thresholds (max drawdown from 1m rolling high, in %).
# More negative = worse.
THRESHOLDS = {
    "Equity":    {"amber": -3.0, "red": -7.0},
    "Bonds":     {"amber": -2.0, "red": -5.0},
    "FX":        {"amber": -3.0, "red": -6.0},
    "Commodity": {"amber": -4.0, "red": -8.0},
}

VIX_BANDS = {"amber": 20.0, "red": 30.0}

# OAS spreads from FRED (basis points)
CREDIT_SPREADS = [
    {
        "id": "BAMLH0A0HYM2",
        "label": "US High Yield OAS",
        "bands": {"amber": 400, "red": 600},
    },
    {
        "id": "BAMLC0A0CM",
        "label": "US Investment Grade OAS",
        "bands": {"amber": 150, "red": 250},
    },
]


def classify(value, amber, red, lower_is_worse=False):
    """Return 'green' | 'amber' | 'red' for a numeric value vs thresholds."""
    if value is None:
        return "unknown"
    if lower_is_worse:
        # e.g. drawdowns: more negative is worse
        if value <= red:
            return "red"
        if value <= amber:
            return "amber"
        return "green"
    else:
        # e.g. VIX, spreads: higher is worse
        if value >= red:
            return "red"
        if value >= amber:
            return "amber"
        return "green"


def fetch_vix():
    ticker = yf.Ticker("^VIX")
    hist = ticker.history(period="3mo")
    if hist.empty:
        return None
    history = [
        {"date": d.strftime("%Y-%m-%d"), "close": round(float(row["Close"]), 2)}
        for d, row in hist.iterrows()
    ]
    latest = float(hist["Close"].iloc[-1])
    return {
        "latest": round(latest, 2),
        "history": history,
        "status": classify(latest, VIX_BANDS["amber"], VIX_BANDS["red"]),
        "bands": VIX_BANDS,
    }


def compute_drawdown_from_1m_high(closes):
    """Max drawdown (most negative) from rolling 1-month high, looking at last ~22 sessions."""
    if len(closes) < 2:
        return None
    window = closes[-22:] if len(closes) >= 22 else closes
    peak = window[0]
    worst = 0.0
    for px in window:
        if px > peak:
            peak = px
        if peak > 0:
            dd = (px - peak) / peak * 100.0
            if dd < worst:
                worst = dd
    return round(worst, 2)


def fetch_cross_assets():
    tickers_str = " ".join([a["ticker"] for a in CROSS_ASSETS])
    data = yf.download(
        tickers_str,
        period="3mo",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    results = []
    for asset in CROSS_ASSETS:
        try:
            if len(CROSS_ASSETS) == 1:
                series = data["Close"]
            else:
                series = data[asset["ticker"]]["Close"]
            closes = [float(x) for x in series.dropna().tolist()]
            if not closes:
                results.append({**asset, "latest": None, "drawdown_1m": None,
                                "change_1w": None, "status": "unknown", "history": []})
                continue
            latest = closes[-1]
            change_1w = None
            if len(closes) >= 6:
                change_1w = round((closes[-1] / closes[-6] - 1.0) * 100.0, 2)
            drawdown = compute_drawdown_from_1m_high(closes)
            t = THRESHOLDS[asset["group"]]
            status = classify(drawdown, t["amber"], t["red"], lower_is_worse=True)
            dates = [d.strftime("%Y-%m-%d") for d in series.dropna().index]
            history = [{"date": d, "close": round(c, 4)} for d, c in zip(dates, closes)]
            results.append({
                **asset,
                "latest": round(latest, 4),
                "change_1w": change_1w,
                "drawdown_1m": drawdown,
                "status": status,
                "thresholds": t,
                "history": history[-66:],  # ~3 months
            })
        except Exception as e:
            results.append({**asset, "latest": None, "drawdown_1m": None,
                            "change_1w": None, "status": "unknown", "history": [],
                            "error": str(e)})
    return results


def fetch_fred_series(series_id):
    """Fetch a FRED series via the public CSV download endpoint (no API key)."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    reader = csv.reader(io.StringIO(r.text))
    rows = list(reader)
    if not rows:
        return []
    header = rows[0]
    # Column index for the series value (second column after DATE)
    val_idx = 1
    out = []
    cutoff = datetime.utcnow() - timedelta(days=120)
    for row in rows[1:]:
        if len(row) < 2:
            continue
        date_str, val_str = row[0], row[val_idx]
        if val_str in (".", "", "NA"):
            continue
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if dt < cutoff:
            continue
        try:
            val = float(val_str)
        except ValueError:
            continue
        out.append({"date": date_str, "value": val})
    return out


def fetch_credit_spreads():
    results = []
    for spec in CREDIT_SPREADS:
        try:
            series = fetch_fred_series(spec["id"])
            if not series:
                results.append({**spec, "latest": None, "status": "unknown", "history": []})
                continue
            # FRED OAS series are in percent; convert to bps
            history_bps = [{"date": p["date"], "value": round(p["value"] * 100.0, 1)} for p in series]
            latest = history_bps[-1]["value"]
            status = classify(latest, spec["bands"]["amber"], spec["bands"]["red"])
            results.append({
                **spec,
                "latest": latest,
                "status": status,
                "history": history_bps,
            })
        except Exception as e:
            results.append({**spec, "latest": None, "status": "unknown", "history": [], "error": str(e)})
    return results


TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Market Risk Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --green: #2da14a;
            --amber: #d99a1d;
            --red:   #c8362a;
            --gray:  #999;
            --bg:    #fafafa;
            --card:  #ffffff;
            --text:  #111;
            --muted: #666;
            --border:#e6e6e6;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            padding: 2.5rem 1rem 4rem;
        }
        .container { max-width: 1100px; margin: 0 auto; }
        header { margin-bottom: 2rem; }
        h1 { font-size: 1.6rem; font-weight: 600; }
        .subtitle { color: var(--muted); font-size: 0.95rem; margin-top: 0.25rem; }
        .timestamp { color: var(--gray); font-size: 0.85rem; margin-top: 0.5rem; }

        section { margin-bottom: 2.5rem; }
        h2 { font-size: 1.05rem; font-weight: 600; color: var(--muted);
             text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 0.9rem; }

        .vix-card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 1.5rem;
        }
        .vix-header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 1rem; flex-wrap: wrap; }
        .vix-value { font-size: 3rem; font-weight: 700; line-height: 1; }
        .vix-status { font-size: 0.85rem; font-weight: 600; padding: 0.25rem 0.6rem;
                      border-radius: 999px; color: #fff; text-transform: uppercase; letter-spacing: 0.05em; }
        .vix-bands { color: var(--muted); font-size: 0.85rem; margin-left: auto; }
        .chart-wrap { position: relative; height: 320px; }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 0.75rem;
        }
        .tile {
            background: var(--card);
            border: 1px solid var(--border);
            border-left: 4px solid var(--gray);
            border-radius: 8px;
            padding: 0.85rem 1rem;
        }
        .tile.green  { border-left-color: var(--green); }
        .tile.amber  { border-left-color: var(--amber); }
        .tile.red    { border-left-color: var(--red); }
        .tile-label { font-size: 0.8rem; color: var(--muted); margin-bottom: 0.25rem; }
        .tile-value { font-size: 1.4rem; font-weight: 600; }
        .tile-row { display: flex; justify-content: space-between; gap: 0.5rem;
                    margin-top: 0.45rem; font-size: 0.85rem; }
        .tile-metric { color: var(--muted); }
        .tile-metric strong { color: var(--text); font-weight: 600; }
        .neg { color: var(--red); }
        .pos { color: var(--green); }

        .group-title { font-size: 0.8rem; color: var(--gray); text-transform: uppercase;
                       letter-spacing: 0.08em; margin: 1rem 0 0.5rem; }

        .legend {
            display: flex; gap: 1rem; font-size: 0.8rem; color: var(--muted);
            flex-wrap: wrap; margin-top: 0.5rem;
        }
        .swatch { display: inline-block; width: 0.7rem; height: 0.7rem; border-radius: 2px;
                  margin-right: 0.35rem; vertical-align: middle; }
        .sw-green { background: var(--green); }
        .sw-amber { background: var(--amber); }
        .sw-red   { background: var(--red); }

        .error { color: var(--red); margin: 1rem 0; }
        .footer { color: var(--gray); font-size: 0.8rem; margin-top: 3rem; text-align: center; }

        .status-bg-green { background: var(--green); }
        .status-bg-amber { background: var(--amber); }
        .status-bg-red   { background: var(--red); }
        .status-bg-unknown { background: var(--gray); }
    </style>
</head>
<body>
<div class="container">
    <header>
        <h1>Market Risk Dashboard</h1>
        <div class="subtitle">Quick view of cross-asset risk conditions &middot; end-of-day data</div>
        <div class="timestamp" id="timestamp"></div>
    </header>

    <div class="error" id="error"></div>

    <section id="vix-section">
        <h2>Volatility</h2>
        <div class="vix-card">
            <div class="vix-header">
                <div>
                    <div class="tile-label">VIX &mdash; CBOE Volatility Index</div>
                    <span class="vix-value" id="vix-value">--</span>
                </div>
                <span class="vix-status status-bg-unknown" id="vix-status">--</span>
                <div class="vix-bands">Green &lt; 20 &middot; Amber 20&ndash;30 &middot; Red &gt; 30</div>
            </div>
            <div class="chart-wrap"><canvas id="vix-chart"></canvas></div>
        </div>
    </section>

    <section id="credit-section">
        <h2>Credit / Liquidity (FRED OAS, bps)</h2>
        <div class="grid" id="credit-grid"></div>
        <div class="legend">
            <span><span class="swatch sw-green"></span>HY &lt;400 / IG &lt;150</span>
            <span><span class="swatch sw-amber"></span>HY 400&ndash;600 / IG 150&ndash;250</span>
            <span><span class="swatch sw-red"></span>HY &gt;600 / IG &gt;250</span>
        </div>
    </section>

    <section id="cross-section">
        <h2>Cross-Asset Drawdowns (max drawdown from 1m high)</h2>
        <div id="cross-groups"></div>
        <div class="legend">
            <span><span class="swatch sw-green"></span>mild</span>
            <span><span class="swatch sw-amber"></span>elevated</span>
            <span><span class="swatch sw-red"></span>stressed</span>
            <span style="margin-left:auto;color:var(--gray)">Thresholds vary by asset class</span>
        </div>
    </section>
</div>

<div class="footer">
    Sources: Yahoo Finance (VIX, ETFs, FX, commodities), FRED (ICE BofA OAS).
    Daily close data only. Not investment advice.
</div>

<script>
function fmtNum(v, dp) {
    if (v === null || v === undefined) return '--';
    return Number(v).toFixed(dp ?? 2);
}
function signed(v) {
    if (v === null || v === undefined) return '--';
    const s = v >= 0 ? '+' : '';
    return s + Number(v).toFixed(2) + '%';
}
function tone(v) {
    if (v === null || v === undefined) return '';
    return v < 0 ? 'neg' : 'pos';
}

fetch('/api/risk')
  .then(r => r.json())
  .then(data => {
    if (data.error) {
        document.getElementById('error').textContent = data.error;
        return;
    }
    document.getElementById('timestamp').textContent = 'As of ' + data.timestamp;

    // VIX
    const v = data.vix;
    if (v) {
        document.getElementById('vix-value').textContent = fmtNum(v.latest);
        const statusEl = document.getElementById('vix-status');
        statusEl.textContent = v.status;
        statusEl.className = 'vix-status status-bg-' + v.status;

        const ctx = document.getElementById('vix-chart').getContext('2d');
        const dates = v.history.map(d => d.date);
        const closes = v.history.map(d => d.close);
        const maxY = Math.max(35, Math.ceil(Math.max(...closes) / 5) * 5 + 5);

        // Background banding plugin
        const bandPlugin = {
            id: 'bandPlugin',
            beforeDraw(chart) {
                const {ctx, chartArea, scales: {y}} = chart;
                if (!chartArea) return;
                const yGreen = y.getPixelForValue(20);
                const yAmber = y.getPixelForValue(30);
                ctx.save();
                // green
                ctx.fillStyle = 'rgba(45,161,74,0.08)';
                ctx.fillRect(chartArea.left, yGreen, chartArea.right - chartArea.left, chartArea.bottom - yGreen);
                // amber
                ctx.fillStyle = 'rgba(217,154,29,0.10)';
                ctx.fillRect(chartArea.left, yAmber, chartArea.right - chartArea.left, yGreen - yAmber);
                // red
                ctx.fillStyle = 'rgba(200,54,42,0.10)';
                ctx.fillRect(chartArea.left, chartArea.top, chartArea.right - chartArea.left, yAmber - chartArea.top);
                ctx.restore();
            }
        };

        new Chart(ctx, {
            type: 'line',
            data: {
                labels: dates,
                datasets: [{
                    label: 'VIX Close',
                    data: closes,
                    borderColor: '#111',
                    borderWidth: 2,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    tension: 0.15,
                    fill: false
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { display: false } },
                scales: {
                    x: { ticks: { maxTicksLimit: 8, color: '#999' }, grid: { display: false } },
                    y: { suggestedMin: 10, suggestedMax: maxY,
                         ticks: { color: '#999' }, grid: { color: '#eee' } }
                }
            },
            plugins: [bandPlugin]
        });
    }

    // Credit spreads
    const credit = document.getElementById('credit-grid');
    (data.credit || []).forEach(c => {
        const tile = document.createElement('div');
        tile.className = 'tile ' + c.status;
        tile.innerHTML = `
            <div class="tile-label">${c.label}</div>
            <div class="tile-value">${fmtNum(c.latest, 0)} <span style="font-size:0.85rem;color:var(--muted);font-weight:400">bps</span></div>
            <div class="tile-row">
                <span class="tile-metric">Amber &ge; ${c.bands.amber}</span>
                <span class="tile-metric">Red &ge; ${c.bands.red}</span>
            </div>`;
        credit.appendChild(tile);
    });

    // Cross-asset by group
    const groupsEl = document.getElementById('cross-groups');
    const groups = {};
    (data.cross || []).forEach(a => {
        if (!groups[a.group]) groups[a.group] = [];
        groups[a.group].push(a);
    });
    Object.keys(groups).forEach(g => {
        const title = document.createElement('div');
        title.className = 'group-title';
        title.textContent = g;
        groupsEl.appendChild(title);
        const grid = document.createElement('div');
        grid.className = 'grid';
        groups[g].forEach(a => {
            const tile = document.createElement('div');
            tile.className = 'tile ' + a.status;
            const dd = a.drawdown_1m;
            const wk = a.change_1w;
            tile.innerHTML = `
                <div class="tile-label">${a.label}</div>
                <div class="tile-value">${fmtNum(a.latest, 2)}</div>
                <div class="tile-row">
                    <span class="tile-metric">1m DD <strong class="${tone(dd)}">${signed(dd)}</strong></span>
                    <span class="tile-metric">1w <strong class="${tone(wk)}">${signed(wk)}</strong></span>
                </div>`;
            grid.appendChild(tile);
        });
        groupsEl.appendChild(grid);
    });
  })
  .catch(err => {
    document.getElementById('error').textContent = 'Failed to load risk data.';
    console.error(err);
  });
</script>
</body>
</html>
"""


def collect_alerts(vix, cross, credit, alert_level):
    """Return list of indicators whose status is at/above alert_level.

    alert_level: 'red' (default) or 'amber' (also includes amber).
    """
    triggers = {"red"}
    if alert_level == "amber":
        triggers.add("amber")

    alerts = []
    if vix and vix.get("status") in triggers:
        alerts.append({
            "name": "VIX",
            "status": vix["status"],
            "value": f"{vix['latest']:.2f}",
            "detail": f"amber {vix['bands']['amber']} / red {vix['bands']['red']}",
        })
    for c in credit or []:
        if c.get("status") in triggers and c.get("latest") is not None:
            alerts.append({
                "name": c["label"],
                "status": c["status"],
                "value": f"{c['latest']:.0f} bps",
                "detail": f"amber {c['bands']['amber']} / red {c['bands']['red']} bps",
            })
    for a in cross or []:
        if a.get("status") in triggers and a.get("drawdown_1m") is not None:
            t = a.get("thresholds", {})
            alerts.append({
                "name": a["label"],
                "status": a["status"],
                "value": f"1m DD {a['drawdown_1m']:+.2f}% (1w {a['change_1w']:+.2f}%)",
                "detail": f"amber {t.get('amber')}% / red {t.get('red')}%",
            })
    # Sort red first, then amber
    alerts.sort(key=lambda x: 0 if x["status"] == "red" else 1)
    return alerts


def render_alert_email(alerts, alert_level):
    n_red = sum(1 for a in alerts if a["status"] == "red")
    n_amber = sum(1 for a in alerts if a["status"] == "amber")
    parts = []
    if n_red:
        parts.append(f"{n_red} red")
    if n_amber:
        parts.append(f"{n_amber} amber")
    subject = "[Risk Alert] " + ", ".join(parts) if parts else "[Risk Alert]"

    rows_html = "".join([
        f"""<tr>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;">
                <span style="display:inline-block;padding:2px 8px;border-radius:4px;color:#fff;font-size:11px;font-weight:600;text-transform:uppercase;
                background:{'#c8362a' if a['status']=='red' else '#d99a1d'};">{a['status']}</span>
            </td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;font-weight:600;">{a['name']}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;">{a['value']}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#666;font-size:12px;">{a['detail']}</td>
        </tr>"""
        for a in alerts
    ])
    html = f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#111;">
        <h2 style="margin:0 0 8px;">Market risk alert</h2>
        <p style="color:#666;margin:0 0 16px;font-size:13px;">
          Alert level: <strong>{alert_level}</strong> &middot; {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
        </p>
        <table style="border-collapse:collapse;width:100%;font-size:14px;">
            <thead><tr style="text-align:left;color:#666;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;">
                <th style="padding:8px 12px;">Status</th>
                <th style="padding:8px 12px;">Indicator</th>
                <th style="padding:8px 12px;">Value</th>
                <th style="padding:8px 12px;">Thresholds</th>
            </tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
    </div>"""

    text_lines = [f"Market risk alert ({alert_level} level)", ""]
    for a in alerts:
        text_lines.append(f"  [{a['status'].upper()}] {a['name']}: {a['value']}  ({a['detail']})")
    text = "\n".join(text_lines)

    return subject, html, text


def send_email_via_resend(subject, html, text, to_addr, from_addr, api_key):
    r = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_addr,
            "to": [to_addr],
            "subject": subject,
            "html": html,
            "text": text,
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


@app.route("/cron/check-risk")
def cron_check_risk():
    secret = os.environ.get("CRON_SECRET")
    if secret:
        if request.args.get("key") != secret:
            abort(403)

    alert_level = (os.environ.get("ALERT_LEVEL") or "red").lower()
    if alert_level not in ("red", "amber"):
        alert_level = "red"

    api_key = os.environ.get("RESEND_API_KEY")
    to_addr = os.environ.get("ALERT_EMAIL")
    from_addr = os.environ.get("ALERT_FROM", "Risk Dashboard <onboarding@resend.dev>")

    if not api_key or not to_addr:
        return jsonify({"error": "RESEND_API_KEY and ALERT_EMAIL must be set"}), 500

    try:
        vix = fetch_vix()
        cross = fetch_cross_assets()
        credit = fetch_credit_spreads()
    except Exception as e:
        logger.exception("data fetch failed")
        return jsonify({"error": f"data fetch failed: {e}"}), 500

    alerts = collect_alerts(vix, cross, credit, alert_level)
    if not alerts:
        logger.info("no alerts at level=%s", alert_level)
        return jsonify({"sent": False, "reason": "no triggers", "level": alert_level})

    subject, html, text = render_alert_email(alerts, alert_level)
    try:
        result = send_email_via_resend(subject, html, text, to_addr, from_addr, api_key)
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        logger.error("resend error: %s %s", e, body)
        return jsonify({"error": f"resend error: {e}", "body": body}), 502
    except Exception as e:
        logger.exception("email send failed")
        return jsonify({"error": f"send failed: {e}"}), 500

    logger.info("alert sent: %d items, level=%s", len(alerts), alert_level)
    return jsonify({"sent": True, "alerts": len(alerts), "level": alert_level, "id": result.get("id")})


@app.route("/")
def index():
    return render_template_string(TEMPLATE)


@app.route("/api/risk")
def api_risk():
    try:
        vix = fetch_vix()
        cross = fetch_cross_assets()
        credit = fetch_credit_spreads()
        return jsonify({
            "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "vix": vix,
            "cross": cross,
            "credit": credit,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
