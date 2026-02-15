# Amibot 24/7 Deployment Guide

Complete step-by-step guide to deploy the Polymarket BTC trading bot on a cloud server.

---

## Part 1: Create a Server on Hetzner ($4.51/month)

### 1.1 Create an Account
1. Go to https://www.hetzner.com/cloud
2. Click "Sign Up" → enter your email → verify it
3. Add a payment method (credit card or PayPal)

### 1.2 Create a Server
1. Log into https://console.hetzner.cloud
2. Click **"+ Create a Server"**
3. Choose these settings:
   - **Location**: Ashburn, VA (closest to Polymarket/Binance servers)
   - **Image**: Ubuntu 24.04
   - **Type**: Shared vCPU → **CX22** (2 vCPU, 4 GB RAM, €4.35/mo)
   - **Networking**: Leave defaults (Public IPv4 checked)
   - **SSH Keys**: Click "Add SSH Key"
     - If you don't have one, open a terminal on YOUR computer and run:
       ```
       ssh-keygen -t ed25519 -C "amibot"
       ```
       Press Enter for all defaults. Then run:
       ```
       cat ~/.ssh/id_ed25519.pub
       ```
       Copy the output and paste it into Hetzner's "Add SSH Key" box.
   - **Name**: `amibot`
4. Click **"Create & Buy Now"**
5. Wait ~30 seconds. You'll see an IP address like `65.108.xxx.xxx`. Copy it.

---

## Part 2: Connect to Your Server

Open a terminal on YOUR computer (Terminal on Mac, PowerShell on Windows, or any terminal on Linux).

```bash
ssh root@YOUR_IP_ADDRESS
```

Replace `YOUR_IP_ADDRESS` with the IP from Hetzner. Type `yes` when asked about fingerprint.

You're now on your server. Everything below runs on the server.

---

## Part 3: Install Everything

Copy and paste this entire block — it installs Python, downloads the bot, and installs dependencies:

```bash
# Update system and install Python
apt update && apt install -y python3 python3-pip python3-venv git

# Create a non-root user for the bot
useradd -m -s /bin/bash botuser

# Switch to that user
su - botuser

# Clone the repository
git clone https://github.com/ddefia/amibot.git /home/botuser/amibot
cd /home/botuser/amibot
git checkout claude/polymarket-bot-research-ZhJjb

# Create a virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Part 4: Create Your Polygon Wallet

Still on the server, still as `botuser`:

```bash
cd /home/botuser/amibot
source venv/bin/activate

python3 -c "
from eth_account import Account
acct = Account.create()
print()
print('========================================')
print('  YOUR NEW POLYGON WALLET')
print('========================================')
print(f'  Address:     {acct.address}')
print(f'  Private Key: 0x{acct.key.hex()}')
print('========================================')
print()
print('  WRITE THESE DOWN AND SAVE THEM.')
print('  You will need the Address to send USDC to.')
print('  You will need the Private Key for the bot config.')
print()
"
```

**SAVE BOTH THE ADDRESS AND PRIVATE KEY.** Screenshot them or write them down.

---

## Part 5: Fund Your Wallet ($100 USDC + gas)

### Option A: From Coinbase
1. Open Coinbase app or website
2. Buy $100 USDC (or you may already have some)
3. Go to **Send** → paste your wallet Address from Step 4
4. **IMPORTANT**: Select **Polygon** as the network (NOT Ethereum)
5. Send $100 USDC
6. Also send $1 worth of MATIC/POL to the same address on Polygon (for gas fees)

### Option B: From any exchange that supports Polygon withdrawals
1. Withdraw USDC to your Address on the **Polygon** network
2. Also withdraw ~$1 of MATIC/POL for gas

### Verify it arrived
Wait 2-3 minutes, then check:
```
https://polygonscan.com/address/YOUR_ADDRESS
```
You should see your USDC balance.

---

## Part 6: Configure the Bot

```bash
cd /home/botuser/amibot

# Create the .env file
cat > .env << 'ENVEOF'
# Polymarket BTC Trading Bot — LIVE CONFIG

# YOUR PRIVATE KEY (paste from Part 4)
PRIVATE_KEY=0xPASTE_YOUR_PRIVATE_KEY_HERE

# Polymarket CLOB API credentials (auto-derived from private key)
POLY_API_KEY=
POLY_API_SECRET=
POLY_PASSPHRASE=

# LIVE TRADING
DRY_RUN=false

# Strategy — tuned from paper trading analysis
STRATEGY=latency_arb
POSITION_USD=10
MAX_POSITION_SIZE=15
MAX_EXPOSURE_USDC=30
MIN_EDGE_THRESHOLD=0.05
CONFIDENCE_THRESHOLD=0.60
MAKER_ONLY=true
PREFER_15MIN=true

# Risk management — tight limits for $100 bankroll
MAX_CONSECUTIVE_LOSSES=4
DAILY_LOSS_LIMIT_USDC=15
COOLDOWN_AFTER_LOSS_STREAK_SECONDS=300
MAX_POSITIONS_PER_INTERVAL=2

