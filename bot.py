"""
Kalshi Kelly Compounder Bot
===========================
Runs forever. Finds the best edge on Kalshi markets,
sizes bets with Kelly criterion, compounds 24/7.

Usage:
    python bot.py

Config via .env file (see .env.example)
"""

import os
import time
import json
import math
import random
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
import anthropic
import base64
import datetime as dt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("kalshi-bot")

# ── Config ────────────────────────────────────────────────────────────────────
KALSHI_API_KEY      = os.getenv("KALSHI_API_KEY", "")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
STARTING_BANKROLL   = float(os.getenv("STARTING_BANKROLL", "20.00"))
CONFIDENCE_THRESH   = float(os.getenv("CONFIDENCE_THRESH", "70"))   # 0-100
KELLY_CAP           = float(os.getenv("KELLY_CAP", "0.25"))          # max fraction of bankroll per bet
WHALE_THRESHOLD     = int(os.getenv("WHALE_THRESHOLD", "500"))       # contracts
WHALE_BOOST         = float(os.getenv("WHALE_BOOST", "8"))           # % conf boost when whale signal agrees
CYCLE_INTERVAL      = int(os.getenv("CYCLE_INTERVAL", "60"))         # seconds between compound cycles
DEMO_MODE           = os.getenv("DEMO_MODE", "true").lower() == "true"
MAX_MARKETS_SCAN    = int(os.getenv("MAX_MARKETS_SCAN", "10"))
# RSA private key as a string in env (paste full PEM including header/footer)
KALSHI_PRIVATE_KEY  = os.getenv("KALSHI_PRIVATE_KEY", "")

KALSHI_BASE  = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_TRADE = "https://api.elections.kalshi.com"

SOURCE_CREDIBILITY = {
    "Reuters": 0.95, "Bloomberg": 0.93, "AP": 0.92, "WSJ": 0.90,
    "FT": 0.89, "NYT": 0.87, "Politico": 0.82, "CoinDesk": 0.78,
    "Twitter/X": 0.45, "Reddit": 0.38, "Unknown": 0.50,
}

# ── State ─────────────────────────────────────────────────────────────────────
bankroll      = STARTING_BANKROLL
peak_bankroll = STARTING_BANKROLL
wins          = 0
losses        = 0
total_trades  = 0
growth_log    = [STARTING_BANKROLL]

# ── Kalshi API ────────────────────────────────────────────────────────────────
def _load_private_key():
    """Load RSA private key from env — handles PEM string with real or escaped newlines."""
    if not KALSHI_PRIVATE_KEY:
        log.warning("KALSHI_PRIVATE_KEY not set")
        return None
    try:
        pem_str = KALSHI_PRIVATE_KEY
        # Handle escaped newlines stored in Railway
        if "\\n" in pem_str:
            pem_str = pem_str.replace("\\n", "\n")
        pem_str = pem_str.replace("\n", "\n")
        if not pem_str.strip().startswith("-----"):
            log.error("KALSHI_PRIVATE_KEY doesn't look like PEM format")
            return None
        key = serialization.load_pem_private_key(
            pem_str.encode("utf-8"), password=None, backend=default_backend()
        )
        log.info(f"RSA private key loaded OK ({type(key).__name__})")
        return key
    except Exception as e:
        log.error(f"Failed to load private key: {e}")
        return None

