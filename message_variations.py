"""
Message phrasing variations — pick a random variant each time so the bot
doesn't send identical text to the same contacts (which WhatsApp/Meta
can flag as spam).

Usage:
    pick(options)               -> random element from list
    build_lead_followup(name)   -> full randomized lead follow-up
    build_manager_handoff_intro()-> random opening for Juliana handoff
    build_report_intro()        -> random opening for cron report to Vemby

Rules:
  - Juliana = MANAGER (sapa "Ibu", never offer visit, reply simple)
  - Visit terminology = "show unit" / "marketing gallery" (NOT "showroom")
  - Format: paragraph + bullet, minimal emoji, rapih spacing
"""
from __future__ import annotations

import random
import re
from datetime import datetime
from typing import Optional


def pick(options: list[str]) -> str:
    """Return a random element from the list. Empty list -> empty string."""
    if not options:
        return ""
    return random.choice(options)


# ------------------------------------------------------------------
# Lead follow-up templates (sent via summarizer to leads)
# ------------------------------------------------------------------

LEAD_FOLLOWUP_OPENINGS = [
    "Halo {name}! Dari tim Mutiara Bogor mengabari info tambahan 😊",
    "Hai {name}, ada info singkat buat Bapak/Ibu dari kami",
    "Selamat {time_of_day}, {name} — kami dari Mutiara Bogor ingin share info",
    "Halo {name}! Semoga hari Bapak/Ibu menyenangkan. Ada info dari kami nih 🙏",
    "Hai {name}, kami follow up singkat dari Mutiara Bogor ya",
    "Salam hangat, {name}! Dari tim Mutiara Bogor ingin kasih info tambahan",
    "Halo {name}, info singkat dari Mutiara Bogor untuk Bapak/Ibu",
    "Hai {name}, kami dari Mutiara Bogor ingin mengabari beberapa hal",
]

LEAD_FOLLOWUP_BODIES = [
    (
        "Berikut 3 tipe unit yang tersedia di Mutiara Bogor:\n\n"
        "• *Birch 6* — 3 KT, 100m², mulai dari Rp 1,9 M\n"
        "• *Cedar 8* — 4 KT, 148m², mulai dari Rp 2,4 M\n"
        "• *Banyon 10* — 4+1 KT, 224m², mulai dari Rp 4,4 M"
    ),
    (
        "Mutiara Bogor punya 3 tipe utama yang bisa dipilih:\n\n"
        "• *Birch 6* — 3 Kamar Tidur, LB 100m², mulai Rp 1,9 M\n"
        "• *Cedar 8* — 4 Kamar Tidur, LB 148m², mulai Rp 2,4 M\n"
        "• *Banyon 10* — 4+1 Kamar Tidur, LB 224m², mulai Rp 4,4 M"
    ),
    (
        "Berikut pilihan unit di Mutiara Bogor:\n\n"
        "• *Birch 6* — 3 KT, 100m², harga mulai 1,9 M\n"
        "• *Cedar 8* — 4 KT, 148m², harga mulai 2,4 M\n"
        "• *Banyon 10* — 4+1 KT, 224m², harga mulai 4,4 M"
    ),
    (
        "Tersedia 3 tipe unit utama yang bisa Bapak/Ibu pilih:\n\n"
        "• *Birch 6* — 3 KT, LB 100m², mulai 1,9 M\n"
        "• *Cedar 8* — 4 KT, LB 148m², mulai 2,4 M\n"
        "• *Banyon 10* — 4+1 KT, LB 224m², mulai 4,4 M"
    ),
]

LEAD_FOLLOWUP_LOCATIONS = [
    (
        "Lokasi kami strategis: Exit Tol 42 Bogor Selatan — "
        "sekitar 11 menit ke Kebun Raya Bogor 🗺️\n"
        "Google Maps: https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA"
    ),
    (
        "Project kami di Exit Tol 42 Bogor Selatan, cuma 11 menit "
        "ke Kebun Raya 🗺️\n"
        "Lihat di Maps: https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA"
    ),
    (
        "Alamat: Exit Tol 42 Bogor Selatan (11 menit ke Kebun Raya Bogor) 🗺️\n"
        "Cek lokasi: https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA"
    ),
    (
        "Lokasinya di Exit Tol 42 Bogor Selatan 🗺️\n"
        "Peta: https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA"
    ),
]

