"""
Harvest Farm — Kebun Kripto (v1)
Game pertanian Telegram Mini App berbasis FaucetPay (DOGE).

Pola mengikuti ~/workspace/telegram-miner-game/bot.py:
- Bot Telegram (python-telegram-bot 21.*) — /start buka Mini App.
- SQLite server-side (farm_pay.db) — semua saldo/state di server.
- HTTP API kecil (ThreadingHTTPServer, stdlib) di $PORT.
- Withdraw manual + otomatis FaucetPay API v1 (server-side saja).

Ekonomi v1 (ubah di CONFIG di bawah, bukan di kode lain):
- Mata uang: Koin Panen. Kurs WD: 200.000 Koin = 0,01 DOGE.
- Min WD 200.000 Koin, min Level 5, maks 1x per 7 hari.
- Harga pasar NPC berubah tiap 30 menit (+/-30%, deterministik).

Yang harus diisi sebelum jalan (env, JANGAN tulis di file ini):
- BOT_TOKEN        : token dari @BotFather
- WEBAPP_URL       : URL publik index.html (mis. GitHub Pages)
- FAUCETPAY_API_KEY: API key FaucetPay (v1) — kosong = WD pending manual
- ADSGRAM_BLOCK_ID : Block ID dari partner.adsgram.ai (dipakai frontend,
                     backend hanya memberi tahu frontend aktif/tidak)
- WITHDRAW_ADMIN_KEY: kunci admin untuk approve WD
- PORT             : port HTTP (Railway mengisi otomatis)

Jalankan:
    pip install -r requirements.txt
    BOT_TOKEN=xxxx WEBAPP_URL=https://<user>.github.io/harvest-farm/ python bot.py
"""

import hashlib
import hmac
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("harvestfarm")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get(
    "WEBAPP_URL", "https://example.github.io/harvest-farm/"
)
ADSGRAM_BLOCK_ID = os.environ.get("ADSGRAM_BLOCK_ID", "YOUR_ADSGRAM_BLOCK_ID")
FAUCETPAY_API_KEY = os.environ.get("FAUCETPAY_API_KEY", "")
WITHDRAW_ADMIN_KEY = os.environ.get("WITHDRAW_ADMIN_KEY", "")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "farm_pay.db")

# ---------------------------------------------------------------------------
# CONFIG — ekonomi & aturan (satu tempat)
# ---------------------------------------------------------------------------
COINS_PER_UNIT = 200_000      # Koin per 1 unit WD
DOGE_PER_UNIT = 0.01          # DOGE per 1 unit WD
DOGE_SMALLEST_PER_DOGE = 100_000_000  # 1 DOGE = 1e8 unit terkecil (v1 send)
WITHDRAW_MIN_COINS = 200_000
WITHDRAW_MIN_LEVEL = 5
WITHDRAW_COOLDOWN_SECS = 7 * 86400   # maks 1x per 7 hari
WITHDRAW_CURRENCY = "DOGE"

ENERGY_MAX = 50
ENERGY_REGEN_SECS = 120       # 1 energi per 2 menit
ENERGY_COST_ACTION = 1        # tanam/siram/panen/pupuk = 1 energi

GRID_PRICES = {3: 0, 4: 5_000, 5: 20_000}  # ukuran grid -> harga upgrade
FERTILIZER_PRICE = 300        # pupuk: percepat 50%
FEED_PRICE = 100              # pakan hewan (1x makan = kenyang 12 jam)
FEED_DURATION_SECS = 12 * 3600

AD_REWARD_COOLDOWN_SECS = 5 * 60
AD_REWARD_MAX_PER_DAY = 10
AD_ENERGY_BONUS = 20

# Anti-cheat kasar
MAX_COINS_DELTA_PER_ACTION = 50_000
MIN_ACTION_INTERVAL_SECS = 5
MAX_SELL_QTY = 10_000

START_COINS = 500
START_SEEDS = {"cabai": 5, "jagung": 3}

# Tanaman: id -> (nama, emoji, harga benih, waktu tumbuh detik,
#                 hasil panen, XP, harga jual dasar per buah)
CROPS = {
    "cabai":    ("Cabai",    "🌶️", 50,    2 * 60,   4,  10, 30),
    "jagung":   ("Jagung",   "🌽", 120,   5 * 60,   5,  15, 50),
    "tomat":    ("Tomat",    "🍅", 250,   10 * 60,  6,  25, 80),
    "wortel":   ("Wortel",   "🥕", 400,   15 * 60,  6,  35, 110),
    "padi":     ("Padi",     "🌾", 800,   30 * 60,  8,  60, 180),
    "labu":     ("Labu",     "🎃", 1500,  60 * 60,  8,  100, 350),
    "stroberi": ("Stroberi", "🍓", 3000,  120 * 60, 10, 180, 600),
    "kopi":     ("Kopi",     "☕", 6000,  240 * 60, 12, 300, 1000),
}
# Harga jual dasar produk hewan (per buah)
ANIMAL_PRODUCTS = {
    "telur": ("Telur", "🥚", 80),
    "susu":  ("Susu",  "🥛", 250),
}
# Hewan: kind -> (nama, emoji, harga, produk, interval produksi detik, pakan)
ANIMALS = {
    "ayam":   ("Ayam",   "🐔", 500,  "telur", 30 * 60,  1),
    "kambing": ("Kambing", "🐐", 2000, "susu", 60 * 60,  1),
    "sapi":   ("Sapi",   "🐄", 5000, "susu",  120 * 60, 1),
}

WEATHERS = {
    "cerah": ("Cerah", "☀️", 1.00, "Tumbuh normal."),
    "hujan": ("Hujan", "🌧️", 0.70, "Tumbuh +30% lebih cepat, gratis siram!"),
    "badai": ("Badai", "⛈️", 1.25, "Tumbuh -20%. Panen cepat sebelum rusak!"),
    "panas": ("Panas", "🔥", 1.00, "Butuh siram 2x supaya tumbuh normal."),
}
WEATHER_CYCLE = ["cerah", "hujan", "cerah", "panas", "cerah", "badai"]

# Acara acak (deterministik per slot 2 jam, biar semua pemain sama)
EVENTS = {
    "panen_raya": ("Panen Raya", "🎉", "Hasil panen 2x lipat selama 10 menit!"),
    "tikus":      ("Tikus Sawah", "🐀", "Hama lebih sering muncul — awasi kebunmu!"),
    "pasar_ramai": ("Pasar Ramai", "🛒", "Harga jual NPC +15% selama 10 menit!"),
    None:         (None, "", ""),
}

MISSIONS_POOL = [
    ("panen", "Panen 10 tanaman", 10, 500),
    ("jual", "Jual 5 hasil panen di Pasar NPC", 5, 400),
    ("hama", "Usir hama 3 kali", 3, 300),
    ("tanam", "Tanam 8 benih", 8, 300),
    ("siram", "Siram tanaman 10 kali", 10, 250),
    ("p2p", "Pasang 1 jualan di Pasar Pemain", 1, 400),
    ("hewan", "Kumpulkan hasil hewan 3 kali", 3, 400),
]

ACHIEVEMENTS = [
    (1, "🌱 Petani Pemula", "Capai Level 1"),
    (5, "🚜 Petani Andal", "Capai Level 5"),
    (10, "👑 Juragan Kebun", "Capai Level 10"),
    (20, "🌾 Legenda Panen", "Capai Level 20"),
]

