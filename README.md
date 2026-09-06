# Wallet Tracker (GMGN-powered)

Tools pantau on-chain ala GMGN, dijalankan dari ZCode — scanner token potensial jadi runner,
watcher trade smart money, dan breakdown wallet (inflow/outflow, side wallet, logika trading).

## Setup sekali saja

### 1. GMGN API key (gratis, read-only cukup)
1. Buka **https://gmgn.ai/ai** → daftar/login.
2. Jalankan `gmgn-cli config` → dia generate pasangan kunci Ed25519 + link pembuatan API key.
3. Paste public key di form GMGN → salin API key yang diberikan.
4. Simpan di salah satu: `config.json` → `"api_key"`, atau env `GMGN_API_KEY`.

> Tanpa key pribadi, script pakai **demo key** read-only (bisa kadang kena limit).
> Catatan: GMGN menolak koneksi IPv6 — `wt.py` sudah otomatis paksa IPv4.

#### 1b. (Opsional) Posisi terbuka (`holdings`) butuh `GMGN_PRIVATE_KEY`
Endpoint `portfolio holdings` GMGN tergolong *critical-auth* — butuh pasangan kunci Ed25519
yang di-generate **lokal** oleh `gmgn-cli config` (ini kunci tanda-tangan request, BUKAN
private key wallet kamu). Jalankan `gmgn-cli config` sekali, lalu ekspor kuncinya ke env
`GMGN_PRIVATE_KEY` bila diminta. Tanpa ini `analyze` tetap jalan — hanya bagian
"POSISI SAAT INI" yang dilewati (posisi masih bisa diperkirakan dari tabel PER-TOKEN).

### 2. Install CLI (data engine)
```
npm install -g gmgn-cli
```

### 3. Telegram alert (opsional tapi disarankan)
1. Chat @BotFather di Telegram → `/newbot` → salin **bot token**.
2. Kirim satu pesan ke bot kamu, lalu buka
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → cari `"chat":{"id": ...}`.
3. Isi keduanya di `config.json` → `"telegram"`.
4. Tes: `python scripts/wt.py test-notify`

## Perintah

| Perintah | Fungsi |
|---|---|
| `python scripts/wt.py scan` | Scan token baru/momentum berpotensi runner (trenches + trending, skor 0-100, alert token baru/traksi naik) |
| `python scripts/wt.py watch` | Alert trade smart money global + trade wallet di watchlist (buy/sell/transfer besar) |
| `python scripts/wt.py watch --loop --interval 120` | Mode jalan terus |
| `python scripts/wt.py analyze 0x... --chain robinhood --deep` | Breakdown wallet: PnL, per-token flow, side wallet, logika trading |
| `python scripts/wt.py groups 0xTOKEN --chain robinhood` | Perilaku grup trader token (A-iklan/B-smart-cepat/C-akumulasi/D-fomo/BOT) + deteksi behavioral shift |
| `python scripts/wt.py watchlist add 0x... --chain robinhood --note "kol"` | Tambah wallet dipantau |
| `python scripts/wt.py watchlist list` / `remove 0x...` | Lihat/hapus |
| `python scripts/wt.py alerts --last 30` | Riwayat alert (juga tersimpan di `data/alerts.jsonl`) |
| `python scripts/wt.py test-notify` | Tes alert |

## Cara kerja scan (heuristik "potensi runner")
Skor 0-100 dari: jumlah **smart-degen** yang pegang (bobot terbesar), jumlah holder,
turnover volume-1h/mcap, umur token (≤24-48h dapat boost), likuiditas, penalti
`rug_ratio` & konsentrasi top-10 holder, momentum 1h, jarak ke ATH.
Alert tipe: **BARU** (pertama terlihat & muda), **TRAKSI NAIK** (holder +30% atau +2 smart money sejak scan sebelumnya).

## Keamanan
- Script ini **read-only** — tidak pernah menandatangani transaksi.
- API key GMGN adalah kunci tanda-tangan request, bukan private key wallet.

## Deploy ke VPS (Linux)

```bash
git clone https://github.com/kodokzx/gmgn-wallet-tracker.git
cd gmgn-wallet-tracker
cp config.example.json config.json      # isi api_key + telegram di dalamnya
chmod 600 config.json
npm install -g gmgn-cli
```

Scheduler pakai crontab (watch tiap 5 menit, scan per jam):
```
*/5 * * * * cd /path/ke/gmgn-wallet-tracker && python3 scripts/wt.py watch >> data/watch.log 2>&1
0 * * * * cd /path/ke/gmgn-wallet-tracker && python3 scripts/wt.py scan --top 20 >> data/scan.log 2>&1
```

Catatan:
- `config.json` di-gitignore — isinya API key + token Telegram + watchlist pribadi, tidak pernah masuk repo.
- Folder `data/` (state & riwayat alert) juga lokal saja.
- Di VPS sebaiknya pakai API key GMGN pribadi (IP datacenter lebih gampang kena limit demo key).
- Semua perintah read-only — tidak ada private key wallet yang disimpan di mana pun.
