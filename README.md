# 🌾 Harvest Farm — Kebun Kripto

Game pertanian Telegram Mini App v1 — tanam, siram, panen, jual, tarik jadi
DOGE lewat FaucetPay. Semua teks Bahasa Indonesia, mobile-first.

## Cara main

1. Buka bot Telegram → ketuk **🌾 Main Game**.
2. **Tanam** benih di petak kosong (1 Energi). Modal awal: 500 Koin + 5 benih
   Cabai + 3 benih Jagung.
3. **Siram** (tumbuh 25% lebih cepat), beri **Pupuk** (+50% lebih cepat).
4. **Usir hama** 🐛 yang muncul acak — tap dalam 60 detik, kalau dibiarkan
   hasil panen −50%.
5. **Panen** saat siap (🌱→🌿→🌾→🧺), lalu **jual**:
   - **Pasar NPC** — harga berubah tiap 30 menit (±30%). Jual saat tren naik!
   - **Pasar Pemain (P2P)** — pasang jualan dengan harga sendiri, fee 5%
     saat terbeli, saldo langsung masuk ke penjual.
6. **Hewan**: Ayam 🐔 (telur/30 mnt), Kambing 🐐 (susu/1 jam), Sapi 🐄
   (susu/2 jam). Harus diberi **Pakan** — kalau lapar, produksi berhenti.
7. **Cuaca** berganti tiap jam: ☀️ Cerah (normal), 🌧️ Hujan (tumbuh +30%,
   gratis siram), ⛈️ Badai (tumbuh −20%, risiko rusak 10%), 🔥 Panas
   (butuh siram 2x).
8. **Acara acak**: 🎉 Panen Raya (hasil 2x, 10 mnt), 🛒 Pasar Ramai
   (harga NPC +15%), 🐀 Tikus Sawah (hama lebih sering).
9. **Misi harian** (3 misi acak/hari) + **bonus login** streak 7 hari
   (hari ke-7 = 700 Koin) + Level/XP + achievement + leaderboard.
10. **Energi**: max 50, tiap aksi 1 Energi, regen 1 per 2 menit.
    Nonton iklan AdGram = +20 Energi (juga bisa percepat petak, panen
    ganda, atau benih gratis).
11. **Dompet** → tarik Koin jadi DOGE via FaucetPay (lihat Ekonomi).

## Tanaman

| Tanaman | Benih | Tumbuh | Hasil | XP | Jual dasar |
|---|---|---|---|---|---|
| 🌶️ Cabai | 50 | 2 mnt | 4 | 10 | 30 |
| 🌽 Jagung | 120 | 5 mnt | 5 | 15 | 50 |
| 🍅 Tomat | 250 | 10 mnt | 6 | 25 | 80 |
| 🥕 Wortel | 400 | 15 mnt | 6 | 35 | 110 |
| 🌾 Padi | 800 | 30 mnt | 8 | 60 | 180 |
| 🎃 Labu | 1.500 | 1 jam | 8 | 100 | 350 |
| 🍓 Stroberi | 3.000 | 2 jam | 10 | 180 | 600 |
| ☕ Kopi | 6.000 | 4 jam | 12 | 300 | 1.000 |

Grid awal 3×3, upgrade 4×4 = 5.000 Koin, 5×5 = 20.000 Koin.
Pupuk 300 Koin, Pakan 100 Koin (kenyang 12 jam).
Hewan: Ayam 500, Kambing 2.000, Sapi 5.000 Koin. Telur jual dasar 80,
Susu 250 (harga pasar juga fluktuatif ±30%).

## Ekonomi & kurs withdraw

- **Kurs v1: 200.000 Koin Panen = 0,01 DOGE** (diubah di `bot.py`:
  `COINS_PER_UNIT` / `DOGE_PER_UNIT`).
- Min WD **200.000 Koin** (kelipatan 200.000), min **Level 5**, maks
  **1x per 7 hari**. Tujuan: email/username FaucetPay.
- Alur: pemain ajukan di tab Dompet → status `pending` → admin approve
  di `GET/POST /api/withdrawals/admin?key=WITHDRAW_ADMIN_KEY` →
  server kirim otomatis via FaucetPay API v1
  (`POST https://faucetpay.io/api/v1/send`, form-encoded, `api_key`
  hanya di server) → status `sent`.
- Kalau `FAUCETPAY_API_KEY` kosong, approve tetap tercatat sebagai
  `approved` + alasan dicatat, admin proses manual — request tidak hilang.
  Reject mengembalikan Koin ke pemain.
- ⚠️ **Isi saldo DOGE di akun FaucetPay dulu** sebelum payout otomatis
  bisa jalan, dan pastikan API key punya scope `send`.

## AdGram

- SDK resmi di `index.html`: `https://sad.adsgram.ai/js/sad.min.js`,
  `Adsgram.init({blockId: 'YOUR_ADSGRAM_BLOCK_ID'})` — **placeholder**,
  isi Block ID asli dari partner.adsgram.ai (atau via env
  `ADSGRAM_BLOCK_ID`; backend hanya memberi tahu frontend aktif/tidak).
- Rewarded ad (reward hanya jika `show()` resolve `done=true`):
  +20 Energi, percepat 1 petak 50%, panen ganda 1x, benih gratis 1 pack.
