# Paper-trading SNIPER simulator (NO real money) - brand-new DEX pools
# $5 per trade, GMGN-like costs, two stop-loss variants side by side (sl20 vs sl35)
# Data: GeckoTerminal (new pools, prices) + GoPlus (honeypot check). One run per execution.
import requests, time, json, os, csv
from datetime import datetime, timezone

GT = "https://api.geckoterminal.com/api/v2"
GP = "https://api.gopluslabs.io/api/v1"
STATE = "sniper10_state.json"
LOG = "sniper10_trades.csv"

# stop loss % (price move) before the target is reached, per variant
VARIANTS = {"sl20": -20.0, "sl35": -35.0}

CFG = dict(
    start_balance=10.0,    # fake dollars per variant
    size=5.0,              # fake dollars per trade
    max_open=2,            # max open trades per variant
    min_age_min=3,         # pool must be at least this old (minutes) to show sells
    max_age_min=15,        # ...and at most this old
    min_liq=6000,          # min pool liquidity (USD)
    min_buys=10,           # activity in the pool's first 15 minutes
    min_sells=3,
    min_sellers=3,         # proof that people could actually sell
    max_tax=0.10,          # max buy/sell tax (EVM)
    target_minutes=60,     # must reach the target within this time
    lock=100.0,            # target profit %: once reached, never sell below this
    trail=30.0,            # trailing stop % below the peak after the target
    max_hold_h=24,         # max hold after the target is reached
    fee_pct=1.0,           # GMGN flat fee per side
    slip_pct=1.0,          # slippage per side
    rug_liq_drop=0.2,      # liquidity below 20% of entry = rug, total loss
    solana_need_security=False,
)

# gas = fake cost per transaction in USD
NETS = {
    "solana": dict(gas=0.24, chain=None),   # ~0.002 SOL at ~$120 (Anti-MEV minimum)
    "base": dict(gas=0.03, chain=8453),
    "bsc": dict(gas=0.10, chain=56),
    "arbitrum": dict(gas=0.05, chain=42161),
}
SOL_FLAGS = ("mintable", "freezable", "closable", "non_transferable",
             "balance_mutable_authority", "default_account_state")


def fnum(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def gt_get(path, params=None):
    for attempt in range(2):
        try:
            r = requests.get(GT + path, params=params, timeout=25,
                             headers={"Accept": "application/json"})
            if r.status_code == 200:
                time.sleep(2.2)
                return r.json()
            print("GT HTTP", r.status_code, path[:60])
            if r.status_code == 429:
                time.sleep(30)
        except Exception as e:
            print("net error:", str(e)[:80])
            time.sleep(3)
    time.sleep(2.2)
    return None


def load():
    s = {"seen": {}, "v": {}}
    if os.path.exists(STATE):
        with open(STATE) as f:
            s = json.load(f)
    for name in VARIANTS:
        s["v"].setdefault(name, {"balance": CFG["start_balance"], "open": []})
    return s


def save(s):
    with open(STATE, "w") as f:
        json.dump(s, f)


def log_trade(row):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "variant", "network", "name", "entry", "exit",
                        "multiple", "pnl_usd", "reason", "minutes", "hit_target"])
        w.writerow(row)