FP_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FP_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{2,31}$")

_last_action: dict[int, float] = {}


# ---------------------------------------------------------------------------
# Util waktu / cuaca / pasar (deterministik dari jam — semua pemain sama)
# ---------------------------------------------------------------------------
def now_ts() -> int:
    return int(time.time())


def weather_now(ts: int | None = None) -> dict:
    """Cuaca berganti tiap jam, deterministik dari nomor jam (UTC)."""
    ts = ts if ts is not None else now_ts()
    hour = ts // 3600
    kind = WEATHER_CYCLE[hour % len(WEATHER_CYCLE)]
    label, emoji, factor, desc = WEATHERS[kind]
    ends_in = 3600 - (ts % 3600)
    return {"kind": kind, "label": label, "emoji": emoji,
            "factor": factor, "desc": desc, "ends_in": ends_in}


def event_now(ts: int | None = None) -> dict | None:
    """Acara 10 menit pertama tiap slot 2 jam (jam genap)."""
    ts = ts if ts is not None else now_ts()
    slot = ts // 7200
    minute_in_slot = (ts % 7200) // 60
    if minute_in_slot >= 10:
        return None
    keys = ["panen_raya", "pasar_ramai", "tikus"]
    key = keys[slot % len(keys)]
    label, emoji, desc = EVENTS[key]
    return {"id": key, "label": label, "emoji": emoji, "desc": desc,
            "ends_in": (10 - minute_in_slot) * 60 - (ts % 60)}


def market_price(item: str, ts: int | None = None) -> dict:
    """Harga NPC berubah tiap 30 menit, naik/turun +/-30% (deterministik)."""
    ts = ts if ts is not None else now_ts()
    bucket = ts // 1800
    if item in CROPS:
        base = CROPS[item][6]
    elif item in ANIMAL_PRODUCTS:
        base = ANIMAL_PRODUCTS[item][2]
    else:
        return {"price": 0, "base": 0, "trend": "stabil", "change_pct": 0}
    # Hash stabil dari (bucket, item) -> -30%..+30%
    h = int(hashlib.sha256(f"{bucket}:{item}".encode()).hexdigest()[:8], 16)
    change = (h % 61) - 30  # -30..+30
    price = max(1, round(base * (100 + change) / 100))
    ev = event_now(ts)
    if ev and ev["id"] == "pasar_ramai":
        price = round(price * 1.15)
        change += 15
    trend = "naik" if change >= 5 else ("turun" if change <= -5 else "stabil")
    return {"price": price, "base": base, "trend": trend, "change_pct": change}


def level_from_xp(xp: int) -> int:
    return int((max(xp, 0) / 100) ** 0.5) + 1


def xp_for_level(level: int) -> int:
    return (level - 1) ** 2 * 100


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "  user_id INTEGER PRIMARY KEY,"
        "  username TEXT, first_name TEXT,"
        "  coins INTEGER NOT NULL DEFAULT 0,"
        "  xp INTEGER NOT NULL DEFAULT 0,"
        "  energy INTEGER NOT NULL DEFAULT 50,"
        "  energy_at INTEGER NOT NULL DEFAULT 0,"
        "  grid_size INTEGER NOT NULL DEFAULT 3,"
        "  fertilizer INTEGER NOT NULL DEFAULT 0,"
        "  feed INTEGER NOT NULL DEFAULT 0,"
        "  double_next INTEGER NOT NULL DEFAULT 0,"
        "  inventory TEXT NOT NULL DEFAULT '{}',"
        "  seeds TEXT NOT NULL DEFAULT '{}',"
        "  login_streak INTEGER NOT NULL DEFAULT 0,"
        "  last_login TEXT NOT NULL DEFAULT '',"
        "  total_harvest INTEGER NOT NULL DEFAULT 0,"
        "  total_sold INTEGER NOT NULL DEFAULT 0,"
        "  flagged INTEGER NOT NULL DEFAULT 0,"
        "  flag_reason TEXT,"
        "  created_at INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS plots ("
        "  user_id INTEGER NOT NULL, idx INTEGER NOT NULL,"
        "  crop TEXT, planted_at INTEGER NOT NULL DEFAULT 0,"
        "  water_count INTEGER NOT NULL DEFAULT 0,"
        "  fertilized INTEGER NOT NULL DEFAULT 0,"
        "  pest_at INTEGER NOT NULL DEFAULT 0,"
        "  pest_damage INTEGER NOT NULL DEFAULT 0,"
        "  speed_boost_until INTEGER NOT NULL DEFAULT 0,"
        "  PRIMARY KEY (user_id, idx))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS animals ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  user_id INTEGER NOT NULL, kind TEXT NOT NULL,"
        "  fed_until INTEGER NOT NULL DEFAULT 0,"
        "  last_produce_at INTEGER NOT NULL DEFAULT 0,"
        "  created_at INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS listings ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  seller_id INTEGER NOT NULL, seller_name TEXT,"
        "  item TEXT NOT NULL, qty INTEGER NOT NULL,"
        "  price_each INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open',"
        "  created_at INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS market_prices ("
        "  item TEXT PRIMARY KEY, price INTEGER NOT NULL,"
        "  updated_at INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS missions ("
        "  user_id INTEGER NOT NULL, mission_id TEXT NOT NULL,"
        "  day TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,"
        "  claimed INTEGER NOT NULL DEFAULT 0,"
        "  PRIMARY KEY (user_id, mission_id, day))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS withdrawals ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  user_id INTEGER NOT NULL, username TEXT,"
        "  coins INTEGER NOT NULL, doge REAL NOT NULL,"
        "  destination TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'DOGE',"
        "  status TEXT NOT NULL DEFAULT 'pending',"
        "  fp_response TEXT,"
        "  created_at INTEGER NOT NULL DEFAULT 0,"
        "  decided_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ad_rewards ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  user_id INTEGER NOT NULL, type TEXT NOT NULL,"
        "  created_at INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    return conn


def _json_loads(raw: str, default):
    try:
        v = json.loads(raw or "")
        return v if isinstance(v, type(default)) else default
    except (ValueError, TypeError):
        return default