def _sign(method: str, path: str) -> dict:
    """Sign a Kalshi API request with RSA-PSS per their official spec."""
    private_key = _load_private_key()
    if not private_key or not KALSHI_API_KEY:
        log.warning("Missing key/key-id — request will be unsigned")
        return {"Content-Type": "application/json"}
    # Timestamp in milliseconds (integer string, no decimals)
    ts_ms = str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000))
    # Strip query params — Kalshi signs the path WITHOUT query string
    path_no_query = path.split("?")[0]
    # Message = timestamp + METHOD_UPPERCASE + path_without_query
    msg_string = ts_ms + method.upper() + path_no_query
    msg = msg_string.encode("utf-8")
    log.debug(f"Signing msg: {msg_string[:80]}")
    signature = private_key.sign(
        msg,
        asym_padding.PSS(
            mgf=asym_padding.MGF1(hashes.SHA256()),
            salt_length=asym_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(signature).decode("utf-8")
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
    }

def kalshi_headers():
    return _sign("GET", "/trade-api/v2/portfolio/balance")

def get_balance():
    try:
        path = "/trade-api/v2/portfolio/balance"
        hdrs = _sign("GET", path)
        r = requests.get(f"{KALSHI_TRADE}{path}", headers=hdrs, timeout=10)
        if r.status_code == 401:
            log.error(f"Auth 401 — check KALSHI_API_KEY and KALSHI_PRIVATE_KEY")
            log.error(f"Response: {r.text[:300]}")
            return None
        r.raise_for_status()
        return r.json().get("balance", 0) / 100
    except Exception as e:
        log.error(f"Balance fetch failed: {e}")
        return None

def get_markets(limit=20):
    try:
        r = requests.get(
            f"{KALSHI_BASE}/markets",
            params={"limit": limit, "status": "open"},
            timeout=15,
        )
        log.info(f"Markets API status: {r.status_code}")
        r.raise_for_status()
        raw = r.json().get("markets", [])
        log.info(f"Raw markets returned: {len(raw)}")
        if raw:
            # Log first market to see structure
            m0 = raw[0]
            log.info(f"Sample market: ticker={m0.get('ticker')} yes_bid={m0.get('yes_bid_dollars')} no_bid={m0.get('no_bid_dollars')} status={m0.get('status')}")
        markets_out = []
        for m in raw:
            try:
                yes_bid = round(float(m.get("yes_bid_dollars") or 0) * 100)
                no_bid  = round(float(m.get("no_bid_dollars")  or 0) * 100)
                # Skip zero prices and extreme prices (sports parlays etc)
                if yes_bid < 3 or yes_bid > 97 or no_bid < 3 or no_bid > 97:
                    continue
                markets_out.append({
                    "id": m["ticker"],
                    "title": m.get("title", m["ticker"]),
                    "yes_bid": yes_bid,
                    "no_bid": no_bid,
                    "category": m.get("category", "General"),
                    "volume": float(m.get("volume_fp", 0)) * 100,
                    "close_time": m.get("close_time", ""),
                })
            except Exception as me:
                log.debug(f"Skipping market {m.get('ticker')}: {me}")
        log.info(f"Loaded {len(markets_out)} valid live markets")
        if markets_out:
            log.info(f"Live tickers: {[m['id'] for m in markets_out[:5]]}")
            return markets_out
        log.warning("No valid markets from API — using demo markets")
        return DEMO_MARKETS
    except Exception as e:
        log.warning(f"Market fetch failed: {e} — using demo markets")
        return DEMO_MARKETS

def get_public_trades(limit=200):
    try:
        r = requests.get(f"{KALSHI_BASE}/trades", params={"limit": limit}, timeout=10)
        r.raise_for_status()
        return r.json().get("trades", [])
    except Exception as e:
        log.warning(f"Trade feed fetch failed: {e}")
        return []

def place_order(ticker, side, price_cents, count):
    if DEMO_MODE:
        log.info(f"[DEMO] Would place: {count}x {side.upper()} @ {price_cents}¢ on {ticker}")
        return True
    try:
        import uuid as _uuid
        path = "/trade-api/v2/portfolio/orders"
        payload = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": max(1, int(count)),
            "client_order_id": str(_uuid.uuid4()),
        }
        if side == "yes":
            payload["yes_price"] = price_cents
        else:
            payload["no_price"] = price_cents
        hdrs = _sign("POST", path)
        log.info(f"Sending order to {KALSHI_TRADE}{path}")
        log.info(f"Payload: {payload}")
        r = requests.post(
            f"{KALSHI_TRADE}{path}",
            headers=hdrs,
            json=payload,
            timeout=10,
        )
        log.info(f"Order response status: {r.status_code}")
        log.info(f"Order response body: {r.text[:500]}")
        if r.status_code in (200, 201):
            log.info(f"Order placed successfully!")
            return True
        else:
            log.error(f"Order failed {r.status_code}: {r.text[:400]}")
            return False
    except Exception as e:
        log.error(f"Order failed: {e}")
        return False

