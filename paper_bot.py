"""rash-signal paper-bot — REALISTISK simulering, ingen rigtige penge.

Kører kontinuerligt: real-time WSS activity-feed → filter på Rash's
proxyWallet → HARD LIMITS check → LIVE orderbook-verifikation → simulér
fill kun hvis best_ask ≤ vores limit OG depth stor nok.

Signal-latency ~1-2s (WSS activity feed) vs. ~8-16s (REST /trades polling).
Baseret på Polymarket real-time-data-client (wss://ws-live-data.polymarket.com,
topic activity/orders_matched, målt indekser-latency 865-910ms + netværk).

Sizing: half-Kelly per bucket, kalibreret på 30d empirisk winrate (2026-09-15).
    Bucket 0.6: p=0.692 @ price 0.622 → ½-Kelly = 9.3% af bankroll
    Bucket 0.7: p=0.783 @ price 0.716 → ½-Kelly = 11.8% af bankroll
    Bucket 0.4: DEAKTIVERET — Kelly = -3.7% (WR 41% < brief's worst-case 46%)

Kør: python paper_bot.py           # main event-loop (WSS + periodic D/exits)
     python paper_bot.py --status  # print state summary
     python paper_bot.py --tick N S # REST-only tick mode for GH Actions cron
"""
import json, os, sys, time, subprocess, asyncio
from pathlib import Path

# ============ HARD LIMITS (fra BRIEF.md §0, revideret 2026-09-15) ============
STRAT_A_BUCKETS = {0.6: 0.622, 0.7: 0.716}
STRAT_D_BUCKETS = {0.1, 0.2, 0.3, 0.4, 0.5, 0.6}
P90_USDC_THRESHOLD = 1615

# Half-Kelly per bucket. Empirisk kalibreret 2026-09-15 på 30d BUY-signaler.
KELLY_HALF = {
    0.6: 0.093,
    0.7: 0.118,
}
POSITION_MIN_USDC = 50
POSITION_MAX_USDC_CAP = 2000
D_POSITION_USDC = 100

MAX_OPEN_POSITIONS = 30
STARTING_CASH = 10000
POLL_INTERVAL_S = 15
STATE_FILE = Path(__file__).parent / "paper_state.json"

# Realistiske frictions (CLAUDE.md §trading: min 3-4 ticks/side)
SLIPPAGE = 0.004

# Strategy D exit-regler (fra live-data analyse 2026-09-13)
D_COOLDOWN_S = 86400
D_TP = 0.05
D_SL = -0.08
D_MAX_HOLD_S = 3600

# Strategy D deaktiveret 2026-09-25: 21% wr / -$193 over 21 trades (hele historik),
# 20% wr / -$141 sidste 7 dage → æder A-edgen før 21/10-beslutningen.
# Åbne D-positioner exit'er stadig via process_exits; kun nye entries blokeres.
# Sæt ENABLE_STRATEGY_D=1 for at genaktivere.
ENABLE_STRATEGY_D = os.environ.get("ENABLE_STRATEGY_D", "0") == "1"

RASH = "0x29b52d98ac9ef9414b04164246c95bc63d74cc6c"
WSS_URL = "wss://ws-live-data.polymarket.com"
WSS_PING_INTERVAL_S = 5
WSS_RECONNECT_S = 5
SEEN_TX_CAP = 2000

# ============ HTTP HELPERS ============
def curl(url, timeout=15):
    r = subprocess.run(["curl","-s","-A","Mozilla/5.0","--max-time",str(timeout),url],
                       capture_output=True, text=True, timeout=timeout+5).stdout
    try: return json.loads(r)
    except: return None

def fetch_rash_trades(since_ts, limit=100):
    """Latest Rash trades in descending time order (REST fallback)."""
    d = curl(f"https://data-api.polymarket.com/trades?user={RASH}&limit={limit}")
    if not isinstance(d, list): return []
    return [t for t in d if isinstance(t, dict) and t.get('timestamp', 0) > since_ts]

def fetch_orderbook(asset_id):
    d = curl(f"https://clob.polymarket.com/book?token_id={asset_id}", timeout=10)
    if not isinstance(d, dict): return None
    return d

def best_ask(ob):
    if not ob or not ob.get('asks'): return None
    return min((float(a['price']), float(a['size'])) for a in ob['asks'])