def get_or_create_user(conn: sqlite3.Connection, user: dict) -> sqlite3.Row:
    uid = int(user["id"])
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if row is None:
        conn.execute(
            "INSERT INTO users (user_id, username, first_name, coins, xp,"
            "  energy, energy_at, seeds, login_streak, last_login, created_at)"
            " VALUES (?, ?, ?, ?, 0, ?, ?, ?, 1, ?, ?)",
            (uid, user.get("username"), user.get("first_name", ""),
             START_COINS, ENERGY_MAX, now_ts(),
             json.dumps(START_SEEDS), today, now_ts()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
        for i in range(9):
            conn.execute(
                "INSERT OR IGNORE INTO plots (user_id, idx) VALUES (?, ?)",
                (uid, i))
        conn.commit()
        return row
    # Bonus login harian: streak naik kalau hari baru
    if row["last_login"] != today:
        try:
            last = datetime.strptime(row["last_login"], "%Y-%m-%d").date()
            cur = datetime.strptime(today, "%Y-%m-%d").date()
            streak = row["login_streak"] + 1 if (cur - last).days == 1 else 1
        except (ValueError, TypeError):
            streak = 1
        bonus = min(streak, 7) * 100  # hari ke-7 = 700 koin
        conn.execute(
            "UPDATE users SET last_login = ?, login_streak = ?,"
            "  coins = coins + ? WHERE user_id = ?",
            (today, streak, bonus, uid))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
    return row


def regen_energy(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    """Regen energi 1 per 2 menit (server-side, dari energy_at)."""
    now = now_ts()
    gained = (now - int(row["energy_at"] or now)) // ENERGY_REGEN_SECS
    if gained > 0 and row["energy"] < ENERGY_MAX:
        new_e = min(ENERGY_MAX, row["energy"] + gained)
        conn.execute(
            "UPDATE users SET energy = ?, energy_at = ? WHERE user_id = ?",
            (new_e, now if new_e >= ENERGY_MAX else
             int(row["energy_at"]) + gained * ENERGY_REGEN_SECS, row["user_id"]))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (row["user_id"],)).fetchone()
    return row


def spend_energy(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[bool, sqlite3.Row]:
    row = regen_energy(conn, row)
    if row["energy"] < ENERGY_COST_ACTION:
        return False, row
    conn.execute(
        "UPDATE users SET energy = energy - 1, energy_at = ? WHERE user_id = ?",
        (now_ts(), row["user_id"]))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?", (row["user_id"],)).fetchone()
    return True, row


def add_xp_coins(conn: sqlite3.Connection, uid: int, xp: int = 0,
                 coins: int = 0) -> None:
    conn.execute("UPDATE users SET xp = xp + ?, coins = coins + ?"
                 " WHERE user_id = ?", (xp, coins, uid))
    conn.commit()


def bump_mission(conn: sqlite3.Connection, uid: int, kind: str, n: int = 1) -> None:
    """Naikkan progress misi harian yang cocok dengan jenis aksi."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT mission_id FROM missions WHERE user_id = ? AND day = ?",
        (uid, today)).fetchall()
    for r in rows:
        if r["mission_id"].startswith(kind):
            conn.execute(
                "UPDATE missions SET progress = progress + ?"
                " WHERE user_id = ? AND mission_id = ? AND day = ?",
                (n, uid, r["mission_id"], today))
    conn.commit()


def daily_missions(conn: sqlite3.Connection, uid: int) -> list[dict]:
    """3 misi acak per hari (seed dari tanggal+user)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rng = random.Random(f"{today}:{uid}")
    picked = rng.sample(MISSIONS_POOL, 3)
    out = []
    for i, (kind, title, target, reward) in enumerate(picked):
        mid = f"{kind}_{i}"
        conn.execute(
            "INSERT OR IGNORE INTO missions (user_id, mission_id, day)"
            " VALUES (?, ?, ?)", (uid, mid, today))
        r = conn.execute(
            "SELECT progress, claimed FROM missions"
            " WHERE user_id = ? AND mission_id = ? AND day = ?",
            (uid, mid, today)).fetchone()
        out.append({"id": mid, "kind": kind, "title": title, "target": target,
                    "progress": min(r["progress"], target),
                    "done": r["progress"] >= target,
                    "claimed": bool(r["claimed"]), "reward": reward})
    conn.commit()
    return out


# ---------------------------------------------------------------------------
# State game (server-side — pertumbuhan, hama, hewan, pasar)
# ---------------------------------------------------------------------------
def plot_view(conn: sqlite3.Connection, uid: int, w: dict,
              ev: dict | None) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM plots WHERE user_id = ? ORDER BY idx", (uid,)).fetchall()
    now = now_ts()
    out = []
    for r in rows:
        v: dict = {"idx": r["idx"], "crop": r["crop"], "crop_name": None,
                   "emoji": None, "planted_at": r["planted_at"],
                   "grow_secs": 0, "progress": 0.0, "ready": False,
                   "watered": r["water_count"] > 0,
                   "water_count": r["water_count"],
                   "fertilized": bool(r["fertilized"]),
                   "pest": False, "pest_expires_in": 0, "yield_qty": 0}
        if not r["crop"]:
            out.append(v)
            continue
        name, emoji, _seed, grow, yld, _xp, _base = CROPS[r["crop"]]
        v.update({"crop_name": name, "emoji": emoji, "grow_secs": grow,
                  "yield_qty": yld})
        # Faktor tumbuh: cuaca x siram (0.75) x pupuk (0.5) x boost iklan
        factor = w["factor"]
        if w["kind"] == "hujan":
            v["watered"] = True  # hujan = gratis siram
        elif w["kind"] == "panas" and r["water_count"] < 2:
            factor *= 1.5  # panas tanpa siram 2x = tumbuh lambat
        if r["water_count"] > 0 and w["kind"] != "hujan":
            factor *= 0.75
        if r["fertilized"]:
            factor *= 0.5
        if r["speed_boost_until"] and now < r["speed_boost_until"]:
            factor *= 0.5
        eff = grow * factor
        elapsed = now - int(r["planted_at"] or now)
        v["progress"] = max(0.0, min(1.0, elapsed / eff if eff else 1.0))
        v["ready"] = v["progress"] >= 1.0
        v["grow_secs"] = int(eff)
        # Hama: muncul acak di petak yang tumbuh (cek tiap state call).
        if r["pest_at"]:
            left = int(r["pest_at"]) + 60 - now
            if left <= 0:
                # Dibiarkan 60 detik -> hasil panen -50%
                conn.execute(
                    "UPDATE plots SET pest_at = 0, pest_damage = 1"
                    " WHERE user_id = ? AND idx = ?", (uid, r["idx"]))
                conn.commit()
                v["pest"] = False
            else:
                v["pest"] = True
                v["pest_expires_in"] = left
        elif not v["ready"] and elapsed > 60 and random.random() < 0.12:
            conn.execute(
                "UPDATE plots SET pest_at = ? WHERE user_id = ? AND idx = ?",
                (now, uid, r["idx"]))
            conn.commit()
            v["pest"] = True
            v["pest_expires_in"] = 60
        if r["pest_damage"]:
            v["yield_qty"] = max(1, yld // 2)
        out.append(v)
    return out


def animal_view(conn: sqlite3.Connection, uid: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM animals WHERE user_id = ? ORDER BY id", (uid,)).fetchall()
    now = now_ts()
    out = []
    for r in rows:
        name, emoji, _price, product, interval, _feed = ANIMALS[r["kind"]]
        pname, pem, _pb = ANIMAL_PRODUCTS[product]
        fed = now < int(r["fed_until"] or 0)
        last = int(r["last_produce_at"] or r["created_at"] or now)
        ready = fed and (now - last) >= interval
        out.append({
            "id": r["id"], "kind": r["kind"], "name": name, "emoji": emoji,
            "produce": product, "produce_name": pname, "produce_emoji": pem,
            "fed": fed, "fed_until": r["fed_until"],
            "ready": ready,
            "produce_in": 0 if ready else max(0, interval - (now - last)),
        })
    return out


def build_state(conn: sqlite3.Connection, user: dict) -> dict:
    row = regen_energy(conn, get_or_create_user(conn, user))
    uid = int(user["id"])
    w = weather_now()
    ev = event_now()
    plots = plot_view(conn, uid, w, ev)
    animals = animal_view(conn, uid)
    inventory = _json_loads(row["inventory"], {})
    seeds = _json_loads(row["seeds"], {})
    missions = daily_missions(conn, uid)
    items = list(CROPS) + list(ANIMAL_PRODUCTS)
    market = {it: market_price(it) for it in items}
    listings = conn.execute(
        "SELECT id, seller_id, seller_name, item, qty, price_each,"
        "  created_at FROM listings WHERE status = 'open'"
        " ORDER BY created_at DESC LIMIT 30").fetchall()
    withdrawals = conn.execute(
        "SELECT id, coins, doge, destination, status, created_at"
        " FROM withdrawals WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
        (uid,)).fetchall()
    xp = int(row["xp"])
    level = level_from_xp(xp)
    ach = [{"id": lv, "title": t, "desc": d, "unlocked": level >= lv}
           for lv, t, d in ACHIEVEMENTS]
    return {
        "user": {
            "user_id": uid, "username": row["username"],
            "first_name": row["first_name"],
            "coins": int(row["coins"]), "xp": xp, "level": level,
            "xp_next": xp_for_level(level + 1),
            "energy": int(row["energy"]), "energy_max": ENERGY_MAX,
            "grid_size": int(row["grid_size"]),
            "fertilizer": int(row["fertilizer"]), "feed": int(row["feed"]),
            "double_next": bool(row["double_next"]),
            "login_streak": int(row["login_streak"]),
            "total_harvest": int(row["total_harvest"]),
            "total_sold": int(row["total_sold"]),
            "ad_rewards_today": ad_count_today(conn, uid),
            "flagged": bool(row["flagged"]),
        },
        "plots": plots,
        "animals": animals,
        "inventory": inventory,
        "seeds": seeds,
        "missions": missions,
        "achievements": ach,
        "weather": w,
        "event": ev,
        "market": market,
        "listings": [{
            "id": r["id"], "seller_id": r["seller_id"],
            "seller_name": r["seller_name"] or "Petani",
            "item": r["item"],
            "item_name": (CROPS[r["item"]][0] if r["item"] in CROPS
                          else ANIMAL_PRODUCTS[r["item"]][0]),
            "emoji": (CROPS[r["item"]][1] if r["item"] in CROPS
                      else ANIMAL_PRODUCTS[r["item"]][1]),
            "qty": r["qty"], "price_each": r["price_each"],
            "total": r["qty"] * r["price_each"],
            "created_at": r["created_at"],
        } for r in listings],
        "withdrawals": [{
            "id": r["id"], "coins": r["coins"], "doge": r["doge"],
            "destination": r["destination"], "status": r["status"],
            "created_at": r["created_at"],
        } for r in withdrawals],
        "config": {
            "coins_per_unit": COINS_PER_UNIT, "doge_per_unit": DOGE_PER_UNIT,
            "min_withdraw_coins": WITHDRAW_MIN_COINS,
            "min_level_withdraw": WITHDRAW_MIN_LEVEL,
            "withdraw_cooldown_days": WITHDRAW_COOLDOWN_SECS // 86400,
            "withdraw_currency": WITHDRAW_CURRENCY,
            "energy_max": ENERGY_MAX,
            "ad_enabled": bool(ADSGRAM_BLOCK_ID
                               and ADSGRAM_BLOCK_ID != "YOUR_ADSGRAM_BLOCK_ID"),
            "grid_prices": GRID_PRICES,
            "fertilizer_price": FERTILIZER_PRICE,
            "feed_price": FEED_PRICE,
        },
    }


def ad_count_today(conn: sqlite3.Connection, uid: int) -> int:
    day_start = now_ts() - (now_ts() % 86400)
    row = conn.execute(
        "SELECT COUNT(*) FROM ad_rewards WHERE user_id = ? AND created_at >= ?",
        (uid, day_start)).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Aksi game
# ---------------------------------------------------------------------------
def do_action(conn: sqlite3.Connection, user: dict, body: dict) -> dict:
    uid = int(user["id"])
    action = str(body.get("action", ""))
    row = get_or_create_user(conn, user)
    if row["flagged"]:
        return {"error": "account_flagged"}
    now = time.time()
    if now - _last_action.get(uid, 0) < MIN_ACTION_INTERVAL_SECS:
        return {"error": "rate_limited"}
    _last_action[uid] = now

    # --- Aksi tanpa energi: beli, jual, klaim, pasar pemain ---
    if action == "buy_seed":
        crop = str(body.get("crop", ""))
        qty = int(body.get("qty", 1) or 1)
        if crop not in CROPS or qty < 1 or qty > 100:
            return {"error": "invalid_values"}
        cost = CROPS[crop][2] * qty
        if row["coins"] < cost:
            return {"error": "insufficient_coins"}
        seeds = _json_loads(row["seeds"], {})
        seeds[crop] = seeds.get(crop, 0) + qty
        conn.execute("UPDATE users SET coins = coins - ?, seeds = ?"
                     " WHERE user_id = ?", (cost, json.dumps(seeds), uid))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Beli {qty}x benih {CROPS[crop][0]} (-{cost} Koin)."}

    if action == "buy_animal":
        kind = str(body.get("kind", ""))
        if kind not in ANIMALS:
            return {"error": "invalid_values"}
        price = ANIMALS[kind][2]
        if row["coins"] < price:
            return {"error": "insufficient_coins"}
        conn.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?",
                     (price, uid))
        conn.execute(
            "INSERT INTO animals (user_id, kind, fed_until, last_produce_at,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (uid, kind, now_ts(), now_ts(), now_ts()))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"{ANIMALS[kind][1]} {ANIMALS[kind][0]} dibeli!"}

    if action == "buy_feed":
        qty = int(body.get("qty", 1) or 1)
        if qty < 1 or qty > 100:
            return {"error": "invalid_values"}
        cost = FEED_PRICE * qty
        if row["coins"] < cost:
            return {"error": "insufficient_coins"}
        conn.execute("UPDATE users SET coins = coins - ?, feed = feed + ?"
                     " WHERE user_id = ?", (cost, qty, uid))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Beli {qty}x Pakan (-{cost} Koin)."}

    if action == "buy_fertilizer":
        qty = int(body.get("qty", 1) or 1)
        if qty < 1 or qty > 100:
            return {"error": "invalid_values"}
        cost = FERTILIZER_PRICE * qty
        if row["coins"] < cost:
            return {"error": "insufficient_coins"}
        conn.execute("UPDATE users SET coins = coins - ?, fertilizer = fertilizer + ?"
                     " WHERE user_id = ?", (cost, qty, uid))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Beli {qty}x Pupuk (-{cost} Koin)."}

    if action == "upgrade_grid":
        cur = int(row["grid_size"])
        nxt = cur + 1
        if nxt not in GRID_PRICES:
            return {"error": "max_grid"}
        cost = GRID_PRICES[nxt]
        if row["coins"] < cost:
            return {"error": "insufficient_coins"}
        conn.execute("UPDATE users SET coins = coins - ?, grid_size = ?"
                     " WHERE user_id = ?", (cost, nxt, uid))
        for i in range(cur * cur, nxt * nxt):
            conn.execute("INSERT OR IGNORE INTO plots (user_id, idx)"
                         " VALUES (?, ?)", (uid, i))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Kebun diperluas jadi {nxt}x{nxt}! 🎉"}

    if action == "sell":  # jual ke Pasar NPC
        item = str(body.get("item", ""))
        qty = int(body.get("qty", 0) or 0)
        if item not in CROPS and item not in ANIMAL_PRODUCTS:
            return {"error": "invalid_values"}
        if qty < 1 or qty > MAX_SELL_QTY:
            return {"error": "invalid_values"}
        inv = _json_loads(row["inventory"], {})
        if inv.get(item, 0) < qty:
            return {"error": "insufficient_item"}
        price = market_price(item)["price"]
        total = price * qty
        if total > MAX_COINS_DELTA_PER_ACTION * 20:
            return {"error": "values_too_large"}
        inv[item] -= qty
        conn.execute("UPDATE users SET inventory = ?, coins = coins + ?,"
                     " total_sold = total_sold + ? WHERE user_id = ?",
                     (json.dumps(inv), total, qty, uid))
        conn.commit()
        bump_mission(conn, uid, "jual", qty)
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Terjual {qty}x {item} — +{total:,} Koin! 🪙"}

    if action == "create_listing":  # pasang jualan di Pasar Pemain
        item = str(body.get("item", ""))
        qty = int(body.get("qty", 0) or 0)
        price_each = int(body.get("price_each", 0) or 0)
        if item not in CROPS and item not in ANIMAL_PRODUCTS:
            return {"error": "invalid_values"}
        if qty < 1 or price_each < 1 or price_each > 100_000:
            return {"error": "invalid_values"}
        inv = _json_loads(row["inventory"], {})
        if inv.get(item, 0) < qty:
            return {"error": "insufficient_item"}
        inv[item] -= qty
        conn.execute("UPDATE users SET inventory = ? WHERE user_id = ?",
                     (json.dumps(inv), uid))
        conn.execute(
            "INSERT INTO listings (seller_id, seller_name, item, qty,"
            " price_each, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, row["first_name"] or row["username"] or "Petani",
             item, qty, price_each, now_ts()))
        conn.commit()
        bump_mission(conn, uid, "p2p", 1)
        return {"ok": True, "state": build_state(conn, user),
                "message": "Jualan dipasang di Pasar Pemain! 🛒"}

    if action == "buy_listing":
        lid = int(body.get("listing_id", 0) or 0)
        lst = conn.execute(
            "SELECT * FROM listings WHERE id = ? AND status = 'open'",
            (lid,)).fetchone()
        if not lst:
            return {"error": "listing_not_found"}
        if lst["seller_id"] == uid:
            return {"error": "own_listing"}
        total = lst["qty"] * lst["price_each"]
        if row["coins"] < total:
            return {"error": "insufficient_coins"}
        fee = total * 5 // 100
        seller_gain = total - fee
        conn.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?",
                     (total, uid))
        conn.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?",
                     (seller_gain, lst["seller_id"]))
        inv = _json_loads(row["inventory"], {})
        inv[lst["item"]] = inv.get(lst["item"], 0) + lst["qty"]
        conn.execute("UPDATE users SET inventory = ? WHERE user_id = ?",
                     (json.dumps(inv), uid))
        conn.execute("UPDATE listings SET status = 'sold' WHERE id = ?", (lid,))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Beli {lst['qty']}x {lst['item']} (-{total:,} Koin)."}

    if action == "claim_daily":
        streak = int(row["login_streak"])
        bonus = min(streak, 7) * 100
        add_xp_coins(conn, uid, xp=20, coins=bonus)
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Bonus login hari ke-{streak}: +{bonus} Koin, +20 XP! 🎁"}

    if action == "claim_mission":
        mid = str(body.get("mission_id", ""))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        missions = daily_missions(conn, uid)
        m = next((x for x in missions if x["id"] == mid), None)
        if not m or not m["done"] or m["claimed"]:
            return {"error": "mission_not_ready"}
        conn.execute("UPDATE missions SET claimed = 1"
                     " WHERE user_id = ? AND mission_id = ? AND day = ?",
                     (uid, mid, today))
        add_xp_coins(conn, uid, xp=30, coins=m["reward"])
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Misi selesai! +{m['reward']} Koin, +30 XP! ✅"}

    # --- Aksi pakai energi: tanam/siram/pupuk/panen/usir hama/hewan ---
    if action in ("plant", "water", "fertilize", "harvest", "pest",
                  "feed_animal", "collect_animal"):
        ok, row = spend_energy(conn, row)
        if not ok:
            return {"error": "no_energy"}
        return _energy_action(conn, user, row, action, body)

    return {"error": "unknown_action"}


def _energy_action(conn, user, row, action, body) -> dict:
    uid = int(user["id"])
    now = now_ts()
    if action in ("plant", "water", "fertilize", "harvest", "pest"):
        try:
            idx = int(body.get("plot", -1))
        except (TypeError, ValueError):
            return {"error": "invalid_values"}
        plot = conn.execute(
            "SELECT * FROM plots WHERE user_id = ? AND idx = ?",
            (uid, idx)).fetchone()
        if not plot:
            return {"error": "invalid_plot"}

    if action == "plant":
        crop = str(body.get("crop", ""))
        if crop not in CROPS:
            return {"error": "invalid_values"}
        if plot["crop"]:
            return {"error": "plot_occupied"}
        seeds = _json_loads(row["seeds"], {})
        if seeds.get(crop, 0) < 1:
            return {"error": "no_seed"}
        seeds[crop] -= 1
        conn.execute("UPDATE users SET seeds = ? WHERE user_id = ?",
                     (json.dumps(seeds), uid))
        conn.execute(
            "UPDATE plots SET crop = ?, planted_at = ?, water_count = 0,"
            " fertilized = 0, pest_at = 0, pest_damage = 0,"
            " speed_boost_until = 0 WHERE user_id = ? AND idx = ?",
            (crop, now, uid, idx))
        conn.commit()
        bump_mission(conn, uid, "tanam", 1)
        return {"ok": True, "state": build_state(conn, user),
                "message": f"{CROPS[crop][1]} {CROPS[crop][0]} ditanam!"}

    if action == "water":
        if not plot["crop"]:
            return {"error": "plot_empty"}
        w = weather_now()
        if w["kind"] == "hujan":
            return {"error": "already_watered"}
        if plot["water_count"] >= 2:
            return {"error": "already_watered"}
        conn.execute("UPDATE plots SET water_count = water_count + 1"
                     " WHERE user_id = ? AND idx = ?", (uid, idx))
        conn.commit()
        bump_mission(conn, uid, "siram", 1)
        return {"ok": True, "state": build_state(conn, user),
                "message": "💧 Disiram! Tumbuh 25% lebih cepat."}

    if action == "fertilize":
        if not plot["crop"]:
            return {"error": "plot_empty"}
        if plot["fertilized"]:
            return {"error": "already_fertilized"}
        if row["fertilizer"] < 1:
            return {"error": "no_fertilizer"}
        conn.execute("UPDATE users SET fertilizer = fertilizer - 1"
                     " WHERE user_id = ?", (uid,))
        conn.execute("UPDATE plots SET fertilized = 1"
                     " WHERE user_id = ? AND idx = ?", (uid, idx))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": "🌱 Pupuk diberikan! Tumbuh 50% lebih cepat."}

    if action == "harvest":
        if not plot["crop"]:
            return {"error": "plot_empty"}
        w = weather_now()
        ev = event_now()
        plots = plot_view(conn, uid, w, ev)
        v = next(p for p in plots if p["idx"] == idx)
        if not v["ready"]:
            return {"error": "not_ready"}
        qty = v["yield_qty"]
        mult = 1
        if row["double_next"]:
            mult *= 2
            conn.execute("UPDATE users SET double_next = 0 WHERE user_id = ?",
                         (uid,))
        if ev and ev["id"] == "panen_raya":
            mult *= 2
        qty *= mult
        if w["kind"] == "badai" and random.random() < 0.10:
            qty = max(1, qty // 2)  # badai merusak sebagian panen
        name, emoji, _s, _g, _y, xp, _b = CROPS[plot["crop"]]
        inv = _json_loads(row["inventory"], {})
        inv[plot["crop"]] = inv.get(plot["crop"], 0) + qty
        conn.execute(
            "UPDATE users SET inventory = ?, xp = xp + ?,"
            " total_harvest = total_harvest + 1 WHERE user_id = ?",
            (json.dumps(inv), xp, uid))
        conn.execute(
            "UPDATE plots SET crop = NULL, planted_at = 0, water_count = 0,"
            " fertilized = 0, pest_at = 0, pest_damage = 0,"
            " speed_boost_until = 0 WHERE user_id = ? AND idx = ?",
            (uid, idx))
        conn.commit()
        bump_mission(conn, uid, "panen", 1)
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Panen {qty}x {emoji} {name}! +{xp} XP 🧺"}

    if action == "pest":
        if not plot["pest_at"]:
            return {"error": "no_pest"}
        conn.execute("UPDATE plots SET pest_at = 0 WHERE user_id = ? AND idx = ?",
                     (uid, idx))
        conn.commit()
        bump_mission(conn, uid, "hama", 1)
        add_xp_coins(conn, uid, xp=5)
        return {"ok": True, "state": build_state(conn, user),
                "message": "🐛 Hama diusir! +5 XP"}

    if action == "feed_animal":
        aid = int(body.get("animal_id", 0) or 0)
        a = conn.execute("SELECT * FROM animals WHERE id = ? AND user_id = ?",
                         (aid, uid)).fetchone()
        if not a:
            return {"error": "invalid_values"}
        if row["feed"] < 1:
            return {"error": "no_feed"}
        conn.execute("UPDATE users SET feed = feed - 1 WHERE user_id = ?",
                     (uid,))
        conn.execute("UPDATE animals SET fed_until = ? WHERE id = ?",
                     (now + FEED_DURATION_SECS, aid))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": "🍽️ Hewan dikasih makan — kenyang 12 jam!"}

    if action == "collect_animal":
        aid = int(body.get("animal_id", 0) or 0)
        a = conn.execute("SELECT * FROM animals WHERE id = ? AND user_id = ?",
                         (aid, uid)).fetchone()
        if not a:
            return {"error": "invalid_values"}
        name, _e, _p, product, interval, _f = ANIMALS[a["kind"]]
        if now >= int(a["fed_until"] or 0):
            return {"error": "animal_hungry"}
        if now - int(a["last_produce_at"] or a["created_at"]) < interval:
            return {"error": "not_ready"}
        inv = _json_loads(row["inventory"], {})
        inv[product] = inv.get(product, 0) + 1
        conn.execute("UPDATE users SET inventory = ? WHERE user_id = ?",
                     (json.dumps(inv), uid))
        conn.execute("UPDATE animals SET last_produce_at = ? WHERE id = ?",
                     (now, aid))
        conn.commit()
        bump_mission(conn, uid, "hewan", 1)
        add_xp_coins(conn, uid, xp=10)
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Kumpulkan 1x {ANIMAL_PRODUCTS[product][1]}"
                           f" {ANIMAL_PRODUCTS[product][0]}! +10 XP"}

    return {"error": "unknown_action"}


# ---------------------------------------------------------------------------
# AdGram reward (server-side: cooldown + batas harian)
# ---------------------------------------------------------------------------
def ad_reward(conn: sqlite3.Connection, user: dict, body: dict) -> dict:
    uid = int(user["id"])
    rtype = str(body.get("type", ""))
    if rtype not in ("energy", "speed", "double", "seed"):
        return {"error": "invalid_values"}
    row = get_or_create_user(conn, user)
    now = now_ts()
    last = conn.execute(
        "SELECT MAX(created_at) FROM ad_rewards WHERE user_id = ?",
        (uid,)).fetchone()[0]
    if last and now - last < AD_REWARD_COOLDOWN_SECS:
        return {"error": "ad_cooldown"}
    if ad_count_today(conn, uid) >= AD_REWARD_MAX_PER_DAY:
        return {"error": "ad_daily_limit"}
    row = regen_energy(conn, row)
    msg = ""
    if rtype == "energy":
        new_e = min(ENERGY_MAX, row["energy"] + AD_ENERGY_BONUS)
        conn.execute("UPDATE users SET energy = ?, energy_at = ?"
                     " WHERE user_id = ?", (new_e, now, uid))
        msg = f"+{AD_ENERGY_BONUS} Energi! ⚡"
    elif rtype == "speed":
        try:
            idx = int(body.get("plot_id", -1))
        except (TypeError, ValueError):
            return {"error": "invalid_values"}
        plot = conn.execute(
            "SELECT * FROM plots WHERE user_id = ? AND idx = ?",
            (uid, idx)).fetchone()
        if not plot or not plot["crop"]:
            return {"error": "invalid_plot"}
        conn.execute(
            "UPDATE plots SET speed_boost_until = ?"
            " WHERE user_id = ? AND idx = ?", (now + 3600, uid, idx))
        msg = "Petak dipercepat 50% selama 1 jam! 🚀"
    elif rtype == "double":
        conn.execute("UPDATE users SET double_next = 1 WHERE user_id = ?",
                     (uid,))
        msg = "Panen berikutnya 2x lipat! 🧺✨"
    elif rtype == "seed":
        seeds = _json_loads(row["seeds"], {})
        for c, n in {"cabai": 3, "jagung": 2, "tomat": 1}.items():
            seeds[c] = seeds.get(c, 0) + n
        conn.execute("UPDATE users SET seeds = ? WHERE user_id = ?",
                     (json.dumps(seeds), uid))
        msg = "Benih gratis: 3x Cabai, 2x Jagung, 1x Tomat! 🌱"
    conn.execute("INSERT INTO ad_rewards (user_id, type, created_at)"
                 " VALUES (?, ?, ?)", (uid, rtype, now))
    conn.commit()
    return {"ok": True, "reward": rtype, "state": build_state(conn, user),
            "message": msg}


# ---------------------------------------------------------------------------
# FaucetPay API v1 — server-side saja (api_key TIDAK PERNAH ke browser)
# Docs: POST https://faucetpay.io/api/v1/send (form-encoded)
#   api_key, amount (unit terkecil), to (email/username), currency
# ---------------------------------------------------------------------------
def faucetpay_send(to: str, amount_units: int,
                   currency: str = WITHDRAW_CURRENCY) -> dict:
    """Kirim payout FaucetPay v1. Kembalikan {ok, ...} atau {error, ...}."""
    if not FAUCETPAY_API_KEY:
        return {"error": "faucetpay_not_configured"}
    data = urllib.parse.urlencode({
        "api_key": FAUCETPAY_API_KEY,
        "amount": str(amount_units),
        "to": to,
        "currency": currency,
    }).encode()
    req = urllib.request.Request(
        "https://faucetpay.io/api/v1/send", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "{}")
        except ValueError:
            payload = {}
        return {"error": "faucetpay_http_%s" % e.code,
                "detail": str(payload)[:200]}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"error": "faucetpay_unavailable", "detail": str(e)[:200]}
    if payload.get("status") == 200 or payload.get("success"):
        return {"ok": True, "response": payload}
    return {"error": "faucetpay_api_error",
            "detail": str(payload.get("message", payload))[:200]}


def request_withdrawal(conn: sqlite3.Connection, user: dict,
                       body: dict) -> dict:
    uid = int(user["id"])
    row = get_or_create_user(conn, user)
    try:
        coins = int(body.get("coins", 0))
    except (TypeError, ValueError):
        return {"error": "invalid_values"}
    dest = str(body.get("destination", "") or "").strip()
    if coins < WITHDRAW_MIN_COINS or coins % COINS_PER_UNIT != 0:
        return {"error": "amount_out_of_range"}
    if not (FP_EMAIL_RE.match(dest) or FP_USER_RE.match(dest)):
        return {"error": "invalid_destination"}
    if level_from_xp(int(row["xp"])) < WITHDRAW_MIN_LEVEL:
        return {"error": "need_level"}
    if row["flagged"]:
        return {"error": "account_flagged"}
    if int(row["coins"]) < coins:
        return {"error": "insufficient_coins"}
    last = conn.execute(
        "SELECT status, created_at FROM withdrawals WHERE user_id = ?"
        " ORDER BY created_at DESC LIMIT 1", (uid,)).fetchone()
    if last and last["status"] == "pending":
        return {"error": "already_pending"}
    if last and last["status"] in ("paid", "sent", "approved"):
        if now_ts() - last["created_at"] < WITHDRAW_COOLDOWN_SECS:
            return {"error": "cooldown"}
    doge = coins / COINS_PER_UNIT * DOGE_PER_UNIT
    conn.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?",
                 (coins, uid))
    cur = conn.execute(
        "INSERT INTO withdrawals (user_id, username, coins, doge, destination,"
        " currency, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (uid, user.get("username"), coins, doge, dest,
         WITHDRAW_CURRENCY, now_ts()))
    conn.commit()
    return {"ok": True, "request_id": cur.lastrowid, "coins": coins,
            "doge": doge, "currency": WITHDRAW_CURRENCY, "status": "pending",
            "note": "Menunggu persetujuan admin."
                    if not FAUCETPAY_API_KEY else
                    "Menunggu persetujuan admin, lalu dibayar otomatis."}


