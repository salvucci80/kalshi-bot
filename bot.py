"""
Kalshi Kelly Compounder Bot — v5 Conservative
"""
import os, time, json, logging, requests, base64, datetime as dt
from datetime import datetime, timezone
from dotenv import load_dotenv
import anthropic
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")])
log = logging.getLogger("kalshi-bot")

KALSHI_API_KEY     = os.getenv("KALSHI_API_KEY", "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY", "")
DEMO_MODE          = os.getenv("DEMO_MODE", "true").lower() == "true"
STARTING_BANKROLL  = float(os.getenv("STARTING_BANKROLL", "20.00"))
CYCLE_INTERVAL     = int(os.getenv("CYCLE_INTERVAL", "300"))

CONFIDENCE_THRESH  = 80.0
MIN_EDGE_CENTS     = 15
KELLY_CAP          = 0.05
MAX_CONTRACTS      = 3
MIN_BALANCE        = 10.00
PRICE_BUFFER       = 3

KALSHI_BASE  = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_TRADE = "https://api.elections.kalshi.com"

MARKET_SERIES = [
    "KXBTCU", "KXETHU", "KXCPI", "KXPCE",
    "KXFED", "KXUNEMPLOYMENT", "KXGDP",
    "KXINXW", "KXSPX", "KXPOTUS", "KXSENATE", "KXDXY",
]

SOURCE_CREDIBILITY = {
    "Reuters":0.95,"Bloomberg":0.93,"AP":0.92,"WSJ":0.90,
    "FT":0.89,"NYT":0.87,"Politico":0.82,"CoinDesk":0.78,
    "Twitter/X":0.45,"Reddit":0.38,"Unknown":0.50,
}

bankroll = STARTING_BANKROLL
peak_bankroll = STARTING_BANKROLL
wins = losses = total_trades = 0

def _load_key():
    if not KALSHI_PRIVATE_KEY: return None
    try:
        pem = KALSHI_PRIVATE_KEY.replace("\\n", "\n")
        if not pem.strip().startswith("-----"): return None
        return serialization.load_pem_private_key(pem.encode(), password=None, backend=default_backend())
    except Exception as e:
        log.error(f"Key load error: {e}"); return None