def best_bid(ob):
    if not ob or not ob.get('bids'): return None
    return max((float(b['price']), float(b['size'])) for b in ob['bids'])

def fetch_market_meta(condition_id):
    """Get closed status + outcomePrices via gamma."""
    for closed in ("true", "false"):
        d = curl(f"https://gamma-api.polymarket.com/markets?condition_ids={condition_id}&closed={closed}&limit=5")
        if isinstance(d, list):
            for m in d:
                if m.get('conditionId') == condition_id:
                    return m
    return None

# ============ FEE MATH ============
def fee_shares(shares, price):
    return shares * 0.05 * price * (1 - price)

# ============ SIZING ============
def calc_stake(state, bucket):
    """Half-Kelly stake i USDC, bundet af min/max caps."""
    kf = KELLY_HALF.get(bucket, 0.0)
    if kf <= 0:
        return 0.0
    bankroll = state['cash'] + sum(p['fill_usdc'] for p in state['open_positions'])
    raw = bankroll * kf
    return max(POSITION_MIN_USDC, min(POSITION_MAX_USDC_CAP, raw))

# ============ STATE ============
def load_state():
    if not STATE_FILE.exists():
        return {
            'started_at': time.time(),
            'last_rash_trade_ts': int(time.time()) - 3600,
            'cash': STARTING_CASH,
            'open_positions': [],
            'closed_positions': [],
            'decisions': [],
            'iterations': 0,
            'cum_pnl': 0.0,
            'd_cooldowns': {},
            'seen_tx_hashes': [],
        }
    s = json.load(open(STATE_FILE))
    s.setdefault('d_cooldowns', {})
    s.setdefault('seen_tx_hashes', [])
    return s

def save_state(s):
    if len(s['decisions']) > 500:
        s['decisions'] = s['decisions'][-500:]
    if len(s.get('seen_tx_hashes', [])) > SEEN_TX_CAP:
        s['seen_tx_hashes'] = s['seen_tx_hashes'][-SEEN_TX_CAP:]
    # Safeguard: dedup closed_positions på id (behold første). Historik-bug
    # 2026-09-08 lukkede samme D-position 20 gange og oppustede cash med +$1144.
    seen = set()
    deduped = []
    for c in s['closed_positions']:
        if c['id'] in seen: continue
        seen.add(c['id']); deduped.append(c)
    if len(deduped) < len(s['closed_positions']):
        n = len(s['closed_positions']) - len(deduped)
        print(f"  [save] deduped {n} duplicate closed positions", flush=True)
    s['closed_positions'] = deduped
    # Safeguard: drop zombie open positions (id findes allerede i closed).
    closed_ids = seen
    zombies = [p for p in s['open_positions'] if p['id'] in closed_ids]
    if zombies:
        s['open_positions'] = [p for p in s['open_positions'] if p['id'] not in closed_ids]
        print(f"  [save] dropped {len(zombies)} zombie open positions: {[z['id'] for z in zombies]}", flush=True)
    tmp = str(STATE_FILE) + ".tmp"
    with open(tmp,"w") as f: json.dump(s, f, indent=2)
    os.replace(tmp, STATE_FILE)

def log_decision(s, kind, msg, **extra):
    entry = {'ts': int(time.time()), 'kind': kind, 'msg': msg}
    entry.update(extra)
    s['decisions'].append(entry)
    print(f"  [{kind}] {msg}", flush=True)

