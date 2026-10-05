"""
CRON REPORT DISABLED 2026-08-09 per user request.
The script is kept as stub but main() returns 0 without sending.

Cron-based periodic report for Vemby (628129262888).

Schedule: every 6 hours at 00:00, 06:00, 12:00, 18:00 (Asia/Jakarta).

Reports:
- Total leads in the last 6 hours (split by WAHA session: Tiara1 vs Tiara2-6388)
- Total leads today (00:00 → now, Jakarta)
- Total leads yesterday
- Total leads this week (Monday 00:00 → now, Jakarta)
- Per-lead list with: phone, classification (warm/cold/belum_respond), WAHA session

Sends via WAHA /api/sendText directly to Vemby's chat_id.
Empty periods are still reported ("0 leads in last 6h").

Usage:
    python3 /root/mutiara/cron_report.py        # one-shot, run by cron
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import sys

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from message_variations import build_report_intro
SESSIONS_DIR = BASE_DIR / "sessions"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "cron_report.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("mutiara.cron_report")

WAHA_BASE = os.getenv("MUTIARA_WAHA_BASE", "http://192.168.50.245:3000")
WAHA_DEFAULT_SESSION = os.getenv("MUTIARA_WAHA_SESSION", "Tiara2-6388")
WAHA_API_KEY = os.getenv("MUTIARA_WAHA_API_KEY", "B4m0ng1122")
# (LLM chain lives in bot.py/summarizer.py; cron_report only aggregates sessions and
#  does not call the LLM — these vars are kept for parity but currently unused here.)
MUTIARA_LLM_PROVIDER = os.getenv("MUTIARA_LLM_PROVIDER", "tokenrouter")
MUTIARA_LLM_MODEL = os.getenv("MUTIARA_LLM_MODEL", "z-ai/glm-5.3-free")

# Vemby (boss) — receives periodic reports. NOT a lead, NOT the manager (Juliana).
BOS_PHONE = os.getenv("MUTIARA_BOS_PHONE", "628129262888")
BOS_CHAT_ID = f"{BOS_PHONE}@c.us"
BOS_NAME = os.getenv("MUTIARA_BOS_NAME", "Vemby")

PROJECT_NAME = os.getenv("MUTIARA_PROJECT_NAME", "The Mutiara Bogor")

# Asia/Jakarta is UTC+7. Use a fixed offset (no DST) — Indonesia does not observe DST.
JAKARTA = timezone(timedelta(hours=7))


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_jakarta(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    return dt.astimezone(JAKARTA).strftime("%Y-%m-%d %H:%M")


def normalize_phone_digits(raw: str) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    elif digits.startswith("8"):
        digits = "62" + digits
    return digits


def is_valid_indonesian_mobile(raw: str) -> bool:
    return bool(__import__("re").fullmatch(r"628\d{8,11}", normalize_phone_digits(raw)))


_lid_to_pn_cache: dict[str, str] = {}
_lid_cache_loaded_for: set[str] = set()


def load_lid_cache(session: str) -> None:
    """Load WAHA Plus LID->PN mappings so cron never prints raw LID as phone."""
    if not session or session in _lid_cache_loaded_for:
        return
    url = f"{WAHA_BASE}/api/{session}/lids?limit=10000&offset=0"
    req = urllib.request.Request(url)
    if WAHA_API_KEY:
        req.add_header("X-Api-Key", WAHA_API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        if isinstance(data, list):
            for item in data:
                lid = str(item.get("lid") or "").replace("@lid", "")
                pn = str(item.get("pn") or "")
                if lid and pn and "@" in pn:
                    _lid_to_pn_cache[lid] = pn.split("@", 1)[0].lstrip("+")
            _lid_cache_loaded_for.add(session)
            log.info("loaded WAHA Plus LID cache for %s: %d entries", session, len(_lid_to_pn_cache))
    except Exception as e:
        log.warning("failed to load WAHA Plus LID cache for %s: %s", session, e)


def resolve_session_phone(d: dict) -> str:
    """Return verified PN digits for a session, resolving @lid through WAHA Plus when needed."""
    lead = d.get("lead") or {}
    phone = normalize_phone_digits(lead.get("phone") or "")
    chat_id = str(d.get("chat_id") or "")
    is_lid_chat = chat_id.endswith("@lid")
    if phone and (not is_lid_chat) and is_valid_indonesian_mobile(phone):
        return phone
    if phone and lead.get("pn_verified") and is_valid_indonesian_mobile(phone):
        return phone
    lid = str(lead.get("lid") or chat_id.split("@", 1)[0])
    session = d.get("waha_session") or WAHA_DEFAULT_SESSION
    load_lid_cache(session)
    pn = _lid_to_pn_cache.get(lid, "")
    if pn and is_valid_indonesian_mobile(pn):
        lead["phone"] = pn
        lead["pn_verified"] = True
        lead["resolved_from_lid_at"] = datetime.now(timezone.utc).isoformat()
        return pn
    return ""


def phone_display(raw: str) -> str:
    digits = normalize_phone_digits(raw)
    if not is_valid_indonesian_mobile(digits):
        return "-"
    if len(digits) > 2:
        country = digits[:2]
        rest = digits[2:]
        if rest.startswith("8") and len(rest) >= 10:
            chunks = [rest[:3], rest[3:7], rest[7:]]
            return f"+{country} " + "-".join(c for c in chunks if c)
        chunks = [rest[i:i+3] for i in range(0, len(rest), 3) if rest]
        return f"+{country} " + "-".join(chunks) if chunks else f"+{digits}"
    return f"+{digits}"


def classify_emoji(cls: str) -> str:
    return {"warm": "🔥", "cold": "❄️", "belum_respond": "💤"}.get(cls or "", "·")


def load_all_sessions() -> list[dict]:
    """Load real leads only; internal owner/manager chats are never reportable."""
    internal_phones = {"6287770111700", "628129262888"}
    out = []
    for p in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text())
            lead = d.get("lead") or {}
            phone = resolve_session_phone(d)
            summary = d.get("summary") or {}
            # Never show/count a LID session unless WAHA Plus resolves it to PN.
            if not phone or not is_valid_indonesian_mobile(phone):
                log.warning("excluding unverified/invalid lead from cron report: %s", p.name)
                continue
            # Persist resolved PN so future reports use PN even if WAHA cache is unavailable.
            if lead.get("phone") != phone or not lead.get("pn_verified"):
                lead["phone"] = phone
                lead["pn_verified"] = True
                p.write_text(json.dumps(d, ensure_ascii=False, indent=2))
            if phone in internal_phones or summary.get("internal_excluded") or summary.get("report_excluded"):
                log.info("excluding internal chat from cron report: %s", p.name)
                continue
            if isinstance(d, dict):
                out.append(d)
        except Exception:
            continue
    return out


def in_window(dt: datetime | None, start: datetime, end: datetime) -> bool:
    if dt is None:
        return False
    return start <= dt <= end


def build_report(now_utc: datetime) -> str:
    """Compose the periodic report body. `now_utc` is the trigger time."""
    now_jkt = now_utc.astimezone(JAKARTA)

    # Six-hour bucket: previous 6-hour window ending now
    six_h_end = now_jkt
    six_h_start = now_jkt - timedelta(hours=6)

    # Today (Jakarta 00:00 → now)
    today_start = now_jkt.replace(hour=0, minute=0, second=0, microsecond=0)

    # Yesterday
    yesterday_start = today_start - timedelta(days=1)

    # This week (Monday 00:00 Jakarta → now)
    week_start = today_start - timedelta(days=today_start.weekday())  # Monday 00:00

    sessions = load_all_sessions()

    def bucket(start: datetime, end: datetime) -> list[dict]:
        return [s for s in sessions
                if in_window(parse_iso(s.get("first_seen_at")), start, end)]

    def aggregate(items: list[dict]) -> dict:
        by_session: dict[str, int] = {}
        by_class: dict[str, int] = {}
        for s in items:
            sess = s.get("waha_session") or "(unset)"
            by_session[sess] = by_session.get(sess, 0) + 1
            cls = (s.get("summary", {}).get("classification") or "-").lower()
            by_class[cls] = by_class.get(cls, 0) + 1
        return {"total": len(items), "by_session": by_session, "by_class": by_class}

    six_h_items = bucket(six_h_start, six_h_end)
    today_items = bucket(today_start, six_h_end)  # up to NOW, not full day
    yesterday_items = bucket(yesterday_start, today_start)
    week_items = bucket(week_start, six_h_end)

    six_h_agg = aggregate(six_h_items)
    today_agg = aggregate(today_items)
    yesterday_agg = aggregate(yesterday_items)
    week_agg = aggregate(week_items)

    def session_lines(by_session: dict) -> str:
        if not by_session:
            return "  (none)"
        return "\n".join(f"  • {s}: {n}" for s, n in sorted(by_session.items()))

    # Per-lead list (last 6h window, sorted newest first)
    six_h_items_sorted = sorted(
        six_h_items,
        key=lambda s: parse_iso(s.get("first_seen_at")) or datetime.min.replace(tzinfo=JAKARTA),
        reverse=True,
    )
    if six_h_items_sorted:
        lead_lines = []
        for s in six_h_items_sorted:
            phone = resolve_session_phone(s) or s.get("lead", {}).get("phone") or ""
            cls = s.get("summary", {}).get("classification") or "-"
            waha = s.get("waha_session") or "-"
            seen = parse_iso(s.get("first_seen_at"))
            lead_lines.append(
                f"  {classify_emoji(cls)} {phone_display(phone)} · {cls} · via {waha} · {fmt_jakarta(seen)}"
            )
        lead_block = "\n".join(lead_lines)
    else:
        lead_block = "  (tidak ada lead di 6 jam terakhir)"

    intro = build_report_intro()
    report = (
        f"*{intro}*\n"
        f"_Untuk {BOS_NAME}_\n"
        f"\n"
        f"*🕐 Window: {fmt_jakarta(six_h_start)} → {fmt_jakarta(six_h_end)} WIB*\n"
        f"\n"
        f"*🔹 6 jam terakhir:* {six_h_agg['total']} lead\n"
        f"{session_lines(six_h_agg['by_session'])}\n"
        f"\n"
        f"*🔹 Hari ini (00:00 → now):* {today_agg['total']} lead\n"
        f"{session_lines(today_agg['by_session'])}\n"
        f"\n"
        f"*🔹 Kemarin:* {yesterday_agg['total']} lead\n"
        f"{session_lines(yesterday_agg['by_session'])}\n"
        f"\n"
        f"*🔹 Minggu ini (Senin 00:00 → now):* {week_agg['total']} lead\n"
        f"{session_lines(week_agg['by_session'])}\n"
        f"\n"
        f"*📋 List leads 6 jam terakhir:*\n"
        f"{lead_block}\n"
        f"\n"
        f"_Auto-report by cron. Sumber WAHA: Tiara1, Tiara2-6388._"
    )
    return report


async def send_report(text: str) -> bool:
    url = f"{WAHA_BASE}/api/sendText"
    body = {
        "chatId": BOS_CHAT_ID,
        "text": text,
        "session": WAHA_DEFAULT_SESSION,
    }
    headers = {"Content-Type": "application/json"}
    if WAHA_API_KEY:
        headers["X-Api-Key"] = WAHA_API_KEY
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=20)) as r:
                resp = await r.text()
                ok = 200 <= r.status < 300
                log.info("send_report status=%s ok=%s body=%s", r.status, ok, resp[:160])
                return ok
    except Exception as e:
        log.exception("send_report error: %s", e)
        return False


async def main() -> int:
    now_utc = datetime.now(timezone.utc)
    text = build_report(now_utc)
    ok = await send_report(text)
    if ok:
        log.info("cron_report sent successfully")
        return 0
    log.error("cron_report failed to send")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