def _sign(method, path):
    key = _load_key()
    if not key or not KALSHI_API_KEY: return {"Content-Type":"application/json"}
    ts = str(int(dt.datetime.now(dt.timezone.utc).timestamp()*1000))
    msg = (ts+method.upper()+path.split("?")[0]).encode()
    sig = base64.b64encode(key.sign(msg, asym_padding.PSS(
        mgf=asym_padding.MGF1(hashes.SHA256()), salt_length=asym_padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())).decode()
    return {"Content-Type":"application/json","KALSHI-ACCESS-KEY":KALSHI_API_KEY,
            "KALSHI-ACCESS-SIGNATURE":sig,"KALSHI-ACCESS-TIMESTAMP":ts}

def get_balance():
    try:
        path = "/trade-api/v2/portfolio/balance"
        r = requests.get(f"{KALSHI_TRADE}{path}", headers=_sign("GET",path), timeout=10)
        if not r.ok: log.error(f"Balance error {r.status_code}: {r.text[:150]}"); return None
        d = r.json()
        bal = d.get("balance",0)/100
        pv  = d.get("portfolio_value",0)/100
        log.info(f"Balance: ${bal:.2f} cash | ${pv:.2f} positions | ${bal+pv:.2f} total")
        return bal
    except Exception as e:
        log.error(f"Balance error: {e}"); return None

def cancel_all_resting():
    try:
        path = "/trade-api/v2/portfolio/orders"
        r = requests.get(f"{KALSHI_TRADE}{path}", params={"status":"resting"},
                         headers=_sign("GET",path), timeout=10)
        if not r.ok: return
        orders = r.json().get("orders",[])
        if not orders: return
        log.info(f"Cancelling {len(orders)} resting orders...")
        for o in orders:
            oid = o.get("order_id","")
            if not oid: continue
            dp = f"/trade-api/v2/portfolio/orders/{oid}"
            dr = requests.delete(f"{KALSHI_TRADE}{dp}", headers=_sign("DELETE",dp), timeout=10)
            log.info(f"  Cancel {oid[:8]}: {dr.status_code}")
    except Exception as e:
        log.error(f"Cancel error: {e}")

def get_markets():
    markets = []
    seen = set()
    for series in MARKET_SERIES:
        try:
            r = requests.get(f"{KALSHI_BASE}/markets",
                             params={"limit":5,"status":"open","series_ticker":series}, timeout=10)
            if not r.ok: continue
            for m in r.json().get("markets",[]):
                tid = m.get("ticker","")
                if tid in seen: continue
                seen.add(tid)
                yb = round(float(m.get("yes_bid_dollars") or 0)*100)
                nb = round(float(m.get("no_bid_dollars") or 0)*100)
                if yb < 5 or yb > 95 or nb < 5 or nb > 95: continue
                markets.append({"id":tid,"title":m.get("title",tid),
                    "yes_bid":yb,"no_bid":nb,"category":m.get("category",series),
                    "close_time":m.get("close_time",""),"volume":float(m.get("volume_fp",0))})
        except Exception as e:
            log.debug(f"Series {series}: {e}")
        time.sleep(0.2)
    log.info(f"Loaded {len(markets)} tradeable markets | {[m['id'] for m in markets[:5]]}")
    return markets

def place_order(ticker, side, price_cents, count):
    if DEMO_MODE:
        log.info(f"[DEMO] {count}x {side.upper()} @ {price_cents}¢ on {ticker}")
        return True
    try:
        import uuid
        path = "/trade-api/v2/portfolio/orders"
        fp = min(99, price_cents + PRICE_BUFFER)
        sc = min(max(1,int(count)), MAX_CONTRACTS)
        payload = {"ticker":ticker,"action":"buy","side":side,"type":"limit",
                   "time_in_force":"immediate_or_cancel","count":sc,
                   "client_order_id":str(uuid.uuid4())}
        if side=="yes": payload["yes_price"]=fp
        else: payload["no_price"]=fp
        log.info(f"IOC order: {sc}x {side.upper()} @ {fp}¢ on {ticker}")
        r = requests.post(f"{KALSHI_TRADE}{path}", headers=_sign("POST",path),
                          json=payload, timeout=10)
        log.info(f"Response {r.status_code}: {r.text[:250]}")
        if r.status_code in (200,201):
            order = r.json().get("order",{})
            filled = float(order.get("fill_count_fp",0))
            status = order.get("status","?")
            log.info(f"Status: {status} | Filled: {filled} contracts")
            return True
        log.error(f"Order failed {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        log.error(f"Order exception: {e}"); return False

def kelly_bet(ai_prob, mkt_price_cents):
    p = min(0.97, max(0.03, ai_prob/100))
    price = max(1, min(99, mkt_price_cents))/100
    b = (1-price)/price
    if b <= 0: return {"f":0,"bet_size":0,"edge":-1,"p":p,"price":price}
    f = min(max(0,(b*p-(1-p))/b), KELLY_CAP)
    bet = round(min(f*bankroll, bankroll*0.90), 2)
    edge = p-(1-p)/b
    return {"f":f,"bet_size":bet,"edge":edge,"p":p,"price":price}

def run_prediction(market):
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prompt = f"""You are a conservative prediction market analyst. Only trade when you have GENUINE edge.

Market: "{market['title']}"
YES price: {market['yes_bid']}¢ (market says {market['yes_bid']}% likely)
NO price: {market['no_bid']}¢ (market says {market['no_bid']}% likely)  
Closes: {market.get('close_time','?')}
Now: {now_utc}

Rules:
- Your probability estimate must differ from market by 15+ percentage points to have edge
- If market seems fairly priced, set no_edge: true
- Be honest and conservative — prediction markets are efficient
- Never invent fake confidence

Reply ONLY with JSON:
{{"side":"yes" or "no","my_prob":<0-100>,"confidence":<50-95>,"no_edge":<true/false>,"reasoning":"<2 sentences>","sources":["Reuters","Bloomberg","AP","WSJ","FT","NYT","CoinDesk","Politico","Unknown"]}}"""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=300,
                                     messages=[{"role":"user","content":prompt}])
        pred = json.loads(msg.content[0].text.strip().replace("```json","").replace("```","").strip())
        if pred.get("no_edge"): return None
        sources = pred.get("sources",["Unknown"])
        src = sum(SOURCE_CREDIBILITY.get(s,0.5) for s in sources)/max(len(sources),1)
        pred["blended_conf"] = round(pred["confidence"]*0.7 + pred["confidence"]*src*0.3)
        side = pred.get("side","yes")
        mkt  = market["yes_bid"] if side=="yes" else market["no_bid"]
        pred["prob_edge"] = pred.get("my_prob",50) - mkt
        return pred
    except Exception as e:
        log.error(f"Prediction error {market['id']}: {e}"); return None

def run_cycle(markets):
    global bankroll, peak_bankroll, wins, losses, total_trades

    if not DEMO_MODE and KALSHI_API_KEY:
        bal = get_balance()
        if bal is not None and bal > 0:
            bankroll = bal
            peak_bankroll = max(peak_bankroll, bankroll)

    if bankroll < MIN_BALANCE and not DEMO_MODE:
        log.warning(f"Balance ${bankroll:.2f} below min ${MIN_BALANCE:.2f} — paused")
        return

    if not DEMO_MODE and KALSHI_API_KEY:
        cancel_all_resting()

    log.info("="*60)
    log.info(f"Cycle | ${bankroll:.2f} | Trades:{total_trades} | W/L:{wins}/{losses}")

    best = None
    for m in markets:
        pred = run_prediction(m)
        if not pred: continue
        conf  = pred["blended_conf"]
        pedge = pred.get("prob_edge",0)
        side  = pred["side"]
        price = m["yes_bid"] if side=="yes" else m["no_bid"]
        if conf < CONFIDENCE_THRESH:
            log.info(f"  {m['id'][:20]:20s} | {side.upper()} | conf {conf}% — below {CONFIDENCE_THRESH:.0f}%")
            continue
        if abs(pedge) < MIN_EDGE_CENTS:
            log.info(f"  {m['id'][:20]:20s} | {side.upper()} | edge {pedge:+.0f}¢ — below {MIN_EDGE_CENTS}¢")
            continue
        k = kelly_bet(pred["my_prob"], price)
        if k["edge"] <= 0 or k["bet_size"] < 0.50:
            log.info(f"  {m['id'][:20]:20s} | Kelly edge negative — skip")
            continue
        log.info(f"  {m['id'][:20]:20s} | {side.upper()} {conf}% | edge {pedge:+.0f}¢ | f*={k['f']*100:.1f}% → ${k['bet_size']:.2f} ✓")
        if best is None or k["edge"] > best["kelly"]["edge"]:
            best = {"market":m,"pred":pred,"kelly":k,"side":side,"price":price}
        time.sleep(0.4)

    if not best:
        log.info(f"No qualifying trades — holding. (need {CONFIDENCE_THRESH:.0f}%+ conf & {MIN_EDGE_CENTS}¢+ edge)")
        return

    m,k,side,price,pred = best["market"],best["kelly"],best["side"],best["price"],best["pred"]
    bet = k["bet_size"]
    contracts = max(1, int(bet/(price/100)))
    log.info(f"BEST: {m['title'][:55]}")
    log.info(f"  {side.upper()} @ {price}¢ | ${bet:.2f} | {contracts}ct | AI:{pred['my_prob']}% vs mkt:{price}¢")
    log.info(f"  {pred['reasoning']}")

    ok = place_order(m["id"], side, price, contracts)
    total_trades += 1
    if ok: wins += 1
    else: losses += 1

    wr = wins/total_trades*100 if total_trades else 0
    dd = (peak_bankroll-bankroll)/peak_bankroll*100 if peak_bankroll>0 else 0
    log.info(f"  Peak:${peak_bankroll:.2f} | Drawdown:{dd:.1f}% | Fill rate:{wr:.0f}%")

def main():
    global bankroll
    log.info("="*60)
    log.info("Kalshi Bot v5 — Conservative")
    log.info(f"  Mode: {'DEMO' if DEMO_MODE else 'LIVE'} | Conf:{CONFIDENCE_THRESH:.0f}% | Edge:{MIN_EDGE_CENTS}¢ | Kelly:{KELLY_CAP*100:.0f}% | Max:{MAX_CONTRACTS}ct | Stop:${MIN_BALANCE}")
    log.info("="*60)
    if not ANTHROPIC_API_KEY: log.critical("No ANTHROPIC_API_KEY"); raise SystemExit(1)
    if not DEMO_MODE and not KALSHI_API_KEY: log.critical("No KALSHI_API_KEY"); raise SystemExit(1)
    if not DEMO_MODE and KALSHI_API_KEY:
        bal = get_balance()
        if bal: bankroll = bal; log.info(f"Live balance: ${bankroll:.2f}")
    cycle = 0
    while True:
        cycle += 1
        log.info(f"\n{'─'*60}\nCYCLE {cycle} | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n{'─'*60}")
        markets = get_markets()
        try: run_cycle(markets)
        except SystemExit: raise
        except Exception as e: log.error(f"Cycle error: {e}", exc_info=True)
        log.info(f"Sleeping {CYCLE_INTERVAL}s...\n")
        time.sleep(CYCLE_INTERVAL)

if __name__ == "__main__":
    main()