# ============ SIGNALS ============
def process_strategy_A(s, rash_trade):
    """Mirror Rash's big BUY-trade hvis HARD LIMITS + orderbook + Kelly-stake OK."""
    # Only mirror BUYs (Rash's SELLs er exits, ikke signal)
    if rash_trade.get('side') != 'BUY':
        return

    size_usdc = rash_trade['size'] * rash_trade['price']
    asset = rash_trade['asset']
    price = rash_trade['price']

    if size_usdc < P90_USDC_THRESHOLD:
        return

    if any(p['asset']==asset and p['strategy']=='A' for p in s['open_positions']):
        log_decision(s, 'SKIP_A', f"already have position on {asset[:10]}...")
        return

    if len(s['open_positions']) >= MAX_OPEN_POSITIONS:
        log_decision(s, 'SKIP_A', f"max open positions reached ({MAX_OPEN_POSITIONS})")
        return

    bucket = round(price * 10) / 10
    if bucket not in STRAT_A_BUCKETS:
        log_decision(s, 'SKIP_A', f"bucket {bucket:.2f} not in {list(STRAT_A_BUCKETS)} (Rash entry ${size_usdc:.0f} @ {price:.3f})")
        return
    max_limit = STRAT_A_BUCKETS[bucket]

    ob = fetch_orderbook(asset)
    ask = best_ask(ob)
    if not ask:
        log_decision(s, 'SKIP_A', f"no ask book for {asset[:10]}...")
        return
    ask_price, ask_depth = ask
    effective_ask = ask_price + SLIPPAGE

    if effective_ask > max_limit:
        log_decision(s, 'SKIP_A', f"effective_ask {effective_ask:.3f} (best {ask_price:.3f} + slip) > limit {max_limit:.3f} (bucket {bucket})")
        return

    # Half-Kelly stake, bundet af cap OG depth OG cash
    target_usdc = calc_stake(s, bucket)
    max_usdc_fillable = ask_depth * effective_ask
    fill_usdc = min(target_usdc, max_usdc_fillable, s['cash'])
    if fill_usdc < POSITION_MIN_USDC:
        log_decision(s, 'SKIP_A', f"cannot meet min stake ${POSITION_MIN_USDC}: kelly={target_usdc:.0f} depth={max_usdc_fillable:.0f} cash={s['cash']:.0f}")
        return

    shares = fill_usdc / effective_ask
    fee = fee_shares(shares, effective_ask)
    net_shares = shares - fee
    pos = {
        'id': f"A_{int(time.time())}_{asset[:10]}",
        'strategy': 'A',
        'asset': asset,
        'condition': rash_trade.get('conditionId'),
        'outcome': rash_trade.get('outcome'),
        'title': rash_trade.get('title','')[:100],
        'entry_price': effective_ask,
        'entry_time': int(time.time()),
        'fill_usdc': fill_usdc,
        'shares_gross': shares,
        'fee_shares': fee,
        'net_shares': net_shares,
        'rash_trade_price': price,
        'rash_trade_usdc': size_usdc,
        'rash_trade_ts': rash_trade['timestamp'],
        'kelly_target_usdc': target_usdc,
        'exit_type': 'resolution',
    }
    s['open_positions'].append(pos)
    s['cash'] -= fill_usdc
    log_decision(s, 'FILL_A', f"BUY ${fill_usdc:.0f} @ {effective_ask:.3f} (best {ask_price:.3f}+slip, kelly target ${target_usdc:.0f}) bucket {bucket} ({shares:.1f} shares) — {pos['title'][:60]}",
                 asset=asset[:20], entry=effective_ask, size=fill_usdc)

