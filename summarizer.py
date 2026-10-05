"""
Mutiara Bogor — 5-minute summarizer & handoff to manager.

Every N seconds this loop scans ~/mutiara/sessions/*.json for entries whose
``summary.scheduled_at_unix <= now`` and that have not yet been processed. It
then:

  1. Builds the conversation transcript + lead metadata
  2. Asks the LLM to produce a short Indonesian summary + classify
     (warm / cold / belum_respond)
  3. Sends the summary to the manager (Juliana) via WAHA /api/sendText
  4. Marks the session as forwarded so it isn't picked up again

Run as a separate process alongside bot.py:

    python3 ~/mutiara/summarizer.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone, timedelta

JAKARTA = timezone(timedelta(hours=7))
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from message_variations import build_lead_followup, build_manager_handoff_intro, build_juliana_report

import aiohttp

SESSIONS_DIR = BASE_DIR / "sessions"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOGS_DIR / "summarizer.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("mutiara.summarizer")

WAHA_BASE = os.getenv("MUTIARA_WAHA_BASE", "http://192.168.50.245:3000")
WAHA_DEFAULT_SESSION = os.getenv("MUTIARA_WAHA_SESSION", "Tiara2-6388")
# Juliana/Vemby reporting summaries use the same LLM chain as bot.py.
# Primary: GLM 5.3 free via TokenRouter.
# Fallback 1: nemotron-3-super via nvidia. Fallback 2 (last): nemotron-3-super via ollama-cloud.
# openai-codex subscription is EXHAUSTED (2026-09-12) — removed from the chain, keep the
# connection configured for later re-enable. Do NOT re-add until the user says so.
# (qwen/qwen3.8-max-free was retired by TokenRouter on 2026-09-01 — do not re-add.)
MUTIARA_LLM_PROVIDER = os.getenv("MUTIARA_LLM_PROVIDER", "tokenrouter")
MUTIARA_LLM_MODEL = os.getenv("MUTIARA_LLM_MODEL", "z-ai/glm-5.3-free")
MUTIARA_LLM_REASONING = os.getenv("MUTIARA_LLM_REASONING", "")
MUTIARA_LLM_FALLBACK_PROVIDER = os.getenv("MUTIARA_LLM_FALLBACK_PROVIDER", "nvidia")
MUTIARA_LLM_FALLBACK_MODEL = os.getenv("MUTIARA_LLM_FALLBACK_MODEL", "nvidia/nemotron-3-super-120b-a12b")
MUTIARA_LLM_FALLBACK_REASONING = os.getenv("MUTIARA_LLM_FALLBACK_REASONING", "")
MUTIARA_LLM_FALLBACK2_PROVIDER = os.getenv("MUTIARA_LLM_FALLBACK2_PROVIDER", "ollama-cloud")
MUTIARA_LLM_FALLBACK2_MODEL = os.getenv("MUTIARA_LLM_FALLBACK2_MODEL", "nemotron-3-super")
MUTIARA_LLM_FALLBACK2_REASONING = os.getenv("MUTIARA_LLM_FALLBACK2_REASONING", "")

# Ordered fallback chain: (provider, model, reasoning, label).
LLM_CANDIDATES = [
    (MUTIARA_LLM_PROVIDER, MUTIARA_LLM_MODEL, MUTIARA_LLM_REASONING or None, "primary"),
    (MUTIARA_LLM_FALLBACK_PROVIDER, MUTIARA_LLM_FALLBACK_MODEL, MUTIARA_LLM_FALLBACK_REASONING or None, "fallback"),
    (MUTIARA_LLM_FALLBACK2_PROVIDER, MUTIARA_LLM_FALLBACK2_MODEL, MUTIARA_LLM_FALLBACK2_REASONING or None, "fallback2"),
]

# Cache for LID -> PN resolution via WAHA contacts API
_lid_to_pn_cache: dict[str, str] = {}
_contacts_cache_loaded_for: set[str] = set()


async def _load_waha_lids(session: str) -> None:
    """Fetch all known LID->PN mappings from WAHA Plus /api/{session}/lids.

    Result is cached per-session; full list is ~5500 entries.
    """
    if session in _contacts_cache_loaded_for:
        return
    url = f"{WAHA_BASE}/api/{session}/lids?limit=10000&offset=0"
    headers = {"X-Api-Key": WAHA_API_KEY} if WAHA_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status != 200:
                    log.warning("WAHA lids fetch failed: %s", r.status)
                    return
                data = json.loads(await r.text())
                if isinstance(data, list):
                    for entry in data:
                        lid = (entry.get("lid") or "").replace("@lid", "")
                        pn = entry.get("pn") or ""
                        if lid and pn and "@" in pn:
                            pn_digits = pn.split("@")[0].lstrip("+")
                            _lid_to_pn_cache[lid] = pn_digits
                    _contacts_cache_loaded_for.add(session)
                    log.info("LID->PN cache loaded for %s: %d entries", session, len(_lid_to_pn_cache))
    except Exception as e:
        log.warning("WAHA lids fetch error: %s", e)


async def _resolve_lid_via_api(session: str, lid: str) -> str:
    """Look up a single LID via WAHA Plus /api/{session}/lids/{lid}. Returns PN digits or ""."""
    url = f"{WAHA_BASE}/api/{session}/lids/{lid}"
    headers = {"X-Api-Key": WAHA_API_KEY} if WAHA_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return ""
                data = json.loads(await r.text())
                pn = data.get("pn") or ""
                if pn and "@" in pn:
                    return pn.split("@")[0].lstrip("+")
    except Exception as e:
        log.warning("WAHA lid lookup error: %s", e)
    return ""


def resolve_lid_to_pn(lid: str, session: str = "") -> str:
    """Look up PN from LID in the cache. Returns empty string if not found."""
    if not lid:
        return ""
    return _lid_to_pn_cache.get(lid, "")
WAHA_API_KEY = os.getenv("MUTIARA_WAHA_API_KEY", "B4m0ng1122")

MANAGER_PHONE = os.getenv("MUTIARA_MANAGER_PHONE", "6287770111700")
MANAGER_NAME = os.getenv("MUTIARA_MANAGER_NAME", "Juliana")
MANAGER_DISPLAY = os.getenv(
    "MUTIARA_MANAGER_DISPLAY", "Ibu Juliana (Manager Marketing)"
)
# Vemby is the owner, not a lead. Must remain in the same non-lead blocklist
# as Juliana even when WhatsApp supplies a LID instead of the phone number.
BOS_PHONE = os.getenv("MUTIARA_BOS_PHONE", "628129262888")
INTERNAL_TEAM_PHONES = frozenset({MANAGER_PHONE, BOS_PHONE})
WAHA_DEFAULT_SESSION = os.getenv("MUTIARA_WAHA_SESSION", "Tiara2-6388")


def normalize_phone_digits(raw: str) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    elif digits.startswith("8"):
        digits = "62" + digits
    return digits


def is_valid_indonesian_mobile(raw: str) -> bool:
    """Only a canonical Indonesian mobile PN may be shown in staff reports."""
    return bool(re.fullmatch(r"628\d{8,11}", normalize_phone_digits(raw)))


# Cache for LID -> PN resolution via WAHA contacts API
_lid_to_pn_cache: dict[str, str] = {}
_contacts_cache_loaded_for: set[str] = set()


async def _load_waha_lids(session: str) -> None:
    """Fetch all known LID->PN mappings from WAHA Plus /api/{session}/lids.

    Result is cached per-session; full list is ~5500 entries.
    """
    if session in _contacts_cache_loaded_for:
        return
    url = f"{WAHA_BASE}/api/{session}/lids?limit=10000&offset=0"
    headers = {"X-Api-Key": WAHA_API_KEY} if WAHA_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status != 200:
                    log.warning("WAHA lids fetch failed: %s", r.status)
                    return
                data = json.loads(await r.text())
                if isinstance(data, list):
                    for entry in data:
                        lid = (entry.get("lid") or "").replace("@lid", "")
                        pn = entry.get("pn") or ""
                        if lid and pn and "@" in pn:
                            pn_digits = pn.split("@")[0].lstrip("+")
                            _lid_to_pn_cache[lid] = pn_digits
                    _contacts_cache_loaded_for.add(session)
                    log.info("LID->PN cache loaded for %s: %d entries", session, len(_lid_to_pn_cache))
    except Exception as e:
        log.warning("WAHA lids fetch error: %s", e)


async def _resolve_lid_via_api(session: str, lid: str) -> str:
    """Look up a single LID via WAHA Plus /api/{session}/lids/{lid}. Returns PN digits or ""."""
    url = f"{WAHA_BASE}/api/{session}/lids/{lid}"
    headers = {"X-Api-Key": WAHA_API_KEY} if WAHA_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return ""
                data = json.loads(await r.text())
                pn = data.get("pn") or ""
                if pn and "@" in pn:
                    return pn.split("@")[0].lstrip("+")
    except Exception as e:
        log.warning("WAHA lid lookup error: %s", e)
    return ""


def resolve_lid_to_pn(lid: str, session: str = "") -> str:
    """Look up PN from LID in the cache. Returns empty string if not found."""
    if not lid:
        return ""
    return _lid_to_pn_cache.get(lid, "")

SCAN_INTERVAL = int(os.getenv("MUTIARA_SCAN_INTERVAL", "20"))  # seconds
PROJECT_NAME = os.getenv("MUTIARA_PROJECT_NAME", "The Mutiara Bogor")
SHUTDOWN = asyncio.Event()


def format_phone_for_display(raw_phone: str) -> str:
    """Same logic as bot.format_phone_for_display. Duplicated here to avoid
    importing bot.py (which would pull aiohttp + webhook deps into the
    summarizer process). Keep in sync if you change the bot version."""
    if not raw_phone:
        return "-"
    digits = raw_phone.lstrip("+")
    # A LID never starts with a country code; a valid Indonesian mobile can be
    # 14 digits (62 + 11). Only flag long non-62 strings as LID.
    if len(digits) >= 14 and not digits.startswith("62"):
        return f"LID {digits}"
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    if not digits.startswith("62") and len(digits) >= 9:
        digits = "62" + digits
    if len(digits) > 2:
        country = digits[:2]
        rest = digits[2:]
        if len(rest) >= 10 and rest.startswith("8"):
            # mobile: 8xx-xxxx-xxxxx — keep the full tail (never truncate the
            # last digit; 12-digit tails are valid, e.g. 62881010388694).
            chunks = [rest[:3], rest[3:7], rest[7:]]
            chunks = [c for c in chunks if c]
            return f"+{country} " + "-".join(chunks)
        chunks = [rest[i:i+3] for i in range(0, len(rest), 3) if rest]
        joined = "-".join(chunks) if chunks else ""
        return f"+{country} {joined}".rstrip()
    return f"+{digits}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def load_session(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.error("bad session file %s: %s", path, e)
        return None


def save_session(session: dict) -> None:
    p = SESSIONS_DIR / f"{session['chat_id'].replace('@', '_at_').replace(':', '_')}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(session, indent=2, ensure_ascii=False))
    tmp.replace(p)


def session_due(session: dict) -> bool:
    s = session.get("summary", {})
    # A session that reached a terminal skip state (unresolvable LID / invalid PN /
    # internal chat) must NOT be re-processed forever. Without this guard the
    # summarizer rewrites the file every scan, which clobbers bot.py's
    # ``recontact.sent`` flag and caused repeated recontact sends (the 62881010388694
    # incident: recontact sent ~8x, once per minute).
    if s.get("report_excluded") or s.get("internal_excluded"):
        return False
    if s.get("forwarded_to_manager"):
        return False
    at_unix = s.get("scheduled_at_unix")
    if not at_unix:
        return False
    return at_unix <= _now()


def classify_lead(session: dict) -> str:
    """Heuristic pre-classification based on message timeline & length.

    The LLM does the final classification. This just gives it sane defaults.
    """
    msgs = session["messages"]
    lead_msgs = [m for m in msgs if m["sender"] == "lead"]
    bot_msgs = [m for m in msgs if m["sender"] == "bot"]

    if not lead_msgs:
        return "tidak_teridentifikasi"
    if len(lead_msgs) == 0:
        return "no_respond"

    last_lead_ts = lead_msgs[-1].get("ts", "")
    last_bot_ts = bot_msgs[-1].get("ts", "") if bot_msgs else ""
    first_lead_ts = lead_msgs[0].get("ts", "")
    try:
        delta_min = (
            datetime.fromisoformat(last_bot_ts.replace("Z", "+00:00"))
            - datetime.fromisoformat(first_lead_ts.replace("Z", "+00:00"))
        ).total_seconds() / 60.0
    except Exception:
        delta_min = 0
    avg_chars = sum(len(m["body"]) for m in lead_msgs) / max(1, len(lead_msgs))
    if len(lead_msgs) == 1 and delta_min >= 5:
        return "belum_respond"
    if avg_chars < 8 or len(lead_msgs) <= 2:
        return "cold"
    return "warm"


def transcript_text(session: dict, max_msgs: int = 50) -> str:
    msgs = session["messages"][-max_msgs:]
    lines = []
    for m in msgs:
        role = "LEAD" if m["sender"] == "lead" else "AGENT"
        ts = m.get("ts", "")[:19]
        lines.append(f"[{ts}] {role}: {m['body']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM call for summary + classification
# ---------------------------------------------------------------------------


SUMMARY_PROMPT = """\
Anda adalah supervisor sales. Analisa percakapan WhatsApp antara \
customer service dan seorang lead di proyek properti {project}. \
Lead ini awalnya dihubungi melalui iklan Meta (CTWA).

