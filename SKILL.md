---
name: wallet-tracker
description: Pantau on-chain ala GMGN — scan token baru potensial jadi runner, alert trade smart money, dan breakdown wallet (inflow/outflow, side wallet, logika trading). Gunakan setiap kali user menyebut wallet tracker, smart wallet, smart money, copy trade, token baru/memecoin potensial, GMGN, Robinhood chain, tracking wallet on-chain, atau minta pantau/ketahui wallet whale sedang beli/jual — bahkan jika tidak menyebut "wallet-tracker" secara eksplisit.
---

# Wallet Tracker (GMGN-powered)

Tools on-chain read-only di `scripts/wt.py` (Python stdlib, zero-dependency), data via `gmgn-cli`
(official GMGN OpenAPI; chain: sol/bsc/base/eth/**robinhood**/arc/stable). Setup (API key, Telegram):
baca `README.md` di folder skill ini hanya saat user butuh setup.

## Perintah inti

```bash
cd C:/Users/kodokzx/.zcode/skills/wallet-tracker

# 1) Scan token potensial runner (skor 0-100; alert BARU / TRAKSI NAIK)
python scripts/wt.py scan --chains robinhood,bsc,sol

# 2) Cek trade smart money + watchlist sekali (untuk cron: sama tanpa --loop)
python scripts/wt.py watch

# 3) Breakdown wallet — PnL, per-token flow, side wallet, logika trading
python scripts/wt.py analyze 0xADDR --chain robinhood --deep

# 4) Perilaku grup trader pada satu token + deteksi behavioral shift
python scripts/wt.py groups 0xTOKEN --chain robinhood

# 5) Database smart wallet + peta side wallet & koloni (dari alert tersimpan)
python scripts/wt.py colony --top 8 --deep 3

# 6) Dashboard HTML dari data aktual (buka data/dashboard.html di browser)
python scripts/wt.py html

# 7) Kelola watchlist / riwayat alert / tes telegram
python scripts/wt.py watchlist add 0xADDR --chain robinhood --note "kol"
python scripts/wt.py alerts --last 30
python scripts/wt.py test-notify
```

## Alur kerja yang diharapkan user

- **"Scan token baru potensial"** → jalankan `scan`, lalu sajikan top skor sebagai tabel ringkas
  (symbol, chain, mcap, holder, smart-degen, umur) + link dexscreener per token. Sampaikan skor dan
  alasannya secara singkat (smart-degen masuk, turnover panas, dsb). Jangan tampilkan JSON mentah.
- **"Breakdown wallet ini"** → jalankan `analyze --deep`, sajikan sebagai narasi:
  ringkasan PnL/winrate → per-token flow (inflow beli vs outflow jual) → kandidat side wallet
  (transfer berulang + funding source) → pola trading (scalp/swing, DCA, sniping launchpad, pemburu runner).
  User berbahasa Indonesia — sajikan dalam Bahasa Indonesia.
- **"Pantau wallet 0x…"** → `watchlist add`, lalu tawarkan jadwalkan via cron automation
  (`watch` + `scan` tiap 30-60 menit) bila belum ada.
- **Deteksi side wallet**: `analyze` menghitung counterparty transfer berulang + `fund_from_address`
  (sumber dana pertama). Gunakan `--deep` untuk menarik stats wallet-wallet terkait dan
  tunjukkan klasternya.
- **Grup perilaku A/B/C/D** (`groups 0xTOKEN`): klasifikasi trader teratas sebuah token menjadi
  **A-iklan** (tag `kol`), **B-smart-cepat** (`smart_degen`, rata-rata pegang <24 jam),
  **C-akumulasi** (`smart_degen` pegang ≥24 jam — beli di bawah, hold lama), **D-fomo** (tag `fomo`),
  plus BOT (sandwich/sniper) dan R (ritel tanpa label). Sajikan tabel grup + narasi "bacaan
  kelakuan". Snapshot net-flow tiap grup disimpan — saat dijalankan ulang, pembalikan arah
  (zero-cross ≥ $1.5k) dilaporkan sebagai ⚠️ SHIFT dengan interpretasinya (mis. B jual + D beli =
  distribusi ke ritel / exit liquidity; C beli saat turun = akumulasi stealth).
- Alert `watch` otomatis berlabel grup: `[A]`/`[B]`/`[D]`/`[BOT]` pada trade smart money, dan
  alert `SHIFT` muncul saat grup berbalik arah pada token yang dipantau.

## Aturan

- Script **read-only** — tidak pernah menandatangani transaksi. Jangan pernah eksekusi
  perintah swap/cooking gmgn-cli dari skill ini.
- Jangan hardcoded API key di output ke user; baca dari config/env (fallback demo key read-only).
- `gmgn-cli` menolak IPv6: `wt.py` sudah set `NODE_OPTIONS=--dns-result-order=ipv4first`.
- Rate-limit demo key rendah — panggil berurutan dengan jeda (script sudah begitu), jangan paralel.
- Token/wallet di chain `robinhood`: link yang benar adalah `https://dexscreener.com/robinhood/<token>`.