def scan_strategy_D(s, watched_assets):
    """For each watched asset, check for >20% crash in last 30min → buy."""
    if len(s['open_positions']) >= MAX_OPEN_POSITIONS:
        return
    cooldowns = s.setdefault('d_cooldowns', {})
    now_ts = int(time.time())
    for asset in list(watched_assets)[:50]:
        if any(p['asset']==asset and p['strategy']=='D' for p in s['open_positions']):
            continue
        last_exit = cooldowns.get(asset)
        if last_exit and now_ts - last_exit < D_COOLDOWN_S:
            continue
        now = now_ts
        url = f"https://clob.polymarket.com/prices-history?market={asset}&startTs={now-2700}&endTs={now}&fidelity=1"
        d = curl(url, timeout=10)
        if not isinstance(d, dict) or not d.get('history'):
            continue
        h = d['history']
        if len(h) < 5: continue
        now_p = h[-1]['p']
        target = now - 1800
        idx = min(range(len(h)), key=lambda i: abs(h[i]['t']-target))
        ago_p = h[idx]['p']
        if now_p >= ago_p * 0.80 or now_p <= 0.05:
            continue
        bucket = round(now_p * 10) / 10
        if bucket not in STRAT_D_BUCKETS:
            log_decision(s, 'SKIP_D', f"crash detected but bucket {bucket} out of range ({asset[:10]}...)")
            continue
        ob = fetch_orderbook(asset)
        ask = best_ask(ob)
        if not ask: continue
        ask_price, ask_depth = ask
        effective_ask = ask_price + SLIPPAGE
        max_limit = now_p + 0.005
        if effective_ask > max_limit:
            log_decision(s, 'SKIP_D', f"crash {ago_p:.3f}->{now_p:.3f} but effective_ask {effective_ask:.3f} > limit {max_limit:.3f}")
            continue
        max_fillable = ask_depth * effective_ask
        fill_usdc = min(D_POSITION_USDC, max_fillable)
        if fill_usdc < POSITION_MIN_USDC:
            log_decision(s, 'SKIP_D', f"depth thin on crash {asset[:10]}: ${max_fillable:.0f}")
            continue
        if s['cash'] < fill_usdc: continue
        shares = fill_usdc / effective_ask
        fee = fee_shares(shares, effective_ask)
        pos = {
            'id': f"D_{int(time.time())}_{asset[:10]}",
            'strategy': 'D',
            'asset': asset,
            'entry_price': effective_ask,
            'entry_time': int(time.time()),
            'fill_usdc': fill_usdc,
            'shares_gross': shares,
            'fee_shares': fee,
            'net_shares': shares - fee,
            'crash_from': ago_p,
            'crash_to': now_p,
            'exit_type': 'time',
            'exit_at': int(time.time()) + D_MAX_HOLD_S,
        }
        s['open_positions'].append(pos)
        s['cash'] -= fill_usdc
        log_decision(s, 'FILL_D', f"CRASH-BUY ${fill_usdc:.0f} @ {effective_ask:.3f} (best {ask_price:.3f}+slip, crashed {ago_p:.3f}->{now_p:.3f})",
                     asset=asset[:20])

# ============ EXITS ============
def process_exits(s):
    """Check each open position for exit condition."""
    now = int(time.time())
    still_open = []
    closed_ids = {c['id'] for c in s['closed_positions']}
    for pos in s['open_positions']:
        # Zombie-guard: position id findes allerede i closed → drop, exit ikke igen.
        if pos['id'] in closed_ids:
            log_decision(s, 'WARN', f"zombie position {pos['id']} already closed — dropping")
            continue
        if pos['exit_type'] == 'resolution':
            meta = fetch_market_meta(pos['condition'])
            if meta and meta.get('closed'):
                prices_out = meta.get('outcomePrices')
                tokens = meta.get('clobTokenIds')
                if isinstance(prices_out, str): prices_out = json.loads(prices_out)
                if isinstance(tokens, str): tokens = json.loads(tokens)
                if tokens and prices_out:
                    try:
                        i = tokens.index(pos['asset'])
                        resolution = float(prices_out[i])
                        payout = pos['net_shares'] * resolution
                        pnl = payout - pos['fill_usdc']
                        s['cash'] += payout
                        s['cum_pnl'] += pnl
                        closed = dict(pos)
                        closed['exit_time'] = now
                        closed['exit_price'] = resolution
                        closed['pnl'] = pnl
                        s['closed_positions'].append(closed)
                        log_decision(s, 'EXIT_A_RES', f"resolved {resolution:.0f} → pnl ${pnl:+.2f} ({pos['title'][:50]})",
                                     asset=pos['asset'][:20], pnl=pnl)
                        continue
                    except (ValueError, IndexError):
                        pass
            still_open.append(pos)
        elif pos['exit_type'] == 'time':
            ob = fetch_orderbook(pos['asset'])
            bid = best_bid(ob)
            if not bid:
                if now >= pos['exit_at']:
                    pos['exit_at'] = now + 600
                    log_decision(s, 'WAIT_D', f"no bid book, extending exit +10min ({pos['asset'][:10]})")
                still_open.append(pos)
                continue
            bid_price, _ = bid
            effective_bid = bid_price - SLIPPAGE
            entry = pos['entry_price']
            r_now = (effective_bid - entry) / entry
            tp_hit = r_now >= D_TP
            sl_hit = r_now <= D_SL
            time_hit = now >= pos['exit_at']
            if not (tp_hit or sl_hit or time_hit):
                still_open.append(pos)
                continue
            reason = 'TP' if tp_hit else ('SL' if sl_hit else 'TIME')
            sell_shares = pos['net_shares']
            fee_out = fee_shares(sell_shares, effective_bid)
            proceeds = (sell_shares - fee_out) * effective_bid
            pnl = proceeds - pos['fill_usdc']
            s['cash'] += proceeds
            s['cum_pnl'] += pnl
            closed = dict(pos)
            closed['exit_time'] = now
            closed['exit_price'] = effective_bid
            closed['exit_reason'] = reason
            closed['pnl'] = pnl
            s['closed_positions'].append(closed)
            s.setdefault('d_cooldowns', {})[pos['asset']] = now
            log_decision(s, f'EXIT_D_{reason}', f"sold @ {effective_bid:.3f} ({reason}, R={r_now*100:+.1f}%) → pnl ${pnl:+.2f}",
                         asset=pos['asset'][:20], pnl=pnl)
    s['open_positions'] = still_open