Tugas Anda:
1. Tulis RINGKASAN singkat (2-3 kalimat maksimum, Bahasa Indonesia natural) \
mengenai arah pembicaraan dan kebutuhan lead.
2. Klasifikasikan lead sebagai SATU dari:
   - "warm"          : lead aktif bertanya, nampak tertarik, atau memberi info kontak/budget
   - "cold"          : lead menjawab singkat/tidak antusias/tidak tertarik
   - "belum_respond" : lead hanya mengirim 1 pesan tanpa membalas lagi
3. Tulis CATATAN_SINGKAT 1 kalimat untuk action oleh tim follow-up.

Output WAJIB dalam format JSON persis seperti ini (tanpa teks lain):

{{
  "summary": "...",
  "classification": "warm|cold|belum_respond",
  "follow_up_note": "..."
}}

Skema JSON: {{ "summary": string, "classification": string, "follow_up_note": string }}\
""".replace("{project}", PROJECT_NAME)


async def call_llm_summary(session: dict) -> dict:
    """Ask the LLM to produce a JSON summary. Falls back to a heuristic."""
    user_payload = {
        "project": PROJECT_NAME,
        "lead": session.get("lead", {}),
        "first_seen_at": session.get("first_seen_at"),
        "transcript": transcript_text(session),
        "message_count": len(session.get("messages", [])),
    }
    prompt = (
        f"{SUMMARY_PROMPT}\n\n"
        f"--- LEAD INFO ---\n{json.dumps(user_payload, ensure_ascii=False)}\n"
    )
    candidates = LLM_CANDIDATES

    for provider, model, reasoning, label in candidates:
        try:
            cmd = [
                "hermes", "chat", "-Q",
                "--provider", provider,
                "-m", model,
            ]
            if reasoning:
                cmd.extend(["--reasoning", reasoning])
            cmd.extend(["-q", prompt])
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            except asyncio.TimeoutError:
                proc.kill()
                log.warning("LLM summary %s timeout provider=%s model=%s", label, provider, model)
                continue
            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                log.warning(
                    "LLM summary %s failed provider=%s model=%s rc=%s stderr=%s",
                    label, provider, model, proc.returncode, err[:300],
                )
                continue
            parsed = parse_json_or_fallback(out, session)
            if parsed:
                if label == "fallback":
                    log.warning("LLM summary fallback used provider=%s model=%s reasoning=%s", provider, model, reasoning)
                return parsed
        except FileNotFoundError:
            log.warning("hermes CLI not found for summary LLM")
            break
        except Exception as e:
            log.warning("LLM summary %s shell-out failed provider=%s model=%s: %s", label, provider, model, e)
            continue
    return heuristic_summary(session)


def parse_json_or_fallback(raw: str, session: dict) -> dict:
    """Parse the LLM output as JSON; fall back if the LLM didn't return JSON."""
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            data = json.loads(m.group(0))
            keys = {"summary", "classification", "follow_up_note"}
            if all(k in data for k in keys):
                if data["classification"] not in ("warm", "cold", "belum_respond"):
                    data["classification"] = classify_lead(session)
                return data
        except Exception:
            pass
    return heuristic_summary(session)


