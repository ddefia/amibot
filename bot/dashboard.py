"""Lightweight internal dashboard — no extra dependencies.

Runs on port 8080 alongside the bot. Serves a single-page dashboard
that auto-refreshes with live stats from the running bot.

Endpoints:
  GET /           → Dashboard HTML page
  GET /api/stats  → JSON: balance, P&L, win rate, trades, errors, markets
"""

import json
import logging
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

logger = logging.getLogger(__name__)

# Reference to the running engine (set by start_dashboard)
_engine = None


def _get_stats() -> dict:
    """Pull live stats from the running engine."""
    if not _engine:
        return {"error": "Engine not started"}

    risk_stats = _engine.risk.get_stats()
    uptime_s = time.time() - _engine._start_time
    hours = int(uptime_s // 3600)
    minutes = int((uptime_s % 3600) // 60)

    # Current prices from feeds
    prices = {}
    for asset in ("btc", "eth", "sol", "xrp"):
        ap = _engine.feeds.get_asset_price(asset)
        if ap:
            prices[asset.upper()] = {
                "price": round(ap.price, 2),
                "source": ap.source,
                "age_s": round(time.time() - ap.timestamp_ms / 1000, 1),
            }

    # Active orders
    open_orders = []
    filled_orders = []
    expired_orders = []
    for oid, order in _engine.executor.tracked_orders.items():
        entry = {
            "id": oid[:12],
            "side": order.side,
            "price": round(order.price, 4),
            "size_usd": round(order.size_usd, 2),
            "status": order.status,
            "age_s": round(time.time() - order.placed_at),
            "is_sell": order.is_sell,
        }
        if order.status == "open":
            open_orders.append(entry)
        elif order.status == "filled":
            filled_orders.append(entry)
        elif order.status in ("expired", "cancelled"):
            expired_orders.append(entry)

    # Markets being watched
    markets = []
    for m in _engine._opportunities[:30]:  # Cap at 30
        markets.append({
            "question": m.question[:80],
            "category": m.category,
            "asset": m.asset.upper() if m.asset else "",
            "arb_edge": round(m.arb_edge * 100, 1) if m.arb_edge else 0,
            "combined_price": round(m.combined_price, 3),
            "volume_24h": round(m.volume_24h, 0),
        })

    # Recent errors from log file
    errors = _read_recent_errors()

    # Recent trades from trades.jsonl
    trades = _read_recent_trades()

    return {
        "mode": "PAPER TEST" if _engine.config.dry_run else "LIVE",
        "uptime": f"{hours}h {minutes}m",
        "uptime_seconds": int(uptime_s),
        "balance": round(risk_stats["current_balance"], 2),
        "start_balance": round(risk_stats["session_start_balance"], 2),
        "pnl": round(risk_stats["total_pnl"], 2),
        "daily_pnl": round(risk_stats["daily_pnl"], 2),
        "total_trades": risk_stats["total_trades"],
        "resolved": risk_stats["resolved"],
        "wins": risk_stats["wins"],
        "losses": risk_stats["losses"],
        "win_rate": round(risk_stats["win_rate"] * 100, 1),
        "consecutive_losses": risk_stats["consecutive_losses"],
        "exposure": round(risk_stats["current_exposure"], 2),
        "drawdown_halted": risk_stats["drawdown_halted"],
        "trades_by_strategy": _engine._trades_by_strategy,
        "signals_evaluated": _engine._signals_evaluated,
        "signals_rejected": _engine._signals_rejected,
        "markets_scanned": _engine._markets_scanned,
        "prices": prices,
        "open_orders": open_orders,
        "filled_orders": filled_orders,
        "expired_orders": expired_orders,
        "markets": markets,
        "recent_trades": trades,
        "recent_errors": errors,
        "tick_count": _engine._tick_count,
        "ts": time.time(),
    }


def _read_recent_trades(max_lines=50) -> list:
    """Read the last N trade events from trades.jsonl."""
    trades = []
    try:
        path = Path(_engine.config.trade_log_file)
        if path.exists():
            lines = path.read_text().strip().split("\n")
            for line in lines[-max_lines:]:
                if line.strip():
                    entry = json.loads(line)
                    if entry.get("event") in ("trade", "fill", "expire", "settle", "place"):
                        trades.append(entry)
    except Exception:
        pass
    return trades[-30:]  # Last 30


def _read_recent_errors(max_lines=20) -> list:
    """Read recent ERROR lines from bot.log."""
    errors = []
    try:
        log_path = Path(_engine.config.log_file)
        if log_path.exists():
            lines = log_path.read_text().strip().split("\n")
            for line in lines[-500:]:  # Scan last 500 lines
                if "[ERROR]" in line or "[WARNING]" in line:
                    errors.append(line.strip())
    except Exception:
        pass
    return errors[-max_lines:]


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Amibot Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, 'Segoe UI', Roboto, monospace; background: #0a0e17; color: #e0e6f0; padding: 16px; }
h1 { font-size: 20px; margin-bottom: 12px; }
.mode-badge { display: inline-block; padding: 3px 10px; border-radius: 4px; font-size: 12px; font-weight: bold; margin-left: 8px; }
.mode-test { background: #f59e0b; color: #000; }
.mode-live { background: #ef4444; color: #fff; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin-bottom: 16px; }
.card { background: #141925; border: 1px solid #1e2738; border-radius: 8px; padding: 14px; }
.card h2 { font-size: 13px; color: #6b7fa3; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 1px; }
.stat { display: flex; justify-content: space-between; padding: 4px 0; font-size: 14px; }
.stat .label { color: #8896b0; }
.stat .value { font-weight: 600; }
.positive { color: #10b981; }
.negative { color: #ef4444; }
.neutral { color: #6b7fa3; }
.warn { color: #f59e0b; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; color: #6b7fa3; padding: 6px 8px; border-bottom: 1px solid #1e2738; font-weight: 600; text-transform: uppercase; font-size: 11px; }
td { padding: 5px 8px; border-bottom: 1px solid #111827; }
tr:hover { background: #1a2035; }
.tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; }
.tag-oracle { background: #1e3a5f; color: #60a5fa; }
.tag-arb { background: #1a3f2e; color: #34d399; }
.tag-data { background: #3f2e1a; color: #fbbf24; }
.tag-mm { background: #3f1a3f; color: #c084fc; }
.tag-open { background: #1e3a5f; color: #60a5fa; }
.tag-filled { background: #1a3f2e; color: #34d399; }
.tag-expired { background: #3f2020; color: #f87171; }
.errors-box { background: #1a1012; border: 1px solid #3f2020; border-radius: 6px; padding: 10px; max-height: 200px; overflow-y: auto; font-size: 11px; font-family: monospace; color: #f87171; }
.errors-box .warn-line { color: #fbbf24; }
.refresh-bar { text-align: right; font-size: 11px; color: #4a5568; margin-bottom: 8px; }
.price-row { display: flex; gap: 16px; flex-wrap: wrap; }
.price-chip { background: #1a2035; border: 1px solid #1e2738; border-radius: 6px; padding: 8px 14px; text-align: center; min-width: 100px; }
.price-chip .asset { font-size: 12px; color: #6b7fa3; font-weight: 600; }
.price-chip .price { font-size: 18px; font-weight: 700; color: #e0e6f0; }
.price-chip .source { font-size: 10px; color: #4a5568; }
.big-number { font-size: 28px; font-weight: 700; }
.sub { font-size: 12px; color: #6b7fa3; }
.halted { background: #3f2020; border-color: #ef4444; }
</style>
</head>
<body>
<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 16px;">
  <h1>Amibot <span id="mode-badge" class="mode-badge mode-test">TEST</span></h1>
  <div class="refresh-bar">Uptime: <strong id="uptime">--</strong> | Updated: <span id="last-update">--</span></div>
</div>

<!-- Top stats -->
<div class="grid">
  <div class="card" id="balance-card">
    <h2>Balance</h2>
    <div class="big-number" id="balance">--</div>
    <div class="sub">Started: $<span id="start-balance">--</span></div>
  </div>
  <div class="card">
    <h2>Profit / Loss</h2>
    <div class="big-number" id="pnl">--</div>
    <div class="sub">Daily: $<span id="daily-pnl">--</span></div>
  </div>
  <div class="card">
    <h2>Win Rate</h2>
    <div class="big-number" id="win-rate">--</div>
    <div class="sub"><span id="wins">0</span>W / <span id="losses">0</span>L of <span id="resolved">0</span> resolved</div>
  </div>
  <div class="card">
    <h2>Activity</h2>
    <div class="stat"><span class="label">Total trades</span><span class="value" id="total-trades">0</span></div>
    <div class="stat"><span class="label">Exposure</span><span class="value" id="exposure">$0</span></div>
    <div class="stat"><span class="label">Loss streak</span><span class="value" id="loss-streak">0</span></div>
    <div class="stat"><span class="label">Signals eval/reject</span><span class="value"><span id="sig-eval">0</span>/<span id="sig-reject">0</span></span></div>
  </div>
</div>

<!-- Prices -->
<div class="card" style="margin-bottom: 12px;">
  <h2>Live Prices</h2>
  <div class="price-row" id="prices-row"></div>
</div>

<!-- Strategy breakdown + Orders -->
<div class="grid">
  <div class="card">
    <h2>Trades by Strategy</h2>
    <div class="stat"><span class="label"><span class="tag tag-oracle">ORACLE</span> Oracle Lag</span><span class="value" id="strat-oracle">0</span></div>
    <div class="stat"><span class="label"><span class="tag tag-arb">ARB</span> Arbitrage</span><span class="value" id="strat-arb">0</span></div>
    <div class="stat"><span class="label"><span class="tag tag-data">DATA</span> Data Edge</span><span class="value" id="strat-data">0</span></div>
    <div class="stat"><span class="label"><span class="tag tag-mm">MM</span> Market Making</span><span class="value" id="strat-mm">0</span></div>
  </div>
  <div class="card">
    <h2>Open Orders (<span id="open-count">0</span>)</h2>
    <div id="open-orders" style="font-size:13px; max-height: 180px; overflow-y: auto;">None</div>
  </div>
</div>

<!-- Markets watched -->
<div class="card" style="margin-bottom: 12px;">
  <h2>Markets Watched (<span id="market-count">0</span>)</h2>
  <div style="max-height: 280px; overflow-y: auto;">
    <table>
      <thead><tr><th>Market</th><th>Type</th><th>Asset</th><th>Edge</th><th>Combined</th></tr></thead>
      <tbody id="markets-body"></tbody>
    </table>
  </div>
</div>

<!-- Recent trades -->
<div class="card" style="margin-bottom: 12px;">
  <h2>Recent Trade Events</h2>
  <div style="max-height: 240px; overflow-y: auto;">
    <table>
      <thead><tr><th>Time</th><th>Event</th><th>Strategy</th><th>Side</th><th>Price</th><th>Size</th><th>Details</th></tr></thead>
      <tbody id="trades-body"></tbody>
    </table>
  </div>
</div>

<!-- Errors -->
<div class="card">
  <h2>Recent Errors & Warnings (<span id="error-count">0</span>)</h2>
  <div class="errors-box" id="errors-box">No errors</div>
</div>

<script>
function fmt(n, decimals=2) {
  if (n === undefined || n === null) return '--';
  return Number(n).toFixed(decimals);
}
function fmtUsd(n) {
  if (n === undefined || n === null) return '--';
  const sign = n >= 0 ? '+' : '';
  return sign + '$' + fmt(Math.abs(n));
}
function fmtTime(ts) {
  if (!ts) return '--';
  return new Date(ts * 1000).toLocaleTimeString();
}
function catTag(cat) {
  const map = {oracle_lag:'oracle', crypto_oracle:'oracle', arb:'arb', data_edge:'data', sports_data:'data', weather_data:'data', mm:'mm'};
  const cls = map[cat] || 'oracle';
  return '<span class="tag tag-'+cls+'">'+cat.toUpperCase().replace('_',' ')+'</span>';
}
function statusTag(s) {
  return '<span class="tag tag-'+s+'">'+s.toUpperCase()+'</span>';
}

async function refresh() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();

    // Mode
    const badge = document.getElementById('mode-badge');
    badge.textContent = d.mode;
    badge.className = 'mode-badge ' + (d.mode.includes('TEST') ? 'mode-test' : 'mode-live');

    // Top stats
    document.getElementById('uptime').textContent = d.uptime;
    document.getElementById('balance').textContent = '$' + fmt(d.balance);
    document.getElementById('balance').className = 'big-number ' + (d.balance >= d.start_balance ? 'positive' : 'negative');
    document.getElementById('start-balance').textContent = fmt(d.start_balance);
    document.getElementById('pnl').textContent = fmtUsd(d.pnl);
    document.getElementById('pnl').className = 'big-number ' + (d.pnl >= 0 ? 'positive' : 'negative');
    document.getElementById('daily-pnl').textContent = fmt(d.daily_pnl);
    document.getElementById('win-rate').textContent = fmt(d.win_rate, 0) + '%';
    document.getElementById('win-rate').className = 'big-number ' + (d.win_rate >= 50 ? 'positive' : d.win_rate > 0 ? 'negative' : 'neutral');
    document.getElementById('wins').textContent = d.wins;
    document.getElementById('losses').textContent = d.losses;
    document.getElementById('resolved').textContent = d.resolved;
    document.getElementById('total-trades').textContent = d.total_trades;
    document.getElementById('exposure').textContent = '$' + fmt(d.exposure);
    document.getElementById('loss-streak').textContent = d.consecutive_losses;
    document.getElementById('sig-eval').textContent = d.signals_evaluated;
    document.getElementById('sig-reject').textContent = d.signals_rejected;
    document.getElementById('last-update').textContent = new Date().toLocaleTimeString();

    // Drawdown halt
    const bc = document.getElementById('balance-card');
    bc.className = d.drawdown_halted ? 'card halted' : 'card';

    // Prices
    const pr = document.getElementById('prices-row');
    pr.innerHTML = '';
    for (const [asset, info] of Object.entries(d.prices || {})) {
      pr.innerHTML += '<div class="price-chip"><div class="asset">'+asset+'</div><div class="price">$'+fmt(info.price, asset==='BTC'?0:2)+'</div><div class="source">'+info.source+' ('+fmt(info.age_s,0)+'s ago)</div></div>';
    }
    if (!Object.keys(d.prices || {}).length) pr.innerHTML = '<span class="neutral">Waiting for price data...</span>';

    // Strategy breakdown
    const s = d.trades_by_strategy || {};
    document.getElementById('strat-oracle').textContent = s.oracle_lag || 0;
    document.getElementById('strat-arb').textContent = s.arb || 0;
    document.getElementById('strat-data').textContent = s.data_edge || 0;
    document.getElementById('strat-mm').textContent = s.mm || 0;

    // Open orders
    document.getElementById('open-count').textContent = (d.open_orders||[]).length;
    const oo = document.getElementById('open-orders');
    if ((d.open_orders||[]).length === 0) {
      oo.innerHTML = '<span class="neutral">No open orders</span>';
    } else {
      oo.innerHTML = d.open_orders.map(o => '<div class="stat"><span class="label">'+o.side+' @ $'+fmt(o.price,3)+'</span><span class="value">$'+fmt(o.size_usd)+' ('+o.age_s+'s)</span></div>').join('');
    }

    // Markets
    document.getElementById('market-count').textContent = (d.markets||[]).length;
    const mb = document.getElementById('markets-body');
    mb.innerHTML = (d.markets||[]).map(m => '<tr><td>'+m.question+'</td><td>'+catTag(m.category)+'</td><td>'+(m.asset||'-')+'</td><td>'+(m.arb_edge?m.arb_edge+'%':'-')+'</td><td>$'+fmt(m.combined_price,3)+'</td></tr>').join('');

    // Recent trades
    const tb = document.getElementById('trades-body');
    const trades = (d.recent_trades||[]).reverse();
    tb.innerHTML = trades.map(t => {
      const time = fmtTime(t.ts);
      const evt = statusTag(t.event);
      const strat = t.strategy || t.event || '-';
      const side = t.side || '-';
      const price = t.price ? '$'+fmt(t.price,3) : '-';
      const size = t.size_usd ? '$'+fmt(t.size_usd) : t.usd ? '$'+fmt(t.usd) : '-';
      const detail = t.market_question ? t.market_question.substring(0,40) : t.order_id ? t.order_id.substring(0,10) : '';
      return '<tr><td>'+time+'</td><td>'+evt+'</td><td>'+strat+'</td><td>'+side+'</td><td>'+price+'</td><td>'+size+'</td><td>'+detail+'</td></tr>';
    }).join('');
    if (!trades.length) tb.innerHTML = '<tr><td colspan="7" class="neutral">No trades yet</td></tr>';

    // Errors
    const errs = d.recent_errors || [];
    document.getElementById('error-count').textContent = errs.length;
    const eb = document.getElementById('errors-box');
    if (errs.length === 0) {
      eb.innerHTML = '<span class="neutral">No errors — all clear</span>';
    } else {
      eb.innerHTML = errs.map(e => {
        const cls = e.includes('[WARNING]') ? 'warn-line' : '';
        return '<div class="'+cls+'">'+e.replace(/</g,'&lt;')+'</div>';
      }).join('');
      eb.scrollTop = eb.scrollHeight;
    }

  } catch(e) {
    console.error('Refresh failed:', e);
  }
}

// Auto-refresh every 5 seconds
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for the dashboard."""

    def do_GET(self):
        if self.path == "/api/stats":
            data = _get_stats()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        elif self.path == "/" or self.path == "/dashboard":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default HTTP access logs."""
        pass


def start_dashboard(engine, port=8080):
    """Start the dashboard server in a background thread."""
    global _engine
    _engine = engine

    def run_server():
        try:
            server = HTTPServer(("0.0.0.0", port), DashboardHandler)
            logger.info("Dashboard running at http://0.0.0.0:%d", port)
            server.serve_forever()
        except Exception:
            logger.exception("Dashboard server failed to start")

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    return thread