def parse_pool(p, net):
    a = p.get("attributes") or {}
    rel = ((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}
    tid = rel.get("id", "")
    token = tid.split("_", 1)[1] if "_" in tid else ""
    try:
        created = datetime.fromisoformat(
            a.get("pool_created_at", "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return None
    tx15 = (a.get("transactions") or {}).get("m15") or {}
    return dict(net=net, pool=a.get("address") or "", token=token,
                name=a.get("name") or "?", created=created,
                price=fnum(a.get("base_token_price_usd")),
                liq=fnum(a.get("reserve_in_usd")), tx15=tx15)


def fetch_new(net):
    pools = []
    for page in (1, 2):
        data = gt_get("/networks/%s/new_pools" % net, {"page": page})
        for p in (data or {}).get("data", []):
            q = parse_pool(p, net)
            if q and q["pool"] and q["token"]:
                pools.append(q)
    return pools


def activity_ok(p):
    t = p["tx15"]
    return (t.get("buys", 0) >= CFG["min_buys"]
            and t.get("sells", 0) >= CFG["min_sells"]
            and t.get("sellers", 0) >= CFG["min_sellers"])


def goplus_evm(chain, tokens):
    try:
        r = requests.get("%s/token_security/%s" % (GP, chain), timeout=25,
                         params={"contract_addresses": ",".join(tokens)})
        if r.status_code == 200:
            res = (r.json().get("result") or {})
            return {k.lower(): v for k, v in res.items()}
        print("GoPlus HTTP", r.status_code)
    except Exception as e:
        print("goplus error:", str(e)[:80])
    return {}


def goplus_sol(tokens):
    try:
        r = requests.get("%s/solana/token_security" % GP, timeout=25,
                         params={"contract_addresses": ",".join(tokens)})
        if r.status_code == 200:
            return r.json().get("result") or {}
        print("GoPlus HTTP", r.status_code)
    except Exception as e:
        print("goplus error:", str(e)[:80])
    return {}


def evm_safe(info):
    if not info:
        return None
    if info.get("is_honeypot") == "1" or info.get("cannot_sell_all") == "1":
        return False
    if info.get("is_blacklisted") == "1" or info.get("transfer_pausable") == "1":
        return False
    return (fnum(info.get("sell_tax")) <= CFG["max_tax"]
            and fnum(info.get("buy_tax")) <= CFG["max_tax"])


def sol_safe(info):
    if not info:
        return None
    for k in SOL_FLAGS:
        v = info.get(k)
        if isinstance(v, dict) and str(v.get("status")) == "1":
            return False
    return True


def net_value(pos, price):
    side = (CFG["fee_pct"] + CFG["slip_pct"]) / 100
    gas = NETS[pos["net"]]["gas"]
    v = (CFG["size"] - gas) * (1 - side) * (price / pos["entry"]) * (1 - side) - gas
    return max(v, 0.0)


def close_pos(v, name, pos, price, reason, mins):
    value = 0.0 if reason == "RUG" else net_value(pos, price)
    v["balance"] += value
    log_trade([datetime.now().strftime("%m-%d %H:%M"), name, pos["net"],
               pos["name"], pos["entry"], price, round(price / pos["entry"], 2),
               round(value - CFG["size"], 3), reason, round(mins),
               "yes" if pos["armed"] else "no"])
    print("CLOSE %s %s %s x%.2f pnl $%.3f (%s)" % (
        name, pos["net"], pos["name"], price / pos["entry"],
        value - CFG["size"], reason))


def fetch_live(s):
    by_net = {}
    for v in s["v"].values():
        for pos in v["open"]:
            by_net.setdefault(pos["net"], set()).add(pos["pool"])
    live = {}
    for net, pools in by_net.items():
        addrs = sorted(pools)
        for i in range(0, len(addrs), 30):
            data = gt_get("/networks/%s/pools/multi/%s" % (net, ",".join(addrs[i:i + 30])))
            for p in (data or {}).get("data", []):
                a = p.get("attributes") or {}
                live[(net, (a.get("address") or "").lower())] = (
                    fnum(a.get("base_token_price_usd")), fnum(a.get("reserve_in_usd")))
    return live


def manage_open(v, name, live):
    sl = VARIANTS[name]
    still = []
    now = time.time()
    for pos in v["open"]:
        mins = (now - pos["t"]) / 60
        got = live.get((pos["net"], pos["pool"].lower()))
        if not got or got[0] <= 0:
            pos["miss"] = pos.get("miss", 0) + 1
            if pos["miss"] >= 3:
                close_pos(v, name, pos, 0.0, "RUG", mins)
            else:
                still.append(pos)
            continue
        pos["miss"] = 0
        price, liq = got
        if liq < pos["liq"] * CFG["rug_liq_drop"]:
            close_pos(v, name, pos, price, "RUG", mins)
            continue
        pos["peak"] = max(pos["peak"], price)
        if not pos["armed"] and price >= pos["entry"] * (1 + CFG["lock"] / 100):
            pos["armed"] = True
        reason = None
        if pos["armed"]:
            stop = max(pos["entry"] * (1 + CFG["lock"] / 100),
                       pos["peak"] * (1 - CFG["trail"] / 100))
            if price <= stop:
                reason = "TRAIL"
            elif mins >= CFG["max_hold_h"] * 60:
                reason = "MAX_HOLD"
        elif price <= pos["entry"] * (1 + sl / 100):
            reason = "SL"
        elif mins >= CFG["target_minutes"]:
            reason = "TIME"
        if reason:
            close_pos(v, name, pos, price, reason, mins)
        else:
            still.append(pos)
    v["open"] = still


def has_room(v):
    return len(v["open"]) < CFG["max_open"] and v["balance"] >= CFG["size"]


def find_entries(s):
    now = time.time()
    s["seen"] = {k: t for k, t in s["seen"].items() if now - t < 86400}
    for net, meta in NETS.items():
        if not any(has_room(v) for v in s["v"].values()):
            return
        cands = []
        for p in fetch_new(net):
            age = (now - p["created"]) / 60
            if (CFG["min_age_min"] <= age <= CFG["max_age_min"] and p["price"] > 0
                    and p["liq"] >= CFG["min_liq"] and activity_ok(p)
                    and p["pool"] not in s["seen"]):
                cands.append(p)
        if not cands:
            continue
        toks = [p["token"] for p in cands]
        info = goplus_sol(toks) if net == "solana" else goplus_evm(meta["chain"], toks)
        for p in cands:
            if net == "solana":
                ok = sol_safe(info.get(p["token"]))
                if ok is None and not CFG["solana_need_security"]:
                    ok = True
            else:
                ok = evm_safe(info.get(p["token"].lower()))
            if ok is None:
                continue  # no security data yet: try again next run
            s["seen"][p["pool"]] = now
            if not ok:
                print("SKIP  %s %s (unsafe)" % (net, p["name"]))
                continue
            for name, v in s["v"].items():
                if not has_room(v) or any(o["pool"] == p["pool"] for o in v["open"]):
                    continue
                v["balance"] -= CFG["size"]
                v["open"].append(dict(net=net, pool=p["pool"], token=p["token"],
                                      name=p["name"], entry=p["price"], t=now,
                                      liq=p["liq"], peak=p["price"], armed=False, miss=0))
                print("OPEN  %s %s %s @ %s (liq $%.0f)" % (
                    name, net, p["name"], p["price"], p["liq"]))


def stats(name):
    if not os.path.exists(LOG):
        return "no closed trades yet"
    rows = [r for r in list(csv.reader(open(LOG)))[1:] if r[1] == name]
    n = len(rows)
    if n == 0:
        return "no closed trades yet"
    wins = sum(1 for r in rows if float(r[7]) > 0)
    hits = sum(1 for r in rows if r[10] == "yes")
    rugs = sum(1 for r in rows if r[8] == "RUG")
    total = sum(float(r[7]) for r in rows)
    return "trades=%d wins=%d hit100=%d rugs=%d P/L=$%.2f" % (n, wins, hits, rugs, total)


def main():
    s = load()
    live = fetch_live(s)
    for name, v in s["v"].items():
        manage_open(v, name, live)
    find_entries(s)
    save(s)
    for name, v in s["v"].items():
        print("%s: cash=$%.2f open=%d | %s" % (name, v["balance"], len(v["open"]), stats(name)))


if __name__ == "__main__":
    main()