def heuristic_summary(session: dict) -> dict:
    cls = classify_lead(session)
    lead_msgs = [m for m in session["messages"] if m["sender"] == "lead"]
    bot_msgs = [m for m in session["messages"] if m["sender"] == "bot"]
    n_lead = len(lead_msgs)
    n_bot = len(bot_msgs)
    if n_lead == 0:
        summary = "Lead tidak mengirim pesan (skip)."
    elif n_lead == 1 and not any(
        datetime.fromisoformat(m["ts"].replace("Z", "+00:00"))
        > datetime.fromisoformat(lead_msgs[0]["ts"].replace("Z", "+00:00"))
        for m in lead_msgs[1:]
    ):
        summary = (
            f"Lead mengirim 1 pesan ({lead_msgs[0]['body'][:60]}) "
            f"dan belum ada balasan setelah ±5 menit."
        )
    else:
        last_lead = lead_msgs[-1]["body"][:80]
        summary = (
            f"Percakapan {n_lead}× balasan lead dan {n_bot}× dari CS. "
            f"Pesan terakhir lead: \"{last_lead}\"."
        )

    follow_up = {
        "warm": "Coba hubungi lead dalam 24 jam untuk jadwalkan visit.",
        "cold": "Tanyakan follow-up ringan via WA setelah 2-3 hari.",
        "belum_respond": "Lead belum respon. Coba hubungi sekali lagi via WA, kalau tetap diam lempar ke tim telesales untuk call.",
        "tidak_teridentifikasi": "Tidak ada pesan masuk dari lead.",
    }.get(cls, "Hubungi lead untuk follow-up lebih lanjut.")

    return {
        "summary": summary,
        "classification": cls,
        "follow_up_note": follow_up,
    }


