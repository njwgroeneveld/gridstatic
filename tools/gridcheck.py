"""Check a running gridstatic grid against the rules the design depends on.

Read-only: it calls the GET routes of the dal and the connector through
`kubectl exec` and changes nothing.

    python3 tools/gridcheck.py SOL
    python3 tools/gridcheck.py BTC --namespace gridstatic
"""
import argparse
import collections
import datetime as dt
import json
import re
import subprocess

OK, BAD, INFO = "  ok  ", "  !!  ", "      "
problems: list[str] = []
namespace = "gridstatic"
release = "gridstatic"


def kx(deploy: str, path: str, container: str | None = None):
    cmd = ["kubectl", "-n", namespace, "exec", f"deploy/{release}-{deploy}"]
    if container:
        cmd += ["-c", container]
    cmd += ["--", "curl", "-s", f"http://localhost:8080{path}"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=90).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise SystemExit(f"no JSON from {deploy}{path}: {out[:200]}")


def dal(path: str):
    return kx("dal", path, "dal")


def connector(path: str):
    return kx("connector", path)


def to_ms(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def bad(text: str) -> None:
    problems.append(text)
    print(BAD + text)


def good(text: str) -> None:
    print(OK + text)


def header(text: str) -> None:
    print(f"\n== {text}")


def check(coin: str) -> None:
    # ── pods ────────────────────────────────────────────────────────────────
    header("pods")
    pods = json.loads(subprocess.run(["kubectl", "-n", namespace, "get", "pods", "-o", "json"],
                                     capture_output=True, text=True).stdout)
    for pod in pods["items"]:
        status = (pod["status"].get("containerStatuses") or [{}])[0]
        name, restarts = pod["metadata"]["name"], status.get("restartCount", 0)
        line = f"{name}  restarts={restarts}  since {pod['status'].get('startTime')}"
        (bad if restarts else good)(line)

    # ── config ──────────────────────────────────────────────────────────────
    header(f"config {coin}")
    configs = dal(f"/grid-configs?coin={coin}")
    live = [c for c in configs if not c["shadow"]]
    for c in configs:
        print(INFO + f"id={c['id']} shadow={c['shadow']} {c['lower']}-{c['upper']} "
                     f"lines={c['num_lines']} leverage={c['leverage']} since {c['created_at']}")
    if len(live) != 1:
        bad(f"expected exactly 1 live config for {coin}, found {len(live)} "
            "(more than 1 means a restart did not recognise its grid)")
        if not live:
            return
    cfg = live[0]
    config_id, leverage = cfg["id"], float(cfg["leverage"])
    start_ms = to_ms(cfg["created_at"]) - 60_000

    # ── bookkeeping ─────────────────────────────────────────────────────────
    header("bookkeeping")
    orders = [o for o in dal(f"/grid-orders?coin={coin}") if o["grid_config_id"] == config_id]
    trades = dal(f"/grid-trades?coin={coin}&grid_config_id={config_id}")
    counts = collections.Counter((o["status"], o["side"]) for o in orders)
    print(INFO + "orders: " + ", ".join(f"{s} {side}={n}" for (s, side), n in sorted(counts.items())))
    open_trades = [t for t in trades if t["status"] == "OPEN"]
    closed_trades = [t for t in trades if t["status"] == "CLOSED"]
    print(INFO + f"trades: open={len(open_trades)} closed={len(closed_trades)}")

    open_buy_levels = collections.Counter(o["level"] for o in orders
                                          if o["status"] == "OPEN" and o["side"] == "BUY")
    doubled = {lv: n for lv, n in open_buy_levels.items() if n > 1}
    bad(f"more than 1 resting BUY on line {doubled}") if doubled else good("at most 1 resting BUY per line")

    trade_levels = collections.Counter(t["buy_level"] for t in open_trades)
    stacked = {lv: n for lv, n in trade_levels.items() if n > 1}
    (bad(f"more than 1 open position on line {stacked} (the stacking pattern)") if stacked
     else good("at most 1 open position per line"))

    both = set(trade_levels) & set(open_buy_levels)
    (bad(f"line with both an open position and a resting BUY: {sorted(both)}") if both
     else good("no line holds a position and a resting buy at the same time"))

    without_level = [t["id"] for t in trades if t.get("buy_level") is None]
    if without_level:
        bad(f"trades without buy_level: {without_level[:10]}")

    # ── exchange vs bookkeeping ─────────────────────────────────────────────
    header("exchange vs bookkeeping")
    exchange_oids = {str(o["oid"]) for o in connector(f"/orders/{coin}")}
    booked_oids = {str(o["exchange_order_id"]) for o in orders
                   if o["status"] == "OPEN" and o["exchange_order_id"]}
    print(INFO + f"open orders: exchange={len(exchange_oids)} database={len(booked_oids)}")
    if exchange_oids - booked_oids:
        bad(f"on the exchange but not in the database (orphans): {sorted(exchange_oids - booked_oids)}")
    if booked_oids - exchange_oids:
        bad(f"OPEN in the database but not on the exchange: {sorted(booked_oids - exchange_oids)}")
    if exchange_oids == booked_oids:
        good("open orders are identical")

    position = float(connector("/positions").get(coin, {}).get("szi", 0) or 0)
    booked = sum(float(t["size_usd"]) * leverage / float(t["buy_price"]) for t in open_trades)
    drift = abs(position - booked) / max(abs(position), abs(booked), 1e-9)
    print(INFO + f"position: exchange={position:.4f} {coin}  database={booked:.4f} {coin}")
    bad(f"position differs by {drift:.1%}") if drift > 0.02 else good("position matches (within 2%)")

    # ── fills ───────────────────────────────────────────────────────────────
    header("fills since the grid started")
    fills = connector(f"/fills/{coin}?since_ms={start_ms}")
    by_oid = collections.defaultdict(list)
    for f in fills:
        by_oid[str(f["oid"])].append(f)
    taker = [f for f in fills if f.get("crossed")]
    fees = sum(float(f.get("fee", 0)) for f in fills)
    exchange_pnl = sum(float(f.get("closedPnl", 0)) for f in fills)
    print(INFO + f"fills={len(fills)} (orders={len(by_oid)})  buys={sum(f['side'] == 'B' for f in fills)} "
                 f"sells={sum(f['side'] == 'A' for f in fills)}")
    print(INFO + f"fees=${fees:.4f}  closedPnl according to the exchange=${exchange_pnl:.4f}")
    if taker:
        bad(f"{len(taker)} taker fills -- the design assumes resting maker orders "
            f"(oids {sorted({str(f['oid']) for f in taker})[:8]})")
    else:
        good("every fill was a maker fill")

    filled_oids = {str(o["exchange_order_id"]) for o in orders
                   if o["status"] == "FILLED" and o["exchange_order_id"]}
    missed = set(by_oid) - filled_oids - booked_oids
    bad(f"fills on the exchange the bot never processed: {sorted(missed)}") if missed \
        else good("every exchange fill was processed")
    unbacked = filled_oids - set(by_oid)
    bad(f"FILLED in the database without a fill on the exchange: {sorted(unbacked)}") if unbacked \
        else good("every FILLED order has a real fill behind it")

    # ── result ──────────────────────────────────────────────────────────────
    header("result")
    profit = sum(float(t["profit_usd"] or 0) for t in closed_trades)
    booked_fees = sum(float(t["fee_usd"] or 0) for t in trades)
    funding = dal(f"/grid-funding/total?coin={coin}&grid_config_id={config_id}")["total_usdc"]
    print(INFO + f"closed trades={len(closed_trades)}  profit after fees=${profit:.4f}")
    print(INFO + f"fees booked=${booked_fees:.4f} (exchange: ${fees:.4f})  funding=${float(funding):.4f}")
    if abs(booked_fees - fees) > 0.01:
        bad(f"booked fees differ from the exchange by ${abs(booked_fees - fees):.4f}")

    # ── logs ────────────────────────────────────────────────────────────────
    header("logs of the current pod")
    log = subprocess.run(["kubectl", "-n", namespace, "logs", f"deploy/{release}-grid-static", "--since=240h"],
                         capture_output=True, text=True).stdout.splitlines()
    errors = collections.Counter()
    for line in log:
        if re.search(r"ERROR|WARNING|Traceback", line):
            errors[re.sub(r"^\S+ \S+ ", "", line)[:140]] += 1
    if errors:
        for text, n in errors.most_common(10):
            bad(f"{n}x {text}")
    else:
        good("no ERROR or WARNING")

    refills = collections.defaultdict(list)
    for line in log:
        m = re.match(rf"(\S+ \S+) .*\[{coin}-\d+\] Backfilling missing BUY at level (\d+)", line)
        if m:
            refills[int(m.group(2))].append(dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    rapid = {lv: len(ts) for lv, ts in refills.items()
             if any((b - a).total_seconds() < 150 for a, b in zip(ts, ts[1:]))}
    print(INFO + f"refills per line: {dict(sorted((lv, len(ts)) for lv, ts in refills.items()))}")
    (bad(f"same line refilled again within 150s: {rapid} (the stacking pattern)") if rapid
     else good("no line was refilled in two consecutive health cycles"))

    header("verdict")
    print(OK + "nothing found that departs from the design" if not problems
          else BAD + f"{len(problems)} item(s) to look at, see above")


def main() -> None:
    global namespace, release
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("coin", nargs="?", default="BTC", help="Hyperliquid symbol, e.g. SOL")
    parser.add_argument("--namespace", default="gridstatic")
    parser.add_argument("--release", default="gridstatic")
    args = parser.parse_args()
    namespace, release = args.namespace, args.release
    check(args.coin.upper())


if __name__ == "__main__":
    main()