def _admin_ok(key: str) -> bool:
    return bool(WITHDRAW_ADMIN_KEY) and hmac.compare_digest(key, WITHDRAW_ADMIN_KEY)


def list_withdrawals_admin(key: str) -> dict | None:
    if not _admin_ok(key):
        return None
    conn = db()
    try:
        rows = conn.execute(
            "SELECT id, user_id, username, coins, doge, destination,"
            " currency, status, fp_response, created_at, decided_at"
            " FROM withdrawals ORDER BY created_at DESC LIMIT 200").fetchall()
    finally:
        conn.close()
    return {"ok": True, "faucetpay_configured": bool(FAUCETPAY_API_KEY),
            "requests": [{**dict(r)} for r in rows]}


def decide_withdrawal(key: str, request_id: int, status: str) -> dict | None:
    """Approve -> coba kirim FaucetPay v1; reject -> kembalikan koin."""
    if not _admin_ok(key):
        return None
    if status not in ("approved", "rejected"):
        return {"error": "invalid_status"}
    conn = db()
    try:
        row = conn.execute("SELECT * FROM withdrawals WHERE id = ?",
                           (request_id,)).fetchone()
        if not row or row["status"] != "pending":
            return {"error": "not_found_or_decided"}
        now = now_ts()
        if status == "rejected":
            conn.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?",
                         (row["coins"], row["user_id"]))
            conn.execute("UPDATE withdrawals SET status = 'rejected',"
                         " decided_at = ? WHERE id = ?", (now, request_id))
            conn.commit()
            return {"ok": True, "request_id": request_id, "status": "rejected"}
        # approved: coba payout otomatis; kalau key kosong / API gagal,
        # tetap 'approved' (proses manual) dan catat alasannya — jangan
        # gagal total dan jangan hilangkan permintaan.
        units = int(round(row["doge"] * DOGE_SMALLEST_PER_DOGE))
        result = faucetpay_send(row["destination"], units, row["currency"])
        if result.get("ok"):
            new_status = "sent"
            note = json.dumps(result.get("response", {}))[:500]
        else:
            new_status = "approved"
            note = json.dumps(result)[:500]
        conn.execute("UPDATE withdrawals SET status = ?, fp_response = ?,"
                     " decided_at = ? WHERE id = ?",
                     (new_status, note, now, request_id))
        conn.commit()
        return {"ok": True, "request_id": request_id, "status": new_status,
                "faucetpay": result}
    finally:
        conn.close()