# ---------------------------------------------------------------------------
# Manager handoff
# ---------------------------------------------------------------------------


def format_manager_message(summary: dict, session: dict) -> str:
    lead = session.get("lead", {})
    name = lead.get("name") or lead.get("push_name") or "(tidak ada nama)"
    phone_digits = lead.get("phone") or ""
    display_phone = format_phone_for_display(phone_digits) if phone_digits else "(unknown — phone belum resolved)"
    cls = summary.get("classification", "unknown").upper()
    cls_emoji = {
        "WARM": "🔥",
        "COLD": "❄️",
        "BELUM_RESPOND": "💤",
        "TIDAK_TERIDENTIFIKASI": "❓",
    }.get(cls, "")

    classification = {
        "WARM": "Warm — aktif & tertarik",
        "COLD": "Cold — kurang tertarik",
        "BELUM_RESPOND": "Belum Respond",
        "TIDAK_TERIDENTIFIKASI": "Tidak teridentifikasi",
    }.get(cls, cls)

    first_seen = session.get("first_seen_at", "")[:19].replace("T", " ")
    msgs = session.get("messages", [])
    n_msg = len(msgs)
    last_msg_at = session.get("last_message_at", "")[:19].replace("T", " ")
    source_session = session.get("waha_session") or WAHA_DEFAULT_SESSION

    intro = build_manager_handoff_intro()
    return (
        f"*{intro}*\n"
        f"\n"
        f"*👤 Nama:* {name}\n"
        f"*📱 No HP:* {display_phone}\n"
        f"*🕐 Chat masuk:* {first_seen} UTC\n"
        f"*💬 Total pesan:* {n_msg} (last: {last_msg_at} UTC)\n"
        f"*🏷️ Status:* {cls_emoji} {classification}\n"
        f"\n"
        f"*📝 Ringkasan:*\n"
        f"{summary.get('summary', '(kosong)')}\n"
        f"\n"
        f"*🎯 Follow-up:*\n"
        f"{summary.get('follow_up_note', '')}\n"
        f"\n"
        f"_Auto-handoff via bot Mutiara. Sumber: WAHA session {source_session}._"
    )


