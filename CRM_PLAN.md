# MUTIARA CRM DASHBOARD — WORKING PLAN
_Status: DRAFT untuk review. Belum ada eksekusi._
_Terakhir update: 2026-08-19_

## Keputusan yang sudah disepakati
1. Akses: semua user sama haknya (bisa edit). Role berbeda = fase lanjutan.
2. Update status sales: via dashboard web DAN via perintah WA ke bot.
3. Klasifikasi: bot klasifikasi otomatis (hot/warm/cold/no-respond), tim sales bisa override di dashboard/WA.
4. Akses luar jaringan: Cloudflare Tunnel + domain milik user.
5. URL final pakai domain (subdomain CRM).

---

## A. Arsitektur

```
Meta Ads CTWA → WAHA → bot.py (port 8645, existing)
                          │ tulis
                          ▼
                  sessions/*.json   ← sumber kebenaran percakapan & klasifikasi awal
                          │ dibaca (read-only)
                          ▼
                  dashboard.py (FastAPI, port 8646)  ← BARU
                    ├── merge JSON + SQLite per request
                    ├── /api/leads, /api/stats, /api/leads/{id}/notes
                    └── /  (HTML dashboard, auto-refresh 15 detik)
                          ▲
                  crm.db (SQLite) ← BARU: hanya data sisi sales
                    (stage, owner, visit, deal, notes, override klasifikasi)

Akses:
  LAN    : http://192.168.50.25:8646
  Publik : https://crm.<domain>.com  (Cloudflare Tunnel, outbound-only, tanpa buka port)
```

Prinsip penting:
- JSON sessions TIDAK diubah strukturnya; bot/summarizer tetap jalan seperti sekarang.
- SQLite hanya menyimpan data baru (sisi sales). Dashboard merge keduanya saat request → tidak ada masalah sync/drift.
- Override klasifikasi dari sales disimpan di DB; dashboard tampilkan override jika ada, sonst klasifikasi bot.

## B. Klasifikasi lead (4 kategori)

| Kategori | Emoji | Kriteria (untuk prompt LLM bot) |
|---|---|---|
| hot | 🔥🔥 | Minta SPK/booking, sebut budget+timeline jelas, minta visit segera, minta dihubungi langsung |
| warm | 🔥 | Aktif bertanya, minta pricelist/simulasi/lokasi, tertarik |
| cold | ❄️ | Jawab singkat, kurang minat, tidak antusias |
| no_respond | 💤 | 1 pesan, tidak balas lagi setelah follow-up |

Perubahan: SUMMARY_PROMPT di summarizer.py diperluas dari 3 → 4 kelas.

## C. Pipeline stage (dimensi kedua, diatur tim sales)

```
New → Qualified → Visit Scheduled → Visited → Negotiation → SPK/Booking → Won
                                                                    └────→ Lost
```
Lead baru otomatis masuk "New". Stage hanya berubah via dashboard/WA.

## D. Skema SQLite (crm.db)

```sql
leads (
  chat_id TEXT PRIMARY KEY,     -- join key ke sessions/*.json
  phone TEXT, name TEXT,
  stage TEXT DEFAULT 'new',
  classification_override TEXT, -- hot/warm/cold/no_respond, NULL = pakai milik bot
  owner_sales TEXT,
  visit_schedule TEXT,          -- ISO datetime
  deal_value INTEGER,           -- rupiah
  updated_at TEXT
)
notes (
  id INTEGER PRIMARY KEY,
  chat_id TEXT, ts TEXT,
  author TEXT,                  -- 'dashboard' / 'wa:Juliana' / nama sales
  text TEXT
)
```

## E. Fitur dashboard (1 URL untuk semua tim)

1. Kartu metrik: total lead, hot, warm, cold, no-respond, lead hari ini, lead minggu ini, per session WAHA.
2. Kanban pipeline: kolom per stage, kartu lead drag & drop.
3. Tabel lead: filter klasifikasi / stage / tanggal / session / search phone-nama.
4. Detail lead (klik): transkrip percakapan, summary bot, follow-up note, notes sales, tombol edit (klasifikasi, stage, owner, jadwal visit, nilai deal).
5. Grafik: lead per hari (line), funnel konversi stage (bar).
6. Auto-refresh 15 detik (polling /api/stats + board).
7. Login: 1 password bersama (semua role sama). Fase lanjutan: user per orang + role.
8. Mobile-friendly (tim buka dari HP).