# Bankroll protection
MAX_PCT_PER_TRADE=0.05
MAX_PCT_PER_INTERVAL=0.10
MAX_PCT_TOTAL_EXPOSURE=0.30
DRAWDOWN_HALT_PCT=0.15
BALANCE_REFRESH_INTERVAL=30

# Execution
FILL_CHECK_INTERVAL=3
ORDER_TIMEOUT=45

# Logging
LOG_LEVEL=INFO
LOG_FILE=bot.log
TRADE_LOG=trades.jsonl
ENVEOF
```

Now edit the file to paste your real private key:

```bash
nano .env
```

- Arrow down to the `PRIVATE_KEY=` line
- Delete `0xPASTE_YOUR_PRIVATE_KEY_HERE`
- Paste your actual private key from Part 4 (starts with `0x`)
- Press `Ctrl+O` then `Enter` to save
- Press `Ctrl+X` to exit

---

## Part 7: Test It (Dry Run First)

```bash
cd /home/botuser/amibot
source venv/bin/activate

# Quick test — should show your balance and find a market
DRY_RUN=true python3 run.py
```

You should see:
```
============================================================
  Polymarket BTC Latency Arb Bot [DRY RUN]
  ...
============================================================
  PAPER TRADING — real data, simulated orders
  ...
Bot starting [DRY RUN]
Available balance: $100.00 USDC
Found 15-min market: Bitcoin Up or Down - ...
```

If you see errors, check:
- Is your private key correct in `.env`?
- Did `pip install` finish without errors?

Press `Ctrl+C` to stop the test.

---

## Part 8: Test Live (One Trade)

```bash
cd /home/botuser/amibot
source venv/bin/activate

# Start in live mode — will verify your wallet on startup
python3 run.py
```

You should see:
```
Polymarket BTC Latency Arb Bot [LIVE]
LIVE MODE — Wallet balance: $100.00 USDC
LIVE MODE — Wallet allowance: $100.00 USDC
```

If it says "Insufficient balance" — your USDC hasn't arrived yet. Wait and retry.

Let it run for one interval (~15 min). Watch the logs. Press `Ctrl+C` to stop.

---

## Part 9: Install as 24/7 Service

Exit back to root first:

```bash
exit  # back to root user
```

Now set up the systemd service:

```bash
# Create the service file
cat > /etc/systemd/system/amibot.service << 'EOF'
[Unit]
Description=Polymarket BTC Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/home/botuser/amibot
ExecStart=/home/botuser/amibot/venv/bin/python3 /home/botuser/amibot/run.py
Restart=always
RestartSec=10
StandardOutput=append:/home/botuser/amibot/bot_output.log
StandardError=append:/home/botuser/amibot/bot_output.log

# Auto-restart limits
StartLimitIntervalSec=600
StartLimitBurst=10

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
systemctl daemon-reload
systemctl enable amibot
systemctl start amibot
```

---

## Part 10: Verify It's Running

```bash
# Check status (should say "active (running)")
systemctl status amibot

# Watch live logs
tail -f /home/botuser/amibot/bot_output.log

# Press Ctrl+C to stop watching (bot keeps running)
```

---

## Daily Operations

### Check how the bot is doing
```bash
ssh root@YOUR_IP_ADDRESS

# Quick status
systemctl status amibot

# Recent trade results
grep "Trade resolved" /home/botuser/amibot/bot_output.log | tail -20

# Current balance
grep "balance" /home/botuser/amibot/bot_output.log | tail -5

# Today's signals
grep "SIGNAL:" /home/botuser/amibot/bot_output.log | tail -10
```

### Stop the bot
```bash
systemctl stop amibot
```

### Restart after changing config
```bash
# Edit config as botuser
su - botuser
nano /home/botuser/amibot/.env
exit

# Restart
systemctl restart amibot
```

### Update the bot code
```bash
su - botuser
cd /home/botuser/amibot
git pull
exit
systemctl restart amibot
```

### View full trade history
```bash
cat /home/botuser/amibot/trades.jsonl | python3 -m json.tool
```

---

## Troubleshooting

### Bot won't start
```bash
journalctl -u amibot -n 50 --no-pager
```
Look for error messages. Common issues:
- Wrong private key in `.env`
- Missing Python packages (re-run `pip install -r requirements.txt`)
- No USDC in wallet

### Bot starts but no trades
This is normal. The bot only trades when conditions are right (~22% of intervals).
Check what it's doing:
```bash
grep "No signal:" /home/botuser/amibot/bot_output.log | tail -10
```

### Bot says "Insufficient balance"
Your USDC hasn't arrived on Polygon, or you sent it on the wrong network.
Check: `https://polygonscan.com/address/YOUR_ADDRESS`

### Server cost
Hetzner charges ~$4.51/month. You can delete the server anytime from the Hetzner console to stop charges.