def top_players(limit: int = 20) -> dict:
    conn = db()
    try:
        rows = conn.execute(
            "SELECT user_id, username, first_name, coins, xp FROM users"
            " WHERE flagged = 0 ORDER BY xp DESC, coins DESC LIMIT ?",
            (max(1, min(limit, 100)),)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "total_players": total, "top": [
        {"rank": i, "name": r["first_name"] or r["username"]
         or f"Petani {r['user_id']}", "username": r["username"],
         "coins": r["coins"], "xp": r["xp"],
         "level": level_from_xp(r["xp"])}
        for i, r in enumerate(rows, start=1)]}


# ---------------------------------------------------------------------------
# Auth initData Telegram (HMAC-SHA256, cara resmi Telegram)
# Fallback DEV: kalau BOT_TOKEN kosong / initData tidak valid TAPI body
# membawa user_id, terima sebagai user demo — TODO: matikan fallback ini
# di produksi (set REQUIRE_INITDATA=1) supaya tidak bisa dipalsukan.
# ---------------------------------------------------------------------------
REQUIRE_INITDATA = os.environ.get("REQUIRE_INITDATA", "") == "1"


def validate_init_data(init_data: str) -> dict | None:
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    except ValueError:
        return None
    data = dict(pairs)
    received = data.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(),
                      hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    try:
        if abs(time.time() - int(data.get("auth_date", "0"))) > 86400:
            return None
        user = json.loads(data.get("user", "{}"))
    except (ValueError, TypeError):
        return None
    return user if isinstance(user, dict) and "id" in user else None


