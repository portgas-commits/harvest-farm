"""
Harvest Farm — Kebun Kripto (v2)
Game pertanian Telegram Mini App berbasis FaucetPay (DOGE).

v2 = evolusi v1 (bukan rebuild): stack tetap Python + SQLite + HTML/JS,
semua endpoint v1 kompatibel mundur. Baru di v2: Level 1-100, skill tree
4 cabang, bangunan produksi, memancing & menambang, benih langka/mutasi,
musim + turnamen mingguan, guild, Permata Panen (premium currency),
achievement v2. WD tetap Koin -> DOGE via FaucetPay (aturan v1).

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
    # v2: Melon Emas — benih TIDAK dijual di toko (harga 0 = tidak dijual),
    # hanya dari mutasi panen (4%). Min Level 20.
    "melon":    ("Melon Emas", "🍈", 0, 120 * 60, 6, 250, 1500),
}
# Level minimum untuk menanam tiap tanaman (v2)
CROP_UNLOCK = {"cabai": 1, "jagung": 1, "tomat": 2, "wortel": 3, "padi": 5,
               "labu": 8, "stroberi": 12, "kopi": 15, "melon": 20}
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
    "domba":  ("Domba",  "🐑", 3500, "wol",   90 * 60,  1),  # v2
}
# Level minimum untuk membeli tiap hewan (v2)
ANIMAL_UNLOCK = {"ayam": 1, "kambing": 4, "sapi": 7, "domba": 9}
# Produk olahan / hasil pancing & tambang: id -> (nama, emoji, harga jual dasar)
# (v2) — wol juga produk hewan Domba, jadi dicari di sini juga.
PRODUCTS = {
    "roti":       ("Roti",       "🍞", 500),
    "keju":       ("Keju",       "🧀", 900),
    "saus":       ("Saus Tomat", "🥫", 300),
    "wol":        ("Wol",        "🧶", 200),
    "ikan":       ("Ikan",       "🐟", 150),
    "ikan_emas":  ("Ikan Emas",  "✨", 800),
    "batu":       ("Batu",       "🪨", 100),
    "bijih_emas": ("Bijih Emas", "⛏️", 600),
}
# Bangunan produksi (v2): kind -> dict
# recipe = bahan dari inventaris -> output; time detik; upgrade menaikkan
# kapasitas job bersamaan (Lv 1 = 1 job, Lv 2 = 2, Lv 3 = 3).
BUILDINGS = {
    "roti": {"name": "Toko Roti", "emoji": "🏭", "price": 3000,
             "min_level": 6, "recipe": {"padi": 5}, "output": "roti",
             "output_qty": 2, "time": 30 * 60},
    "keju": {"name": "Pabrik Susu", "emoji": "🏭", "price": 8000,
             "min_level": 10, "recipe": {"susu": 3}, "output": "keju",
             "output_qty": 1, "time": 60 * 60},
    "saus": {"name": "Dapur Saus", "emoji": "🏭", "price": 5000,
             "min_level": 14, "recipe": {"tomat": 4}, "output": "saus",
             "output_qty": 2, "time": 20 * 60},
}
BUILDING_MAX_LEVEL = 3

# --- v2: Permata Panen, skill, musim, guild, mancing/tambang ---
GEM_TO_COINS = 500            # kurs tetap: 1 Permata = 500 Koin
GEM_CONVERT_MAX_DAY = 50      # maks konversi Permata/hari/pemain
MAX_LEVEL = 100
SKILL_MAX = 5                 # maks level per cabang skill
SKILL_BRANCHES = {
    "bertani":  ("Bertani",  "🌾"),
    "produksi": ("Produksi", "🏭"),
    "dagang":   ("Dagang",   "🤝"),
    "jelajah":  ("Jelajah",  "🧭"),
}
FISH_COOLDOWN_SECS = 30 * 60
MINE_COOLDOWN_SECS = 30 * 60
MUTATION_CHANCE = 0.04        # panen -> +1 benih Melon Emas
LEGENDARY_CHANCE = 0.005      # panen -> +1 Permata Panen
GUILD_CREATE_COST = 1000
GUILD_MAX_MEMBERS = 20
GUILD_GOAL_POINTS = 500       # total poin musim anggota per minggu
GUILD_REWARD_GEMS = 10
TOURNAMENT_MIN_POINTS = 50

# --- v3: referral & multi-bahasa ---
BOT_USERNAME = "harvestfarm_game_bot"
REF_NEW_BONUS_COINS = 200       # bonus pemain baru yang masuk via link referral
REF_MILESTONE_LEVEL = 3         # pengundang dapat hadiah saat yang diundang capai Lv 3
REF_REWARD_COINS = 500          # hadiah pengundang per undangan yang capai Lv 3
REF_REWARD_GEMS = 2
LANGS = ("id", "en", "ru", "es", "pt")
SEASON_SECONDS = 7 * 86400
SEASONS = [
    ("semi",   "Musim Semi",   "🌸", "+10% XP semua aksi."),
    ("panas",  "Musim Panas",  "☀️", "Cooldown mancing & menambang -25%."),
    ("panen",  "Musim Panen",  "🍂", "+10% hasil panen."),
    ("dingin", "Musim Dingin", "❄️", "+10% harga jual NPC."),
]

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
    ("ikan", "Mancing 2 kali", 2, 300),          # v2
    ("tambang", "Menambang 2 kali", 2, 300),      # v2
    ("produksi", "Selesaikan 1 produksi", 1, 500),  # v2
]

ACHIEVEMENTS = [
    (1, "🌱 Petani Pemula", "Capai Level 1"),
    (5, "🚜 Petani Andal", "Capai Level 5"),
    (10, "👑 Juragan Kebun", "Capai Level 10"),
    (20, "🌾 Legenda Panen", "Capai Level 20"),
]

# Achievement v2: (id, judul, deskripsi, jenis_syarat, target,
#                  hadiah_koin, hadiah_permata)
# jenis_syarat: level | harvest | sold | produce | fish | mine | guild | melon
ACHIEVEMENTS_V2 = [
    (1,  "🌱 Petani Pemula",    "Capai Level 1",          "level",   1,     100,  0),
    (2,  "🚜 Petani Andal",     "Capai Level 5",          "level",   5,     500,  2),
    (3,  "👑 Juragan Kebun",    "Capai Level 10",         "level",   10,    1000, 5),
    (4,  "🌾 Legenda Panen",    "Capai Level 20",         "level",   20,    2000, 10),
    (5,  "🏆 Master Tani",      "Capai Level 50",         "level",   50,    5000, 20),
    (6,  "👑 Legenda Harvest",  "Capai Level 100",        "level",   100,   10000, 50),
    (7,  "🧺 Panen 100×",       "Total panen 100 kali",   "harvest", 100,   500,  2),
    (8,  "🧺 Panen 1.000×",     "Total panen 1.000 kali", "harvest", 1000,  2000, 10),
    (9,  "💰 Jualan 10.000",    "Total terjual 10.000",   "sold",    10000, 1000, 5),
    (10, "🏭 Produksi Pertama", "Selesaikan 1 produksi",  "produce", 1,     500,  3),
    (11, "🎣 Pemancing Handal", "Mancing 25 kali",        "fish",    25,    500,  3),
    (12, "⛏️ Penambang Ulung",  "Menambang 25 kali",      "mine",    25,    500,  3),
    (13, "🤝 Anggota Guild",    "Gabung sebuah guild",    "guild",   1,     300,  2),
    (14, "🍈 Melon Emas",       "Panen Melon Emas",       "melon",   1,     1000, 5),
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
    elif item in PRODUCTS:
        base = PRODUCTS[item][2]
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
    # v2: Musim Dingin -> harga jual NPC +10%
    if season_now(ts)["kind"] == "dingin":
        price = round(price * 1.10)
        change += 10
    trend = "naik" if change >= 5 else ("turun" if change <= -5 else "stabil")
    return {"price": price, "base": base, "trend": trend, "change_pct": change}


def xp_for_level(level: int) -> int:
    """Total XP kumulatif untuk mencapai level L (v2, maks Level 100).

    L<=20 sama seperti v1 ((L-1)^2*100); di atas itu melambat supaya
    grind ke Level 100 butuh waktu panjang.
    """
    if level <= 1:
        return 0
    if level <= 20:
        return (level - 1) ** 2 * 100
    return 40_000 + int(400 * (level - 20) ** 1.7)


def level_from_xp(xp: int) -> int:
    xp = max(int(xp), 0)
    level = 1
    for lv in range(2, MAX_LEVEL + 1):
        if xp >= xp_for_level(lv):
            level = lv
        else:
            break
    return level


def season_now(ts: int | None = None) -> dict:
    """Musim bergilir tiap minggu (Senin 00:00 UTC), deterministik."""
    ts = ts if ts is not None else now_ts()
    week = ts // SEASON_SECONDS
    kind, label, emoji, desc = SEASONS[week % len(SEASONS)]
    return {"week": week, "kind": kind, "label": label, "emoji": emoji,
            "desc": desc,
            "ends_in": SEASON_SECONDS - (ts % SEASON_SECONDS)}


def skill_level(skills: dict, branch: str) -> int:
    try:
        return max(0, min(SKILL_MAX, int((skills or {}).get(branch, 0))))
    except (TypeError, ValueError):
        return 0


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
    # --- v2: tabel baru ---
    conn.execute(
        "CREATE TABLE IF NOT EXISTS buildings ("
        "  user_id INTEGER NOT NULL, kind TEXT NOT NULL,"
        "  level INTEGER NOT NULL DEFAULT 1,"
        "  created_at INTEGER NOT NULL DEFAULT 0,"
        "  PRIMARY KEY (user_id, kind))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS production_jobs ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  user_id INTEGER NOT NULL, building TEXT NOT NULL,"
        "  recipe TEXT NOT NULL, qty INTEGER NOT NULL DEFAULT 1,"
        "  started_at INTEGER NOT NULL DEFAULT 0,"
        "  ready_at INTEGER NOT NULL DEFAULT 0,"
        "  status TEXT NOT NULL DEFAULT 'running')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS achievements_claimed ("
        "  user_id INTEGER NOT NULL, ach_id INTEGER NOT NULL,"
        "  claimed_at INTEGER NOT NULL DEFAULT 0,"
        "  PRIMARY KEY (user_id, ach_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS guilds ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name TEXT NOT NULL, leader_id INTEGER NOT NULL,"
        "  created_at INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS guild_members ("
        "  guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,"
        "  joined_at INTEGER NOT NULL DEFAULT 0,"
        "  claimed_week INTEGER NOT NULL DEFAULT -1,"
        "  PRIMARY KEY (guild_id, user_id))"
    )
    # --- v3: referral ---
    conn.execute(
        "CREATE TABLE IF NOT EXISTS referral_events ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  referrer_id INTEGER NOT NULL,"
        "  referee_id INTEGER NOT NULL,"
        "  event TEXT NOT NULL,"
        "  coins INTEGER NOT NULL DEFAULT 0,"
        "  gems INTEGER NOT NULL DEFAULT 0,"
        "  created_at INTEGER NOT NULL DEFAULT 0,"
        "  UNIQUE (referee_id, event))"
    )
    # --- v2: migrasi kolom users (idempotent, DB v1 tetap jalan) ---
    existing = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for coldef in (
        "gems INTEGER NOT NULL DEFAULT 0",
        "skill_points INTEGER NOT NULL DEFAULT 0",
        "skills TEXT NOT NULL DEFAULT '{}'",
        "season_points INTEGER NOT NULL DEFAULT 0",
        "season_week INTEGER NOT NULL DEFAULT 0",
        "claimed_season INTEGER NOT NULL DEFAULT -1",
        "last_fish_at INTEGER NOT NULL DEFAULT 0",
        "last_mine_at INTEGER NOT NULL DEFAULT 0",
        "fish_count INTEGER NOT NULL DEFAULT 0",
        "mine_count INTEGER NOT NULL DEFAULT 0",
        "produce_count INTEGER NOT NULL DEFAULT 0",
        "melon_harvest INTEGER NOT NULL DEFAULT 0",
        "gems_converted_today INTEGER NOT NULL DEFAULT 0",
        "gems_convert_day TEXT NOT NULL DEFAULT ''",
        "guild_id INTEGER NOT NULL DEFAULT 0",
        "total_produced INTEGER NOT NULL DEFAULT 0",
        # v3: referral & bahasa
        "referrer_id INTEGER NOT NULL DEFAULT 0",
        "referred_rewarded INTEGER NOT NULL DEFAULT 0",
        "language TEXT NOT NULL DEFAULT 'id'",
    ):
        col = coldef.split()[0]
        if col not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {coldef}")
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


# ---------------------------------------------------------------------------
# v3: referral & bahasa
# ---------------------------------------------------------------------------
def norm_lang(code) -> str:
    """Normalisasi kode bahasa Telegram (id/en/ru/es/pt), fallback 'id'."""
    if not code:
        return "id"
    c = str(code).lower().replace("_", "-").split("-")[0]
    return c if c in LANGS else "id"


def apply_referral(conn: sqlite3.Connection, uid: int,
                   referrer_id) -> str:
    """Catat referrer untuk pemain baru yang masuk via link referral.

    Kembalikan: 'ok' | 'self' | 'not_found' | 'already' | 'invalid'.
    Anti-abuse: self-referral ditolak, referrer harus sudah terdaftar,
    satu referrer per pemain (tidak bisa diganti), bonus pemain baru
    +200 Koin hanya sekali saat pertama kali masuk lewat link.
    """
    try:
        referrer_id = int(referrer_id)
    except (TypeError, ValueError):
        return "invalid"
    if referrer_id <= 0 or referrer_id == uid:
        return "self"
    row = conn.execute(
        "SELECT referrer_id FROM users WHERE user_id = ?", (uid,)).fetchone()
    if row is None:
        return "invalid"
    if int(row["referrer_id"] or 0) > 0:
        return "already"
    ref = conn.execute(
        "SELECT 1 FROM users WHERE user_id = ?", (referrer_id,)).fetchone()
    if not ref:
        return "not_found"
    conn.execute(
        "UPDATE users SET referrer_id = ?, coins = coins + ?"
        " WHERE user_id = ?",
        (referrer_id, REF_NEW_BONUS_COINS, uid))
    conn.execute(
        "INSERT OR IGNORE INTO referral_events"
        " (referrer_id, referee_id, event, coins, gems, created_at)"
        " VALUES (?, ?, 'joined', ?, 0, ?)",
        (referrer_id, uid, REF_NEW_BONUS_COINS, now_ts()))
    conn.commit()
    return "ok"


def _check_referral_milestone(conn: sqlite3.Connection, uid: int) -> None:
    """Hadiah pengundang (+500 Koin + 2 Permata) saat yang diundang capai
    Level 3 — sekali saja per undangan (flag referred_rewarded)."""
    row = conn.execute(
        "SELECT referrer_id, referred_rewarded FROM users"
        " WHERE user_id = ?", (uid,)).fetchone()
    if not row or not int(row["referrer_id"] or 0):
        return
    if int(row["referred_rewarded"] or 0):
        return
    referrer = int(row["referrer_id"])
    if not conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?",
            (referrer,)).fetchone():
        return
    conn.execute(
        "UPDATE users SET referred_rewarded = 1 WHERE user_id = ?", (uid,))
    conn.execute(
        "UPDATE users SET coins = coins + ?, gems = gems + ?"
        " WHERE user_id = ?",
        (REF_REWARD_COINS, REF_REWARD_GEMS, referrer))
    conn.execute(
        "INSERT OR IGNORE INTO referral_events"
        " (referrer_id, referee_id, event, coins, gems, created_at)"
        " VALUES (?, ?, 'milestone', ?, ?, ?)",
        (referrer, uid, REF_REWARD_COINS, REF_REWARD_GEMS, now_ts()))
    conn.commit()


def referral_info(conn: sqlite3.Connection, uid: int) -> dict:
    """Info referral untuk tab Referral: link, statistik, daftar undangan."""
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    invitees = conn.execute(
        "SELECT user_id, username, first_name, xp, referred_rewarded"
        " FROM users WHERE referrer_id = ? ORDER BY user_id",
        (uid,)).fetchall()
    ev = conn.execute(
        "SELECT COALESCE(SUM(coins), 0) AS c, COALESCE(SUM(gems), 0) AS g"
        " FROM referral_events WHERE referrer_id = ? AND event = 'milestone'",
        (uid,)).fetchone()
    return {
        "link": link,
        "invited_count": len(invitees),
        "rewarded_count": sum(1 for r in invitees
                              if int(r["referred_rewarded"] or 0)),
        "total_coins": int(ev["c"] or 0),
        "total_gems": int(ev["g"] or 0),
        "invitees": [{
            "name": r["first_name"] or r["username"]
                    or f"Petani {r['user_id']}",
            "level": level_from_xp(int(r["xp"])),
            "rewarded": bool(int(r["referred_rewarded"] or 0)),
        } for r in invitees],
    }


def add_xp_coins(conn: sqlite3.Connection, uid: int, xp: int = 0,
                 coins: int = 0, gems: int = 0) -> None:
    """Tambah XP/Koin/Permata. v2: naik level -> +1 poin skill per level,
    anggota guild -> +5% XP (bonus guild pasif)."""
    if xp:
        row = conn.execute(
            "SELECT xp, guild_id FROM users WHERE user_id = ?", (uid,)
        ).fetchone()
        if row is not None:
            old_level = level_from_xp(int(row["xp"]))
            if int(row["guild_id"] or 0) > 0:
                xp = max(1, round(xp * 1.05))
            if season_now()["kind"] == "semi":  # v2: Musim Semi +10% XP
                xp = max(1, round(xp * 1.10))
            new_xp = int(row["xp"]) + xp
            new_level = level_from_xp(new_xp)
            gained_points = max(0, new_level - old_level)
            conn.execute(
                "UPDATE users SET xp = ?, skill_points = skill_points + ?"
                " WHERE user_id = ?", (new_xp, gained_points, uid))
            # v3: milestone referral — yang diundang capai Level 3
            if new_level >= REF_MILESTONE_LEVEL:
                _check_referral_milestone(conn, uid)
    if coins or gems:
        conn.execute("UPDATE users SET coins = coins + ?, gems = gems + ?"
                     " WHERE user_id = ?", (coins, gems, uid))
    conn.commit()


def ensure_season(conn: sqlite3.Connection, uid: int,
                  row: sqlite3.Row | None = None) -> sqlite3.Row:
    """Reset poin musim kalau minggu sudah berganti (server-side)."""
    if row is None:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?",
                           (uid,)).fetchone()
    week = season_now()["week"]
    if row is not None and int(row["season_week"] or 0) != week:
        conn.execute("UPDATE users SET season_points = 0, season_week = ?"
                     " WHERE user_id = ?", (week, uid))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id = ?",
                           (uid,)).fetchone()
    return row


def bump_season(conn: sqlite3.Connection, uid: int, n: int = 1) -> None:
    """Tambah poin turnamen musim ini (reset otomatis kalau minggu baru)."""
    ensure_season(conn, uid)
    conn.execute("UPDATE users SET season_points = season_points + ?"
                 " WHERE user_id = ?", (n, uid))
    conn.commit()


def achievement_progress(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """Nilai syarat tiap achievement v2 untuk satu pemain."""
    return {
        "level": level_from_xp(int(row["xp"])),
        "harvest": int(row["total_harvest"]),
        "sold": int(row["total_sold"]),
        "produce": int(row["produce_count"]),
        "fish": int(row["fish_count"]),
        "mine": int(row["mine_count"]),
        "guild": 1 if int(row["guild_id"] or 0) > 0 else 0,
        "melon": int(row["melon_harvest"]),
    }


def achievements_v2_state(conn: sqlite3.Connection, uid: int,
                          row: sqlite3.Row) -> list[dict]:
    prog = achievement_progress(conn, row)
    claimed = {r["ach_id"] for r in conn.execute(
        "SELECT ach_id FROM achievements_claimed WHERE user_id = ?", (uid,))}
    out = []
    for aid, title, desc, kind, target, coins, gems in ACHIEVEMENTS_V2:
        cur = prog.get(kind, 0)
        out.append({"id": aid, "title": title, "desc": desc,
                    "target": target, "progress": min(cur, target),
                    "done": cur >= target, "claimed": aid in claimed,
                    "reward_coins": coins, "reward_gems": gems})
    return out


def guild_info(conn: sqlite3.Connection, uid: int) -> dict | None:
    """Info guild pemain: guild, jumlah anggota, progres tujuan mingguan."""
    row = conn.execute("SELECT guild_id FROM users WHERE user_id = ?",
                       (uid,)).fetchone()
    gid = int(row["guild_id"] or 0) if row else 0
    if not gid:
        return None
    g = conn.execute("SELECT * FROM guilds WHERE id = ?", (gid,)).fetchone()
    if not g:
        return None
    week = season_now()["week"]
    members = conn.execute(
        "SELECT u.user_id, u.first_name, u.username, u.season_points,"
        "  u.season_week FROM guild_members gm JOIN users u"
        "  ON u.user_id = gm.user_id WHERE gm.guild_id = ?"
        "  ORDER BY u.season_points DESC", (gid,)).fetchall()
    progress = sum(int(m["season_points"])
                   for m in members if int(m["season_week"] or 0) == week)
    claimed = conn.execute(
        "SELECT claimed_week FROM guild_members WHERE guild_id = ?"
        " AND user_id = ?", (gid, uid)).fetchone()
    return {
        "id": g["id"], "name": g["name"], "leader_id": g["leader_id"],
        "members": [{"name": m["first_name"] or m["username"]
                     or f"Petani {m['user_id']}",
                     "season_points": int(m["season_points"])
                     if int(m["season_week"] or 0) == week else 0}
                    for m in members],
        "member_count": len(members),
        "goal": GUILD_GOAL_POINTS, "goal_progress": progress,
        "goal_reached": progress >= GUILD_GOAL_POINTS,
        "reward_claimed_this_week":
            bool(claimed and int(claimed["claimed_week"]) == week),
        "reward_gems": GUILD_REWARD_GEMS,
    }


def list_guilds(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT g.id, g.name, g.leader_id,"
        "  (SELECT COUNT(*) FROM guild_members WHERE guild_id = g.id)"
        "  AS members FROM guilds g ORDER BY members DESC, g.id ASC"
        "  LIMIT 30").fetchall()
    return [{"id": r["id"], "name": r["name"], "leader_id": r["leader_id"],
             "members": r["members"]} for r in rows]


def season_leaderboard(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Top poin musim minggu ini saja (season_week == minggu berjalan)."""
    week = season_now()["week"]
    rows = conn.execute(
        "SELECT user_id, username, first_name, season_points FROM users"
        " WHERE flagged = 0 AND season_week = ?"
        " ORDER BY season_points DESC, xp DESC LIMIT ?",
        (week, max(1, min(limit, 100)))).fetchall()
    return [{"rank": i, "name": r["first_name"] or r["username"]
             or f"Petani {r['user_id']}",
             "season_points": int(r["season_points"])}
            for i, r in enumerate(rows, start=1)]