LEAD_FOLLOWUP_CLOSINGS = [
    (
        "Kalau ada yang ingin ditanyakan atau mau langsung jadwal visit ke "
        "show unit kami, silakan hubungi Ibu Juliana ya 🙏"
    ),
    (
        "Kalau butuh detail atau mau atur kunjungan ke marketing gallery "
        "kami, tim kami siap membantu. Hubungi Juliana kapan saja ya, Bapak/Ibu 🙏"
    ),
    (
        "Tertarik untuk lihat langsung unitnya? Tim kami bisa atur "
        "kunjungan ke show unit. Hubungi Juliana untuk info lebih lanjut 🙏"
    ),
    (
        "Kalau mau diskusi lebih lanjut atau ingin atur jadwal ke "
        "marketing gallery, Ibu Juliana siap membantu ya, Bapak/Ibu 🙏"
    ),
    (
        "Untuk lihat langsung unit, tim kami bisa atur kunjungan ke "
        "show unit atau marketing gallery. Hubungi Ibu Juliana ya 🙏"
    ),
]


def build_lead_followup(name: str) -> str:
    """Compose a randomized lead follow-up message."""
    time_of_day = _time_of_day()
    greeting_name = _clean_name(name)
    return (
        f"{pick(LEAD_FOLLOWUP_OPENINGS).format(name=greeting_name, time_of_day=time_of_day)}\n"
        f"\n"
        f"{pick(LEAD_FOLLOWUP_BODIES)}\n"
        f"\n"
        f"{pick(LEAD_FOLLOWUP_LOCATIONS)}\n"
        f"\n"
        f"{pick(LEAD_FOLLOWUP_CLOSINGS)}"
    )


# ------------------------------------------------------------------
# Manager handoff messages (sent to Juliana)
# ------------------------------------------------------------------
# Note: Juliana is the MANAGER. Greeting is "Bu" or "Ibu", reply simple.
# Never offer visit/showroom to her. She coordinates visits, doesn't take them.

MANAGER_HINT_OPENINGS = [
    "Info singkat untuk Ibu Juliana",
    "Update terbaru untuk Ibu Juliana",
    "Catatan lead masuk untuk Ibu Juliana",
    "Ringkasan harian untuk Ibu Juliana",
    "Halo Bu, ada info lead baru",
    "Bu, ini update lead yang masuk",
    "Untuk Ibu Juliana — info lead",
    "Catatan masuk untuk Ibu Juliana",
]

MANAGER_HINT_BODY_OPENERS = [
    "Berikut ringkasan lead yang baru masuk:",
    "Ada beberapa lead yang masuk, dirangkum di bawah ini:",
    "Detail lead yang perlu ditindaklanjuti:",
    "Ringkasan singkat lead yang masuk:",
    "Catatan lead masuk periode ini:",
    "Berikut update lead yang perlu ditindaklanjuti:",
]


def _time_of_day() -> str:
    h = datetime.now().hour
    if 5 <= h < 11:
        return "pagi"
    if 11 <= h < 15:
        return "siang"
    if 15 <= h < 18:
        return "sore"
    return "malam"


