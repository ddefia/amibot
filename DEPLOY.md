# VPS Deployment Guide — Polymarket Multi-Strategy Bot

## Why a VPS?

| | Current Server | London VPS |
|---|---|---|
| Binance WebSocket | BLOCKED (HTTP 403) | **~240ms** (working) |
| Polymarket CLOB | ~500ms | **~1-5ms** (same AWS region) |
| Price updates | REST every 500ms | **WebSocket sub-10ms** |
| Uptime | Tied to session | **24/7 systemd** |

## Step 1: Create Your VPS

### Option A: Vultr London — $6/month (recommended)

1. Go to [vultr.com](https://www.vultr.com) and create an account
2. Click **Deploy New Server**
3. Choose:
   - Type: **Cloud Compute (Regular)**
   - Location: **London**
   - OS: **Ubuntu 24.04 LTS**
   - Plan: **$6/mo** (1 vCPU, 1GB RAM, 25GB NVMe)
4. Click **Deploy Now**
5. Wait ~60 seconds, copy the IP address and root password

### Option B: Oracle Cloud — $0/month (free forever)

1. Go to [oracle.com/cloud/free](https://www.oracle.com/cloud/free/)
2. Sign up — choose **UK South (London)** as your home region
3. Go to **Compute → Instances → Create Instance**
4. Choose:
   - Shape: **VM.Standard.A1.Flex** (ARM) — 1 OCPU, 6GB RAM
   - OS: **Ubuntu 24.04**
5. Add your SSH key and launch
6. Note: ARM instances may be out of capacity — keep retrying or use the automation script at github.com/mohankumarpaluru/oracle-freetier-instance-creation

### Option C: OVHcloud London — ~$3.50/month

1. Go to [ovhcloud.com](https://www.ovhcloud.com)
2. VPS → Starter plan → London datacenter → Ubuntu 24.04

## Step 2: Copy Code to VPS

From your local machine (or wherever the repo lives):

```bash
# Replace YOUR_VPS_IP with the actual IP
rsync -avz \
  --exclude='.env' \
  --exclude='venv' \
  --exclude='__pycache__' \
  --exclude='*.log' \
  --exclude='*.jsonl' \
  --exclude='data/' \
  /path/to/amibot/ root@YOUR_VPS_IP:~/amibot/
```

Or if using git:
```bash
ssh root@YOUR_VPS_IP
git clone YOUR_REPO_URL ~/amibot
```

## Step 3: Run Deployment Script

```bash
ssh root@YOUR_VPS_IP
cd ~/amibot
bash deploy.sh
```

The script will:
- Install Python 3, pip, venv, git
- Create virtual environment and install dependencies
- Test all network endpoints and report latencies
- Check if Binance WebSocket works (it should on the VPS!)
- Check Polymarket geo-block status for your VPS IP
- Create a default `.env` file (paper trading mode)
- Set up systemd service for 24/7 operation

You should see output like:
```
  Binance WS:       OK  (240ms, BTC=$84523)
  Kraken REST:      OK  (15ms, status=200)
  Polymarket CLOB:  OK  (3ms, status=200)
  Gamma API:        OK  (5ms, status=200)
  Geo-block check:  blocked=false

  >>> WebSocket mode available — sub-10ms price updates <<<
```

## Step 4: Start the Bot

### Paper trading (default — no real money):
```bash
sudo systemctl start amibot
```

### Watch it run:
```bash
# Live service logs
journalctl -u amibot -f

# Bot application log
tail -f ~/amibot/bot.log

# Trade log (every trade as JSON)
tail -f ~/amibot/trades.jsonl
```

### Control commands:
```bash
sudo systemctl status amibot     # Check if running
sudo systemctl stop amibot       # Stop
sudo systemctl restart amibot    # Restart (after config changes)
```

## Step 5: Go Live (Real Money)

**Only after you've verified paper trading works for at least 48 hours.**

1. Fund a Polygon wallet with USDC
2. Edit the config:
```bash
nano ~/amibot/.env
```

3. Set these values:
```
PRIVATE_KEY=0xYOUR_ACTUAL_POLYGON_PRIVATE_KEY
DRY_RUN=false
POSITION_USD=10        # Start small
MAX_EXPOSURE_USDC=30   # Total risk cap
```

4. Restart:
```bash
sudo systemctl restart amibot
```

5. Monitor:
```bash
tail -f ~/amibot/trades.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    t = json.loads(line)
    print(f\"{t.get('event','?'):8} | {t.get('strategy','?'):12} | {t.get('side','?'):10} | \${t.get('size_usd',0):6.0f} | {t.get('market_question','')[:50]}\")
"
```

## Architecture on VPS

```
┌─────────────────────────────────────────────┐
│  VPS (London)                               │
│                                             │
│  MultiFeed ──── Binance WS (240ms) ────→ Binance Tokyo
│      │                                      │
│      ├──────── Kraken REST (15ms) ─────→ Kraken EU
│      │                                      │
│  UnifiedEngine                              │
│      │                                      │
│      ├── Oracle Lag (BTC/ETH/SOL/XRP)       │
│      ├── Arbitrage (all markets)            │
│      ├── Data Edge (NOAA/ESPN/Reddit)       │
│      └── Market Making (high-vol markets)   │
│      │                                      │
│  Executor ──── Polymarket CLOB (1-5ms) ─→ Polymarket London
│                                             │
└─────────────────────────────────────────────┘
```

## Troubleshooting

**Bot won't start:**
```bash
cd ~/amibot && source venv/bin/activate && python run.py
# Read the error message — usually a missing dep or bad .env
```

**Binance WS blocked on VPS:**
The feed auto-falls back to REST. Check with:
```bash
journalctl -u amibot | grep "WebSocket\|REST\|detecting"
```

**Geo-blocked by Polymarket:**
Check your VPS IP:
```bash
curl -s https://polymarket.com/api/geoblock | python3 -m json.tool
```
If `blocked: true`, your VPS is in a restricted country. Use a different region.

**High latency:**
```bash
# Test Polymarket CLOB latency
curl -w "time_total: %{time_total}s\n" -o /dev/null -s https://clob.polymarket.com/time

# Test Binance
curl -w "time_total: %{time_total}s\n" -o /dev/null -s https://api.binance.com/api/v3/time
```

## Cost Summary

| Component | Cost |
|---|---|
| VPS (Vultr London) | $6/month |
| Binance API | Free (public WebSocket, no account needed) |
| Polymarket API | Free |
| NOAA / ESPN / Reddit APIs | Free |
| Total | **$6/month** (or $0 with Oracle Cloud) |