# ============ WATCH LIST for Strategy D ============
def get_watched_assets(s, recent_rash_trades):
    return {t['asset'] for t in recent_rash_trades}

# ============ WSS LISTENER (primary A signal) ============
def _record_tx(state, tx_hash):
    """Dedup via transactionHash; True hvis ny, False hvis allerede set."""
    if not tx_hash:
        return True
    seen = state.setdefault('seen_tx_hashes', [])
    if tx_hash in seen:
        return False
    seen.append(tx_hash)
    return True

async def _wss_pinger(ws):
    try:
        while True:
            await ws.send("ping")
            await asyncio.sleep(WSS_PING_INTERVAL_S)
    except Exception:
        return

def _catch_up_rest(state):
    """Ved WSS-connect: hent evt. trades vi missede via REST."""
    since = state.get('last_rash_trade_ts', 0)
    trades = fetch_rash_trades(since, limit=100)
    if not trades:
        return
    trades.reverse()
    print(f"  [catch-up] {len(trades)} REST trades siden last_rash_trade_ts={since}", flush=True)
    for t in trades:
        if not _record_tx(state, t.get('transactionHash')):
            continue
        process_strategy_A(state, t)
        state['last_rash_trade_ts'] = max(state.get('last_rash_trade_ts', 0), t.get('timestamp', 0))
    save_state(state)

async def wss_listen(state, lock):
    """Persistent WSS-listener: filtrér på Rash's proxyWallet, kør A."""
    import websockets
    rash_low = RASH.lower()
    while True:
        try:
            async with websockets.connect(WSS_URL, ping_interval=None, close_timeout=5) as ws:
                await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                    {"topic": "activity", "type": "trades"},
                    {"topic": "activity", "type": "orders_matched"},
                ]}))
                print(f"  [wss] connected {WSS_URL}", flush=True)
                async with lock:
                    await asyncio.to_thread(_catch_up_rest, state)
                pinger = asyncio.create_task(_wss_pinger(ws))
                try:
                    async for raw in ws:
                        if not isinstance(raw, str) or "payload" not in raw:
                            continue
                        try:
                            m = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        p = m.get("payload")
                        if not p:
                            continue
                        items = p if isinstance(p, list) else [p]
                        for t in items:
                            if not isinstance(t, dict):
                                continue
                            if (t.get("proxyWallet", "") or "").lower() != rash_low:
                                continue
                            async with lock:
                                if not _record_tx(state, t.get("transactionHash")):
                                    continue
                                await asyncio.to_thread(process_strategy_A, state, t)
                                state['last_rash_trade_ts'] = max(
                                    state.get('last_rash_trade_ts', 0),
                                    t.get('timestamp', 0),
                                )
                                await asyncio.to_thread(save_state, state)
                finally:
                    pinger.cancel()
        except Exception as e:
            print(f"  [wss] error, reconnect in {WSS_RECONNECT_S}s: {e}", flush=True)
            await asyncio.sleep(WSS_RECONNECT_S)