def auth_user(body: dict) -> dict | None:
    """Kembalikan dict user {id, username, first_name} atau None."""
    user = validate_init_data(str(body.get("initData", "") or ""))
    if user:
        return user
    if REQUIRE_INITDATA:
        return None
    # Fallback demo/test (local): user_id dari body.
    try:
        uid = int(body.get("user_id", 0))
    except (TypeError, ValueError):
        return None
    if uid <= 0:
        return None
    logger.warning("Auth fallback demo dipakai untuk user_id=%s"
                   " (initData tidak valid / BOT_TOKEN kosong).", uid)
    return {"id": uid,
            "username": str(body.get("username", "") or ""),
            "first_name": str(body.get("first_name", "") or f"Petani {uid}")}


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, ctype: str) -> None:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self._send_json(404, {"error": "not_found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self._send_json(204, {})

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/":
            # Sajikan Mini App langsung dari server yang sama (praktis
            # untuk test lokal; produksi bisa pakai GitHub Pages).
            here = os.path.dirname(os.path.abspath(__file__))
            self._send_file(os.path.join(here, "index.html"),
                            "text/html; charset=utf-8")
        elif path == "/health":
            self._send_json(200, {"ok": True, "game": "harvestfarm",
                                  "weather": weather_now(),
                                  "event": event_now()})
        elif path == "/api/leaderboard":
            try:
                limit = int(query.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            self._send_json(200, top_players(limit))
        elif path == "/api/market":
            items = list(CROPS) + list(ANIMAL_PRODUCTS)
            self._send_json(200, {"ok": True,
                                  "market": {it: market_price(it)
                                             for it in items},
                                  "weather": weather_now(),
                                  "event": event_now()})
        elif path == "/api/withdrawals/admin":
            result = list_withdrawals_admin(query.get("key", [""])[0])
            if result is None:
                self._send_json(403, {"error": "forbidden"})
            else:
                self._send_json(200, result)
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(max(length, 0)) if length > 0 else b""
        try:
            body = json.loads(raw.decode() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid_json"})
            return
        if path == "/api/withdrawals/admin":
            key = query.get("key", [""])[0]
            try:
                rid = int(body.get("request_id", 0))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "invalid_values"})
                return
            result = decide_withdrawal(key, rid, str(body.get("status", "")))
            if result is None:
                self._send_json(403, {"error": "forbidden"})
            elif "error" in result:
                self._send_json(400, result)
            else:
                self._send_json(200, result)
            return
        user = auth_user(body)
        if not user:
            self._send_json(401, {"error": "invalid_init_data"})
            return
        conn = db()
        try:
            if path == "/api/state":
                self._send_json(200, {"ok": True,
                                      "state": build_state(conn, user)})
            elif path == "/api/action":
                result = do_action(conn, user, body)
                status = 400 if "error" in result else 200
                if result.get("error") == "rate_limited":
                    status = 429
                self._send_json(status, result)
            elif path == "/api/market":
                self._send_json(200, {"ok": True,
                                      "state": build_state(conn, user)})
            elif path == "/api/withdraw":
                result = request_withdrawal(conn, user, body)
                self._send_json(400 if "error" in result else 200, result)
            elif path == "/api/ad-reward":
                result = ad_reward(conn, user, body)
                status = 400 if "error" in result else 200
                if result.get("error") in ("ad_cooldown",):
                    status = 429
                self._send_json(status, result)
            elif path == "/api/leaderboard":
                self._send_json(200, top_players(
                    int(body.get("limit", 20) or 20)))
            else:
                self._send_json(404, {"error": "not_found"})
        except Exception as exc:  # jangan matikan server karena 1 request
            logger.exception("API %s gagal: %s", path, exc)
            self._send_json(500, {"error": "server_error"})
        finally:
            conn.close()


def serve_api() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), ApiHandler)
    logger.info("Harvest Farm API jalan di port %s.", port)
    server.serve_forever()