- Server-side: `POST /api/ad-reward` — cooldown 5 menit, maks 10/hari.
- Interstitial setelah panen besar, maks 1x/10 menit, gagal = diam saja.
- Kalau Block ID masih placeholder, tombol iklan menampilkan pesan
  "Iklan belum aktif — isi Block ID AdGram dulu" (tidak crash).

## File

| File | Isi |
|---|---|
| `index.html` | Mini App single-file (44 KB, tanpa build step) |
| `bot.py` | Backend + bot Telegram (SQLite `farm_pay.db`) |
| `requirements.txt` | `python-telegram-bot==21.*` |
| `.env.example` | Contoh env — salin ke `.env`, jangan commit `.env` |
| `README.md` | File ini |

## API (ringkas)

- `GET /` — sajikan `index.html` (praktis untuk test lokal)
- `GET /health` — `{ok, game, weather, event}`
- `GET /api/leaderboard?limit=20`, `GET /api/market`
- `POST /api/state` `{initData|user_id}` — seluruh state pemain
- `POST /api/action` — tanam/siram/pupuk/panen/usir hama/beli/jual/
  pasar pemain/hewan/misi/dll (`{action, ...}`)
- `POST /api/withdraw` `{coins, destination}`
- `POST /api/ad-reward` `{type: energy|speed|double|seed, plot_id?}`
- `GET/POST /api/withdrawals/admin?key=...`

Auth: initData Telegram dicek HMAC-SHA256 pakai `BOT_TOKEN` (cara resmi).
Fallback demo `user_id` dari body hanya aktif kalau `BOT_TOKEN` kosong /
initData tidak valid — **di produksi set `REQUIRE_INITDATA=1`** untuk
mematikannya. Anti-cheat kasar: min interval aksi 5 detik, batas delta
Koin, akun flagged tidak bisa tarik.

## Deploy (langkah Shanks)

1. **Bot baru di @BotFather** → buat bot, salin token → `BOT_TOKEN`.
   Set Menu Button / WebApp URL bot ke URL Mini App.
2. **FaucetPay** → buat akun & faucet baru, buat API key (v1, scope send),
   isi saldo DOGE → `FAUCETPAY_API_KEY`.
3. **AdGram** → daftar di partner.adsgram.ai, buat block iklan, salin
   Block ID → `ADSGRAM_BLOCK_ID` (dan ganti placeholder di `index.html`
   kalau tidak lewat env — frontend membaca `window.HARVESTFARM_API_URL`
   untuk API, atau `?api=` di URL).
4. **Hosting Mini App**: upload `index.html` ke GitHub Pages
   (repo publik, Settings → Pages) → `WEBAPP_URL`.
5. **Hosting backend (Railway)**: buat project dari repo berisi `bot.py`
   + `requirements.txt`, isi semua env di atas (Variables), deploy.
   Railway memberi `$PORT` otomatis. Pastikan `WEBAPP_URL` di env bot
   sama dengan URL GitHub Pages, dan Mini App memanggil API Railway
   (lewat `?api=https://<app>.railway.app` atau `window.HARVESTFARM_API_URL`).

Jalankan lokal:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # isi BOT_TOKEN, WEBAPP_URL, dst.
PORT=8080 .venv/bin/python bot.py
# tanpa BOT_TOKEN: hanya API HTTP yang jalan (mode test lokal)
curl http://127.0.0.1:8080/health
```

## Test yang sudah dijalankan (22 Nov 2026, lokal)

- `python3 -m py_compile bot.py` ✅
- Server lokal port 8091: `GET /health` → 200 ✅, `POST /api/state` → JSON
  valid, user baru (500 Koin, grid 3×3, 9 petak) ✅
- Alur: tanam Cabai → siram → beli benih → AdGram +20 Energi ✅,
  AdGram kedua → `ad_cooldown` (429) ✅, withdraw di bawah Level 5 →
  `need_level` ✅, admin key salah → 403 ✅
- Panen (difast-forward via DB) → 4x Cabai +10 XP ✅, jual NPC 2x →
  +70 Koin ✅, pasang listing P2P → dibeli user lain, penjual terima
  48 Koin (50 − fee 5%) ✅
- Hewan: beli Ayam → beli Pakan → beri makan → kumpulkan telur ✅
  (kumpul sebelum siap → `not_ready` ✅)
- `GET /` menyajikan `index.html` (200) ✅, JS inline lolos
  `node --check` ✅, tidak ada secret hardcoded ✅
- DB test (`farm_pay.db`) & venv dihapus setelah test — folder bersih.

## TODO v2

- [ ] Harga pasar pakai riwayat nyata + grafik garis (bukan 1 titik).
- [ ] Chat/obrolan antar pemain & kirim hadiah ke teman.
- [ ] Tanaman musiman & event mingguan (turnamen panen).
- [ ] Notifikasi Telegram saat tanaman siap panen (job harian).
- [ ] FaucetPay API v2 (kalau key baru hanya support v2) + cek saldo owner.
- [ ] Anti-cheat lebih kuat (state penuh server-side, replay aksi).
- [ ] `REQUIRE_INITDATA=1` wajib di produksi.
- [ ] Mode gelap/terang & suara panen.
