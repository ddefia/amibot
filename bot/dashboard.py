from __future__ import annotations

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
        "pnl_history": _engine._pnl_history[-240:],
        "signal_log": _engine._signal_log[-100:],
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


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Amibot Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, 'Segoe UI', Roboto, monospace; background: #0a0e17; color: #e0e6f0; padding: 16px; min-height: 100vh; }
h1 { font-size: 22px; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #1e2738; }
.header-right { text-align: right; font-size: 11px; color: #4a5568; }
.header-right strong { color: #e0e6f0; }
.mode-badge { display: inline-block; padding: 3px 10px; border-radius: 4px; font-size: 12px; font-weight: bold; margin-left: 8px; vertical-align: middle; }
.mode-test { background: #f59e0b; color: #000; }
.mode-live { background: #ef4444; color: #fff; animation: pulse 2s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.7; } }
.grid { display: grid; gap: 12px; margin-bottom: 16px; }
.grid-4 { grid-template-columns: repeat(4, 1fr); }
.grid-2 { grid-template-columns: repeat(2, 1fr); }
.grid-3 { grid-template-columns: 1fr 1fr 1fr; }
@media (max-width: 1200px) { .grid-4 { grid-template-columns: repeat(2, 1fr); } .grid-3 { grid-template-columns: 1fr; } }
@media (max-width: 768px) { .grid-4, .grid-2 { grid-template-columns: 1fr; } }
.card { background: #141925; border: 1px solid #1e2738; border-radius: 8px; padding: 14px; }
.card h2 { font-size: 11px; color: #6b7fa3; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 1.5px; font-weight: 700; }
.stat { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; }
.stat .label { color: #8896b0; }
.stat .value { font-weight: 600; }
.positive { color: #10b981; }
.negative { color: #ef4444; }
.neutral { color: #6b7fa3; }
.warn { color: #f59e0b; }
.big-number { font-size: 32px; font-weight: 800; letter-spacing: -1px; }
.sub { font-size: 12px; color: #6b7fa3; margin-top: 2px; }
.halted { background: #1a0a0a; border-color: #ef4444; }

/* Prices */
.price-row { display: flex; gap: 12px; flex-wrap: wrap; }
.price-chip { background: #1a2035; border: 1px solid #1e2738; border-radius: 6px; padding: 10px 16px; text-align: center; min-width: 120px; flex: 1; transition: border-color 0.3s; }
.price-chip.fresh { border-color: #10b981; }
.price-chip .asset { font-size: 11px; color: #6b7fa3; font-weight: 700; letter-spacing: 1px; }
.price-chip .price { font-size: 22px; font-weight: 800; color: #e0e6f0; margin: 2px 0; }
.price-chip .source { font-size: 10px; color: #4a5568; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; color: #6b7fa3; padding: 6px 8px; border-bottom: 1px solid #1e2738; font-weight: 700; text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; }
td { padding: 5px 8px; border-bottom: 1px solid #111827; }
tr:hover { background: #1a2035; }

/* Tags */
.tag { display: inline-block; padding: 2px 7px; border-radius: 3px; font-size: 10px; font-weight: 700; letter-spacing: 0.3px; }
.tag-oracle { background: #1e3a5f; color: #60a5fa; }
.tag-arb { background: #1a3f2e; color: #34d399; }
.tag-data { background: #3f2e1a; color: #fbbf24; }
.tag-mm { background: #3f1a3f; color: #c084fc; }
.tag-open { background: #1e3a5f; color: #60a5fa; }
.tag-filled { background: #1a3f2e; color: #34d399; }
.tag-expired { background: #3f2020; color: #f87171; }
.tag-trade { background: #1a3f2e; color: #34d399; }
.tag-place { background: #1e3a5f; color: #60a5fa; }
.tag-settle { background: #3f2e1a; color: #fbbf24; }
.tag-accepted { background: #1a3f2e; color: #34d399; }
.tag-rejected { background: #2a1a1a; color: #6b7fa3; }

/* Signal feed */
.signal-feed { max-height: 400px; overflow-y: auto; font-size: 11px; font-family: 'SF Mono', 'Fira Code', monospace; }
.signal-item { padding: 4px 8px; border-bottom: 1px solid #111827; display: flex; gap: 8px; align-items: center; }
.signal-item.accepted { border-left: 2px solid #10b981; }
.signal-item.rejected { border-left: 2px solid #1e2738; opacity: 0.6; }
.signal-time { color: #4a5568; min-width: 70px; }
.signal-asset { font-weight: 700; min-width: 35px; }
.signal-reason { color: #8896b0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.signal-edge { min-width: 55px; text-align: right; font-weight: 600; }

/* Chart containers */
.chart-container { position: relative; height: 200px; }

/* Errors */
.errors-box { background: #1a1012; border: 1px solid #3f2020; border-radius: 6px; padding: 10px; max-height: 150px; overflow-y: auto; font-size: 11px; font-family: monospace; color: #f87171; }
.errors-box .warn-line { color: #fbbf24; }

/* Status dot */
.status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.dot-green { background: #10b981; box-shadow: 0 0 6px #10b981; }
.dot-red { background: #ef4444; box-shadow: 0 0 6px #ef4444; }
.dot-yellow { background: #f59e0b; }
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div>
    <h1>
      <span class="status-dot dot-green" id="status-dot"></span>
      Amibot
      <span id="mode-badge" class="mode-badge mode-test">PAPER</span>
    </h1>
  </div>
  <div class="header-right">
    Uptime: <strong id="uptime">--</strong><br>
    Updated: <span id="last-update">--</span> | Ticks: <span id="tick-count">0</span>
  </div>
</div>

<!-- Top stats row -->
<div class="grid grid-4">
  <div class="card" id="balance-card">
    <h2>Balance</h2>
    <div class="big-number" id="balance">--</div>
    <div class="sub">Start: $<span id="start-balance">--</span> | Exposure: $<span id="exposure">0</span></div>
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
    <h2>Trading Activity</h2>
    <div class="stat"><span class="label">Total trades</span><span class="value" id="total-trades">0</span></div>
    <div class="stat"><span class="label">Open orders</span><span class="value" id="open-count">0</span></div>
    <div class="stat"><span class="label">Loss streak</span><span class="value" id="loss-streak">0</span></div>
    <div class="stat"><span class="label">Signals</span><span class="value"><span id="sig-eval">0</span> eval / <span id="sig-reject">0</span> skip</span></div>
  </div>
</div>

<!-- Live Prices -->
<div class="card" style="margin-bottom: 12px;">
  <h2>Live Prices</h2>
  <div class="price-row" id="prices-row">
    <span class="neutral">Waiting for price data...</span>
  </div>
</div>

<!-- Charts row -->
<div class="grid grid-2" style="margin-bottom: 12px;">
  <div class="card">
    <h2>P&L Over Time</h2>
    <div class="chart-container"><canvas id="pnl-chart"></canvas></div>
  </div>
  <div class="card">
    <h2>Balance & Exposure</h2>
    <div class="chart-container"><canvas id="balance-chart"></canvas></div>
  </div>
</div>

<!-- Strategy + Orders row -->
<div class="grid grid-3" style="margin-bottom: 12px;">
  <div class="card">
    <h2>Trades by Strategy</h2>
    <div style="display:flex; gap: 12px; align-items: center;">
      <div style="width: 120px; height: 120px;"><canvas id="strategy-chart"></canvas></div>
      <div style="flex:1;">
        <div class="stat"><span class="label"><span class="tag tag-oracle">ORACLE</span> Lag</span><span class="value" id="strat-oracle">0</span></div>
        <div class="stat"><span class="label"><span class="tag tag-arb">ARB</span> Arb</span><span class="value" id="strat-arb">0</span></div>
        <div class="stat"><span class="label"><span class="tag tag-data">DATA</span> Edge</span><span class="value" id="strat-data">0</span></div>
        <div class="stat"><span class="label"><span class="tag tag-mm">MM</span> Making</span><span class="value" id="strat-mm">0</span></div>
      </div>
    </div>
  </div>
  <div class="card">
    <h2>Open Orders</h2>
    <div id="open-orders" style="font-size:12px; max-height: 180px; overflow-y: auto;">
      <span class="neutral">No open orders</span>
    </div>
  </div>
  <div class="card">
    <h2>Recent Trades</h2>
    <div style="max-height: 180px; overflow-y: auto;">
      <table>
        <thead><tr><th>Time</th><th>Event</th><th>Side</th><th>Price</th><th>Size</th></tr></thead>
        <tbody id="trades-body"><tr><td colspan="5" class="neutral">No trades yet</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<!-- Live Signal Feed -->
<div class="card" style="margin-bottom: 12px;">
  <h2>Live Signal Feed <span style="font-size:10px; color:#4a5568; text-transform:none; letter-spacing:0;">(<span id="signal-count">0</span> signals)</span></h2>
  <div class="signal-feed" id="signal-feed">
    <div class="signal-item rejected"><span class="signal-time">--</span><span class="neutral">Waiting for signals...</span></div>
  </div>
</div>

<!-- Markets Watched -->
<div class="card" style="margin-bottom: 12px;">
  <h2>Markets Tracked (<span id="market-count">0</span>)</h2>
  <div style="max-height: 320px; overflow-y: auto;">
    <table>
      <thead><tr><th>Market</th><th>Type</th><th>Asset</th><th>Edge</th><th>Combined</th><th>Volume 24h</th></tr></thead>
      <tbody id="markets-body"></tbody>
    </table>
  </div>
</div>

<!-- Errors -->
<div class="card">
  <h2>Errors & Warnings (<span id="error-count">0</span>)</h2>
  <div class="errors-box" id="errors-box"><span class="neutral">No errors</span></div>
</div>

<script>
// ---- Helpers ----
function fmt(n, d=2) { return n == null ? '--' : Number(n).toFixed(d); }
function fmtUsd(n) { if (n == null) return '--'; return (n >= 0 ? '+$' : '-$') + fmt(Math.abs(n)); }
function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString() : '--'; }
function fmtShortTime(ts) { if (!ts) return '--'; const d = new Date(ts*1000); return d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0'); }
function catTag(cat) {
  const map = {oracle_lag:'oracle',crypto_oracle:'oracle',arb:'arb',data_edge:'data',sports_data:'data',weather_data:'data',news_data:'data',mm:'mm'};
  return '<span class="tag tag-'+(map[cat]||'oracle')+'">'+cat.replace(/_/g,' ').toUpperCase()+'</span>';
}
function escHtml(s) { return s.replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ---- Chart Setup ----
const chartDefaults = {
  responsive: true, maintainAspectRatio: false,
  plugins: { legend: { display: false } },
  scales: {
    x: { grid: { color: '#1e2738' }, ticks: { color: '#4a5568', font: { size: 10 } } },
    y: { grid: { color: '#1e2738' }, ticks: { color: '#4a5568', font: { size: 10 } } }
  },
  elements: { point: { radius: 0 }, line: { tension: 0.3, borderWidth: 2 } },
  animation: { duration: 300 }
};

// P&L Chart
const pnlCtx = document.getElementById('pnl-chart').getContext('2d');
const pnlChart = new Chart(pnlCtx, {
  type: 'line',
  data: {
    labels: [],
    datasets: [{
      label: 'P&L',
      data: [],
      borderColor: '#10b981',
      backgroundColor: 'rgba(16,185,129,0.1)',
      fill: true,
    }]
  },
  options: {
    ...chartDefaults,
    scales: {
      ...chartDefaults.scales,
      y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, callback: v => '$'+v } }
    },
    plugins: {
      ...chartDefaults.plugins,
      tooltip: { callbacks: { label: ctx => 'P&L: $' + ctx.parsed.y.toFixed(2) } }
    }
  }
});

// Balance/Exposure Chart
const balCtx = document.getElementById('balance-chart').getContext('2d');
const balChart = new Chart(balCtx, {
  type: 'line',
  data: {
    labels: [],
    datasets: [
      { label: 'Balance', data: [], borderColor: '#60a5fa', backgroundColor: 'rgba(96,165,250,0.08)', fill: true },
      { label: 'Exposure', data: [], borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.08)', fill: true, borderDash: [4,4] }
    ]
  },
  options: {
    ...chartDefaults,
    plugins: { legend: { display: true, labels: { color: '#6b7fa3', font: { size: 10 } } } },
    scales: {
      ...chartDefaults.scales,
      y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, callback: v => '$'+v } }
    }
  }
});

// Strategy Donut
const stratCtx = document.getElementById('strategy-chart').getContext('2d');
const stratChart = new Chart(stratCtx, {
  type: 'doughnut',
  data: {
    labels: ['Oracle', 'Arb', 'Data', 'MM'],
    datasets: [{
      data: [0, 0, 0, 0],
      backgroundColor: ['#60a5fa', '#34d399', '#fbbf24', '#c084fc'],
      borderWidth: 0,
    }]
  },
  options: {
    responsive: true, maintainAspectRatio: true,
    cutout: '60%',
    plugins: { legend: { display: false } },
    animation: { duration: 300 }
  }
});

// ---- Main Refresh ----
async function refresh() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    if (d.error) { document.getElementById('balance').textContent = d.error; return; }

    // Status
    const badge = document.getElementById('mode-badge');
    badge.textContent = d.mode;
    badge.className = 'mode-badge ' + (d.mode.includes('TEST') ? 'mode-test' : 'mode-live');
    const dot = document.getElementById('status-dot');
    dot.className = 'status-dot ' + (d.drawdown_halted ? 'dot-red' : 'dot-green');

    // Header
    document.getElementById('uptime').textContent = d.uptime;
    document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
    document.getElementById('tick-count').textContent = d.tick_count || 0;

    // Top stats
    document.getElementById('balance').textContent = '$' + fmt(d.balance);
    document.getElementById('balance').className = 'big-number ' + (d.pnl >= 0 ? 'positive' : 'negative');
    document.getElementById('start-balance').textContent = fmt(d.start_balance);
    document.getElementById('exposure').textContent = fmt(d.exposure);
    document.getElementById('pnl').textContent = fmtUsd(d.pnl);
    document.getElementById('pnl').className = 'big-number ' + (d.pnl >= 0 ? 'positive' : 'negative');
    document.getElementById('daily-pnl').textContent = fmt(d.daily_pnl);
    document.getElementById('win-rate').textContent = fmt(d.win_rate, 0) + '%';
    document.getElementById('win-rate').className = 'big-number ' + (d.win_rate >= 50 ? 'positive' : d.win_rate > 0 ? 'negative' : 'neutral');
    document.getElementById('wins').textContent = d.wins;
    document.getElementById('losses').textContent = d.losses;
    document.getElementById('resolved').textContent = d.resolved;
    document.getElementById('total-trades').textContent = d.total_trades;
    document.getElementById('open-count').textContent = (d.open_orders||[]).length;
    document.getElementById('loss-streak').textContent = d.consecutive_losses;
    document.getElementById('sig-eval').textContent = d.signals_evaluated;
    document.getElementById('sig-reject').textContent = d.signals_rejected;

    const bc = document.getElementById('balance-card');
    bc.className = d.drawdown_halted ? 'card halted' : 'card';

    // Prices
    const pr = document.getElementById('prices-row');
    const priceEntries = Object.entries(d.prices || {});
    if (priceEntries.length) {
      pr.innerHTML = priceEntries.map(([asset, info]) => {
        const isFresh = info.age_s < 5;
        return '<div class="price-chip'+(isFresh?' fresh':'')+'"><div class="asset">'+asset+'</div><div class="price">$'+fmt(info.price, asset==='BTC'?0:2)+'</div><div class="source">'+info.source+' &middot; '+fmt(info.age_s,0)+'s</div></div>';
      }).join('');
    } else {
      pr.innerHTML = '<span class="neutral">Waiting for price data...</span>';
    }

    // P&L Chart
    const hist = d.pnl_history || [];
    if (hist.length > 1) {
      pnlChart.data.labels = hist.map(h => fmtShortTime(h.ts));
      pnlChart.data.datasets[0].data = hist.map(h => h.pnl);
      const anyNeg = hist.some(h => h.pnl < 0);
      pnlChart.data.datasets[0].borderColor = anyNeg ? '#ef4444' : '#10b981';
      pnlChart.data.datasets[0].backgroundColor = anyNeg ? 'rgba(239,68,68,0.1)' : 'rgba(16,185,129,0.1)';
      pnlChart.update('none');

      balChart.data.labels = hist.map(h => fmtShortTime(h.ts));
      balChart.data.datasets[0].data = hist.map(h => h.balance);
      balChart.data.datasets[1].data = hist.map(h => h.exposure);
      balChart.update('none');
    }

    // Strategy donut
    const st = d.trades_by_strategy || {};
    const stVals = [st.oracle_lag||0, st.arb||0, st.data_edge||0, st.mm||0];
    stratChart.data.datasets[0].data = stVals.some(v=>v>0) ? stVals : [1,1,1,1];
    stratChart.update('none');
    document.getElementById('strat-oracle').textContent = st.oracle_lag || 0;
    document.getElementById('strat-arb').textContent = st.arb || 0;
    document.getElementById('strat-data').textContent = st.data_edge || 0;
    document.getElementById('strat-mm').textContent = st.mm || 0;

    // Open orders
    const oo = document.getElementById('open-orders');
    const openOrders = d.open_orders || [];
    if (openOrders.length === 0) {
      oo.innerHTML = '<span class="neutral">No open orders</span>';
    } else {
      oo.innerHTML = openOrders.map(o =>
        '<div class="stat"><span class="label">'+o.side+' @ $'+fmt(o.price,3)+'</span><span class="value">$'+fmt(o.size_usd)+' <span class="neutral">('+o.age_s+'s)</span></span></div>'
      ).join('');
    }

    // Recent trades
    const tb = document.getElementById('trades-body');
    const trades = (d.recent_trades||[]).reverse().slice(0, 20);
    if (trades.length) {
      tb.innerHTML = trades.map(t => {
        const cls = t.event === 'trade' ? 'tag-trade' : t.event === 'fill' ? 'tag-filled' : t.event === 'settle' ? 'tag-settle' : 'tag-place';
        return '<tr><td>'+fmtTime(t.ts)+'</td><td><span class="tag '+cls+'">'+t.event.toUpperCase()+'</span></td><td>'+(t.side||'-')+'</td><td>'+(t.price?'$'+fmt(t.price,3):'-')+'</td><td>'+(t.size_usd?'$'+fmt(t.size_usd):'-')+'</td></tr>';
      }).join('');
    } else {
      tb.innerHTML = '<tr><td colspan="5" class="neutral">No trades yet</td></tr>';
    }

    // Signal feed
    const signals = (d.signal_log || []).reverse().slice(0, 80);
    document.getElementById('signal-count').textContent = d.signals_evaluated || 0;
    const sf = document.getElementById('signal-feed');
    if (signals.length) {
      sf.innerHTML = signals.map(s => {
        const cls = s.accepted ? 'accepted' : 'rejected';
        const edgeCls = s.edge > 0.03 ? 'positive' : s.edge > 0 ? 'warn' : 'neutral';
        return '<div class="signal-item '+cls+'">'
          + '<span class="signal-time">'+fmtTime(s.ts)+'</span>'
          + '<span class="signal-asset">'+s.asset+'</span>'
          + '<span class="tag tag-'+(s.accepted?'accepted':'rejected')+'">'+(s.accepted?'TRADE':'SKIP')+'</span>'
          + '<span class="signal-reason">'+escHtml(s.reason)+'</span>'
          + '<span class="signal-edge '+edgeCls+'">'+fmt(s.edge*100,1)+'%</span>'
          + '</div>';
      }).join('');
    } else {
      sf.innerHTML = '<div class="signal-item rejected"><span class="signal-time">--</span><span class="neutral">Waiting for signals...</span></div>';
    }

    // Markets
    const markets = d.markets || [];
    document.getElementById('market-count').textContent = markets.length;
    const mb = document.getElementById('markets-body');
    mb.innerHTML = markets.map(m => {
      const edgeCls = m.arb_edge > 5 ? 'positive' : m.arb_edge > 2 ? 'warn' : 'neutral';
      return '<tr><td>'+escHtml(m.question)+'</td><td>'+catTag(m.category)+'</td><td>'+(m.asset||'-')+'</td><td class="'+edgeCls+'">'+(m.arb_edge?m.arb_edge+'%':'-')+'</td><td>$'+fmt(m.combined_price,3)+'</td><td>$'+fmt(m.volume_24h,0)+'</td></tr>';
    }).join('');
    if (!markets.length) mb.innerHTML = '<tr><td colspan="6" class="neutral">Scanning markets...</td></tr>';

    // Errors
    const errs = d.recent_errors || [];
    document.getElementById('error-count').textContent = errs.length;
    const eb = document.getElementById('errors-box');
    if (errs.length === 0) {
      eb.innerHTML = '<span class="neutral">No errors</span>';
    } else {
      eb.innerHTML = errs.map(e => '<div class="'+(e.includes('[WARNING]')?'warn-line':'')+'">'+escHtml(e)+'</div>').join('');
      eb.scrollTop = eb.scrollHeight;
    }
  } catch(e) {
    console.error('Refresh failed:', e);
    document.getElementById('status-dot').className = 'status-dot dot-red';
  }
}

// Initial state
document.getElementById('balance').textContent = 'Loading...';
document.getElementById('balance').className = 'big-number neutral';
document.getElementById('pnl').textContent = '...';
document.getElementById('win-rate').textContent = '...';

// Auto-refresh every 3 seconds
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for the dashboard."""

    def do_GET(self):
        if self.path == "/api/stats":
            try:
                data = _get_stats()
                body = json.dumps(data).encode()
            except Exception as e:
                logger.exception("Dashboard /api/stats error")
                body = json.dumps({"error": str(e)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
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