## F. Update via WhatsApp (perintah internal)

Whitelist nomor internal: Juliana, Vemby, + nomor tim sales (menyusul).
Format perintah (case-insensitive, nomor boleh 08xx/628xx):

| Perintah | Fungsi |
|---|---|
| `#cek 0812xxx` | Status lead: kelas, stage, notes terakhir |
| `#hot 0812xxx` / `#warm` / `#cold` | Override klasifikasi |
| `#stage 0812xxx visit` | Pindah stage (new/qualified/visit/visited/nego/spk/won/lost) |
| `#visit 0812xxx 21/08 10:00` | Set jadwal visit |
| `#note 0812xxx sudah ditelepon, minat Cedar 10` | Tambah catatan |
| `#deal 0812xxx 2994000000` | Tandai Won + nilai deal |
| `#lost 0812xxx [alasan]` | Tandai Lost |
| `#rekap` | Ringkasan pipeline hari ini (angka per stage & kelas) |

Implementasi: parser di bot.py, hanya diproses jika nomor pengirim ada di whitelist internal. Tidak menyentuh alur lead biasa.

## G. Akses publik — Cloudflare Tunnel (langkah detail)

Metode: **remote-managed tunnel via Zero Trust dashboard** (paling gampang untuk server headless; semua konfigurasi di browser, di VM cuma install token).

Yang kamu lakukan di browser:
1. Pastikan domain sudah aktif di Cloudflare (nameserver domain sudah pointing ke Cloudflare). Cek di dashboard Cloudflare → domain → status "Active".
2. Buka https://one.dash.cloudflare.com (Zero Trust) → pakai akun yang sama.
3. Menu **Networks → Tunnels → Create a tunnel**.
4. Pilih **Cloudflared**, beri nama: `mutiara-crm` → Next.
5. Halaman berikutnya menampilkan **token install** (baris `cloudflared service install <TOKEN>`) — copy token-nya, kirim ke saya.
6. Di halaman yang sama, isi **Public Hostname**:
   - Subdomain: `crm`
   - Domain: pilih domain kamu
   - Service: `HTTP` → `localhost:8646`
   → Save. Cloudflare otomatis membuat DNS record-nya.

Yang saya lakukan di VM:
1. Install cloudflared (1 perintah, binary resmi).
2. `sudo cloudflared service install <TOKEN>` → jalan sebagai systemd service, auto-start saat boot.
3. Verifikasi: tunnel connected di dashboard Zero Trust.
4. Test buka https://crm.<domain>.com dari luar.

Catatan:
- Tunnel outbound-only → tidak perlu buka port di router/firewall, lebih aman.
- HTTPS otomatis dari Cloudflare.
- Akses LAN http://192.168.50.25:8646 tetap jalan berbarengan.
- Proteksi tambahan di sisi app: login password (Cloudflare Access bisa ditambah nanti kalau mau 2FA/email gate).

## H. Fase eksekusi

| Fase | Isi | Estimasi |
|---|---|---|
| 1 | SQLite schema + dashboard.py (API read-only: leads, stats) | 1 sesi |
| 2 | Frontend: metrik + tabel + detail lead + auto-refresh | 1 sesi |
| 3 | Kanban drag-drop + edit lead (stage/kelas/owner/visit/deal) + notes | 1 sesi |
| 4 | summarizer.py: klasifikasi 4 kelas (tambah hot) | cepat |
| 5 | bot.py: parser perintah WA internal | 1 sesi |
| 6 | Cloudflare Tunnel + domain + login password | butuh token dari kamu |
| 7 | Polish: grafik funnel, backup DB harian, systemd, dokumentasi | cepat |

Urutan bisa berubah; fase 1-3 inti, sisanya pelengkap.

## I. Yang masih dibutuhkan dari kamu
1. Nama domain + konfirmasi DNS sudah di Cloudflare (status Active).
2. Password dashboard (atau saya generate random lalu kasih ke kamu).
3. Nomor WA tim sales untuk whitelist perintah (boleh menyusul).
4. Port 8646 untuk dashboard — OK?

## J. Risiko & mitigasi
- VM systemd user-unit bermasalah (known issue gateway) → cloudflared pakai system service (root), fallback: proses background + watchdog.
- LID belum resolve PN → tetap tidak ditampilkan (aturan existing), dashboard ikut aturan yang sama.
- Data internal (Juliana/Vemby) tidak pernah muncul sebagai lead (blocklist existing tetap berlaku).
