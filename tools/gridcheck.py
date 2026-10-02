"""Check a gridstatic grid against the exchange, read-only.

Reads the grid settings from the cluster (or a file) and the account straight
from Hyperliquid's public info API -- no key, no connector, nothing changed. For
every grid it checks the rules the design depends on and prints what the bot
would do in its next round.

    python3 tools/gridcheck.py --address 0xYOUR_ACCOUNT
    python3 tools/gridcheck.py --address 0x... --settings settings.yaml --network mainnet

--address is the account the bot trades on: the subaccount if you set
wallet_address, otherwise the key's own address. Needs PyYAML.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "grid-static"))
from src.cloid import BUY, SELL, decode, fingerprint  # noqa: E402
from src.grid_math import calculate_levels, calculate_size_usd  # noqa: E402
from src.reconcile import TaggedFill, plan  # noqa: E402

INFO_URL = {"testnet": "https://api.hyperliquid-testnet.xyz/info",
            "mainnet": "https://api.hyperliquid.xyz/info"}
OK, BAD, INFO = "  ok  ", "  !!  ", "      "
problems: list[str] = []


def bad(text: str) -> None:
    problems.append(text)
    print(BAD + text)


def good(text: str) -> None:
    print(OK + text)


def info(url: str, payload: dict):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def kubectl_json(namespace: str, *args: str):
    out = subprocess.run(["kubectl", "-n", namespace, *args, "-o", "json"],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def load_settings(args) -> dict:
    if args.settings:
        return yaml.safe_load(Path(args.settings).read_text(encoding="utf-8"))
    cm = kubectl_json(args.namespace, "get", "configmap", f"{args.release}-settings")
    return yaml.safe_load(cm["data"]["settings.yaml"])


def network_from_cluster(args) -> str:
    deploy = kubectl_json(args.namespace, "get", "deploy", f"{args.release}-connector")
    for env in deploy["spec"]["template"]["spec"]["containers"][0].get("env", []):
        if env["name"] == "HYPERLIQUID_TESTNET":
            return "mainnet" if str(env.get("value")).lower() == "false" else "testnet"
    return "testnet"


def check_grid(key: str, c: dict, cfg: dict, url: str, address: str, lookback_h: float) -> None:
    print(f"\n== {key}: {c['coin']} {c['lower']}-{c['upper']}, {c['num_lines']} lines, "
          f"{c.get('leverage', 1)}x")
    coin = c["coin"]
    leverage = float(c.get("leverage", 1))
    levels = calculate_levels(c["lower"], c["upper"], c["num_lines"])
    fp = fingerprint(coin, c["lower"], c["upper"], c["num_lines"], leverage)
    balance = c.get("start_balance", cfg.get("start_balance"))
    size_usd = calculate_size_usd(balance, cfg.get("strategy_allocation_pct", 100),
                                  c["allocation_pct"], c["num_lines"])

    dex = coin.split(":", 1)[0] if ":" in coin else ""

    def full(name: str) -> str:
        return f"{dex}:{name}" if dex and ":" not in name else name

    meta, ctxs = info(url, {"type": "metaAndAssetCtxs", "dex": dex})
    i = next(n for n, a in enumerate(meta["universe"]) if a["name"] == coin)
    sz_decimals = meta["universe"][i]["szDecimals"]
    price = float(ctxs[i]["markPx"])          # what the bot decides from, not the mid
    mid = ctxs[i].get("midPx")
    orders = [o for o in info(url, {"type": "frontendOpenOrders", "user": address, "dex": dex})
              if full(o.get("coin", "")) == coin]
    state = info(url, {"type": "clearinghouseState", "user": address, "dex": dex})
    position = next((float(p["position"]["szi"]) for p in state.get("assetPositions", [])
                     if full(p["position"]["coin"]) == coin), 0.0)
    since = int((time.time() - lookback_h * 3600) * 1000)
    fills = [f for f in info(url, {"type": "userFillsByTime", "user": address,
                                   "startTime": since})
             if f.get("coin") == coin]
    print(INFO + f"mark {price:g} (mid {mid}) | position {position:g} | {len(orders)} open "
                 f"orders | {len(fills)} fills in {lookback_h:g}h"
                 + (f" | dex '{dex}' balance {float(state['marginSummary']['accountValue']):g}"
                    if dex else ""))

    # ── invariants, checked on the raw data rather than through reconcile ──
    ours, earlier, foreign = [], [], []
    for o in orders:
        tag = decode(o.get("cloid"))
        (foreign if tag is None else ours if tag.fingerprint == fp else earlier).append((o, tag))
    if earlier:
        bad(f"{len(earlier)} order(s) from an earlier grid on {coin} -- the bot holds until "
            f"they are cancelled")
    else:
        good("no orders from an earlier grid")
    if foreign:
        print(INFO + f"{len(foreign)} order(s) on {coin} not placed by gridstatic")

    per_cell: dict[tuple[int, str], int] = {}
    for o, tag in ours:
        per_cell[(tag.cell, tag.side)] = per_cell.get((tag.cell, tag.side), 0) + 1
        if tag.side == SELL and not o.get("reduceOnly", False):
            bad(f"sell on cell {tag.cell} is not reduce-only")
        if tag.side == BUY and float(o["limitPx"]) >= price:
            bad(f"buy on cell {tag.cell} at {o['limitPx']} is at or above the mark price")
    doubles = {k: n for k, n in per_cell.items() if n > 1}
    if doubles:
        for (cell, side), n in sorted(doubles.items()):
            bad(f"{n} {side} orders on cell {cell}")
    else:
        good("at most one buy and one sell per cell")
    if position < 0:
        bad(f"short position {position:g} -- a grid only ever holds long")

    # ── what the bot would do now ──
    cloid_of = {int(o["oid"]): o.get("cloid") for o in orders}
    tied = []
    for f in fills:
        oid = int(f["oid"])
        if oid not in cloid_of:
            st = info(url, {"type": "orderStatus", "user": address, "oid": oid})
            cloid_of[oid] = (st.get("order") or {}).get("order", {}).get("cloid")
        tag = decode(cloid_of[oid])
        if tag is not None and tag.fingerprint == fp:
            tied.append(TaggedFill(tag.cell, tag.side, float(f["sz"]), int(f["time"])))

    p = plan(levels=levels, fp=fp, orders=orders, fills=tied, position=position, price=price,
             size_usd=size_usd, leverage=leverage, sz_decimals=sz_decimals, min_notional=10.0)
    if p.hold:
        bad(f"on hold: {p.hold}")
    else:
        good(f"position adds up (residual {p.residual:+.{sz_decimals}f})")
    for n in p.alerts:
        bad(n)
    if not p.cancels and not p.places:
        good("grid complete -- the next round has nothing to do")
    for x in p.cancels:
        print(INFO + f"next round cancels {x.side} on cell {x.cell}: {x.reason}")
    for x in p.places:
        print(INFO + f"next round places {x.side} {x.sz:g} at {x.price:g} (cell {x.cell})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--address", required=True,
                        help="the account the bot trades on (0x...)")
    parser.add_argument("--settings", help="settings.yaml to read instead of the cluster's")
    parser.add_argument("--network", choices=["testnet", "mainnet"],
                        help="default: read from the cluster, else testnet")
    parser.add_argument("--namespace", default="gridstatic")
    parser.add_argument("--release", default="gridstatic")
    args = parser.parse_args()

    cfg = load_settings(args)
    network = args.network or (network_from_cluster(args) if not args.settings else "testnet")
    url = INFO_URL[network]
    print(f"gridcheck on {network} for {args.address}")
    lookback = cfg.get("fill_lookback_hours", 72)
    for key, c in (cfg.get("coins") or {}).items():
        if c.get("active"):
            check_grid(key, c, cfg, url, args.address, lookback)

    print()
    if problems:
        print(f"{len(problems)} problem(s) found")
        sys.exit(1)
    print("no problems found")


if __name__ == "__main__":
    main()