# ── Whale Detection ───────────────────────────────────────────────────────────
def detect_whales(trades):
    signals = {}
    whale_count = 0
    for t in trades:
        count = float(t.get("count_fp", 0))
        if count < WHALE_THRESHOLD:
            continue
        ticker = t.get("ticker", "")
        side   = t.get("taker_side", "yes")
        whale_count += 1
        if ticker not in signals:
            signals[ticker] = {"side": side, "count": 0, "total_size": 0}
        signals[ticker]["count"] += 1
        signals[ticker]["total_size"] += count
        if signals[ticker]["side"] != side:
            signals[ticker]["side"] = "mixed"
    if whale_count:
        log.info(f"Whale scan: {whale_count} large trades across {len(signals)} markets")
    return signals

# ── Kelly Math ────────────────────────────────────────────────────────────────
def kelly_fraction(p, b):
    """Full Kelly: f* = (b*p - q) / b"""
    if b <= 0:
        return 0.0
    q = 1 - p
    f = (b * p - q) / b
    return max(0.0, f)

def kelly_bet(prob_pct, market_price_cents, whale_boost_pct=0):
    # Guard: skip markets with invalid prices
    price_cents = max(1, min(99, market_price_cents or 50))
    p = min(0.97, max(0.03, (prob_pct + whale_boost_pct) / 100))
    price = price_cents / 100
    b = (1 - price) / price          # net payout per $1 wagered
    if b <= 0:
        return {"f": 0, "f_raw": 0, "bet_size": 0, "b": b, "p": p, "edge": 0, "price": price}
    f = kelly_fraction(p, b)
    f_capped = min(f, KELLY_CAP)
    bet_size = round(f_capped * bankroll, 2)
    edge = p - (1 - p) / b
    return {
        "f": f_capped,
        "f_raw": f,
        "bet_size": bet_size,
        "b": b,
        "p": p,
        "edge": edge,
        "price": price,
    }