def season_rank(conn: sqlite3.Connection, uid: int) -> int | None:
    week = season_now()["week"]
    row = conn.execute(
        "SELECT COUNT(*) FROM users WHERE flagged = 0 AND season_week = ?"
        " AND (season_points > (SELECT season_points FROM users"
        " WHERE user_id = ?) OR (season_points = (SELECT season_points"
        " FROM users WHERE user_id = ?) AND user_id < ?))",
        (week, uid, uid, uid)).fetchone()
    return (row[0] + 1) if row else None


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
        # x skill Bertani (-5%/level) x Musim Semi tidak berpengaruh di sini
        factor = w["factor"]
        if uid is not None:
            _urow = conn.execute(
                "SELECT skills FROM users WHERE user_id = ?", (uid,)).fetchone()
            if _urow:
                factor *= (1 - 0.05 * skill_level(
                    _json_loads(_urow["skills"], {}), "bertani"))
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
        if product in ANIMAL_PRODUCTS:
            pname, pem, _pb = ANIMAL_PRODUCTS[product]
        else:  # v2: wol dll. dari PRODUCTS
            pname, pem, _pb = PRODUCTS[product]
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
    row = ensure_season(conn, uid, row)  # v2: reset poin musim bila perlu
    w = weather_now()
    ev = event_now()
    season = season_now()
    plots = plot_view(conn, uid, w, ev)
    animals = animal_view(conn, uid)
    inventory = _json_loads(row["inventory"], {})
    seeds = _json_loads(row["seeds"], {})
    missions = daily_missions(conn, uid)
    items = list(CROPS) + list(ANIMAL_PRODUCTS) + list(PRODUCTS)
    market = {it: market_price(it) for it in items}
    # v2: bangunan & job produksi pemain
    buildings = {r["kind"]: {"level": r["level"]}
                 for r in conn.execute(
                     "SELECT kind, level FROM buildings WHERE user_id = ?",
                     (uid,)).fetchall()}
    jobs = conn.execute(
        "SELECT * FROM production_jobs WHERE user_id = ? AND status = 'running'"
        " ORDER BY ready_at", (uid,)).fetchall()
    skills = _json_loads(row["skills"], {})
    now = now_ts()
    fish_cd = max(0, int(row["last_fish_at"] or 0)
                  + FISH_COOLDOWN_SECS - now)
    mine_cd = max(0, int(row["last_mine_at"] or 0)
                  + MINE_COOLDOWN_SECS - now)
    if season["kind"] == "panas":  # Musim Panas: cooldown -25%
        fish_cd = int(fish_cd * 0.75)
        mine_cd = int(mine_cd * 0.75)
    # cooldown efektif skill Jelajah (-10%/level) — tampilkan di state
    jel = skill_level(skills, "jelajah")
    fish_cd = int(fish_cd * (1 - 0.10 * jel))
    mine_cd = int(mine_cd * (1 - 0.10 * jel))
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
            "xp_next": xp_for_level(level + 1) if level < MAX_LEVEL else None,
            "max_level": MAX_LEVEL,
            "energy": int(row["energy"]), "energy_max": ENERGY_MAX,
            "grid_size": int(row["grid_size"]),
            "fertilizer": int(row["fertilizer"]), "feed": int(row["feed"]),
            "double_next": bool(row["double_next"]),
            "login_streak": int(row["login_streak"]),
            "total_harvest": int(row["total_harvest"]),
            "total_sold": int(row["total_sold"]),
            "ad_rewards_today": ad_count_today(conn, uid),
            "flagged": bool(row["flagged"]),
            # v2
            "gems": int(row["gems"]),
            "skill_points": int(row["skill_points"]),
            "skills": skills,
            "season_points": int(row["season_points"]),
            "season_week": int(row["season_week"]),
            "claimed_season": int(row["claimed_season"]),
            "guild_id": int(row["guild_id"] or 0),
            "fish_count": int(row["fish_count"]),
            "mine_count": int(row["mine_count"]),
            "produce_count": int(row["produce_count"]),
            "fish_cooldown_in": fish_cd,
            "mine_cooldown_in": mine_cd,
            # v3
            "language": row["language"] or "id",
            "referrer_id": int(row["referrer_id"] or 0),
        },
        "referral": referral_info(conn, uid),
        "season": season,
        "season_top": season_leaderboard(conn, 10),
        "achievements_v2": achievements_v2_state(conn, uid, row),
        "buildings": buildings,
        "building_defs": {
            k: {"name": v["name"], "emoji": v["emoji"], "price": v["price"],
                "min_level": v["min_level"], "recipe": v["recipe"],
                "output": v["output"], "output_qty": v["output_qty"],
                "time": v["time"],
                "output_name": PRODUCTS[v["output"]][0],
                "output_emoji": PRODUCTS[v["output"]][1]}
            for k, v in BUILDINGS.items()},
        "production_jobs": [{
            "id": j["id"], "building": j["building"],
            "building_name": BUILDINGS[j["building"]]["name"],
            "recipe": j["recipe"], "qty": j["qty"],
            "output": PRODUCTS[BUILDINGS[j["building"]]["output"]][0],
            "output_emoji": PRODUCTS[BUILDINGS[j["building"]]["output"]][1],
            "ready_at": j["ready_at"], "ready": now >= int(j["ready_at"]),
            "ready_in": max(0, int(j["ready_at"]) - now),
        } for j in jobs],
        "guild": guild_info(conn, uid),
        "guilds": list_guilds(conn),
        "config_v2": {
            "gem_to_coins": GEM_TO_COINS,
            "gem_convert_max_day": GEM_CONVERT_MAX_DAY,
            "fish_cooldown": FISH_COOLDOWN_SECS,
            "mine_cooldown": MINE_COOLDOWN_SECS,
            "guild_create_cost": GUILD_CREATE_COST,
            "guild_goal": GUILD_GOAL_POINTS,
            "guild_reward_gems": GUILD_REWARD_GEMS,
            "tournament_min_points": TOURNAMENT_MIN_POINTS,
            "crop_unlock": CROP_UNLOCK,
            "animal_unlock": ANIMAL_UNLOCK,
            "skill_branches": {k: {"name": v[0], "emoji": v[1],
                                    "max": SKILL_MAX}
                               for k, v in SKILL_BRANCHES.items()},
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
        if crop == "melon":
            # v2: benih Melon Emas tidak dijual — hanya dari mutasi panen
            return {"error": "not_for_sale"}
        if level_from_xp(int(row["xp"])) < CROP_UNLOCK.get(crop, 1):
            return {"error": "level_locked"}
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
        if level_from_xp(int(row["xp"])) < ANIMAL_UNLOCK.get(kind, 1):
            return {"error": "level_locked"}
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
        if (item not in CROPS and item not in ANIMAL_PRODUCTS
                and item not in PRODUCTS):
            return {"error": "invalid_values"}
        if qty < 1 or qty > MAX_SELL_QTY:
            return {"error": "invalid_values"}
        inv = _json_loads(row["inventory"], {})
        if inv.get(item, 0) < qty:
            return {"error": "insufficient_item"}
        price = market_price(item)["price"]
        # v2: skill Dagang +5% harga jual per level
        dagang = skill_level(_json_loads(row["skills"], {}), "dagang")
        if dagang:
            price = round(price * (1 + 0.05 * dagang))
        total = price * qty
        if total > MAX_COINS_DELTA_PER_ACTION * 20:
            return {"error": "values_too_large"}
        inv[item] -= qty
        conn.execute("UPDATE users SET inventory = ?, coins = coins + ?,"
                     " total_sold = total_sold + ? WHERE user_id = ?",
                     (json.dumps(inv), total, qty, uid))
        conn.commit()
        bump_mission(conn, uid, "jual", qty)
        bump_season(conn, uid, 1)  # v2: jual = +1 poin musim
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Terjual {qty}x {item} — +{total:,} Koin! 🪙"}

    if action == "create_listing":  # pasang jualan di Pasar Pemain
        item = str(body.get("item", ""))
        qty = int(body.get("qty", 0) or 0)
        price_each = int(body.get("price_each", 0) or 0)
        if (item not in CROPS and item not in ANIMAL_PRODUCTS
                and item not in PRODUCTS):
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
        gems = 2 if streak >= 7 else 0  # v2: streak 7 hari -> +2 Permata
        add_xp_coins(conn, uid, xp=20, coins=bonus, gems=gems)
        msg = f"Bonus login hari ke-{streak}: +{bonus} Koin, +20 XP! 🎁"
        if gems:
            msg += f" +{gems} 💎 Permata Panen!"
        return {"ok": True, "state": build_state(conn, user), "message": msg}

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

    # ==================== v2: bangunan produksi ====================
    if action == "buy_building":
        kind = str(body.get("kind", ""))
        if kind not in BUILDINGS:
            return {"error": "invalid_values"}
        b = BUILDINGS[kind]
        if level_from_xp(int(row["xp"])) < b["min_level"]:
            return {"error": "level_locked"}
        if conn.execute("SELECT 1 FROM buildings WHERE user_id = ? AND kind = ?",
                        (uid, kind)).fetchone():
            return {"error": "already_owned"}
        if row["coins"] < b["price"]:
            return {"error": "insufficient_coins"}
        conn.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?",
                     (b["price"], uid))
        conn.execute("INSERT INTO buildings (user_id, kind, level, created_at)"
                     " VALUES (?, ?, 1, ?)", (uid, kind, now_ts()))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"{b['emoji']} {b['name']} dibangun! 🏭"}

    if action == "upgrade_building":
        kind = str(body.get("kind", ""))
        if kind not in BUILDINGS:
            return {"error": "invalid_values"}
        brow = conn.execute("SELECT level FROM buildings WHERE user_id = ?"
                            " AND kind = ?", (uid, kind)).fetchone()
        if not brow:
            return {"error": "not_owned"}
        if brow["level"] >= BUILDING_MAX_LEVEL:
            return {"error": "max_level"}
        cost = BUILDINGS[kind]["price"] // 2
        if row["coins"] < cost:
            return {"error": "insufficient_coins"}
        conn.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?",
                     (cost, uid))
        conn.execute("UPDATE buildings SET level = level + 1"
                     " WHERE user_id = ? AND kind = ?", (uid, kind))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"{BUILDINGS[kind]['name']} naik ke Level"
                           f" {brow['level'] + 1}! Kapasitas job +1. 🏭"}

    if action == "start_production":
        kind = str(body.get("kind", ""))
        if kind not in BUILDINGS:
            return {"error": "invalid_values"}
        b = BUILDINGS[kind]
        brow = conn.execute("SELECT level FROM buildings WHERE user_id = ?"
                            " AND kind = ?", (uid, kind)).fetchone()
        if not brow:
            return {"error": "not_owned"}
        running = conn.execute(
            "SELECT COUNT(*) FROM production_jobs WHERE user_id = ?"
            " AND building = ? AND status = 'running'",
            (uid, kind)).fetchone()[0]
        if running >= brow["level"]:
            return {"error": "no_capacity"}
        inv = _json_loads(row["inventory"], {})
        for item, need in b["recipe"].items():
            if inv.get(item, 0) < need:
                return {"error": "insufficient_item"}
        for item, need in b["recipe"].items():
            inv[item] -= need
        conn.execute("UPDATE users SET inventory = ? WHERE user_id = ?",
                     (json.dumps(inv), uid))
        # v2: skill Produksi -10% waktu per level
        prod_skill = skill_level(_json_loads(row["skills"], {}), "produksi")
        secs = int(b["time"] * (1 - 0.10 * prod_skill))
        conn.execute(
            "INSERT INTO production_jobs (user_id, building, recipe, qty,"
            " started_at, ready_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, kind, json.dumps(b["recipe"]), b["output_qty"],
             now_ts(), now_ts() + secs))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Produksi {PRODUCTS[b['output']][1]}"
                           f" {PRODUCTS[b['output']][0]} dimulai"
                           f" — siap {secs // 60} menit lagi! 🏭"}

    if action == "collect_production":
        try:
            jid = int(body.get("job_id", 0) or 0)
        except (TypeError, ValueError):
            return {"error": "invalid_values"}
        job = conn.execute(
            "SELECT * FROM production_jobs WHERE id = ? AND user_id = ?"
            " AND status = 'running'", (jid, uid)).fetchone()
        if not job:
            return {"error": "not_found"}
        if now_ts() < int(job["ready_at"]):
            return {"error": "not_ready"}
        b = BUILDINGS[job["building"]]
        inv = _json_loads(row["inventory"], {})
        inv[b["output"]] = inv.get(b["output"], 0) + int(job["qty"])
        conn.execute("UPDATE users SET inventory = ?,"
                     " produce_count = produce_count + 1,"
                     " total_produced = total_produced + ? WHERE user_id = ?",
                     (json.dumps(inv), int(job["qty"]), uid))
        conn.execute("UPDATE production_jobs SET status = 'done' WHERE id = ?",
                     (jid,))
        conn.commit()
        bump_mission(conn, uid, "produksi", 1)
        bump_season(conn, uid, 3)  # v2: produksi selesai = +3 poin musim
        add_xp_coins(conn, uid, xp=25)
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Produksi selesai! +{job['qty']}x"
                           f" {PRODUCTS[b['output']][1]}"
                           f" {PRODUCTS[b['output']][0]} (+25 XP) 🏭"}

    # ==================== v2: skill tree ====================
    if action == "learn_skill":
        branch = str(body.get("branch", ""))
        if branch not in SKILL_BRANCHES:
            return {"error": "invalid_values"}
        skills = _json_loads(row["skills"], {})
        if skill_level(skills, branch) >= SKILL_MAX:
            return {"error": "max_level"}
        if int(row["skill_points"]) < 1:
            return {"error": "no_skill_points"}
        skills[branch] = skill_level(skills, branch) + 1
        conn.execute("UPDATE users SET skills = ?, skill_points = skill_points - 1"
                     " WHERE user_id = ?", (json.dumps(skills), uid))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Skill {SKILL_BRANCHES[branch][1]}"
                           f" {SKILL_BRANCHES[branch][0]} naik ke"
                           f" Level {skills[branch]}! ⭐"}

    # ==================== v2: Permata Panen ====================
    if action == "convert_gems":
        try:
            n = int(body.get("gems", 0) or 0)
        except (TypeError, ValueError):
            return {"error": "invalid_values"}
        if n < 1 or n > GEM_CONVERT_MAX_DAY:
            return {"error": "invalid_values"}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        converted = 0
        if row["gems_convert_day"] == today:
            converted = int(row["gems_converted_today"] or 0)
        if converted + n > GEM_CONVERT_MAX_DAY:
            return {"error": "daily_limit"}
        if int(row["gems"]) < n:
            return {"error": "insufficient_gems"}
        conn.execute(
            "UPDATE users SET gems = gems - ?, coins = coins + ?,"
            " gems_converted_today = ?, gems_convert_day = ?"
            " WHERE user_id = ?",
            (n, n * GEM_TO_COINS, converted + n, today, uid))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Tukar {n} 💎 jadi +{n * GEM_TO_COINS:,} Koin! 💱"}

    # ==================== v2: achievement ====================
    if action == "claim_achievement":
        try:
            aid = int(body.get("ach_id", 0) or 0)
        except (TypeError, ValueError):
            return {"error": "invalid_values"}
        ach = next((a for a in ACHIEVEMENTS_V2 if a[0] == aid), None)
        if not ach:
            return {"error": "invalid_values"}
        _id, _t, _d, kind, target, coins, gems = ach
        prog = achievement_progress(conn, row)
        if prog.get(kind, 0) < target:
            return {"error": "not_done"}
        if conn.execute("SELECT 1 FROM achievements_claimed"
                        " WHERE user_id = ? AND ach_id = ?",
                        (uid, aid)).fetchone():
            return {"error": "already_claimed"}
        conn.execute("INSERT INTO achievements_claimed"
                     " (user_id, ach_id, claimed_at) VALUES (?, ?, ?)",
                     (uid, aid, now_ts()))
        add_xp_coins(conn, uid, coins=coins, gems=gems)
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Achievement '{_t}' diklaim! +{coins:,} Koin"
                           + (f" +{gems} 💎" if gems else "") + " 🏆"}

    # ==================== v2: guild ====================
    if action == "create_guild":
        name = str(body.get("name", "") or "").strip()
        if len(name) < 3 or len(name) > 20:
            return {"error": "invalid_values"}
        if int(row["guild_id"] or 0) > 0:
            return {"error": "already_in_guild"}
        if row["coins"] < GUILD_CREATE_COST:
            return {"error": "insufficient_coins"}
        if conn.execute("SELECT 1 FROM guilds WHERE name = ?",
                        (name,)).fetchone():
            return {"error": "name_taken"}
        conn.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?",
                     (GUILD_CREATE_COST, uid))
        cur = conn.execute(
            "INSERT INTO guilds (name, leader_id, created_at)"
            " VALUES (?, ?, ?)", (name, uid, now_ts()))
        gid = cur.lastrowid
        conn.execute("INSERT INTO guild_members (guild_id, user_id, joined_at)"
                     " VALUES (?, ?, ?)", (gid, uid, now_ts()))
        conn.execute("UPDATE users SET guild_id = ? WHERE user_id = ?",
                     (gid, uid))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Guild '{name}' dibuat! Kamu jadi leader. 🤝"}

    if action == "join_guild":
        try:
            gid = int(body.get("guild_id", 0) or 0)
        except (TypeError, ValueError):
            return {"error": "invalid_values"}
        if int(row["guild_id"] or 0) > 0:
            return {"error": "already_in_guild"}
        g = conn.execute("SELECT id FROM guilds WHERE id = ?",
                         (gid,)).fetchone()
        if not g:
            return {"error": "not_found"}
        count = conn.execute("SELECT COUNT(*) FROM guild_members"
                             " WHERE guild_id = ?", (gid,)).fetchone()[0]
        if count >= GUILD_MAX_MEMBERS:
            return {"error": "guild_full"}
        conn.execute("INSERT INTO guild_members (guild_id, user_id, joined_at)"
                     " VALUES (?, ?, ?)", (gid, uid, now_ts()))
        conn.execute("UPDATE users SET guild_id = ? WHERE user_id = ?",
                     (gid, uid))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": "Kamu bergabung dengan guild! 🤝"}

    if action == "leave_guild":
        gid = int(row["guild_id"] or 0)
        if not gid:
            return {"error": "not_in_guild"}
        g = conn.execute("SELECT leader_id FROM guilds WHERE id = ?",
                         (gid,)).fetchone()
        conn.execute("DELETE FROM guild_members WHERE guild_id = ?"
                     " AND user_id = ?", (gid, uid))
        conn.execute("UPDATE users SET guild_id = 0 WHERE user_id = ?",
                     (uid,))
        # Kalau leader keluar dan guild kosong, hapus guild-nya
        left = conn.execute("SELECT COUNT(*) FROM guild_members"
                            " WHERE guild_id = ?", (gid,)).fetchone()[0]
        if left == 0:
            conn.execute("DELETE FROM guilds WHERE id = ?", (gid,))
        elif g and int(g["leader_id"]) == uid:
            # serahkan leader ke anggota paling lama
            nxt = conn.execute("SELECT user_id FROM guild_members"
                               " WHERE guild_id = ? ORDER BY joined_at ASC"
                               " LIMIT 1", (gid,)).fetchone()
            if nxt:
                conn.execute("UPDATE guilds SET leader_id = ? WHERE id = ?",
                             (nxt["user_id"], gid))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": "Kamu keluar dari guild."}

    if action == "claim_guild_reward":
        info = guild_info(conn, uid)
        if not info:
            return {"error": "not_in_guild"}
        if not info["goal_reached"]:
            return {"error": "goal_not_reached"}
        if info["reward_claimed_this_week"]:
            return {"error": "already_claimed"}
        week = season_now()["week"]
        conn.execute("UPDATE guild_members SET claimed_week = ?"
                     " WHERE guild_id = ? AND user_id = ?",
                     (week, info["id"], uid))
        add_xp_coins(conn, uid, gems=GUILD_REWARD_GEMS)
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Tujuan guild tercapai! +{GUILD_REWARD_GEMS} 💎"
                           " untuk semua anggota! 🤝"}

    # ==================== v2: turnamen musim ====================
    if action == "claim_season_reward":
        ensure_season(conn, uid, row)
        row = conn.execute("SELECT * FROM users WHERE user_id = ?",
                           (uid,)).fetchone()
        week = season_now()["week"]
        if int(row["claimed_season"]) == week:
            return {"error": "already_claimed"}
        pts = int(row["season_points"])
        if pts < TOURNAMENT_MIN_POINTS:
            return {"error": "not_enough_points"}
        rank = season_rank(conn, uid) or 999
        if rank == 1:
            gems = 50
        elif rank <= 3:
            gems = 30
        elif rank <= 10:
            gems = 15
        else:
            gems = 5
        conn.execute("UPDATE users SET claimed_season = ? WHERE user_id = ?",
                     (week, uid))
        add_xp_coins(conn, uid, gems=gems)
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Hadiah turnamen musim: peringkat #{rank}"
                           f" ({pts} poin) — +{gems} 💎! 🏆"}

    # ==================== v3: bahasa ====================
    if action == "set_language":
        lang = norm_lang(str(body.get("lang", "")))
        conn.execute("UPDATE users SET language = ? WHERE user_id = ?",
                     (lang, uid))
        conn.commit()
        return {"ok": True, "state": build_state(conn, user),
                "message": "OK"}

    # --- Aksi pakai energi: tanam/siram/pupuk/panen/usir hama/hewan ---
    if action in ("plant", "water", "fertilize", "harvest", "pest",
                  "feed_animal", "collect_animal", "fish", "mine"):
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
        if level_from_xp(int(row["xp"])) < CROP_UNLOCK.get(crop, 1):
            return {"error": "level_locked"}
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
        # v2: skill Bertani +1 hasil per 2 level; Musim Panen +10% hasil
        skills = _json_loads(row["skills"], {})
        qty += skill_level(skills, "bertani") // 2
        if season_now()["kind"] == "panen":
            qty = max(1, round(qty * 1.10))
        name, emoji, _s, _g, _y, xp, _b = CROPS[plot["crop"]]
        inv = _json_loads(row["inventory"], {})
        inv[plot["crop"]] = inv.get(plot["crop"], 0) + qty
        extra_msg = ""
        # v2: mutasi — 4% dapat benih Melon Emas, 0,5% Panen Legendaris (+1 💎)
        if random.random() < MUTATION_CHANCE:
            seeds = _json_loads(row["seeds"], {})
            seeds["melon"] = seeds.get("melon", 0) + 1
            conn.execute("UPDATE users SET seeds = ? WHERE user_id = ?",
                         (json.dumps(seeds), uid))
            extra_msg += " 🍈 Mutasi! Dapat 1 Benih Melon Emas!"
        legendary = False
        if random.random() < LEGENDARY_CHANCE:
            legendary = True
            extra_msg += " ✨ PANEN LEGENDARIS! +1 💎 Permata Panen!"
        melon_bonus = 1 if plot["crop"] == "melon" else 0
        conn.execute(
            "UPDATE users SET inventory = ?, total_harvest = total_harvest + 1,"
            " melon_harvest = melon_harvest + ? WHERE user_id = ?",
            (json.dumps(inv), melon_bonus, uid))
        conn.execute(
            "UPDATE plots SET crop = NULL, planted_at = 0, water_count = 0,"
            " fertilized = 0, pest_at = 0, pest_damage = 0,"
            " speed_boost_until = 0 WHERE user_id = ? AND idx = ?",
            (uid, idx))
        conn.commit()
        # XP lewat add_xp_coins supaya naik level memberi poin skill (v2)
        # dan bonus guild +5% XP ikut jalan; Permata legendaris juga di sini.
        add_xp_coins(conn, uid, xp=xp, gems=1 if legendary else 0)
        bump_mission(conn, uid, "panen", 1)
        bump_season(conn, uid, 1)  # v2: panen = +1 poin musim
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Panen {qty}x {emoji} {name}! +{xp} XP 🧺"
                           + extra_msg}

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
        _pm = ANIMAL_PRODUCTS.get(product) or PRODUCTS.get(product)
        return {"ok": True, "state": build_state(conn, user),
                "message": f"Kumpulkan 1x {_pm[1]} {_pm[0]}! +10 XP"}

    # --- v2: memancing & menambang (cooldown server-side, hadiah acak) ---
    if action in ("fish", "mine"):
        skills = _json_loads(row["skills"], {})
        jel = skill_level(skills, "jelajah")
        season = season_now()
        cd = FISH_COOLDOWN_SECS if action == "fish" else MINE_COOLDOWN_SECS
        cd = int(cd * (1 - 0.10 * jel))
        if season["kind"] == "panas":
            cd = int(cd * 0.75)
        last_col = "last_fish_at" if action == "fish" else "last_mine_at"
        last = int(row[last_col] or 0)
        if now - last < cd:
            return {"error": "cooldown"}
        inv = _json_loads(row["inventory"], {})
        gems = 0
        if action == "fish":
            qty = random.randint(1, 3)
            qty = max(1, round(qty * (1 + 0.10 * jel)))
            if random.random() < 0.10:
                item, got = "ikan_emas", 1
                msg = f"Mancing dapat {qty}x 🐟 Ikan + 1x ✨ Ikan Emas! 🎣"
            else:
                item, got = "ikan", qty
                msg = f"Mancing dapat {qty}x 🐟 Ikan! 🎣"
            inv[item] = inv.get(item, 0) + got
            count_col, mission_kind = "fish_count", "ikan"
        else:
            qty = random.randint(1, 3)
            qty = max(1, round(qty * (1 + 0.10 * jel)))
            if random.random() < 0.02:
                gems = 1
                msg = f"Menambang dapat {qty}x 🪨 Batu + 1 💎 Permata! ⛏️"
                inv["batu"] = inv.get("batu", 0) + qty
            elif random.random() < 0.15:
                msg = f"Menambang dapat 1x ⛏️ Bijih Emas! ⛏️"
                inv["bijih_emas"] = inv.get("bijih_emas", 0) + 1
            else:
                msg = f"Menambang dapat {qty}x 🪨 Batu! ⛏️"
                inv["batu"] = inv.get("batu", 0) + qty
            count_col, mission_kind = "mine_count", "tambang"
        conn.execute(
            f"UPDATE users SET inventory = ?, {last_col} = ?,"
            f" {count_col} = {count_col} + 1 WHERE user_id = ?",
            (json.dumps(inv), now, uid))
        conn.commit()
        add_xp_coins(conn, uid, xp=15, gems=gems)
        bump_mission(conn, uid, mission_kind, 1)
        bump_season(conn, uid, 2)  # v2: mancing/menambang = +2 poin musim
        return {"ok": True, "state": build_state(conn, user),
                "message": msg + " (+15 XP)"}

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
            items = list(CROPS) + list(ANIMAL_PRODUCTS) + list(PRODUCTS)
            self._send_json(200, {"ok": True,
                                  "market": {it: market_price(it)
                                             for it in items},
                                  "weather": weather_now(),
                                  "event": event_now()})
        elif path == "/api/guilds":  # v2
            conn = db()
            try:
                self._send_json(200, {"ok": True,
                                      "guilds": list_guilds(conn)})
            finally:
                conn.close()
        elif path == "/api/season":  # v2
            conn = db()
            try:
                self._send_json(200, {"ok": True,
                                      "season": season_now(),
                                      "top": season_leaderboard(conn, 20)})
            finally:
                conn.close()
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
            elif path == "/api/referral":  # v3
                self._send_json(200, {"ok": True,
                                      "referral": referral_info(
                                          conn, int(user["id"]))})
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
# ---------------------------------------------------------------------------
# Bot Telegram — teks 5 bahasa (v3)
# ---------------------------------------------------------------------------
BOT_TEXTS = {
    "play": {
        "id": "🌾 Main Game", "en": "🌾 Play Game", "ru": "🌾 Играть",
        "es": "🌾 Jugar", "pt": "🌾 Jogar",
    },
    "howto_btn": {
        "id": "❓ Cara Main", "en": "❓ How to Play", "ru": "❓ Как играть",
        "es": "❓ Cómo jugar", "pt": "❓ Como jogar",
    },
    "start": {
        "id": (
            "Halo {name}! 👋\n\n"
            "Selamat datang di *Harvest Farm — Kebun Kripto* 🌾\n"
            "Game bertani di Telegram: tanam, panen, jual — kumpulkan\n"
            "Koin & Permata Panen, tukar jadi DOGE via FaucetPay.\n\n"
            "*Cara main singkat:*\n"
            "🌱 Tanam benih → 💧 siram & 🧪 pupuk → 🧺 panen\n"
            "💰 Jual di pasar saat harga tinggi\n"
            "🐔 Pelihara hewan, 🏭 olah jadi Roti/Keju/Saus\n"
            "🎣 Mancing & ⛏️ menambang, 🤝 gabung guild\n"
            "🎁 Undang teman lewat link referral di dalam game\n\n"
            "*Mata uang:*\n"
            "🪙 Koin — dari panen, jualan & misi\n"
            "💎 Permata Panen — hanya dari main (1 💎 = 500 Koin)\n\n"
            "*Tukar jadi DOGE:*\n"
            "200.000 Koin = 0,01 DOGE • Min Level 5 • Maks 1x/7 hari\n"
            "• Disetujui admin • Bukan janji profit • Tanpa deposit\n\n"
            "*Aturan:* main jujur, dilarang bot/cheat. Iklan AdGram\n"
            "opsional untuk bonus — bukan syarat main.\n\n"
            "👇 Ketuk *Main Game* untuk mulai bertani!"
        ),
        "en": (
            "Hello {name}! 👋\n\n"
            "Welcome to *Harvest Farm — Crypto Garden* 🌾\n"
            "A farming game on Telegram: plant, harvest, sell — collect\n"
            "Coins & Harvest Gems, exchange them for DOGE via FaucetPay.\n\n"
            "*Quick guide:*\n"
            "🌱 Plant seeds → 💧 water & 🧪 fertilize → 🧺 harvest\n"
            "💰 Sell at the market when prices are high\n"
            "🐔 Raise animals, 🏭 process them into Bread/Cheese/Sauce\n"
            "🎣 Fish & ⛏️ mine, 🤝 join a guild\n"
            "🎁 Invite friends with your referral link inside the game\n\n"
            "*Currencies:*\n"
            "🪙 Coins — from harvests, sales & quests\n"
            "💎 Harvest Gems — earned only by playing (1 💎 = 500 Coins)\n\n"
            "*Exchange for DOGE:*\n"
            "200,000 Coins = 0.01 DOGE • Min Level 5 • Max 1×/7 days\n"
            "• Admin approval required • Not a profit promise • No deposit\n\n"
            "*Rules:* play fair, no bots/cheats. AdGram ads are\n"
            "optional for bonuses — never required to play.\n\n"
            "👇 Tap *Play Game* to start farming!"
        ),
        "ru": (
            "Привет, {name}! 👋\n\n"
            "Добро пожаловать в *Harvest Farm — Криптоферма* 🌾\n"
            "Игра-ферма в Telegram: сажай, собирай урожай, продавай —\n"
            "копи Монеты и Кристаллы урожая, меняй их на DOGE через FaucetPay.\n\n"
            "*Кратко об игре:*\n"
            "🌱 Посади семена → 💧 полей и 🧪 удобри → 🧺 собери урожай\n"
            "💰 Продавай на рынке, когда цены высокие\n"
            "🐔 Разводи животных, 🏭 перерабатывай в Хлеб/Сыр/Соус\n"
            "🎣 Рыбачь и ⛏️ добывай руду, 🤝 вступай в гильдию\n"
            "🎁 Приглашай друзей по реферальной ссылке в игре\n\n"
            "*Валюта:*\n"
            "🪙 Монеты — за урожай, продажи и задания\n"
            "💎 Кристаллы урожая — только за игру (1 💎 = 500 монет)\n\n"
            "*Обмен на DOGE:*\n"
            "200 000 монет = 0,01 DOGE • Мин. уровень 5 • Макс. 1 раз в 7 дней\n"
            "• Нужно одобрение админа • Это не обещание прибыли • Без вложений\n\n"
            "*Правила:* играй честно, боты и читы запрещены. Реклама AdGram\n"
            "необязательна — это бонус, а не условие игры.\n\n"
            "👇 Нажми *Играть*, чтобы начать!"
        ),
        "es": (
            "¡Hola, {name}! 👋\n\n"
            "Bienvenido a *Harvest Farm — Granja Cripto* 🌾\n"
            "Un juego de granja en Telegram: planta, cosecha, vende —\n"
            "gana Monedas y Gemas de Cosecha y cámbialas por DOGE con FaucetPay.\n\n"
            "*Guía rápida:*\n"
            "🌱 Planta semillas → 💧 riega y 🧪 abona → 🧺 cosecha\n"
            "💰 Vende en el mercado cuando los precios estén altos\n"
            "🐔 Cría animales, 🏭 procesa Pan/Queso/Salsa\n"
            "🎣 Pesca y ⛏️ mina, 🤝 únete a un gremio\n"
            "🎁 Invita amigos con tu enlace de referidos dentro del juego\n\n"
            "*Monedas:*\n"
            "🪙 Monedas — de cosechas, ventas y misiones\n"
            "💎 Gemas de Cosecha — solo se ganan jugando (1 💎 = 500 monedas)\n\n"
            "*Cambiar por DOGE:*\n"
            "200.000 monedas = 0,01 DOGE • Niv. mín. 5 • Máx. 1×/7 días\n"
            "• Requiere aprobación del admin • No es promesa de ganancias • Sin depósito\n\n"
            "*Reglas:* juega limpio, sin bots ni trampas. Los anuncios de AdGram\n"
            "son opcionales para bonos — nunca obligatorios para jugar.\n\n"
            "👇 Toca *Jugar* para empezar a cultivar."
        ),
        "pt": (
            "Olá, {name}! 👋\n\n"
            "Bem-vindo ao *Harvest Farm — Fazenda Cripto* 🌾\n"
            "Um jogo de fazenda no Telegram: plante, colha, venda —\n"
            "ganhe Moedas e Gemas da Colheita e troque por DOGE via FaucetPay.\n\n"
            "*Guia rápido:*\n"
            "🌱 Plante sementes → 💧 regue e 🧪 adube → 🧺 colha\n"
            "💰 Venda no mercado quando os preços estiverem altos\n"
            "🐔 Crie animais, 🏭 processe em Pão/Queijo/Molho\n"
            "🎣 Pesque e ⛏️ minere, 🤝 entre para uma guilda\n"
            "🎁 Convide amigos com seu link de indicação dentro do jogo\n\n"
            "*Moedas:*\n"
            "🪙 Moedas — de colheitas, vendas e missões\n"
            "💎 Gemas da Colheita — só se ganha jogando (1 💎 = 500 moedas)\n\n"
            "*Trocar por DOGE:*\n"
            "200.000 moedas = 0,01 DOGE • Nív. mín. 5 • Máx. 1×/7 dias\n"
            "• Precisa de aprovação do admin • Não é promessa de lucro • Sem depósito\n\n"
            "*Regras:* jogue limpo, sem bots ou trapaças. Os anúncios AdGram\n"
            "são opcionais para bônus — nunca obrigatórios para jogar.\n\n"
            "👇 Toque em *Jogar* para começar a cultivar!"
        ),
    },
    "ref_joined": {
        "id": "🎁 Kamu masuk lewat undangan teman — bonus +200 Koin sudah masuk ke kebunmu! Kalau kamu mencapai Level 3, temanmu itu dapat +500 Koin + 2 💎. Ajak temanmu juga lewat tab Referral di dalam game!",
        "en": "🎁 You joined through a friend's invitation — a +200 Coin bonus is already in your farm! When you reach Level 3, your friend gets +500 Coins + 2 💎. Invite your friends too, from the Referral tab in the game!",
        "ru": "🎁 Ты пришёл по приглашению друга — бонус +200 монет уже на твоей ферме! Когда ты достигнешь 3-го уровня, твой друг получит +500 монет и 2 💎. Приглашай друзей — ссылка во вкладке «Рефералка» в игре!",
        "es": "🎁 Entraste por la invitación de un amigo — ¡el bono de +200 monedas ya está en tu granja! Cuando llegues al Nivel 3, tu amigo recibirá +500 monedas y 2 💎. ¡Invita a tus amigos desde la pestaña Referidos del juego!",
        "pt": "🎁 Você entrou pelo convite de um amigo — o bônus de +200 moedas já está na sua fazenda! Quando você chegar ao Nível 3, seu amigo ganha +500 moedas e 2 💎. Convide seus amigos na aba Indicação do jogo!",
    },
    "ref_self": {
        "id": "⚠️ Link referral itu milikmu sendiri — tidak bisa dipakai untuk mengundang dirimu sendiri. Bagikan link itu ke temanmu ya!",
        "en": "⚠️ That referral link is your own — you can't invite yourself with it. Share it with your friends instead!",
        "ru": "⚠️ Эта реферальная ссылка — твоя собственная, пригласить самого себя нельзя. Поделись ею с друзьями!",
        "es": "⚠️ Ese enlace de referidos es el tuyo — no puedes invitarte a ti mismo. ¡Compártelo con tus amigos!",
        "pt": "⚠️ Esse link de indicação é o seu — você não pode convidar a si mesmo. Compartilhe com seus amigos!",
    },
    "help": {
        "id": (
            "Cara main Harvest Farm:\n"
            "1. Ketuk Main Game.\n"
            "2. Tanam benih di petak kosong (pakai 1 Energi).\n"
            "3. Siram biar tumbuh 25% lebih cepat, beri pupuk +50%.\n"
            "4. Usir hama sebelum hasil panen berkurang!\n"
            "5. Panen, lalu jual di Pasar NPC saat harga tinggi,\n"
            "   atau pasang jualan sendiri di Pasar Pemain.\n"
            "6. Beli hewan, beri pakan, kumpulkan telur/susu/wol.\n"
            "7. Nonton iklan AdGram buat +20 Energi.\n"
            "8. Di Dompet, tarik Koin jadi DOGE via FaucetPay.\n"
            "9. v2: bangun Toko Roti / Pabrik Susu / Dapur Saus di tab Produksi,\n"
            "   mancing & menambang tiap 30 menit, gabung guild, dan rebut\n"
            "   hadiah turnamen mingguan (Permata Panen 💎).\n"
            "10. Undang teman dari tab Referral: temanmu dapat +200 Koin\n"
            "    saat masuk, dan kamu dapat +500 Koin + 2 💎 saat dia capai Level 3.\n\n"
            "Perintah: /start /help /saldo"
        ),
        "en": (
            "How to play Harvest Farm:\n"
            "1. Tap Play Game.\n"
            "2. Plant seeds in empty plots (uses 1 Energy).\n"
            "3. Water for +25% growth, fertilize for +50%.\n"
            "4. Chase pests away before they cut your harvest!\n"
            "5. Harvest, then sell at the NPC Market when prices are high,\n"
            "   or list your own goods at the Player Market.\n"
            "6. Buy animals, feed them, collect eggs/milk/wool.\n"
            "7. Watch AdGram ads for +20 Energy.\n"
            "8. In the Wallet tab, withdraw Coins as DOGE via FaucetPay.\n"
            "9. v2: build the Bakery / Dairy / Sauce Kitchen in the Factory tab,\n"
            "   fish & mine every 30 minutes, join a guild, and win\n"
            "   weekly tournament prizes (Harvest Gems 💎).\n"
            "10. Invite friends from the Referral tab: your friend gets +200 Coins\n"
            "    when joining, and you get +500 Coins + 2 💎 when they reach Level 3.\n\n"
            "Commands: /start /help /saldo"
        ),
        "ru": (
            "Как играть в Harvest Farm:\n"
            "1. Нажми «Играть».\n"
            "2. Посади семена на пустых грядках (1 энергия).\n"
            "3. Поливай: рост +25%, удобряй: +50%.\n"
            "4. Прогоняй вредителей, пока они не испортили урожай!\n"
            "5. Собери урожай и продай на рынке NPC, когда цены высокие,\n"
            "   или выстави свой товар на рынке игроков.\n"
            "6. Купи животных, корми их, собирай яйца/молоко/шерсть.\n"
            "7. Смотри рекламу AdGram и получай +20 энергии.\n"
            "8. Во вкладке «Кошелёк» выводи монеты в DOGE через FaucetPay.\n"
            "9. v2: построй Пекарню / Молочную фабрику / Кухню соусов во вкладке\n"
            "   «Производство», рыбачь и добывай руду каждые 30 минут, вступай\n"
            "   в гильдию и выигрывай еженедельные турниры (Кристаллы 💎).\n"
            "10. Приглашай друзей из вкладки «Рефералка»: друг получит +200 монет\n"
            "    при входе, а ты — +500 монет и 2 💎, когда он достигнет 3-го уровня.\n\n"
            "Команды: /start /help /saldo"
        ),
        "es": (
            "Cómo jugar a Harvest Farm:\n"
            "1. Toca Jugar.\n"
            "2. Planta semillas en parcelas vacías (usa 1 de Energía).\n"
            "3. Riega para +25% de crecimiento, abona para +50%.\n"
            "4. ¡Ahuyenta las plagas antes de que reduzcan tu cosecha!\n"
            "5. Cosecha y vende en el Mercado NPC cuando los precios estén altos,\n"
            "   o publica tus productos en el Mercado de Jugadores.\n"
            "6. Compra animales, aliméntalos y recoge huevos/leche/lana.\n"
            "7. Mira anuncios de AdGram para +20 de Energía.\n"
            "8. En la pestaña Billetera, retira Monedas como DOGE con FaucetPay.\n"
            "9. v2: construye la Panadería / Lechería / Cocina de Salsas en Producción,\n"
            "   pesca y mina cada 30 minutos, únete a un gremio y gana\n"
            "   premios semanales (Gemas de Cosecha 💎).\n"
            "10. Invita amigos desde Referidos: tu amigo recibe +200 monedas\n"
            "    al entrar, y tú +500 monedas y 2 💎 cuando llegue al Nivel 3.\n\n"
            "Comandos: /start /help /saldo"
        ),
        "pt": (
            "Como jogar Harvest Farm:\n"
            "1. Toque em Jogar.\n"
            "2. Plante sementes em canteiros vazios (usa 1 de Energia).\n"
            "3. Regue para +25% de crescimento, adube para +50%.\n"
            "4. Afaste as pragas antes que reduzam sua colheita!\n"
            "5. Colha e venda no Mercado NPC quando os preços estiverem altos,\n"
            "   ou anuncie seus produtos no Mercado de Jogadores.\n"
            "6. Compre animais, alimente-os e colete ovos/leite/lã.\n"
            "7. Assista anúncios AdGram para +20 de Energia.\n"
            "8. Na aba Carteira, saque Moedas como DOGE via FaucetPay.\n"
            "9. v2: construa a Padaria / Laticínio / Cozinha de Molhos na aba Produção,\n"
            "   pesque e minere a cada 30 minutos, entre para uma guilda e ganhe\n"
            "   prêmios semanais (Gemas da Colheita 💎).\n"
            "10. Convide amigos na aba Indicação: seu amigo ganha +200 moedas\n"
            "    ao entrar, e você +500 moedas e 2 💎 quando ele chegar ao Nível 3.\n\n"
            "Comandos: /start /help /saldo"
        ),
    },
    "saldo_none": {
        "id": "Kamu belum punya kebun. Ketuk Main Game dulu di /start! 🌱",
        "en": "You don't have a farm yet. Tap Play Game in /start first! 🌱",
        "ru": "У тебя пока нет фермы. Сначала нажми «Играть» в /start! 🌱",
        "es": "Aún no tienes granja. ¡Toca Jugar en /start primero! 🌱",
        "pt": "Você ainda não tem fazenda. Toque em Jogar no /start primeiro! 🌱",
    },
    "saldo": {
        "id": (
            "💰 Saldo Harvest Farm\n\n"
            "🪙 Koin: *{coins:,}*\n"
            "💎 Permata Panen: *{gems:,}* (1 💎 = 500 Koin)\n"
            "⭐ Level {level} ({xp:,} XP)\n"
            "🎯 Poin skill: {skill} • Poin musim: {season}\n"
            "⚡ Energi: {energy}/{max}\n"
            "🔥 Streak login: {streak} hari\n\n"
            "Kurs WD: {unit:,} Koin = {doge} DOGE (min Level {minlevel})"
        ),
        "en": (
            "💰 Harvest Farm Balance\n\n"
            "🪙 Coins: *{coins:,}*\n"
            "💎 Harvest Gems: *{gems:,}* (1 💎 = 500 Coins)\n"
            "⭐ Level {level} ({xp:,} XP)\n"
            "🎯 Skill points: {skill} • Season points: {season}\n"
            "⚡ Energy: {energy}/{max}\n"
            "🔥 Login streak: {streak} days\n\n"
            "Withdraw rate: {unit:,} Coins = {doge} DOGE (min Level {minlevel})"
        ),
        "ru": (
            "💰 Баланс Harvest Farm\n\n"
            "🪙 Монеты: *{coins:,}*\n"
            "💎 Кристаллы урожая: *{gems:,}* (1 💎 = 500 монет)\n"
            "⭐ Уровень {level} ({xp:,} опыта)\n"
            "🎯 Очки навыков: {skill} • Очки сезона: {season}\n"
            "⚡ Энергия: {energy}/{max}\n"
            "🔥 Серия входов: {streak} дн.\n\n"
            "Курс вывода: {unit:,} монет = {doge} DOGE (мин. уровень {minlevel})"
        ),
        "es": (
            "💰 Saldo de Harvest Farm\n\n"
            "🪙 Monedas: *{coins:,}*\n"
            "💎 Gemas de Cosecha: *{gems:,}* (1 💎 = 500 monedas)\n"
            "⭐ Nivel {level} ({xp:,} XP)\n"
            "🎯 Puntos de habilidad: {skill} • Puntos de temporada: {season}\n"
            "⚡ Energía: {energy}/{max}\n"
            "🔥 Racha de inicio: {streak} días\n\n"
            "Tasa de retiro: {unit:,} monedas = {doge} DOGE (niv. mín. {minlevel})"
        ),
        "pt": (
            "💰 Saldo do Harvest Farm\n\n"
            "🪙 Moedas: *{coins:,}*\n"
            "💎 Gemas da Colheita: *{gems:,}* (1 💎 = 500 moedas)\n"
            "⭐ Nível {level} ({xp:,} XP)\n"
            "🎯 Pontos de habilidade: {skill} • Pontos da temporada: {season}\n"
            "⚡ Energia: {energy}/{max}\n"
            "🔥 Sequência de login: {streak} dias\n\n"
            "Taxa de saque: {unit:,} moedas = {doge} DOGE (nív. mín. {minlevel})"
        ),
    },
    "howto": {
        "id": (
            "❓ *Cara Main Harvest Farm*\n\n"
            "• Tanam → Siram → Panen → Jual.\n"
            "• Cuaca berubah tiap jam: Hujan mempercepat, Badai berbahaya.\n"
            "• Hama muncul acak — tap cepat untuk mengusir!\n"
            "• Hewan butuh pakan, kalau lapar tidak berproduksi.\n"
            "• Harga pasar berubah tiap 30 menit — jual saat tinggi!"
        ),
        "en": (
            "❓ *How to Play Harvest Farm*\n\n"
            "• Plant → Water → Harvest → Sell.\n"
            "• Weather changes every hour: Rain speeds growth, Storms are dangerous.\n"
            "• Pests appear at random — tap fast to chase them away!\n"
            "• Animals need feed; hungry animals stop producing.\n"
            "• Market prices change every 30 minutes — sell when high!"
        ),
        "ru": (
            "❓ *Как играть в Harvest Farm*\n\n"
            "• Посади → Полей → Собери → Продай.\n"
            "• Погода меняется каждый час: дождь ускоряет рост, гроза опасна.\n"
            "• Вредители появляются случайно — быстро прогоняй их!\n"
            "• Животным нужен корм: голодные не дают продукцию.\n"
            "• Цены на рынке меняются каждые 30 минут — продавай на пике!"
        ),
        "es": (
            "❓ *Cómo jugar a Harvest Farm*\n\n"
            "• Planta → Riega → Cosecha → Vende.\n"
            "• El clima cambia cada hora: la lluvia acelera, la tormenta es peligrosa.\n"
            "• Las plagas aparecen al azar — ¡tócalas rápido para ahuyentarlas!\n"
            "• Los animales necesitan comida; si tienen hambre, no producen.\n"
            "• Los precios cambian cada 30 minutos — ¡vende cuando estén altos!"
        ),
        "pt": (
            "❓ *Como jogar Harvest Farm*\n\n"
            "• Plante → Regue → Colha → Venda.\n"
            "• O clima muda a cada hora: a chuva acelera o crescimento, a tempestade é perigosa.\n"
            "• Pragas aparecem aleatoriamente — toque rápido para afastá-las!\n"
            "• Os animais precisam de ração; com fome, não produzem.\n"
            "• Os preços mudam a cada 30 minutos — venda quando estiverem altos!"
        ),
    },
}


