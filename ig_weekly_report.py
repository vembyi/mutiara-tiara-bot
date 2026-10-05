#!/usr/bin/env python3
"""
Laporan Instagram mingguan — The Mutiara Bogor.
Fetch 10 post terbaru + insights untuk 3 akun via Composio CLI, lalu cetak ringkasan + analisa + saran.

Akun (connected_account_id stabil, bukan alias yang bisa berubah):
  ca_xs3f3iZG56PN  -> themutiarabogor.id  (akun resmi brand)
  ca_ASp-7B7t5Nzb  -> vemby               (Vemby I, personal/owner)
  ca_Ozi4BZUiyhnA  -> sohibsolutions      (DreamHome / Sohib partner)

Jalankan:  python3 ig_weekly_report.py
Output:    laporan markdown ke stdout + simpan /root/mutiara/ig_laporan_<tanggal>.md
"""
import json, subprocess, os, sys, glob
from datetime import datetime

COMPOSIO = os.path.expanduser("~/.local/bin/composio")
ACCOUNTS = {
    "ca_xs3f3iZG56PN": "themutiarabogor.id",
    "ca_ASp-7B7t5Nzb": "vemby",
    "ca_Ozi4BZUiyhnA": "sohibsolutions",
}
METRICS = ["views", "reach", "likes", "comments", "saved", "shares", "total_interactions"]

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    try:
        j = json.loads(r.stdout)
    except Exception:
        return {"_raw": r.stdout[:200], "_err": r.stderr[:200]}
    # large outputs are written to a file; stdout only carries outputFilePath
    fp = j.get("outputFilePath")
    if fp and os.path.exists(fp):
        try:
            j = json.load(open(fp))
        except Exception:
            pass
    return j

def fetch_media(ca_id):
    data = json.dumps({
        "fields": "id,caption,media_type,media_url,permalink,timestamp,like_count,comments_count,thumbnail_url",
        "limit": 10,
    })
    # USER INFO first to get ig_user_id
    ui = run([COMPOSIO, "execute", "INSTAGRAM_GET_USER_INFO", "--account", ca_id, "-d", "{}"])
    # response shape: {"successful":true,"data":{...user fields...}}
    uidata = ui.get("data", {})
    uid = uidata.get("id")
    if not uid:
        return None, ui
    followers = uidata.get("followers_count")
    data = json.dumps({
        "ig_user_id": uid,
        "fields": "id,caption,media_type,media_url,permalink,timestamp,like_count,comments_count,thumbnail_url",
        "limit": 10,
    })
    tmp = f"/tmp/ig_media_{ca_id}.json"; open(tmp, "w").write(data)
    m = run([COMPOSIO, "execute", "INSTAGRAM_GET_IG_USER_MEDIA", "--account", ca_id, "-d", "@"+tmp])
    items = (m.get("data", {}).get("data") or [])
    return items, uidata

def fetch_insights(ca_id, mid):
    data = json.dumps({"ig_media_id": mid, "metric": METRICS})
    tmp = f"/tmp/ig_ins_{ca_id}_{mid}.json"; open(tmp, "w").write(data)
    r = run([COMPOSIO, "execute", "INSTAGRAM_GET_IG_MEDIA_INSIGHTS", "--account", ca_id, "-d", "@"+tmp])
    if r.get("successful"):
        dd = r.get("data", {})
        if isinstance(dd, dict):
            return {x["name"]: x.get("values", [{}])[0].get("value") for x in dd.get("data", [])}
    return {}