# ---------------------------------------------------------------------------
# Bot Telegram
# ---------------------------------------------------------------------------
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌾 Main Game",
                              web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton("❓ Cara Main", callback_data="howto")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (
        f"Halo {user.first_name}! 👋\n\n"
        "Selamat datang di *Harvest Farm — Kebun Kripto* 🌾\n\n"
        "🌱 Tanam benih, siram, panen\n"
        "🐔 Pelihara ayam, kambing & sapi\n"
        "🛒 Jual hasil panen di pasar (harga naik-turun!)\n"
        "🤝 Jual-beli dengan pemain lain\n"
        "🪙 Kumpulkan Koin Panen, tarik jadi DOGE via FaucetPay\n\n"
        "Kurs: 200.000 Koin = 0,01 DOGE • Min WD Level 5"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard(),
                                    parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Cara main Harvest Farm:\n"
        "1. Ketuk Main Game.\n"
        "2. Tanam benih di petak kosong (pakai 1 Energi).\n"
        "3. Siram biar tumbuh 25% lebih cepat, beri pupuk +50%.\n"
        "4. Usir hama sebelum hasil panen berkurang!\n"
        "5. Panen, lalu jual di Pasar NPC saat harga tinggi,\n"
        "   atau pasang jualan sendiri di Pasar Pemain.\n"
        "6. Beli hewan, beri pakan, kumpulkan telur/susu.\n"
        "7. Nonton iklan AdGram buat +20 Energi.\n"
        "8. Di Dompet, tarik Koin jadi DOGE via FaucetPay.\n\n"
        "Perintah: /start /help /saldo"
    )


