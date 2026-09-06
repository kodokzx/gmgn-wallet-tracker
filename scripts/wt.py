#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wt.py — Wallet Tracker (GMGN-powered)
Scanner token baru potensial, watcher smart money, dan analyzer wallet on-chain.
Data engine: gmgn-cli (official GMGN OpenAPI) + fallback keyless (GeckoTerminal/DexScreener).
Zero-dependency (stdlib only). Python 3.10+
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_DIR / "data"
CONFIG_PATH = SKILL_DIR / "config.json"
SCAN_STATE = DATA_DIR / "state_scan.json"
WATCH_STATE = DATA_DIR / "state_watch.json"
ALERTS_LOG = DATA_DIR / "alerts.jsonl"

DEMO_KEY = "gmgn_solbscbaseethmonadtron"  # read-only demo key dari repo GMGN
ALL_CHAINS = ["sol", "bsc", "base", "eth", "robinhood", "arc", "stable"]
GMGN_WALLET_URL = "https://gmgn.ai/{chain}/wallet/{addr}"
DEXSCREENER_TOKEN_URL = "https://dexscreener.com/{chain}/{addr}"


# ---------------------------------------------------------------- utilities
def log(msg=""):
    print(msg, flush=True)


def now_iso():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def ts_fmt(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d-%b %H:%M")
    except Exception:
        return "?"


def usd(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v/1_000:.1f}k"
    return f"${v:.2f}"


def short(addr, n=4):
    if not addr:
        return "?"
    return f"{addr[:n+2]}...{addr[-n:]}"


def load_config():
    cfg = {
        "api_key": "",
        "chains": ["robinhood", "bsc", "sol"],
        "scan": {
            "min_smart_degen": 2,
            "min_holder_count": 200,
            "min_liquidity_usd": 20000,
            "min_volume_1h_usd": 3000,
            "max_top10_rate": 0.5,
            "max_rug_ratio": 0.2,
            "max_age_hours": 72,
        },
        "watch": {
            "smartmoney_min_usd": 500,
            "watchlist_min_usd": 100,
            "transfer_min_usd": 1000,
            "limit_smartmoney": 100,
            "limit_wallet_activity": 50,
        },
        "telegram": {"bot_token": "", "chat_id": ""},
        "watchlist": [],
    }
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            log(f"[warn] config.json tidak valid: {e}")
    return cfg


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- gmgn-cli bridge
_GMGN_BIN_CACHE = None


def find_gmgn_bin():
    """Resolve gmgn-cli: env WT_GMGN_BIN -> dist/index.js via node -> .cmd shim."""
    global _GMGN_BIN_CACHE
    if _GMGN_BIN_CACHE:
        return _GMGN_BIN_CACHE
    env_bin = os.environ.get("WT_GMGN_BIN")
    if env_bin and Path(env_bin).exists():
        _GMGN_BIN_CACHE = ("node_file", env_bin)
        return _GMGN_BIN_CACHE
    npm_global = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "gmgn-cli"
    js_entry = npm_global / "dist" / "index.js"
    if js_entry.exists():
        _GMGN_BIN_CACHE = ("node_file", str(js_entry))
        return _GMGN_BIN_CACHE
    # Unix-style install (npm prefix -g)
    for prefix in ("/usr/local/lib/node_modules", os.path.expanduser("~/.npm-global/lib/node_modules")):
        js_entry = Path(prefix) / "gmgn-cli" / "dist" / "index.js"
        if js_entry.exists():
            _GMGN_BIN_CACHE = ("node_file", str(js_entry))
            return _GMGN_BIN_CACHE
    which = shutil.which("gmgn-cli.cmd") or shutil.which("gmgn-cli")
    if which:
        _GMGN_BIN_CACHE = ("shim", which)
        return _GMGN_BIN_CACHE
    raise SystemExit("[error] gmgn-cli tidak ditemukan. Install: npm install -g gmgn-cli")


def gmgn(args, api_key=None, timeout=60):
    """Jalankan gmgn-cli dengan --raw, return dict JSON."""
    kind, bin_path = find_gmgn_bin()
    key = api_key or os.environ.get("GMGN_API_KEY") or load_config().get("api_key") or DEMO_KEY
    env = dict(os.environ)
    env["GMGN_API_KEY"] = key
    env["NODE_OPTIONS"] = "--dns-result-order=ipv4first"  # GMGN menolak IPv6 (401/403)
    if kind == "node_file":
        cmd = ["node", bin_path] + list(args) + ["--raw"]
    else:
        cmd = [bin_path] + list(args) + ["--raw"]
    attempts = 0
    while True:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
        out = (p.stdout or "").strip()
        if p.returncode == 0:
            break
        err = ((p.stderr or "") + out).strip()
        up = err.upper()
        m = re.search(r"~\s*(\d+)\s*s\b", err)  # "~30s remaining" dari pesan GMGN
        if ("429" in up or "RATE_LIMIT" in up) and attempts < 4:
            wait = max(6, min(int(m.group(1)) + 3 if m else 20 * (attempts + 1), 300))
            log(f"[rate-limit] kena limit, tunggu {wait}s lalu retry ({attempts + 1}/4)…")
            time.sleep(wait)
            attempts += 1
            continue
        raise RuntimeError(f"gmgn-cli gagal ({' '.join(args[:3])}): {err[:300]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"[{\[]", out)
        if m:
            return json.loads(out[m.start():])
        raise RuntimeError(f"gmgn-cli output bukan JSON: {out[:200]}")


def dig(d, *keys, default=None):
    """Ambil key pertama yang ada (mendukung beberapa nama field alternatif)."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


# ---------------------------------------------------------------- grup perilaku A/B/C/D
# A = suka iklan (KOL) | B = smart positioning, main cepat | C = beli di bawah, hold lama
# D = FOMO buy | BOT = sandwich/sniper bot | R = ritel tanpa label
GROUP_LABEL = {"A": "A-iklan(KOL)", "B": "B-smart-cepat", "C": "C-akumulasi", "D": "D-fomo",
               "R": "R-ritel", "BOT": "BOT"}
GROUP_ORDER = ("A", "B", "C", "D", "R", "BOT")
SHIFT_NOTE = {
    ("A", "beli"): "KOL mulai masuk — biasanya iklan menyusul, waspada jadi late entry",
    ("A", "jual"): "KOL kabur — kampanye iklan selesai, jangan nunggu hype lagi",
    ("B", "beli"): "smart cepat positioning — sinyal entry awal",
    ("B", "jual"): "smart cepat keluar — momentum biasanya segera habis",
    ("C", "beli"): "akumulasi oleh holder panjang — sinyal paling sehat",
    ("C", "jual"): "holder panjang lepas — struktur token berubah, hati-hati",
    ("D", "beli"): "ritel FOMO ngejar — kalau bareng B/C jual = exit liquidity",
    ("D", "jual"): "ritel FOMO menyerah — biasanya dekat dasar lokal",
}


def group_of_tags(tags):
    """Klasifikasi cepat dari tag GMGN (feed-level). C butuh stats wallet (cmd groups)."""
    t = set(tags or [])
    if "kol" in t:
        return "A"
    if "smart_degen" in t:
        return "B"  # default; C diputuskan via avg_holding_period di cmd_groups
    if "fomo" in t:
        return "D"
    return None


GROUP_CACHE_PATH = DATA_DIR / "wallet_groups.json"
GROUPS_STATE = DATA_DIR / "state_groups.json"


def load_group_cache():
    c = load_json(GROUP_CACHE_PATH, {})
    return c if isinstance(c, dict) else {}


def classify_smart(addr, ch, key, cache, budget):
    """Pisah B vs C dari stats wallet (avg_holding_period >= 24 jam -> C). Cache 24 jam."""
    c = cache.get((addr or "").lower())
    if c and (time.time() - c.get("ts", 0)) < 86400:
        return c.get("g") or "B"
    if budget[0] <= 0:
        return "B"
    budget[0] -= 1
    try:
        st = fetch_stats(addr, ch, key)
        hold_h = float((st.get("pnl_stat") or {}).get("avg_holding_period") or 0) / 3600
        g = "C" if hold_h >= 24 else "B"
        cache[(addr or "").lower()] = {"g": g, "ts": time.time(), "hold_h": round(hold_h, 1)}
        return g
    except Exception:
        return "B"


def note_shift(cfg, tok_sym, chain, g, direction, net_usd, taddr, quiet=False):
    title = f"🔄 SHIFT [{g}] {tok_sym} ({chain}) → {direction.upper()}"
    body = (f"{GROUP_LABEL.get(g, g)} berbalik {direction} (net {usd(abs(net_usd))}) — "
            f"{SHIFT_NOTE.get((g, direction), '')}\n"
            f"{DEXSCREENER_TOKEN_URL.format(chain=chain, addr=taddr)}")
    notify("SHIFT", title, body, cfg, data={"token": taddr, "group": g}, quiet=quiet)


def check_zero_cross(prev, cur, min_abs):
    """True kalau tanda berubah (positif<->negatif) dan magnitudonya berarti."""
    return prev != 0 and cur != 0 and ((prev > 0) != (cur > 0)) \
        and abs(cur) >= min_abs and abs(prev) >= min_abs / 2


# ---------------------------------------------------------------- alerts / notify
def notify(kind, title, body, cfg, data=None, quiet=False):
    rec = {"ts": now_iso(), "type": kind, "title": title, "body": body, "data": data or {}}
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERTS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if not quiet:
        log(f"\n>>> [{kind}] {title}")
        if body:
            log(body)
    tg = cfg.get("telegram") or {}
    if tg.get("bot_token") and tg.get("chat_id"):
        try:
            url = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage"
            payload = json.dumps({"chat_id": tg["chat_id"],
                                  "text": f"{title}\n{body}".strip()[:3800]}).encode()
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"[warn] telegram gagal: {e}")


# ---------------------------------------------------------------- fallback keyless (GeckoTerminal)
def gt_new_pools(chain, limit=20):
    net = {"bsc": "bsc", "sol": "solana", "eth": "eth", "base": "base", "robinhood": "robinhood"}.get(chain)
    if not net:
        return []
    url = f"https://api.geckoterminal.com/api/v2/networks/{net}/new_pools?page=1"
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "wt/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    out = []
    for p in (d.get("data") or [])[:limit]:
        a = p.get("attributes", {})
        rel = (p.get("relationships", {}).get("base_token", {}).get("data", {}) or {}).get("id", "")
        addr = rel.split("_", 1)[1] if "_" in rel else rel
        out.append({
            "address": addr,
            "symbol": (a.get("name") or "?").split(" / ")[0],
            "chain": chain,
            "pool_created_at": a.get("pool_created_at"),
            "liquidity_usd": float(a.get("reserve_in_usd") or 0),
            "volume_24h_usd": float(a.get("volume_usd", {}).get("h24") or 0),
            "market_cap_usd": float(a.get("fdv_usd") or 0),
            "source": "geckoterminal",
        })
    return out


# ---------------------------------------------------------------- SCAN: token potensial
def scan_chain_trending(chain, scfg, key):
    """Token bermomentum via GMGN trending (1h) + trenches (baru)."""
    results = []
    # 1) Trending 1h — momentum
    try:
        d = gmgn(["market", "trending", "--chain", chain, "--interval", "1h",
                  "--limit", "80",
                  "--min-holder-count", str(scfg.get("min_holder_count", 200)),
                  "--min-liquidity", str(scfg.get("min_liquidity_usd", 20000))], api_key=key)
        for t in (d.get("data", {}).get("rank") or []):
            results.append(("trending", t))
    except Exception as e:
        log(f"[warn] trending {chain}: {e}")
    # 2) Trenches — token baru lahir
    try:
        d = gmgn(["market", "trenches", "--chain", chain,
                  "--type", "new_creation", "near_completion", "--limit", "80"], api_key=key)
        blob = d.get("data", {})
        cats = blob.get("rank") if isinstance(blob.get("rank"), list) else None
        if cats is None:
            cats = []
            for v in blob.values():
                if isinstance(v, list):
                    cats.extend(v)
        for t in cats:
            results.append(("trenches", t))
    except Exception as e:
        log(f"[warn] trenches {chain}: {e}")
    return results


def score_token(t, src, scfg, now_ts, chain):
    addr = dig(t, "address", "token_address", default="")
    if not addr:
        return None
    holders = int(float(dig(t, "holder_count", "holders", default=0) or 0))
    sd = int(float(dig(t, "smart_degen_count", "smart_degen_holder_count",
                       "smart_money_count", "renowned_count", default=0) or 0))
    liq = float(dig(t, "liquidity", "liquidity_usd", "reserve_in_usd", default=0) or 0)
    vol24 = float(dig(t, "volume", "volume_24h", "volume_24h_usd", default=0) or 0)
    vol1h = float(dig(t, "volume_1h", "volume_1h_usd", default=0) or 0)
    mcap = float(dig(t, "market_cap", "usd_market_cap", "market_cap_usd", default=0) or 0)
    top10 = float(dig(t, "top_10_holder_rate", "top10_rate", default=0) or 0)
    rug = float(dig(t, "rug_ratio", default=0) or 0)
    created = int(float(dig(t, "creation_timestamp", "open_timestamp", "created_timestamp",
                            "pool_created_at", default=0) or 0))
    if isinstance(created, str) and "T" in created:  # ISO dari geckoterminal
        try:
            created = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp())
        except Exception:
            created = 0
    age_h = (now_ts - created) / 3600 if created else None
    chg1h = float(dig(t, "price_change_percent1h", "price_change_1h", default=0) or 0)

    # filter keras
    if holders < scfg.get("min_holder_count", 200) and src == "trenches":
        return None
    if liq and liq < scfg.get("min_liquidity_usd", 20000):
        return None
    if vol1h and vol1h < scfg.get("min_volume_1h_usd", 3000) and src == "trending":
        return None
    if top10 and top10 > scfg.get("max_top10_rate", 0.5):
        return None
    if rug and rug > scfg.get("max_rug_ratio", 0.2):
        return None

    score = 0
    score += min(30, sd * 6)              # smart money di dalam
    score += min(15, holders / 100)       # distribusi holder
    if mcap and vol1h:
        score += min(15, (vol1h / mcap) * 100)  # turnover panas
    if age_h is not None:
        if age_h <= 24:
            score += 15
        elif age_h <= 48:
            score += 10
        elif age_h <= 72:
            score += 5
    if liq >= 50000:
        score += 10
    elif liq >= 20000:
        score += 6
    score -= rug * 30
    if top10 > 0.35:
        score -= 8
    if 5 <= chg1h <= 60:
        score += 5
    if chg1h > 150 or chg1h < -40:
        score -= 5
    hist_high = float(dig(t, "history_highest_market_cap", default=0) or 0)
    if hist_high and mcap and mcap > hist_high * 0.6:
        score += 5  # dekat ATH — momentum kuat

    return {
        "address": addr, "symbol": dig(t, "symbol", default="?"),
        "name": dig(t, "name", default="?"), "chain": chain,
        "price_usd": dig(t, "price", "price_usd", default=0),
        "change_1h_pct": round(chg1h, 1),
        "market_cap_usd": mcap, "liquidity_usd": liq,
        "volume_24h_usd": vol24, "volume_1h_usd": vol1h,
        "holder_count": holders, "smart_degen_count": sd,
        "top10_rate": round(top10, 3), "rug_ratio": round(rug, 3),
        "age_hours": round(age_h, 1) if age_h is not None else None,
        "launchpad": dig(t, "launchpad_platform", "launchpad", default=""),
        "source": src, "score": max(0, min(100, round(score))),
    }


def cmd_scan(args):
    cfg = load_config()
    chains = [c.strip() for c in (args.chains or ",".join(cfg["chains"])).split(",") if c.strip()]
    scfg = cfg.get("scan", {})
    state = load_json(SCAN_STATE, {})
    now_ts = int(time.time())
    candidates, alerts = {}, []

    for ch in chains:
        for src, t in scan_chain_trending(ch, scfg, args.api_key):
            s = score_token(t, src, scfg, now_ts, ch)
            if s:
                prev = candidates.get(s["address"])
                if not prev or s["score"] > prev["score"]:
                    candidates[s["address"]] = s

    ranked = sorted(candidates.values(), key=lambda x: -x["score"])
    if args.source == "geckoterminal":
        log("[info] memaksa fallback GeckoTerminal…")
        ranked = []
        for ch in chains:
            try:
                for p in gt_new_pools(ch):
                    p["score"] = 0
                    ranked.append(p)
            except Exception as e:
                log(f"[warn] GT {ch}: {e}")

    max_alerts = 12
    for s in ranked[:args.top]:
        key = f'{s["chain"]}:{s["address"]}'
        prev = state.get(key)
        tag = None
        if not prev:
            tag = "BARU" if (s.get("age_hours") or 999) <= scfg.get("max_age_hours", 72) else "TEMUAN"
        else:
            try:
                dh = (s["holder_count"] - prev.get("holders", 0)) / max(1, prev.get("holders", 1))
                dsd = s["smart_degen_count"] - prev.get("sd", 0)
                if dh >= 0.30 or dsd >= 2:
                    tag = "TRAKSI NAIK"
            except Exception:
                pass
        state[key] = {"holders": s["holder_count"], "sd": s["smart_degen_count"],
                      "mcap": s["market_cap_usd"], "last": now_iso()}
        line = (f'{s["score"]:>3} | {s["symbol"]:<10} | {s["chain"]:<9} | '
                f'mcap {usd(s["market_cap_usd"]):>8} | liq {usd(s["liquidity_usd"]):>7} | '
                f'vol1h {usd(s["volume_1h_usd"]):>7} | holders {s["holder_count"]:>5} | '
                f'smart {s["smart_degen_count"]:>3} | umur {s["age_hours"]}h')
        log(line)
        if tag and len(alerts) < max_alerts:
            alerts.append((s, tag))

    for s, tag in alerts:
        notify("TOKEN",
               f'[{tag}] {s["symbol"]} ({s["chain"]}) score {s["score"]}',
               (f'mcap {usd(s["market_cap_usd"])} | liq {usd(s["liquidity_usd"])} | '
                f'vol1h {usd(s["volume_1h_usd"])} | holders {s["holder_count"]} | '
                f'smart-degen {s["smart_degen_count"]} | umur {s["age_hours"]}h | '
                f'1h {s["change_1h_pct"]}%\n{DEXSCREENER_TOKEN_URL.format(chain=s["chain"], addr=s["address"])}'),
               cfg, data=s)

    save_json(SCAN_STATE, state)
    log(f"\n[done] {len(ranked)} kandidat, {len(alerts)} alert. State: {SCAN_STATE.name}")


# ---------------------------------------------------------------- WATCH: trade smart money
def cmd_watch(args):
    cfg = load_config()
    wcfg = cfg.get("watch", {})
    chains = [c.strip() for c in (args.chains or ",".join(cfg["chains"])).split(",") if c.strip()]
    state = load_json(WATCH_STATE, {})
    first_run = args.init or not state.get("initialized")
    n_alerts = 0

    def poll_smartmoney(ch):
        nonlocal n_alerts
        shifted = set()  # cegah SHIFT dobel dalam satu run
        try:
            d = gmgn(["track", "smartmoney", "--chain", ch,
                      "--limit", str(wcfg.get("limit_smartmoney", 100))], api_key=args.api_key)
        except Exception as e:
            log(f"[warn] smartmoney {ch}: {e}")
            return
        last_key = f"sm:{ch}"
        last_ts = state.get(last_key, 0)
        max_ts = last_ts
        trades = (d.get("list") or [])
        for tr in sorted(trades, key=lambda x: x.get("timestamp", 0)):
            ts = int(tr.get("timestamp") or 0)
            if ts <= last_ts:
                continue
            max_ts = max(max_ts, ts)
            if first_run:
                continue
            if float(tr.get("amount_usd") or 0) < wcfg.get("smartmoney_min_usd", 500):
                continue
            side = (tr.get("side") or "?").upper()
            icon = "🟢 BUY " if side == "BUY" else "🔴 SELL"
            mtags = (tr.get("maker_info") or {}).get("tags") or []
            tok = (tr.get("base_token") or {}).get("symbol") or tr.get("base_address") or "?"
            tags = ",".join(mtags)
            if "sandwich_bot" in mtags or "sniper_bot" in mtags:
                gl = "[BOT] "
            else:
                g = group_of_tags(mtags)
                gl = f"[{g}] " if g else ""
            title = f"{icon} {gl}SMART {usd(tr.get('amount_usd'))} {tok}"
            body = (f'{short(tr.get("maker"))} [{tags}] {ch} @ ${tr.get("price_usd")}\n'
                    f'{DEXSCREENER_TOKEN_URL.format(chain=ch, addr=tr.get("base_address"))}')
            notify("TRADE", title, body, cfg, data={"maker": tr.get("maker"), "token": tok})
            n_alerts += 1
            if g and tr.get("base_address") and not first_run:
                gfkey = f'{ch}:{tr["base_address"]}'
                gf = state.setdefault("gflow", {}).setdefault(
                    gfkey, {"sym": tok, "A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0})
                prev = state.get("gflow_prev", {}).get(gfkey, {}).get(g, 0.0)
                gf[g] = gf.get(g, 0.0) + (1 if side == "BUY" else -1) * float(tr.get("amount_usd") or 0)
                cur = gf[g]
                if check_zero_cross(prev, cur, 1500) and gfkey not in shifted:
                    shifted.add(gfkey)
                    note_shift(cfg, tok, ch, g, "beli" if cur > 0 else "jual", cur, tr["base_address"])
                    n_alerts += 1
        state[last_key] = max_ts

    def poll_wallet(entry, ch):
        nonlocal n_alerts
        addr = entry["address"]
        try:
            d = gmgn(["portfolio", "activity", "--chain", ch, "--wallet", addr,
                      "--limit", str(wcfg.get("limit_wallet_activity", 50))], api_key=args.api_key)
        except Exception as e:
            log(f"[warn] activity {short(addr)} {ch}: {e}")
            return
        acts = (d.get("activities") or [])
        last_key = f"w:{ch}:{addr.lower()}"
        last_ts = state.get(last_key, 0)
        max_ts = last_ts
        for a in sorted(acts, key=lambda x: int(x.get("timestamp") or 0)):
            ts = int(a.get("timestamp") or 0)
            if ts <= last_ts:
                continue
            max_ts = max(max_ts, ts)
            if first_run:
                continue
            ev = a.get("event_type") or "?"
            usd_amt = float(a.get("cost_usd") or 0)
            tok = (a.get("token") or {}).get("symbol") or "?"
            note = entry.get("note") or tags_of(a)
            if ev in ("buy", "sell") and usd_amt >= wcfg.get("watchlist_min_usd", 100):
                icon = "🟢 BUY" if ev == "buy" else "🔴 SELL"
                title = f"{icon} {usd(usd_amt)} {tok} — {short(addr)} ({note})"
                body = (f'{ch} @ ${a.get("price_usd")} | {ts_fmt(ts)}\n'
                        f'{DEXSCREENER_TOKEN_URL.format(chain=ch, addr=(a.get("token") or {}).get("address"))}')
                notify("WALLET", title, body, cfg, data={"wallet": addr, "event": ev})
                n_alerts += 1
            elif ev in ("transferIn", "transferOut") and usd_amt >= wcfg.get("transfer_min_usd", 1000):
                counter = a.get("from_address") if ev == "transferIn" else a.get("to_address")
                title = f"💸 TRANSFER {usd(usd_amt)} {tok} — {short(addr)}"
                body = f'{ev} {"dari" if ev == "transferIn" else "ke"} {short(counter)} | {ts_fmt(ts)}'
                notify("TRANSFER", title, body, cfg, data={"wallet": addr})
                n_alerts += 1
        state[last_key] = max_ts

    def tags_of(a):
        return ""

    while True:
        for ch in chains:
            poll_smartmoney(ch)
            time.sleep(0.4)
        for entry in cfg.get("watchlist", []):
            chs = [entry.get("chain")] if entry.get("chain") else chains
            for ch in chs:
                poll_wallet(entry, ch)
                time.sleep(0.4)
        state["gflow_prev"] = {k: {gg: v.get(gg, 0.0) for gg in ("A", "B", "C", "D")}
                               for k, v in state.get("gflow", {}).items()}
        state["initialized"] = True
        save_json(WATCH_STATE, state)
        if first_run:
            log("[init] baseline tersimpan — poll berikutnya mulai kirim alert.")
        if args.loop:
            time.sleep(args.interval)
        else:
            log(f"[done] watch selesai. {n_alerts} alert baru.")
            break


# ---------------------------------------------------------------- ANALYZE: breakdown wallet
def common_of(stats):
    """Field umum wallet (tags, funding source) ada di sub-object 'common'."""
    c = stats.get("common")
    return c if isinstance(c, dict) else stats
def fetch_stats(addr, ch, key):
    return gmgn(["portfolio", "stats", "--chain", ch, "--wallet", addr], api_key=key)


def fetch_activity(addr, ch, limit, key, pages=5):
    out, cursor = [], None
    for _ in range(pages):  # API cap 20 event/halaman
        args = ["portfolio", "activity", "--chain", ch, "--wallet", addr, "--limit", str(limit)]
        if cursor:
            args += ["--cursor", cursor]
        d = gmgn(args, api_key=key)
        page = d.get("activities") or []
        out.extend(page)
        cursor = d.get("next") or d.get("cursor") or d.get("next_cursor")
        if not cursor or not page:
            break
    return out


def fetch_holdings(addr, ch, key):
    d = gmgn(["portfolio", "holdings", "--chain", ch, "--wallet", addr,
              "--limit", "20", "--order-by", "usd_value"], api_key=key)
    return d.get("holdings") or d.get("list") or d.get("data") or []


def analyze_token_flows(acts):
    per = {}
    transfers = []
    for a in acts:
        ev = a.get("event_type")
        tok = a.get("token") or {}
        sym = tok.get("symbol") or "?"
        taddr = tok.get("address") or "?"
        usd_amt = float(a.get("cost_usd") or 0)
        ts = int(a.get("timestamp") or 0)
        if ev in ("buy", "sell"):
            p = per.setdefault(taddr, {"symbol": sym, "buys_usd": 0.0, "sells_usd": 0.0,
                                       "realized": 0.0, "n_buys": 0, "n_sells": 0,
                                       "first": ts, "last": ts, "launchpad": a.get("launchpad")})
            p["first"] = min(p["first"], ts); p["last"] = max(p["last"], ts)
            if ev == "buy":
                p["buys_usd"] += usd_amt; p["n_buys"] += 1
            else:
                p["sells_usd"] += usd_amt; p["n_sells"] += 1
                p["realized"] += usd_amt - float(a.get("buy_cost_usd") or 0)
        elif ev in ("transferIn", "transferOut"):
            counter = a.get("from_address") if ev == "transferIn" else a.get("to_address")
            transfers.append({"dir": ev, "symbol": sym, "usd": usd_amt, "counter": counter,
                              "ts": ts, "address": taddr})
    return per, transfers


def side_wallets(transfers, self_addr):
    """Counterparty transfer yang berulang = kandidat side wallet."""
    agg = {}
    for t in transfers:
        c = (t.get("counter") or "").lower()
        if not c or c == (self_addr or "").lower():
            continue
        a = agg.setdefault(c, {"n": 0, "usd": 0.0, "tokens": set()})
        a["n"] += 1; a["usd"] += t.get("usd") or 0; a["tokens"].add(t.get("symbol"))
    return sorted(({"address": k, **v, "tokens": ",".join(v["tokens"])} for k, v in agg.items()),
                  key=lambda x: (-x["usd"], -x["n"]))


def trading_logic(stats, per, holdings, transfers):
    lines = []
    ps = stats.get("pnl_stat") or {}
    winrate = float(ps.get("winrate") or 0) * 100
    hold_s = float(ps.get("avg_holding_period") or 0)
    hold_h = hold_s / 3600
    n_tok = int(ps.get("token_num") or 0)
    style = ("scalper cepat" if hold_h < 1 else
             "intraday (Naik-turun harian)" if hold_h < 24 else
             "swing (pegang berhari-hari)" if hold_h < 24 * 7 else "position/long")
    lines.append(f"- Gaya: **{style}** — rata-rata pegang {hold_h:.1f} jam, "
                 f"winrate {winrate:.0f}%, sudah main {n_tok} token")
    buckets = (f"0.5x↓:{ps.get('pnl_lt_nd5_num', 0)} | 0.5-1x:{ps.get('pnl_nd5_0x_num', 0)} | "
               f"1-2x:{ps.get('pnl_0x_2x_num', 0)} | 2-5x:{ps.get('pnl_2x_5x_num', 0)} | "
               f"5x↑:{ps.get('pnl_gt_5x_num', 0)}")
    lines.append(f"- Sebaran hasil per token: {buckets}")
    big5 = int(ps.get("pnl_gt_5x_num") or 0) + int(ps.get("pnl_2x_5x_num") or 0)
    if big5 and hold_h < 24:
        lines.append("- Pola: memburu **runner** — sabar pegang sampai 2x+ meski entry-scarp harian")
    elif not big5 and winrate > 50:
        lines.append("- Pola: **pemotong kecil konsisten** — TP cepat, jarang nunggu 2x")
    buys = [p for p in per.values() if p["n_buys"]]
    if buys:
        avg_buy = sum(p["buys_usd"] for p in buys) / len(buys)
        lines.append(f"- Ukuran entry: rata-rata {usd(avg_buy)}/beli "
                     f"({'DCA bertahap' if any(p['n_buys'] >= 3 for p in buys) else 'sekali tembak'})")
    lp = [p for p in per.values() if p.get("launchpad")]
    if lp:
        lps = sorted({p["launchpad"] for p in lp})
        lines.append(f'- Ikut lahiran langsung dari launchpad: {", ".join(lps)} (early sniper)')
    if transfers:
        lines.append(f"- {len(transfers)} transfer terdeteksi — cek bagian side wallet "
                     f"(dana sering muter antar wallet sendiri)")
    ff = common_of(stats).get("fund_from_address") or stats.get("fund_from_address")
    if ff:
        lines.append(f"- Dana awal wallet ini berasal dari `{short(ff, 6)}` → kandidat wallet induk")
    return lines


def cmd_analyze(args):
    cfg = load_config()
    ch = args.chain or (cfg["chains"][0] if cfg["chains"] else "robinhood")
    addr = args.wallet.strip()
    log(f"# Breakdown wallet `{short(addr, 6)}` ({ch}) — {now_iso()}")

    stats = fetch_stats(addr, ch, args.api_key)
    realized = float(dig(stats, "realized_profit", default=0) or 0)
    log(f"\n## RINGKASAN")
    log(f"- Realized PnL: **{usd(realized)}** | buy {stats.get('buy', '?')}x / sell {stats.get('sell', '?')}x"
        f" | total beli {usd(stats.get('bought_cost'))} | total jual {usd(stats.get('sold_income'))}")
    cmon = common_of(stats)
    tags = cmon.get("tags") or stats.get("tags") or []
    if tags:
        log(f"- Tag GMGN: {', '.join(tags)} | token dibuat: {stats.get('created_token_count', 0)}")

    acts = fetch_activity(addr, ch, args.limit, args.api_key)
    log(f"  ({len(acts)} event terakhir diambil)")
    per, transfers = analyze_token_flows(acts)
    log(f"\n## PER-TOKEN (dari {len(acts)} event terakhir)")
    rows = sorted(per.values(), key=lambda p: -p["realized"])[:12]
    log("```\ntoken        buy$      sell$     realized   nb/ns  first-last")
    for p in rows:
        log(f'{p["symbol"]:<12} {usd(p["buys_usd"]):>9} {usd(p["sells_usd"]):>9} '
            f'{usd(p["realized"]):>10}  {p["n_buys"]:>2}/{p["n_sells"]:<2}  '
            f'{ts_fmt(p["first"])} → {ts_fmt(p["last"])}')
    log("```")
    tot_buy = sum(p["buys_usd"] for p in per.values())
    tot_sell = sum(p["sells_usd"] for p in per.values())
    log(f"- Outflow ke token (beli): {usd(tot_buy)} | Inflow dari token (jual): {usd(tot_sell)} "
        f"| net trading {usd(tot_sell - tot_buy)}")

    log(f"\n## TRANSFER & SIDE WALLET")
    sw = side_wallets(transfers, addr)
    if not sw:
        log("- Tidak ada transfer token terdeteksi di window ini")
    for s in sw[:8]:
        log(f"- {short(s['address'], 6)}: {s['n']}x transfer, {usd(s['usd'])} "
            f"({s['tokens']}) ← kandidat side wallet")
    ff = common_of(stats).get("fund_from_address") or stats.get("fund_from_address")
    if ff:
        log(f"- Funding source (dana pertama): `{ff}` — coba analyze wallet ini untuk cek klaster")
    if args.deep and (ff or sw):
        targets = [ff] if ff else []
        targets += [s["address"] for s in sw[:3]]
        seen = {addr.lower()}
        for t in [x for x in targets if x and x.lower() not in seen][:4]:
            try:
                st2 = fetch_stats(t, ch, args.api_key)
                log(f"  - {short(t, 6)}: PnL {usd(dig(st2, 'realized_profit', default=0))}, "
                    f"winrate {float((st2.get('pnl_stat') or {}).get('winrate') or 0)*100:.0f}%, "
                    f"tags {','.join(st2.get('tags') or [])}")
            except Exception as e:
                log(f"  - {short(t, 6)}: gagal ({e})")

    log(f"\n## POSISI SAAT INI (holdings)")
    try:
        holds = fetch_holdings(addr, ch, args.api_key)
    except Exception as e:
        holds = []
        if "PRIVATE_KEY" in str(e):
            log("- Butuh GMGN_PRIVATE_KEY (holdings = fitur critical-auth GMGN) — setup lihat README.md. "
                "Estimasi posisi dari aktivitas terakhir tetap tersedia di bagian PER-TOKEN.")
        else:
            log(f"- Gagal ambil holdings: {e}")
    if not holds:
        log("- Kosong / tidak tersedia")
    for h in holds[:10]:
        tok = h.get("token") or h
        sym = tok.get("symbol") or "?"
        uv = dig(h, "usd_value", default=0)
        up = dig(h, "unrealized_profit", default=0)
        log(f"- {sym}: {usd(uv)} | uPnL {usd(up)}")

    log(f"\n## LOGIKA TRADING")
    for line in trading_logic(stats, per, holds, transfers):
        log(line)

    if ch in ("sol", "bsc", "base", "eth"):
        log(f"\nProfil: {GMGN_WALLET_URL.format(chain=ch, addr=addr)}")


# ---------------------------------------------------------------- GROUPS: perilaku trader per token
def cmd_groups(args):
    cfg = load_config()
    ch = args.chain or (cfg["chains"][0] if cfg["chains"] else "robinhood")
    addr = args.token.strip()
    log(f"# Perilaku grup trader `{short(addr, 6)}` ({ch}) — {now_iso()}")

    sym, created = "?", 0
    try:
        ti = gmgn(["token", "info", "--chain", ch, "--address", addr], api_key=args.api_key)
        tb = ti.get("token") or ti.get("data") or ti
        sym = dig(tb, "symbol", default="?")
        created = int(float(dig(tb, "creation_timestamp", "open_timestamp",
                                "pool_creation_timestamp", default=0) or 0))
    except Exception as e:
        log(f"[warn] token info: {e}")

    d = gmgn(["token", "traders", "--chain", ch, "--address", addr,
              "--limit", str(args.limit)], api_key=args.api_key)
    traders = d.get("list") or []
    if sym == "?" and traders:
        sym = traders[0].get("name") or "?"

    cache = load_group_cache()
    budget = [max(0, args.stats_budget)]
    agg = {}
    for t in traders:
        a = (t.get("address") or "").lower()
        if not a or a == "0x000000000000000000000000000000000000dead":
            continue
        tags = t.get("tags") or []
        mtags = set(t.get("maker_token_tags") or [])
        if "sandwich_bot" in tags or "sniper_bot" in tags:
            g = "BOT"
        elif "kol" in tags:
            g = "A"
        elif "smart_degen" in tags:
            g = classify_smart(a, ch, args.api_key, cache, budget)
        elif "fomo" in tags:
            g = "D"
        else:
            g = "R"
        row = agg.setdefault(g, {"n": 0, "buy": 0.0, "sell": 0.0, "net": 0.0,
                                 "hold": 0, "transfer_in": 0, "first_in": None, "bundlers": 0})
        row["n"] += 1
        row["buy"] += float(t.get("buy_volume_cur") or 0)
        row["sell"] += float(t.get("sell_volume_cur") or 0)
        row["net"] += float(t.get("netflow_usd") or 0)
        if float(t.get("amount_percentage") or 0) > 0:
            row["hold"] += 1
        if "transfer_in" in mtags:
            row["transfer_in"] += 1
        if "bundler" in mtags:
            row["bundlers"] += 1
        st = t.get("start_holding_at")
        if st and (row["first_in"] is None or int(st) < row["first_in"]):
            row["first_in"] = int(st)
    save_json(GROUP_CACHE_PATH, cache)

    log(f"\n## Tabel grup ({len(traders)} trader teratas, token {sym})")
    log("```\ngrup             n   buy$      sell$     net$      pegang  entry-awal  tf-in")
    for g in GROUP_ORDER:
        if g not in agg:
            continue
        r = agg[g]
        early = f'+{max(0, (r["first_in"] - created) // 60)}m' if (created and r["first_in"]) else "?"
        log(f'{GROUP_LABEL[g]:<16} {r["n"]:>3} {usd(r["buy"]):>9} {usd(r["sell"]):>9} '
            f'{usd(r["net"]):>9} {r["hold"]:>4}    {early:>6}    {r["transfer_in"]}')
    log("```")

    log(f"\n## BACAAN KELAKUAN")
    if agg.get("A", {}).get("n"):
        net_a = agg["A"]["net"]
        log(f'- A-iklan(KOL): net {usd(net_a)} — '
            f'{"siap-siap ada iklan/promosi" if net_a > 0 else "KOL sudah/di luar, hype organik saja"}')
    if agg.get("B", {}).get("n"):
        log(f'- B-smart-cepat: net {usd(agg["B"]["net"])} dari {agg["B"]["n"]} wallet '
            f'({"positioning masuk" if agg["B"]["net"] > 0 else "sudah mulai keluar"})')
    if agg.get("C", {}).get("n"):
        log(f'- C-akumulasi: net {usd(agg["C"]["net"])} dari {agg["C"]["n"]} wallet '
            f'({"menumpuk posisi — sinyal sehat" if agg["C"]["net"] > 0 else "holder panjang berkurang"})')
    if agg.get("D", {}).get("n"):
        bc = agg.get("B", {}).get("net", 0) + agg.get("C", {}).get("net", 0)
        if agg["D"]["net"] > 2000 and bc < 0:
            log(f'- ⚠️ D-fomo net {usd(agg["D"]["net"])} sementara smart net {usd(bc)} '
                f'→ pola DISTRIBUSI: smart memberi, ritel menampung')
        else:
            log(f'- D-fomo: net {usd(agg["D"]["net"])} dari {agg["D"]["n"]} wallet')
    bots = agg.get("BOT", {})
    if bots.get("n"):
        log(f'- BOT (MEV): {bots["n"]} wallet, net {usd(bots["net"])} — '
            f'{"eksploitasi sandwich aktif" if bots.get("net", 0) < -1000 else "aktifitas normal"}')
    bundle = sum(r.get("bundlers", 0) for r in agg.values())
    tin = sum(r.get("transfer_in", 0) for r in agg.values())
    if bundle or tin >= 3:
        log(f'- ⚠️ Supply indikator: {bundle} bundler, {tin} wallet nerima token via transfer '
            f'→ indikasi insider/cluster supply, cek sebaran holder')

    log(f"\n## DETEKSI SHIFT (vs snapshot sebelumnya)")
    snap = load_json(GROUPS_STATE, {})
    skey = f"{ch}:{addr.lower()}"
    prev = (snap.get(skey) or {}).get("groups") or {}
    cur_groups = {g: agg[g]["net"] for g in agg}
    shifts = 0
    for g in ("A", "B", "C", "D"):
        p, c = prev.get(g, 0.0), cur_groups.get(g, 0.0)
        if check_zero_cross(p, c, args.min_shift):
            direction = "beli" if c > 0 else "jual"
            log(f'- ⚠️ [{g}] {usd(p)} → {usd(c)} — {SHIFT_NOTE.get((g, direction), "")}')
            note_shift(cfg, sym, ch, g, direction, c, addr)
            shifts += 1
    if not prev:
        log("- Baseline tersimpan — jalankan lagi nanti untuk membandingkan pergeseran perilaku")
    elif not shifts:
        log("- Tidak ada pembalikan arah grup sejak snapshot terakhir")
    snap[skey] = {"ts": now_iso(), "groups": cur_groups}
    save_json(GROUPS_STATE, snap)


# ---------------------------------------------------------------- watchlist CRUD
def cmd_watchlist(args):
    cfg = load_config()
    if args.action == "list":
        for i, w in enumerate(cfg["watchlist"]):
            log(f'{i}. {w["address"]}  chain={w.get("chain") or "auto"}  note={w.get("note", "")}')
        if not cfg["watchlist"]:
            log("(kosong — add dulu: python wt.py watchlist add 0x... --chain robinhood --note 'kol')")
    elif args.action == "add":
        addr = args.address.strip()
        if not re.match(r"^(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$", addr):
            raise SystemExit("[error] alamat wallet tidak valid (EVM 0x… / Solana base58)")
        cfg["watchlist"] = [w for w in cfg["watchlist"] if w["address"].lower() != addr.lower()]
        cfg["watchlist"].append({"address": addr, "chain": args.chain, "note": args.note or ""})
        save_json(CONFIG_PATH, cfg)
        log(f"[ok] {short(addr, 6)} masuk watchlist ({args.chain or 'auto'})")
    elif args.action == "remove":
        before = len(cfg["watchlist"])
        cfg["watchlist"] = [w for w in cfg["watchlist"] if w["address"] != args.address.strip()]
        save_json(CONFIG_PATH, cfg)
        log(f"[ok] dihapus {before - len(cfg['watchlist'])} entri")


def cmd_alerts(args):
    rows = load_json(ALERTS_LOG, None)
    lines = ALERTS_LOG.read_text(encoding="utf-8").splitlines() if ALERTS_LOG.exists() else []
    for ln in lines[-args.last:]:
        try:
            r = json.loads(ln)
            log(f'{r["ts"]} [{r["type"]}] {r["title"]}')
        except Exception:
            log(ln)


def cmd_test_notify(args):
    cfg = load_config()
    tg = cfg.get("telegram") or {}
    if not tg.get("bot_token") or not tg.get("chat_id"):
        log("[info] Telegram belum diisi di config.json — alert hanya ke console + data/alerts.jsonl")
        notify("TEST", "Wallet tracker siap ✅", "Test alert lokal.", cfg)
        return
    notify("TEST", "Wallet tracker siap ✅", "Kalau ini muncul di Telegram, alert sudah nyambung.", cfg)
    log("[ok] terkirim — cek Telegram kamu")


# ---------------------------------------------------------------- HTML dashboard (data aktual)
HTML_TEMPLATE = r"""<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wallet Tracker — Dashboard</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#21262d;--tx:#e6edf3;--mut:#8b949e;
--grn:#3fb950;--red:#f85149;--blu:#58a6ff;--org:#f0883e;--pur:#bc8cff;--tea:#39d2c0}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.5 ui-sans-serif,system-ui,Segoe UI,Roboto}
header{padding:20px 22px 8px}h1{margin:0;font-size:20px}h1 small{color:var(--mut);font-weight:400}
.sub{color:var(--mut);font-size:12px;margin-top:2px}
.cards{display:flex;gap:10px;flex-wrap:wrap;padding:12px 22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:130px}
.card b{display:block;font-size:20px}.card span{color:var(--mut);font-size:11px;text-transform:uppercase}
section{padding:8px 22px 18px}h2{font-size:15px;margin:18px 0 8px;color:var(--blu)}
table{border-collapse:collapse;width:100%;font-size:13px}
th{color:var(--mut);text-align:left;font-weight:600;font-size:11px;text-transform:uppercase;
padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:hover td{background:#1a212b}
.chip{display:inline-block;border:1px solid var(--line);border-radius:20px;padding:2px 10px;
margin:0 6px 6px 0;cursor:pointer;font-size:12px;color:var(--mut);user-select:none}
.chip.on{background:var(--blu);color:#000;border-color:var(--blu)}
.badge{display:inline-block;border-radius:6px;padding:1px 7px;font-size:11px;font-weight:700}
.b-SHIFT{background:#3d2200;color:var(--org)}.b-TOKEN{background:#0b2b4a;color:var(--blu)}
.b-TRADE{background:#1b2430;color:#9db6d3}.b-WALLET{background:#2a1e3d;color:var(--pur)}
.b-TRANSFER{background:#0d2f2c;color:var(--tea)}.b-TEST{background:#222;color:#888}
.up{color:var(--grn)}.dn{color:var(--red)}
.a{color:var(--blu);text-decoration:none}.a:hover{text-decoration:underline}
.item{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin-bottom:6px}
.item .t{color:var(--mut);font-size:11px}
.item.shift{border-color:var(--org)}
.bar{height:9px;border-radius:5px;min-width:2px}
.gA{background:var(--org)}.gB{background:var(--blu)}.gC{background:var(--grn)}.gD{background:var(--pur)}
.legend{color:var(--mut);font-size:12px;margin:4px 0 10px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin:0 4px 0 12px}
input{background:#0a0e14;border:1px solid var(--line);color:var(--tx);border-radius:8px;
padding:7px 12px;width:280px;font-size:13px}
.score{font-weight:800}.mut{color:var(--mut)}
@media(max-width:760px){.cards{padding:8px 12px}section{padding:8px 12px 14px}}
</style></head><body>
<header><h1>🐋 Wallet Tracker <small>— dashboard data aktual</small></h1>
<div class="sub">Digenerate: <span id="gen"></span> · sumber: data/alerts.jsonl + state lokal · read-only</div></header>
<div class="cards" id="cards"></div>
<section><h2>Radar Token <span class="mut">(alert token, dedup — terbaru per token)</span></h2>
<div style="overflow-x:auto"><table id="radar"></table></div></section>
<section><h2>Arus Grup Perilaku A/B/C/D <span class="mut">(kumulatif dari feed smart money)</span></h2>
<div class="legend"><i class="gA"></i>A-iklan(KOL) <i class="gB"></i>B-smart-cepat <i class="gC"></i>C-akumulasi <i class="gD"></i>D-fomo — hijau/merah = net beli/jual</div>
<div id="gflow"></div></section>
<section><h2>Feed Alert</h2>
<div><span class="chip on" data-f="SEMUA">semua</span><span class="chip" data-f="SHIFT">SHIFT</span>
<span class="chip" data-f="TOKEN">token</span><span class="chip" data-f="TRADE">trade</span>
<span class="chip" data-f="WALLET">watchlist</span><span class="chip" data-f="TRANSFER">transfer</span>
<input id="q" placeholder="cari symbol / wallet / chain…"></div>
<div id="feed" style="margin-top:10px"></div></section>
<section><h2>Watchlist Wallet</h2><div style="overflow-x:auto"><table id="wlist"></table></div></section>
<section><h2>Snapshot Grup per Token <span class="mut">(cmd: wt.py groups)</span></h2>
<div id="gsnap" class="mut"></div></section>
<script>const DATA=__DATA__;</script>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const usd=v=>{v=+v||0;const a=Math.abs(v);if(a>=1e6)return"$"+(v/1e6).toFixed(2)+"M";if(a>=1e3)return"$"+(v/1e3).toFixed(1)+"k";return"$"+v.toFixed(2)};
const link=(ch,a)=>`https://dexscreener.com/${ch}/${a}`;
$("#gen").textContent=DATA.generated;
const al=DATA.alerts||[],today=DATA.generated.slice(0,10);
const nToday=al.filter(a=>(a.ts||"").startsWith(today)).length;
const nShift=al.filter(a=>a.type==="SHIFT").length;
$("#cards").innerHTML=[["Alert tersimpan",al.length],["Alert hari ini",nToday],["SHIFT terdeteksi",nShift],
["Token di radar",Object.keys(radarMap()).length],["Grup token",Object.keys(DATA.gflow||{}).length],
["Watchlist",(DATA.config.watchlist||[]).length]].map(c=>`<div class="card"><b>${c[1]}</b><span>${c[0]}</span></div>`).join("");
function radarMap(){const m={};(al||[]).filter(a=>a.type==="TOKEN"&&a.data&&a.data.address)
.forEach(a=>{const k=a.chain+":"+a.data.address;m[k]=a});return m}
const rad=Object.values(radarMap()).sort((x,y)=>(y.data.score||0)-(x.data.score||0));
$("#radar").innerHTML="<tr><th>skor</th><th>token</th><th>chain</th><th>mcap</th><th>liq</th><th>hold</th><th>smart</th><th>1h</th><th>umur</th><th>alert</th></tr>"+
rad.map(a=>{const d=a.data;return `<tr><td class="score">${d.score||"?"}</td>
<td><b>${esc(d.symbol)}</b></td><td>${esc(a.chain||d.chain)}</td><td>${usd(d.market_cap_usd)}</td>
<td>${usd(d.liquidity_usd)}</td><td>${d.holder_count||0}</td><td>${d.smart_degen_count||0}</td>
<td class="${(d.change_1h_pct||0)>=0?"up":"dn"}">${d.change_1h_pct||0}%</td><td class="mut">${d.age_hours??"?"}h</td>
<td><a class="a" href="${link(d.chain||a.chain,d.address)}" target="_blank">chart ↗</a></td></tr>`}).join("");
const gf=DATA.gflow||{},maxv=Math.max(1,...Object.values(gf).flatMap(o=>Object.values(o).slice(1).map(Number).map(Math.abs)));
$("#gflow").innerHTML=Object.entries(gf).map(([k,o])=>{const[chain,addr]=k.split(":");
const mx=Math.max(1,...["A","B","C","D"].map(g=>Math.abs(+o[g]||0)));
return `<div class="item"><b>${esc(o.sym||"?")}</b> <span class="mut">${esc(chain)}</span>
<span style="float:right"><a class="a" href="${link(chain,addr)}" target="_blank">chart ↗</a></span>
${["A","B","C","D"].map(g=>{const v=+o[g]||0,pct=Math.abs(v)/mx*46;
return `<div style="display:flex;align-items:center;gap:6px;margin:3px 0"><span class="mut" style="width:14px">${g}</span>
<div style="width:47%;background:#0a0e14;border-radius:5px;position:relative;height:11px">
<div class="bar g${g}" style="width:${pct}%;${v<0?"background:var(--red)":""}"></div></div>
<span class="${v>=0?"up":"dn"}">${v>=0?"+":""}${usd(v)}</span></div>`}).join("")}</div>`}).join("")||"<i>belum ada arus grup terekam</i>";
let ftype="SEMUA";
document.querySelectorAll(".chip").forEach(ch=>ch.onclick=()=>{document.querySelectorAll(".chip").forEach(c=>c.classList.remove("on"));ch.classList.add("on");ftype=ch.dataset.f;renderFeed()});
$("#q").oninput=renderFeed;
function bodyHtml(b){return esc(b||"").replace(/(https?:\/\/[^\s]+)/g,'<a class="a" href="$1" target="_blank">$1</a>').replace(/\n/g,"<br>")}
function renderFeed(){const q=($("#q").value||"").toLowerCase();
const rows=al.slice().reverse().filter(a=>(ftype==="SEMUA"||a.type===ftype)&&
(!q||((a.title||"")+(a.body||"")).toLowerCase().includes(q)));
$("#feed").innerHTML=rows.slice(0,400).map(a=>`<div class="item ${a.type==="SHIFT"?"shift":""}">
<span class="badge b-${a.type}">${a.type}</span> <b>${esc(a.title||"")}</b>
<div class="t">${esc(a.ts||"")}</div><div>${bodyHtml(a.body)}</div></div>`).join("")||"<i class='mut'>tidak ada alert yang cocok</i>"}
renderFeed();
const wl=DATA.config.watchlist||[];
$("#wlist").innerHTML="<tr><th>wallet</th><th>chain</th><th>note</th><th></th></tr>"+
(wl.map(w=>`<tr><td>${esc(w.address)}</td><td>${esc(w.chain||"auto")}</td><td class="mut">${esc(w.note||"")}</td>
<td><a class="a" href="https://gmgn.ai/${w.chain||"sol"}/wallet/${w.address}" target="_blank">gmgn ↗</a></td></tr>`).join("")||"<tr><td class='mut'>kosong</td></tr>");
const gs=DATA.groups||{},ks=Object.keys(gs);
$("#gsnap").innerHTML=ks.length?ks.map(k=>{const v=gs[k],g=v.groups||{};
return `<div class="item"><b>${esc(k)}</b> <span class="mut">snapshot ${esc(v.ts||"")}</span><br>
${Object.entries(g).map(([gr,net])=>`<span class="${net>=0?"up":"dn"}">${gr}: ${net>=0?"+":""}${usd(net)}</span>`).join(" · ")}</div>`}).join(""):"belum ada snapshot (jalankan: python scripts/wt.py groups 0xTOKEN)";
</script></body></html>"""


def cmd_html(args):
    """Generate dashboard HTML dari seluruh data lokal (aktual)."""
    cfg = load_config()
    alerts = []
    if ALERTS_LOG.exists():
        for ln in ALERTS_LOG.read_text(encoding="utf-8").splitlines():
            try:
                alerts.append(json.loads(ln))
            except Exception:
                pass
    watch_state = load_json(WATCH_STATE, {})
    payload = {
        "generated": now_iso(),
        "config": {"chains": cfg.get("chains", []), "watchlist": cfg.get("watchlist", [])},
        "alerts": alerts[-max(0, args.max_alerts):],
        "gflow": watch_state.get("gflow", {}),
        "groups": load_json(GROUPS_STATE, {}),
    }
    blob = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")
    out = DATA_DIR / "dashboard.html"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(HTML_TEMPLATE.replace("__DATA__", blob), encoding="utf-8")
    log(f"[ok] dashboard {len(alerts)} alert -> {out}")


# ---------------------------------------------------------------- COLONY: database smart wallet + side wallet
COLONY_STATE = DATA_DIR / "state_colony.json"


def harvest_makers():
    """Kumpulkan wallet unik dari alert TRADE/WALLET (database lokal kita)."""
    rows = {}
    if not ALERTS_LOG.exists():
        return rows
    for ln in ALERTS_LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("type") not in ("TRADE", "WALLET"):
            continue
        d = r.get("data") or {}
        addr = (d.get("maker") or d.get("wallet") or "").strip()
        if not addr:
            continue
        m = re.search(r"\$([\d.,]+)\s*([kM]?)\b", r.get("title") or "")
        usd = float(m.group(1).replace(",", "")) * {"k": 1e3, "M": 1e6}.get(m.group(2), 1) if m else 0.0
        t = re.search(r"\[([a-z_,]{3,60})\]", r.get("body") or "")
        cm = re.search(r"\b(sol|bsc|base|eth|robinhood|arc|stable)\b", (r.get("body") or "").lower())
        e = rows.setdefault(addr, {"n": 0, "usd": 0.0, "tags": set(), "chain": cm.group(1) if cm else None})
        e["n"] += 1
        e["usd"] += usd
        if t:
            e["tags"].update(x for x in t.group(1).split(",") if x and x != "gmgn")
    return rows


def cmd_colony(args):
    cfg = load_config()
    default_ch = cfg["chains"][0] if cfg["chains"] else "robinhood"
    log(f"# Database smart wallet & peta koloni — {now_iso()}")
    rows = harvest_makers()
    if not rows:
        log("Belum ada alert TRADE/WALLET tersimpan (jalankan watch dulu).")
        return
    ranked = sorted(rows.items(), key=lambda kv: (-kv[1]["n"], -kv[1]["usd"]))[:args.top]

    profiles = []
    for addr, e in ranked:
        ch = e["chain"] or default_ch
        try:
            st = fetch_stats(addr, ch, args.api_key)
        except Exception as ex:
            log(f"[warn] stats {short(addr, 6)} gagal: {ex}")
            continue
        ps = st.get("pnl_stat") or {}
        p = {"address": addr, "chain": ch, "n_alert": e["n"], "vol": e["usd"],
             "tags": sorted(e["tags"] | set(common_of(st).get("tags") or [])),
             "pnl": float(dig(st, "realized_profit", default=0) or 0),
             "winrate": float(ps.get("winrate") or 0) * 100,
             "hold_h": float(ps.get("avg_holding_period") or 0) / 3600,
             "n_tok": int(ps.get("token_num") or 0),
             "fund": common_of(st).get("fund_from_address") or ""}
        profiles.append(p)

    profiles.sort(key=lambda p: -p["pnl"])
    log(f"\n## DATABASE — {len(profiles)} wallet profil lengkap (dari {len(rows)} wallet unik)")
    log("```\nwallet          alert   vol     PnL       win  hold   token  tags")
    for p in profiles:
        log(f'{short(p["address"], 6):<15} {p["n_alert"]:>5} {usd(p["vol"]):>7} '
            f'{usd(p["pnl"]):>9} {p["winrate"]:>4.0f}% {p["hold_h"]:>5.1f}h {p["n_tok"]:>5}  '
            f'{",".join(p["tags"][:3])}')
    log("```")

    # --- deep: activity -> transfer -> side wallet candidates
    log(f"\n## SIDE WALLET & KOLONI (deep {args.deep} wallet PnL terbaik)")

    def wkey(a):
        """Kunci cluster: EVM dinormalisasi lowercase, Solana base58 case-sensitive."""
        return a.lower() if a.startswith("0x") else a

    uf = {wkey(p["address"]): wkey(p["address"]) for p in profiles}

    def find(x):
        x = wkey(x)
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            uf[rb] = ra

    all_transfers = {}
    for p in profiles[:args.deep]:
        addr = p["address"]
        try:
            acts = fetch_activity(addr, p["chain"], 20, args.api_key, pages=args.pages)
        except Exception as ex:
            log(f"[warn] activity {short(addr, 6)} gagal: {ex}")
            continue
        _, transfers = analyze_token_flows(acts)
        all_transfers[addr] = transfers
        sw = side_wallets(transfers, addr)
        cand = [s for s in sw if s["n"] >= 2][:args.cand]
        line = f'- {short(addr, 6)} (PnL {usd(p["pnl"])}):'
        if p["fund"]:
            line += f' funder {short(p["fund"], 5)};'
        if cand:
            line += " kandidat side wallet: " + "; ".join(
                f'{short(s["address"], 5)} ({s["n"]}x {usd(s["usd"])})' for s in cand)
        if not p["fund"] and not cand:
            line += " belum ada jejak side wallet di window ini"
        log(line)
        for s in cand:
            union(addr, s["address"])

    # --- gabungkan klaster: funder sama = satu koloni
    funded = {}
    for p in profiles:
        if p["fund"]:
            funded.setdefault(wkey(p["fund"]), []).append(p["address"])
    for members in funded.values():
        for w in members[1:]:
            union(members[0], w)

    clusters = {}
    for p in profiles:
        clusters.setdefault(find(p["address"]), []).append(p)
    solo = [c for c in clusters.values() if len(c) == 1]
    multi = sorted((c for c in clusters.values() if len(c) > 1), key=len, reverse=True)

    log(f"\n## PETA KOLONI")
    for i, c in enumerate(multi, 1):
        tot = sum(x["pnl"] for x in c)
        log(f'- Koloni {i}: {len(c)} wallet terhubung, PnL gabungan {usd(tot)} — '
            + ", ".join(f"{short(x['address'], 5)} ({usd(x['pnl'])})" for x in c))
    if solo:
        log(f"- {len(solo)} wallet berdiri sendiri (belum ada jejak hubungan)")

    save = {"ts": now_iso(), "profiles": profiles,
            "colonies": [[x["address"] for x in c] for c in multi]}
    save_json(COLONY_STATE, save)
    log(f"\n[done] tersimpan: {COLONY_STATE.name}")


def main():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="wt", description="Wallet Tracker (GMGN-powered)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="scan token baru/momentum berpotensi jadi runner")
    p.add_argument("--chains", help="mis. robinhood,bsc,sol")
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--api-key")
    p.add_argument("--source", choices=["gmgn", "geckoterminal"], default="gmgn")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("watch", help="alert trade smart money + wallet watchlist")
    p.add_argument("--chains")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=120)
    p.add_argument("--init", action="store_true", help="set baseline tanpa alert")
    p.add_argument("--api-key")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("analyze", help="breakdown wallet: inflow/outflow, side wallet, logika")
    p.add_argument("wallet")
    p.add_argument("--chain")
    p.add_argument("--limit", type=int, default=100, help="jumlah event aktivitas")
    p.add_argument("--deep", action="store_true", help="sekalian cek stats side wallet/funder")
    p.add_argument("--api-key")
    p.set_defaults(fn=cmd_analyze)

    p = sub.add_parser("groups", help="perilaku grup A/B/C/D pada satu token + deteksi shift")
    p.add_argument("token")
    p.add_argument("--chain")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--stats-budget", type=int, default=6, help="maks panggilan stats utk pisah B/C")
    p.add_argument("--min-shift", type=float, default=1500, help="ambang USD deteksi shift")
    p.add_argument("--api-key")
    p.set_defaults(fn=cmd_groups)

    p = sub.add_parser("colony", help="database smart wallet + peta side wallet/koloni dari alert tersimpan")
    p.add_argument("--top", type=int, default=8, help="wallet paling aktif yang diprofilkan")
    p.add_argument("--deep", type=int, default=3, help="wallet PnL terbaik yang dianalisis aktivitasnya")
    p.add_argument("--pages", type=int, default=2, help="halaman activity per wallet deep")
    p.add_argument("--cand", type=int, default=3, help="kandidat side wallet per wallet")
    p.add_argument("--api-key")
    p.set_defaults(fn=cmd_colony)

    p = sub.add_parser("html", help="generate dashboard HTML dari data aktual lokal")
    p.add_argument("--max-alerts", type=int, default=600)
    p.set_defaults(fn=cmd_html)

    p = sub.add_parser("watchlist", help="kelola daftar wallet yang dipantau")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("address", nargs="?")
    p.add_argument("--chain")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_watchlist)

    p = sub.add_parser("alerts", help="riwayat alert")
    p.add_argument("--last", type=int, default=20)
    p.set_defaults(fn=cmd_alerts)

    p = sub.add_parser("test-notify", help="tes koneksi alert (console/telegram)")
    p.set_defaults(fn=cmd_test_notify)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