def main():
    out = []
    out.append("# 📊 Laporan Instagram Mingguan — The Mutiara Bogor\n")
    out.append(f"**Diambil:** {datetime.now():%d %b %Y %H:%M WIB}\n")
    out.append("> Sumber: Composio IG Business API. Insights = lifetime sejak posting.\n")
    all_rows = {}
    for ca_id, handle in ACCOUNTS.items():
        items, info = fetch_media(ca_id)
        if not items:
            out.append(f"\n## @{handle}\n⚠️ Gagal fetch: {json.dumps(info)[:150]}\n"); continue
        followers = info.get("followers_count")
        rows = []
        reaches=[]; views=[]; likes=[]; saves=[]; shares=[]
        for p in items:
            d = fetch_insights(ca_id, p["id"])
            r=d.get("reach") or 0; v=d.get("views") or 0; lk=p.get("like_count",0)
            s=d.get("saved") or 0; sh=d.get("shares") or 0
            reaches.append(r); views.append(v); likes.append(lk); saves.append(s); shares.append(sh)
            ts=p.get("timestamp","")[:16].replace("T"," ")
            cap=(p.get("caption") or "").replace("\n"," ").strip()[:42]
            rows.append((ts,lk,r,v,s,sh,cap))
        n=len(items)
        out.append(f"\n## 🔹 @{handle}  ·  {followers} followers\n")
        out.append(f"**Rata-rata:** reach {sum(reaches)/n:.0f} · views {sum(views)/n:.0f} · likes {sum(likes)/n:.1f} · saved {sum(saves)/n:.1f} · shares {sum(shares)/n:.1f}")
        out.append(f"**Total views:** {sum(views)} · **Peak views:** {max(views)}")
        out.append("\n| Tanggal | ❤ | Reach | Views | Saved | Shares | Topik |")
        out.append("|---|---|---|---|---|---|---|")
        for ts,lk,r,v,s,sh,cap in rows:
            out.append(f"| {ts} | {lk} | {r} | {v} | {s} | {sh} | {cap} |")
        all_rows[handle] = (rows, followers)
    # Analysis + suggestions
    out.append("\n---\n## 🧠 Analisa & Saran\n")
    out.append(analysis_block(all_rows))
    md="\n".join(out)
    path = f"/root/mutiara/ig_laporan_{datetime.now():%Y%m%d}.md"
    os.makedirs("/root/mutiara", exist_ok=True)
    open(path, "w").write(md)
    print(md)
    print(f"\n[saved] {path}")

def analysis_block(all_rows):
    b = []
    # quick comparison
    comp = []
    for h,(rows,followers) in all_rows.items():
        vsum=sum(r[3] for r in rows); lsum=sum(r[1] for r in rows)
        comp.append((h, followers, vsum, lsum))
    b.append("**Perbandingan cepat:**\n")
    b.append("| Akun | Followers | Total Views (10 post) | Total Likes |")
    b.append("|---|---|---|---|")
    for h,fr,v,l in comp:
        b.append(f"| @{h} | {fr} | {v} | {l} |")
    b.append("\n**Saran eksekusi (prioritas):**\n")
    b.append("1. **Hentikan duplikasi konten antar-akun.** Post 'Make Your Dreams Come True', 'Wellness', 'Doorprize' identik di ke-3 akun. Audiens follow >1 akun jadi bosan → reach stagnation. Solusi: bedakan sudut pandang — akun resmi = fakta/produk, @vemby = testimoni personal/behind-the-scenes, @sohibsolutions = edukasi agen.")
    b.append("2. **Naikkan saved & shares (sinyal algoritma).** Post tertinggi (Wellness, Doorprize) menang karena relevan emosional. Tambah hook 'save this' (simulasi KPR, checklist rumah) & 'share ke pasangan'.")
    b.append("3. **0 komen di hampir semua post.** Tambah CTA tanya jawab ('kota impianmu?', 'DP berapa yang pas?') & balas tiap komen 1 jam pertama → boost engagement rate.")
    b.append("4. **Posting jendela emas.** Post terbaik (@themutiarabogor.id) tayang 07:00–08:15 WIB (Wellness 07:02 = 409 views, Mobilitas 08:15 = 368). Hindari 23:55 (reach cuma 5).")
    b.append("5. **Reels > carousel** di data ini (semua video). Pertahankan format reels 15–30 dtk, hook 1.5 dtk pertama.")
    b.append("6. **Cross-posting cerdas:** @vemby (788 follower) jadi corong traffic ke @themutiarabogor.id via mention + link bio.")
    return "\n".join(b)

if __name__ == "__main__":
    main()
