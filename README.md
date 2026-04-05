# Kalshi Kelly Compounder Bot

Runs 24/7, finds the best-edge Kalshi markets, sizes bets with the Kelly
criterion, and compounds your bankroll automatically — forever.

---

## Quick start (your own machine)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set up config
cp .env.example .env
# Open .env in any text editor and fill in your API keys

# 3. Run in demo mode first (no real trades)
python bot.py

# 4. When you're happy, set DEMO_MODE=false in .env and re-run
python bot.py
```

The bot prints everything to the console AND writes a `bot.log` file so you
can check history any time.

---

## Run 24/7 on a server (recommended)

### Option A — Free cloud (Railway / Render)
1. Push this folder to a GitHub repo
2. Go to railway.app or render.com → New project → Deploy from GitHub
3. Add your environment variables in the dashboard (same keys as .env)
4. Deploy — it runs forever and auto-restarts if it crashes

### Option B — Linux VPS (DigitalOcean, Linode, etc.)

```bash
# On the server, inside the bot folder:
pip install -r requirements.txt
cp .env.example .env
nano .env   # fill in your keys

# Run with nohup so it keeps going after you disconnect
nohup python bot.py > bot.log 2>&1 &

# Check it's running
tail -f bot.log

# Stop it
kill $(pgrep -f bot.py)
```

### Option C — systemd service (stays running after reboots)

```bash
# Create service file
sudo nano /etc/systemd/system/kalshi-bot.service
```

Paste this (update paths to match your install):
```ini
[Unit]
Description=Kalshi Kelly Compounder Bot
After=network.target

[Service]
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/kalshi_bot
ExecStart=/usr/bin/python3 /home/YOUR_USERNAME/kalshi_bot/bot.py
Restart=always
RestartSec=10
EnvironmentFile=/home/YOUR_USERNAME/kalshi_bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable kalshi-bot
sudo systemctl start kalshi-bot
sudo systemctl status kalshi-bot   # check it's running
journalctl -u kalshi-bot -f        # live log
```

---

## How it works

Every cycle (default 60 seconds):

1. Fetches open Kalshi markets
2. Pulls the public trade feed — detects whale trades above your threshold
3. Runs AI predictions on each market, blending source credibility scores
4. Calculates Kelly fraction for every qualifying trade:
   `f* = (b·p − q) / b`
   where p = estimated win prob, q = 1−p, b = net payout odds
5. Picks the market with the highest positive edge above your confidence threshold
6. Bets `f* × bankroll` (capped at your Kelly cap %)
7. Updates bankroll, logs everything, sleeps, repeats

---

## Key settings (.env)

| Setting | Default | What it does |
|---|---|---|
| `DEMO_MODE` | `true` | Simulates trades — set to `false` for real money |
| `STARTING_BANKROLL` | `20.00` | Your starting capital in dollars |
| `CONFIDENCE_THRESH` | `70` | Min AI confidence % to place a trade |
| `KELLY_CAP` | `0.25` | Max bet = 25% of bankroll (protects against variance) |
| `WHALE_THRESHOLD` | `500` | Contracts to count as a whale trade |
| `WHALE_BOOST` | `8` | Conf % boost when whale signal agrees with AI |
| `CYCLE_INTERVAL` | `60` | Seconds between compound cycles |
| `MAX_MARKETS_SCAN` | `10` | Markets to analyze per cycle |

---

## Important notes

- **Always test in DEMO_MODE=true first** — watch several cycles and make sure
  the predictions and Kelly sizes look reasonable before going live
- The bot never bets below $0.01 and shuts itself down if bankroll drops below $0.10
- All activity is logged to `bot.log` with timestamps
- Kalshi's API requires a Bearer token — get yours from kalshi.com → Settings → API
- Anthropic API key from console.anthropic.com (claude-sonnet-4 is used for predictions)