async def periodic_task(state, lock):
    """D-scan + exits + housekeeping. Kører hvert POLL_INTERVAL_S."""
    while True:
        t0 = time.time()
        try:
            async with lock:
                last_rash = await asyncio.to_thread(fetch_rash_trades, 0, 50)
                watched = get_watched_assets(state, last_rash)
                if ENABLE_STRATEGY_D:
                    await asyncio.to_thread(scan_strategy_D, state, watched)
                await asyncio.to_thread(process_exits, state)
                state['iterations'] += 1
                if len(state['closed_positions']) > 500:
                    state['closed_positions'] = state['closed_positions'][-500:]
                await asyncio.to_thread(save_state, state)
                print(f"  [periodic] iter={state['iterations']} cash=${state['cash']:.2f} open={len(state['open_positions'])} closed={len(state['closed_positions'])} cum=${state['cum_pnl']:+.2f}", flush=True)
        except Exception as e:
            print(f"  [periodic] error: {e}", flush=True)
        dt = time.time() - t0
        await asyncio.sleep(max(1.0, POLL_INTERVAL_S - dt))

# ============ MAIN LOOPS ============
def status():
    s = load_state()
    n_open = len(s['open_positions'])
    n_closed = len(s['closed_positions'])
    print(f"=== rash-signal paper-bot status ===")
    print(f"started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s['started_at']))}")
    print(f"iterations: {s['iterations']}")
    print(f"cash: ${s['cash']:.2f}")
    bankroll = s['cash'] + sum(p['fill_usdc'] for p in s['open_positions'])
    print(f"bankroll (cash + open exposure): ${bankroll:.2f}")
    print(f"cum PnL: ${s['cum_pnl']:+.2f}")
    print(f"open positions: {n_open}")
    print(f"closed positions: {n_closed}")
    if n_closed:
        wins = sum(1 for p in s['closed_positions'] if p['pnl']>0)
        print(f"  win rate: {wins/n_closed*100:.1f}%")
    if s['decisions']:
        print(f"\nlast 5 decisions:")
        for d in s['decisions'][-5:]:
            ts = time.strftime('%H:%M:%S', time.localtime(d['ts']))
            print(f"  {ts}  {d['kind']:12s} {d['msg']}")

def run_one_tick(s):
    """REST-only single tick — fallback for GH Actions cron."""
    s['iterations'] += 1
    print(f"\n=== iter {s['iterations']} @ {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} ===")

    new_rash = fetch_rash_trades(s['last_rash_trade_ts'], limit=50)
    new_rash.reverse()
    if new_rash:
        print(f"  {len(new_rash)} new Rash trades since last tick")
        for t in new_rash:
            if _record_tx(s, t.get('transactionHash')):
                process_strategy_A(s, t)
        s['last_rash_trade_ts'] = max(t['timestamp'] for t in new_rash)
    else:
        print(f"  no new Rash trades since {time.strftime('%H:%M:%S UTC', time.gmtime(s['last_rash_trade_ts']))}")

    last_rash = fetch_rash_trades(0, limit=50)
    watched = get_watched_assets(s, last_rash)
    if ENABLE_STRATEGY_D:
        scan_strategy_D(s, watched)
    process_exits(s)

    if len(s['closed_positions']) > 500:
        s['closed_positions'] = s['closed_positions'][-500:]

    save_state(s)
    print(f"  cash=${s['cash']:.2f} open={len(s['open_positions'])} closed={len(s['closed_positions'])} cum=${s['cum_pnl']:+.2f}")

async def async_main():
    state = load_state()
    print(f"paper-bot start · state has {len(state['open_positions'])} open, ${state['cash']:.2f} cash", flush=True)
    lock = asyncio.Lock()
    await asyncio.gather(
        wss_listen(state, lock),
        periodic_task(state, lock),
    )

def main_loop():
    asyncio.run(async_main())

def multi_tick(n_ticks=1, sleep_s=60):
    """N REST-only ticks — GH Actions cron mode."""
    s = load_state()
    for i in range(n_ticks):
        try:
            run_one_tick(s)
        except Exception as e:
            print(f"ERROR tick {i+1}/{n_ticks}: {e}")
            save_state(s)
        if i < n_ticks - 1:
            time.sleep(sleep_s)

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass
    if "--status" in sys.argv:
        status()
    elif "--tick" in sys.argv:
        idx = sys.argv.index("--tick")
        n = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) and sys.argv[idx + 1].isdigit() else 1
        slp = int(sys.argv[idx + 2]) if idx + 2 < len(sys.argv) and sys.argv[idx + 2].isdigit() else 60
        multi_tick(n, slp)
    else:
        main_loop()