FOLLOWUP_TEMPLATE = """Halo {name}! Kami dari The Mutiara Bogor ingin kasih info tambahan 😊

Berikut 3 tipe rumah yang tersedia:

• Birch 6 — 3 KT, 100m², LB 100m²
  Mulai dari Rp 1,9 M

• Cedar 8 — 4 KT, 148m², LB 148m²
  Mulai dari Rp 2,4 M

• Banyon 10 — 4+1 KT, 224m², LB 224m²
  Mulai dari Rp 4,4 M

Lokasi kami di Exit Tol 42 Bogor Selatan — 11 menit ke Kebun Raya Bogor 🗺️

Link Google Maps: https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA

Ada yang ingin ditanyakan lebih lanjut? Atau mau langsung jadwalkan visit, bisa hubungi Juliana ya, Bapak/Ibu 🙏"""


async def send_followup_to_lead(session: dict, summary: dict) -> bool:
    """Send a randomized follow-up with 3 house types to the lead.

    Called after the manager handoff succeeds. Uses the session's waha_session
    to route the message back through the same session the lead used.
    Message is randomized each time (opening + body + location + closing)
    to avoid WhatsApp spam detection.
    """
    lead = session.get("lead", {})
    name = lead.get("name") or lead.get("push_name") or "Bapak/Ibu"
    chat_id = session.get("chat_id", "")
    lead_session = session.get("waha_session") or WAHA_DEFAULT_SESSION

    text = build_lead_followup(name)

    url = f"{WAHA_BASE}/api/sendText"
    body = {
        "chatId": chat_id,
        "text": text,
        "session": lead_session,
    }
    headers = {"Content-Type": "application/json"}
    if WAHA_API_KEY:
        headers["X-Api-Key"] = WAHA_API_KEY
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                resp_text = await r.text()
                ok = 200 <= r.status < 300
                log.info(
                    "send_followup_to_lead chat=%s session=%s status=%s ok=%s body=%s",
                    chat_id, lead_session, r.status, ok, resp_text[:150],
                )
                return ok
    except Exception as e:
        log.exception("send_followup_to_lead error: %s", e)
        return False