def bot_text(key: str, lang: str, **kw) -> str:
    """Ambil teks bot untuk kunci & bahasa (fallback Indonesia)."""
    lang = norm_lang(lang)
    tpl = BOT_TEXTS.get(key, {}).get(lang) or BOT_TEXTS.get(key, {}).get("id", "")
    try:
        return tpl.format(**kw) if kw else tpl
    except (KeyError, ValueError):
        return tpl


def main_keyboard(lang: str = "id") -> InlineKeyboardMarkup:
    lang = norm_lang(lang)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(bot_text("play", lang),
                              web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(bot_text("howto_btn", lang),
                              callback_data="howto")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    lang = norm_lang(getattr(user, "language_code", None))
    # v3: referral — /start ref_<telegram_user_id>
    ref_id = None
    for arg in (context.args or []):
        m = re.match(r"ref_(\d+)$", str(arg))
        if m:
            ref_id = int(m.group(1))
            break
    ref_status = None
    conn = db()
    try:
        is_new = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?",
            (user.id,)).fetchone() is None
        get_or_create_user(conn, {"id": user.id, "username": user.username,
                                  "first_name": user.first_name})
        if is_new and ref_id:
            ref_status = apply_referral(conn, user.id, ref_id)
    finally:
        conn.close()
    text = bot_text("start", lang, name=user.first_name)
    if ref_status == "ok":
        text += "\n\n" + bot_text("ref_joined", lang)
    elif ref_status == "self":
        text += "\n\n" + bot_text("ref_self", lang)
    await update.message.reply_text(text, reply_markup=main_keyboard(lang),
                                    parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = norm_lang(getattr(update.effective_user, "language_code", None))
    await update.message.reply_text(bot_text("help", lang))


async def saldo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = norm_lang(getattr(update.effective_user, "language_code", None))
    conn = db()
    try:
        row = conn.execute(
            "SELECT coins, gems, xp, energy, login_streak, skill_points,"
            " season_points FROM users"
            " WHERE user_id = ?", (update.effective_user.id,)).fetchone()
    finally:
        conn.close()
    if not row:
        await update.message.reply_text(bot_text("saldo_none", lang))
        return
    level = level_from_xp(row["xp"])
    await update.message.reply_text(
        bot_text("saldo", lang, coins=row["coins"], gems=row["gems"],
                 level=level, xp=row["xp"],
                 skill=row["skill_points"], season=row["season_points"],
                 energy=row["energy"], max=ENERGY_MAX,
                 streak=row["login_streak"], unit=COINS_PER_UNIT,
                 doge=DOGE_PER_UNIT, minlevel=WITHDRAW_MIN_LEVEL),
        parse_mode="Markdown")


async def howto_callback(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    lang = norm_lang(getattr(update.effective_user, "language_code", None))
    await query.answer()
    await query.edit_message_text(
        bot_text("howto", lang),
        reply_markup=main_keyboard(lang), parse_mode="Markdown")


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