async def saldo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db()
    try:
        row = conn.execute(
            "SELECT coins, xp, energy, login_streak FROM users"
            " WHERE user_id = ?", (update.effective_user.id,)).fetchone()
    finally:
        conn.close()
    if not row:
        await update.message.reply_text(
            "Kamu belum punya kebun. Ketuk Main Game dulu di /start! 🌱")
        return
    level = level_from_xp(row["xp"])
    await update.message.reply_text(
        f"💰 Saldo Harvest Farm\n\n"
        f"🪙 Koin: *{row['coins']:,}*\n"
        f"⭐ Level {level} ({row['xp']:,} XP)\n"
        f"⚡ Energi: {row['energy']}/{ENERGY_MAX}\n"
        f"🔥 Streak login: {row['login_streak']} hari\n\n"
        f"Kurs WD: {COINS_PER_UNIT:,} Koin = {DOGE_PER_UNIT} DOGE"
        f" (min Level {WITHDRAW_MIN_LEVEL})",
        parse_mode="Markdown")


async def howto_callback(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "❓ *Cara Main Harvest Farm*\n\n"
        "• Tanam → Siram → Panen → Jual.\n"
        "• Cuaca berubah tiap jam: Hujan mempercepat, Badai berbahaya.\n"
        "• Hama muncul acak — tap cepat untuk mengusir!\n"
        "• Hewan butuh pakan, kalau lapar tidak berproduksi.\n"
        "• Harga pasar berubah tiap 30 menit — jual saat tinggi!",
        reply_markup=main_keyboard(), parse_mode="Markdown")


def main() -> None:
    db().close()
    threading.Thread(target=serve_api, daemon=True).start()
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN kosong — hanya API HTTP yang jalan"
                       " (mode test lokal). Isi BOT_TOKEN untuk bot Telegram.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    if "example.github.io" in WEBAPP_URL:
        logger.warning("WEBAPP_URL masih placeholder — isi URL Mini App dulu.")
    app = Application.builder().token(BOT_TOKEN).build()
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("saldo", saldo_cmd))
    app.add_handler(CallbackQueryHandler(howto_callback, pattern="^howto$"))
    logger.info("Harvest Farm bot jalan (polling).")
    app.run_polling()


if __name__ == "__main__":
    main()