def _clean_name(name: str) -> str:
    """Strip common Indonesian salutation prefixes from a name."""
    if not name:
        return "Bapak/Ibu"
    cleaned = re.sub(
        r"^(Bapak|Ibu|Bpk|Bung|Bung|Bung|Bpk|Saudara|Sdra|Sdri|Mr|Mrs|Ms)\s+",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned or "Bapak/Ibu"


def _class_emoji(cls: str) -> str:
    return {
        "warm": "🔥",
        "cold": "❄️",
        "belum_respond": "💤",
    }.get((cls or "").lower(), "·")


def _class_label(cls: str) -> str:
    return {
        "warm": "Warm — Aktif & Tertarik",
        "cold": "Cold — Kurang Tertarik",
        "belum_respond": "Belum ada Respon",
    }.get((cls or "").lower(), (cls or "Tidak Teridentifikasi").title())


def build_manager_handoff_intro() -> str:
    """Return a randomized opening line for the manager handoff."""
    return f"{pick(MANAGER_HINT_OPENINGS)} — {pick(MANAGER_HINT_BODY_OPENERS)}"


# ------------------------------------------------------------------
# Cron report openers (sent to Vemby)
# ------------------------------------------------------------------

REPORT_OPENINGS = [
    "Report periodik dari Mutiara Bogor",
    "Rekap lead harian Mutiara Bogor",
    "Laporan periodik Mutiara Bogor",
    "Update lead dari Mutiara Bogor",
    "Rekap rutin Mutiara Bogor",
    "Ringkasan periodik Mutiara Bogor",
    "Catatan lead periodik dari Mutiara Bogor",
]

REPORT_GREETINGS = [
    "Hai Bos, ini rekap terbaru ya",
    "Halo Bos, ini info periodik",
    "Bos, ini rekap dari tim",
    "Hai Bos, ini laporan berkala",
    "Halo Bos, ini rekap hari ini",
    "Bos, ini update rutin",
    "Hai Bos, ini rekap masuk",
]


def build_report_intro() -> str:
    """Return a randomized opening for the cron report."""
    return f"{pick(REPORT_OPENINGS)} — {pick(REPORT_GREETINGS)}"

# ------------------------------------------------------------------
# Juliana report (simple, 1x per lead, PN + short convo summary + status)
# ------------------------------------------------------------------
# Format: short, clear, PN + chat time + short summary + classification
# NO long transcript, NO follow-up to lead from here.
# 3 randomized structures to avoid WhatsApp spam detection.

JULIANA_REPORT_OPENERS = [
    "*Lead masuk — Mutiara Bogor*",
    "*Info lead baru — Mutiara Bogor*",
    "*Catatan lead — Mutiara Bogor*",
]

JULIANA_REPORT_FOOTERS = [
    "chat: {chat_time}",
    "waktu chat: {chat_time}",
    "mulai chat: {chat_time}",
]


JULIANA_FIELDS_VARIANTS = [
    # (name_line, phone_line) order
    ("name_first", "👤 {name}\n📱 {phone}"),
    ("phone_first", "📱 {phone}\n👤 {name}"),
]

JULIANA_SUMMARY_LABELS = [
    "Ringkas chat",
    "Summary chat",
    "Inti pembicaraan",
]


def build_juliana_report(
    name: str,
    phone_display: str,
    classification: str,
    chat_time: str,
    conversation_summary: str,
) -> str:
    """Compose a short randomized report for Juliana.

    Args:
        name: Lead name (or "(tidak ada nama)")
        phone_display: Formatted phone e.g. "+62 877-7011-1700"
        classification: warm / cold / belum_respond
        chat_time: e.g. "19:45 WIB"
        conversation_summary: short 1-2 sentence summary of the chat
    """
    cls_emoji = {
        "warm": "🔥",
        "cold": "❄️",
        "belum_respond": "💤",
    }.get(classification.lower(), "·")

    cls_label = {
        "warm": "Warm",
        "cold": "Cold",
        "belum_respond": "Belum respond",
    }.get(classification.lower(), classification.title())

    opener = pick(JULIANA_REPORT_OPENERS)
    footer = pick(JULIANA_REPORT_FOOTERS).format(chat_time=chat_time)
    summary_label = pick(JULIANA_SUMMARY_LABELS)
    summary_text = (conversation_summary or "Belum ada ringkasan percakapan.").strip()

    # Randomize name/phone order
    layout = pick(JULIANA_FIELDS_VARIANTS)
    body = layout[1].format(name=name, phone=phone_display)

    return (
        f"{opener}\n"
        f"\n"
        f"{body}\n"
        f"{cls_emoji} {cls_label}\n"
        f"• {summary_label}: {summary_text}\n"
        f"\n"
        f"{footer}"
    )