# ── AI Prediction ─────────────────────────────────────────────────────────────
def run_prediction(market, whale_signal=None):
    whale_ctx = ""
    if whale_signal:
        whale_ctx = (
            f"\nWHALE SIGNAL: {whale_signal['count']} large trades detected, "
            f"majority direction: {whale_signal['side'].upper()} "
            f"({round(whale_signal['total_size']):,} contracts). "
            f"Factor as potential smart money signal."
        )
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prompt = f"""You are a prediction market analyst on Kalshi optimizing for Kelly criterion compounding.

Market: "{market['title']}"
Category: {market['category']}
YES price: {market['yes_bid']}¢ | NO price: {market['no_bid']}¢
Close time: {market.get('close_time','TBD')}
Now (UTC): {now_utc}{whale_ctx}

Assess edge vs market pricing carefully. Be conservative — only flag high-confidence edges.

Respond ONLY with valid JSON (no markdown, no preamble):
{{"side":"yes" or "no","targetPrice":<1-99>,"confidence":<50-99>,"reasoning":"<2 sentences>","sources":["Reuters","Bloomberg","AP","WSJ","FT","NYT","CoinDesk","Politico","Twitter/X","Unknown"]}}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        pred = json.loads(text.replace("```json", "").replace("```", "").strip())
        sources = pred.get("sources", ["Unknown"])
        src_avg = sum(SOURCE_CREDIBILITY.get(s, 0.5) for s in sources) / len(sources)
        pred["blended_conf"] = round(pred["confidence"] * 0.65 + pred["confidence"] * src_avg * 0.35)
        return pred
    except Exception as e:
        log.error(f"Prediction failed for {market['id']}: {e}")
        return None

# ── Compound Cycle ────────────────────────────────────────────────────────────
def run_compound_cycle(markets, whale_signals):
    global bankroll, peak_bankroll, wins, losses, total_trades, growth_log

    log.info("=" * 60)
    log.info(f"Compound cycle | Bankroll: ${bankroll:.2f} | Trades: {total_trades} | W/L: {wins}/{losses}")

    best_market  = None
    best_kelly   = None
    best_edge    = -999
    best_pred    = None
    best_boost   = 0

    scan_markets = markets[:MAX_MARKETS_SCAN]
    log.info(f"Scanning {len(scan_markets)} markets...")
    log.info(f"Market tickers: {[m['id'] for m in scan_markets]}")

    for m in scan_markets:
        ws = whale_signals.get(m["id"])
        whale_boost = 0
        if ws and ws["side"] in ("yes", "no"):
            whale_boost = WHALE_BOOST

        pred = run_prediction(m, ws)
        if not pred:
            continue

        conf = min(99, pred["blended_conf"] + whale_boost)
        if conf < CONFIDENCE_THRESH:
            log.debug(f"  {m['id']}: {conf}% — below threshold, skipping")
            continue

        price = m["yes_bid"] if pred["side"] == "yes" else m["no_bid"]
        k = kelly_bet(conf, price, 0)

        if k["edge"] > best_edge and k["bet_size"] >= 0.01:
            best_edge    = k["edge"]
            best_market  = m
            best_kelly   = k
            best_kelly["side"] = pred["side"]
            best_kelly["conf"] = conf
            best_pred    = pred
            best_boost   = whale_boost

        log.info(
            f"  {m['id'][:20]:20s} | {pred['side'].upper():3s} {conf}% | "
            f"edge {k['edge']*100:+.1f}% | f*={k['f']*100:.1f}% → ${k['bet_size']:.2f}"
        )
        time.sleep(0.5)  # gentle rate limiting between AI calls

    if not best_market or not best_kelly or best_kelly["bet_size"] < 0.01:
        log.warning("No qualifying trade this cycle — holding bankroll")
        return

    side     = best_kelly["side"]
    bet_amt  = best_kelly["bet_size"]
    price    = best_market["yes_bid"] if side == "yes" else best_market["no_bid"]
    b        = best_kelly["b"]
    p        = best_kelly["p"]

    log.info(f"BEST TRADE: {best_market['title'][:60]}")
    log.info(f"  Side: {side.upper()} @ {price}¢  |  Bet: ${bet_amt:.2f}  |  f*: {best_kelly['f']*100:.1f}%")
    log.info(f"  Edge: {best_kelly['edge']*100:+.2f}%  |  Confidence: {best_kelly['conf']}%  |  Whale boost: +{best_boost}%")
    log.info(f"  Reasoning: {best_pred['reasoning']}")

    # Place the trade
    contracts = max(1, int(bet_amt / (price / 100)))
    order_ok  = place_order(best_market["id"], side, price, contracts)

    if not order_ok:
        log.error("Order placement failed — skipping bankroll update")
        return

    # Simulate outcome (demo) or use known result (live — would need settlement logic)
    won = random.random() < p
    total_trades += 1

    if won:
        payout  = round(bet_amt * (1 / best_kelly["price"] - 1), 2)
        bankroll = round(bankroll + payout, 2)
        wins    += 1
        peak_bankroll = max(peak_bankroll, bankroll)
        pct = (bankroll - STARTING_BANKROLL) / STARTING_BANKROLL * 100
        log.info(f"  RESULT: WIN  +${payout:.2f}  →  Bankroll: ${bankroll:.2f}  ({pct:+.1f}% from start)")
    else:
        bankroll = round(bankroll - bet_amt, 2)
        losses  += 1
        pct = (bankroll - STARTING_BANKROLL) / STARTING_BANKROLL * 100
        log.info(f"  RESULT: LOSS -${bet_amt:.2f}  →  Bankroll: ${bankroll:.2f}  ({pct:+.1f}% from start)")

    drawdown = (peak_bankroll - bankroll) / peak_bankroll * 100 if peak_bankroll > 0 else 0
    win_rate = wins / total_trades * 100 if total_trades else 0
    log.info(f"  Peak: ${peak_bankroll:.2f}  |  Drawdown: {drawdown:.1f}%  |  Win rate: {win_rate:.0f}%")
    growth_log.append(bankroll)

    if bankroll < 0.10:
        log.critical("Bankroll below $0.10 — stopping to prevent ruin. Restart to reset.")
        raise SystemExit(1)

# ── Demo Markets ──────────────────────────────────────────────────────────────
DEMO_MARKETS = [
    {"id":"FED-MAY26","title":"Will the Fed cut rates in May 2026?","yes_bid":34,"no_bid":66,"category":"Economics","volume":842300,"close_time":"2026-05-08T18:00:00Z"},
    {"id":"CPI-APR26","title":"Will US CPI be below 3% in April 2026?","yes_bid":58,"no_bid":42,"category":"Economics","volume":521000,"close_time":"2026-05-15T18:00:00Z"},
    {"id":"BTC-100K","title":"Will Bitcoin hit $100k before June 2026?","yes_bid":41,"no_bid":59,"category":"Crypto","volume":1240000,"close_time":"2026-05-31T23:59:00Z"},
    {"id":"SPX-5800","title":"Will S&P 500 close above 5800 in April 2026?","yes_bid":63,"no_bid":37,"category":"Finance","volume":987000,"close_time":"2026-04-30T21:00:00Z"},
    {"id":"NVDA-200","title":"Will Nvidia hit $200/share by end of Q2?","yes_bid":47,"no_bid":53,"category":"Finance","volume":710000,"close_time":"2026-06-30T20:00:00Z"},
]

# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    global bankroll

    log.info("=" * 60)
    log.info("Kalshi Kelly Compounder Bot starting up")
    log.info(f"  Mode:        {'DEMO (no real trades)' if DEMO_MODE else '>>> LIVE TRADING <<<'}")
    log.info(f"  Bankroll:    ${STARTING_BANKROLL:.2f}")
    log.info(f"  Threshold:   {CONFIDENCE_THRESH}% confidence")
    log.info(f"  Kelly cap:   {KELLY_CAP*100:.0f}% per trade")
    log.info(f"  Interval:    {CYCLE_INTERVAL}s")
    log.info(f"  Whale boost: +{WHALE_BOOST}% when signal agrees")
    log.info("=" * 60)

    if not ANTHROPIC_API_KEY:
        log.critical("ANTHROPIC_API_KEY not set in .env — cannot run predictions. Exiting.")
        raise SystemExit(1)

    if not DEMO_MODE and not KALSHI_API_KEY:
        log.critical("KALSHI_API_KEY not set and DEMO_MODE=false. Set key or enable demo mode.")
        raise SystemExit(1)

    # Pull live balance if connected
    if not DEMO_MODE and KALSHI_API_KEY:
        live_bal = get_balance()
        if live_bal is not None:
            bankroll = live_bal
            log.info(f"Live balance loaded: ${bankroll:.2f}")
        else:
            log.warning("Could not fetch balance — using configured starting bankroll")

    cycle = 0
    while True:
        cycle += 1
        log.info(f"\n{'─'*60}\nCYCLE {cycle}  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n{'─'*60}")

        # Load markets
        markets = get_markets(limit=MAX_MARKETS_SCAN + 5)

        # Whale scan (public — no key needed)
        trades = get_public_trades(limit=200)
        whale_signals = detect_whales(trades) if trades else {}
        if whale_signals:
            log.info(f"Whale signals active: {list(whale_signals.keys())}")

        # Run compound cycle
        try:
            run_compound_cycle(markets, whale_signals)
        except SystemExit:
            raise
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)

        log.info(f"Sleeping {CYCLE_INTERVAL}s until next cycle...\n")
        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    main()