async def send_to_manager(message: str) -> bool:
    """Send manager-handoff message to Juliana via WAHA. Default session
    (Tiara1) is used because Juliana's replies will be received by whichever
    session is WORKING — she just needs the message to arrive on any session.

    Returns True if WAHA accepted the message (202/201), even if the
    underlying WhatsApp socket is slow and returns PENDING — WAHA will
    retry delivery asynchronously and the message usually arrives within
    seconds. We treat PENDING as success to avoid hammering a working
    WAHA endpoint with retry loops.
    """
    url = f"{WAHA_BASE}/api/sendText"
    body = {
        "chatId": f"{MANAGER_PHONE}@c.us",
        "text": message,
        "session": WAHA_DEFAULT_SESSION,
    }
    headers = {"Content-Type": "application/json"}
    if WAHA_API_KEY:
        headers["X-Api-Key"] = WAHA_API_KEY
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                text = await r.text()
                ok = 200 <= r.status < 300
                # PENDING means WAHA accepted it and will retry async — treat as success
                # to avoid flooding a working endpoint with useless retries.
                log.info("send_to_manager status=%s ok=%s body=%s", r.status, ok, text[:200])
                return ok
    except Exception as e:
        log.exception("send_to_manager error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def process_due_sessions() -> int:
    processed = 0
    for path in SESSIONS_DIR.glob("*.json"):
        s = load_session(path)
        if not s:
            continue
        if not session_due(s):
            continue
        try:
            summary = await call_llm_summary(s)
            s["summary"].update(
                {
                    "summary_text": summary.get("summary"),
                    "classification": summary.get("classification"),
                    "follow_up_note": summary.get("follow_up_note"),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            manager_msg = format_manager_message(summary, s)
            s["summary"]["manager_message"] = manager_msg
            # 2026-08-09: Re-enabled with SIMPLE format. Once per session.
            # Bold skip follow-up to lead (user wants ONLY juliana report).
            # Load WAHA Plus LID->PN mapping (cached per-session)
            waha_session = s.get("waha_session") or WAHA_DEFAULT_SESSION
            await _load_waha_lids(waha_session)

            lead = s.get("lead", {})
            lead_phone = lead.get("phone","")
            lead_lid = lead.get("lid","")

            # For an @lid chat, resolve only through WAHA Plus. A LID must
            # never be rendered as a phone and a malformed endpoint value is
            # not reportable to Juliana.
            is_lid_chat = str(s.get("chat_id") or "").endswith("@lid")
            if is_lid_chat:
                pn = resolve_lid_to_pn(lead_lid, waha_session)
                if not pn:
                    lid_full = lead_lid + "@lid" if not lead_lid.endswith("@lid") else lead_lid
                    pn = await _resolve_lid_via_api(waha_session, lid_full)
                if is_valid_indonesian_mobile(pn):
                    lead_phone = normalize_phone_digits(pn)
                    s.setdefault("lead", {})["phone"] = lead_phone
                    s["lead"]["pn_verified"] = True
                    s["lead"]["resolved_from_lid_at"] = datetime.now(timezone.utc).isoformat()
                else:
                    log.warning("LID has no verified Indonesian PN; skip Juliana report chat=%s lid=%s", s.get("chat_id"), lead_lid)
                    s["summary"]["forwarded_to_manager"] = False
                    s["summary"]["forwarded_at"] = None
                    s["summary"]["report_excluded"] = "unresolved_or_invalid_lid_pn"
                    s["summary"]["manager_message"] = "(skipped — LID has no verified Indonesian PN)"
                    save_session(s)
                    processed += 1
                    continue
            elif not is_valid_indonesian_mobile(lead_phone):
                log.warning("invalid direct PN; skip Juliana report chat=%s phone=%s", s.get("chat_id"), lead_phone)
                s["summary"]["forwarded_to_manager"] = False
                s["summary"]["forwarded_at"] = None
                s["summary"]["report_excluded"] = "invalid_direct_pn"
                s["summary"]["manager_message"] = "(skipped — invalid direct PN)"
                save_session(s)
                processed += 1
                continue

            # Never report internal owner/manager conversations as leads. This
            # catches historical LID-origin sessions that predate the webhook
            # blocklist or were resolved after creation.
            if normalize_phone_digits(lead_phone) in INTERNAL_TEAM_PHONES:
                log.warning("internal session excluded from lead report: chat=%s pn=%s", s.get("chat_id"), lead_phone)
                s["summary"]["forwarded_to_manager"] = False
                s["summary"]["forwarded_at"] = None
                s["summary"]["internal_excluded"] = True
                s["summary"]["manager_message"] = "(excluded — internal owner/manager conversation)"
                s["summary"]["followup_sent"] = False
                save_session(s)
                processed += 1
                continue

            phone_display = format_phone_for_display(lead_phone) if lead_phone else ""

            # Skip if no PN could be resolved (LID-only lead)
            if not phone_display or "LID" in phone_display:
                log.warning("lead LID-only no PN, skip juliana report: chat=%s lid=%s",
                            s.get("chat_id"), lead_lid)
                s["summary"]["forwarded_to_manager"] = False
                s["summary"]["forwarded_at"] = None
                s["summary"]["report_excluded"] = "lid_only_no_pn"
                s["summary"]["manager_message"] = "(skipped — LID-only, no PN)"
                s["summary"]["followup_sent"] = False
                save_session(s)
                processed += 1
                continue

            # chat_time = first_seen in WIB (UTC+7)
            first_seen = s.get("first_seen_at","")[:19].replace("T"," ")
            try:
                _dt = datetime.fromisoformat(s.get("first_seen_at","").replace("Z","+00:00"))
                _dt_wib = _dt.astimezone(JAKARTA)
                chat_time = _dt_wib.strftime("%H:%M WIB")
            except Exception:
                chat_time = first_seen
            name = lead.get("name") or lead.get("push_name") or "(tidak ada nama)"
            cls = summary.get("classification", "unknown")
            convo_summary = summary.get("summary") or s.get("summary", {}).get("summary_text") or "Belum ada ringkasan percakapan."
            juliana_msg = build_juliana_report(
                name,
                phone_display or "(belum resolved)",
                cls,
                chat_time,
                convo_summary,
            )
            s["summary"]["manager_message"] = juliana_msg
            sent = await send_to_manager(juliana_msg)
            if sent:
                s["summary"]["forwarded_to_manager"] = True
                s["summary"]["forwarded_at"] = datetime.now(timezone.utc).isoformat()
            else:
                s["summary"]["forwarded_to_manager"] = False
                s["summary"]["forwarded_at"] = None
                log.warning("juliana report failed, will retry: chat=%s", s["chat_id"])
            # Skip follow-up to lead (no longer)
            s["summary"]["followup_sent"] = False
            log.info(
                "processed chat=%s classification=%s juliana_sent=%s",
                s["chat_id"], s["summary"].get("classification"), sent,
            )
            save_session(s)
            processed += 1
        except Exception as e:
            log.exception("failed to process %s: %s", path, e)
    return processed


async def main_loop() -> None:
    log.info(
        "summarizer loop starting; scan=%ss manager=%s",
        SCAN_INTERVAL, MANAGER_PHONE,
    )
    while not SHUTDOWN.is_set():
        try:
            n = await process_due_sessions()
            if n:
                log.info("cycle processed %d sessions", n)
        except Exception as e:
            log.exception("loop error: %s", e)
        try:
            await asyncio.wait_for(SHUTDOWN.wait(), timeout=SCAN_INTERVAL)
        except asyncio.TimeoutError:
            pass


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def stop() -> None:
        log.info("shutdown requested")
        SHUTDOWN.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop)
        except NotImplementedError:
            pass


def main() -> None:
    global SCAN_INTERVAL  # noqa: PLW0603
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=SCAN_INTERVAL,
        help="Seconds between session scans (default: 20)",
    )
    args = parser.parse_args()
    SCAN_INTERVAL = args.scan_interval
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(main_loop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()

