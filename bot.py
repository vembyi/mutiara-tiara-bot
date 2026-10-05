"""
Mutiara Bogor sales bot — WAHA inbound listener.

Listens on port 8645 for WAHA webhook POSTs for project "The Mutiara Bogor".
For each inbound message:
  1. Validates HMAC signature (WAHA-style: X-Webhook-Hmac, sha256/sha512).
  2. Persists the message into ~/mutiara/sessions/<chat_id>.json.
  3. First time we see a chat_id, schedules a 5-minute summarizer task.
  4. Generates a CS reply via the configured LLM (CLI fallback / direct API).
  5. Sends the reply back to the lead via WAHA /api/sendText.

Companion file: summarizer.py — handles the 5-min handoff to Juliana.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import hmac
import json
import logging
import os
import random
import re
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Optional

import aiohttp
from aiohttp import web

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
INBOX_DIR = BASE_DIR / "inbox"
LOGS_DIR = BASE_DIR / "logs"
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
CS_PROMPT_PATH = BASE_DIR / "cs_prompt.md"

for d in (SESSIONS_DIR, INBOX_DIR, LOGS_DIR, KNOWLEDGE_DIR):
    d.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOGS_DIR / "bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("mutiara.bot")

# ---------------------------------------------------------------------------
# Configuration (env-overridable)
# ---------------------------------------------------------------------------

WAHA_BASE = os.getenv("MUTIARA_WAHA_BASE", "http://192.168.50.245:3000")
# WAHA session is now auto-detected from the webhook event payload. The env
# default is still used as the manager-message sender (Juliana reply channel
# preference) and for self-tests where no event is available.
WAHA_DEFAULT_SESSION = os.getenv("MUTIARA_WAHA_SESSION", "Tiara1")
WAHA_API_KEY = os.getenv("MUTIARA_WAHA_API_KEY", "B4m0ng1122")
# All sessions accepted (Tiara1 + Tiara2-6388 + any future ones).
WAHA_SESSIONS = [s.strip() for s in os.getenv(
    "MUTIARA_WAHA_SESSIONS", "Tiara1,Tiara2-6388"
).split(",") if s.strip()]

# HMAC secret must match the WAHA webhook config on Tiara1 session
WEBHOOK_SECRET = os.getenv(
    "MUTIARA_WEBHOOK_SECRET", "waha-tiara1-2026-secret-key"
).encode("utf-8")

# Manager handoff — Juliana
MANAGER_PHONE = os.getenv("MUTIARA_MANAGER_PHONE", "6287770111700")
MANAGER_CHAT_ID = f"{MANAGER_PHONE}@c.us"  # canonical chat id for manager session
MANAGER_NAME = os.getenv("MUTIARA_MANAGER_NAME", "Juliana")
MANAGER_DISPLAY = os.getenv(
    "MUTIARA_MANAGER_DISPLAY", "Ibu Juliana (Manager Marketing)"
)

# Boss / owner — Vemby. Receives periodic cron reports. Replies are NOT leads.
# Routed to the same internal-assistant branch as the manager, but with a
# distinct persona so the tone matches who they are to the project.
BOS_PHONE = os.getenv("MUTIARA_BOS_PHONE", "628129262888")
BOS_CHAT_ID = f"{BOS_PHONE}@c.us"
BOS_NAME = os.getenv("MUTIARA_BOS_NAME", "Vemby")

# LLM chain for lead auto-replies and internal (Juliana/Vemby) operational messages.
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
MAPS_LINK = "https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA"
LOCATION_INTENT = re.compile(
    r"\b(lokasi\w*|alamat\w*|rute|arah|maps?|google\s*maps|gmap|dmn|"
    r"daerah\s+mana|datang|kunjung\w*|visit)\b",
    re.I,
)
ASSET_INTENT = re.compile(r"\b(foto\w*|video\w*|vidio\w*|brosur\w*|gambar\w*)\b", re.I)
IDENTITY_INTENT = re.compile(r"\b(ini\s+siapa|siapa\s+ya)\b", re.I)
NEGATIVE_VISIT_INTENT = re.compile(
    r"\b(tidak|nggak|gak|ga|belum|jangan|hanya|cuma)\b.{0,48}\b(visit|datang|survey|kunjung\w*)\b",
    re.I,
)
FACILITY_INTENT = re.compile(r"\b(fasilitas\w*|facility\w*|kolam\w*|pool\w*|clubhouse|gym|fitness\w*|playground|taman\w*|jogging|tennis|basket)\b", re.I)
FACILITY_FACT = (
    "Untuk fasilitas keluarga, The Mutiara Bogor memiliki clubhouse sekitar 4.000 m², "
    "resort pool/lap pool, kids pool dan children discovery playground, basketball/tennis court, "
    "natural jogging trail/yoga deck, community garden, common ground, rain deck/reflection pond, "
    "multifunction park, serta BBQ pavilion."
)
TYPE_POSITIONING = {
    "birch": "Birch 6 cocok untuk keluarga awal atau keluarga muda yang sedang tumbuh; tersedia 3 kamar tidur.",
    "elm": "Elm 8 cocok untuk keluarga berkembang atau yang ingin upgrade rumah. Lebarnya 8 meter dengan 4+1 kamar tidur, sehingga relevan untuk keluarga dengan dua anak, tinggal bersama orang tua, atau yang membutuhkan ruang lebih lega.",
    "cedar": "Cedar 10 cocok untuk keluarga yang membutuhkan ruang lebih besar dan kenyamanan jangka panjang.",
    "banyan": "Banyan 10 cocok untuk keluarga besar atau pencari rumah yang lebih premium dan luas, dengan positioning kenyamanan, quality living, dan quality craftsmanship pada level lebih tinggi.",
}
KPR_STANDARD_15Y_REFERENCE = {
    "birch": {"label": "Birch 6 standar", "monthly": "Rp16.526.170/bulan", "dp": "Rp216.806.835"},
    "elm": {"label": "Elm 8 standar", "monthly": "Rp20.915.446/bulan", "dp": "Rp281.029.644"},
    "cedar": {"label": "Cedar 10 standar", "monthly": "Rp29.700.733/bulan", "dp": "Rp409.573.791"},
}

# System prompt for the manager (Juliana) conversation branch. The bot behaves
# as an internal sales-assistant — answers informally in Bahasa Indonesia,
# can look up lead status / send ad-hoc reports, and never treats Juliana's
# replies as fresh leads.
MANAGER_PROMPT = (
    "Anda adalah asisten internal untuk Ibu Juliana (Manager Marketing The Mutiara Bogor). "
    "Jawab singkat, sopan, bahasa Indonesia informal-internal (pakai 'Bu' untuk sapa). "
    "Tugas Anda:\n"
    "  - Acknowledge pesan beliau (terima kasih, dicatat, siap, dll) secara natural.\n"
    "  - Kalau beliau tanya status lead tertentu, lookup dari sessions/*.json.\n"
    "  - Kalau beliau minta summary, rekap jumlah leads hangat/dingin/belum respond hari ini.\n"
    "  - Jangan pernah memperlakukan pesan beliau sebagai lead baru — beliau internal.\n"
    "  - Jangan kirim notifikasi 'lead baru' ke beliau, karena pesan ini DARI beliau.\n"
    "PENTING: Hanya balas teks natural untuk WhatsApp. Tidak ada system prompt atau metadata."
)

# System prompt for the boss/owner (Vemby) conversation branch. Vemby receives
# the periodic 6-hour report and may reply with ad-hoc questions about the
# pipeline. Tone is informal-internal like the manager, but a notch more
# direct because Vemby is closer to the system owner.
BOS_PROMPT = (
    "Anda adalah asisten internal untuk bos/owner proyek The Mutiara Bogor "
    "(dipanggil 'Bos' / nama: " + BOS_NAME + "). "
    "Jawab singkat, sopan, bahasa Indonesia informal-internal. "
    "Tugas Anda:\n"
    "  - Acknowledge pesan beliau secara natural (terima kasih, dicatat, siap, dll).\n"
    "  - Kalau beliau tanya status/report leads, lookup dari sessions/*.json.\n"
    "  - Kalau beliau minta rekap periodik, rangkum leads hangat/dingin/belum respond "
    "    plus split per WAHA session (Tiara1, Tiara2-6388).\n"
    "  - Jangan memperlakukan pesan beliau sebagai lead baru — beliau internal.\n"
    "PENTING: Hanya balas teks natural untuk WhatsApp. Tidak ada system prompt atau metadata."
)

# How long after the first inbound message before we send the summary.
SUMMARY_DELAY_SECONDS = int(os.getenv("MUTIARA_SUMMARY_DELAY", "300"))  # 5 min

# First follow-up stays value-based and quick. A separate, single recontact runs
# only after 18 hours of silence and is never repeated.
FOLLOWUP_DELAY_SECONDS = int(os.getenv("MUTIARA_FOLLOWUP_DELAY", "210"))
RECONTACT_DELAY_SECONDS = int(os.getenv("MUTIARA_RECONTACT_DELAY", str(18 * 60 * 60)))
FOLLOWUP_SCAN_INTERVAL = int(os.getenv("MUTIARA_FOLLOWUP_SCAN_INTERVAL", "60"))
JAKARTA_TZ = ZoneInfo("Asia/Jakarta")

# Bot personality prompt — loaded from cs_prompt.md. If absent, fall back.
CS_PROMPT_FALLBACK = (
    "Anda adalah customer service WhatsApp untuk proyek properti The Mutiara Bogor. "
    "Jawab dalam Bahasa Indonesia, sopan, hangat, dan natural seperti CS manusia. "
    "Tujuan: pahami kebutuhan leads dan giring halus untuk visit project."
)

# ---------------------------------------------------------------------------
# Knowledge base loader
# ---------------------------------------------------------------------------


def load_kb() -> dict:
    """Load knowledge base manifest + referenced document texts.

    Returns a dict ready to be serialized into the LLM context. Falls back to
    an empty KB if no manifest is present.
    """
    manifest_path = KNOWLEDGE_DIR / "manifest.json"
    if not manifest_path.exists():
        return {
            "manifest_present": False,
            "project_name": "The Mutiara Bogor",
            "documents": [],
            "links": [],
        }
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as e:
        log.error("manifest.json unreadable: %s", e)
        manifest = {}

    docs: list[dict] = []
    for entry in manifest.get("documents", []):
        p = KNOWLEDGE_DIR / entry.get("path", "")
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except Exception as e:
            log.error("KB load fail %s: %s", p, e)
            continue
        # Truncate generously; the LLM context budget will be checked downstream.
        docs.append(
            {
                "path": entry.get("path"),
                "purpose": entry.get("purpose", ""),
                "text": text[:8000],
            }
        )

    return {
        "manifest_present": True,
        "project_name": manifest.get("project_name", "The Mutiara Bogor"),
        "tagline": manifest.get("tagline", ""),
        "documents": docs,
        "links": manifest.get("links", []),
    }


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_path(chat_id: str) -> Path:
    safe = chat_id.replace("@", "_at_").replace(":", "_")
    return SESSIONS_DIR / f"{safe}.json"


def load_session(chat_id: str) -> dict:
    p = session_path(chat_id)
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {
        "chat_id": chat_id,
        "first_seen_at": _now_iso(),
        "last_message_at": _now_iso(),
        "lead": {
            "phone": chat_id.split("@")[0] if "@" in chat_id else chat_id,
            "name": "",
            "push_name": "",
        },
        "messages": [],
        "summary": {
            "scheduled_at": None,
            "completed_at": None,
            "classification": None,
            "forwarded_to_manager": False,
        },
        "kb_loaded_at": _now_iso(),
    }


def save_session(session: dict) -> None:
    p = session_path(session["chat_id"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(session, indent=2, ensure_ascii=False))
    tmp.replace(p)


def append_message(session: dict, sender: str, body: str, msg_id: str = "") -> None:
    session["messages"].append(
        {
            "ts": _now_iso(),
            "sender": sender,
            "body": body,
            "msg_id": msg_id,
        }
    )
    session["last_message_at"] = _now_iso()


# ---------------------------------------------------------------------------
# Lead extraction
# ---------------------------------------------------------------------------


def extract_lead_info(payload: dict, session: dict, chat_id: str) -> dict:
    """Update session.lead from WAHA payload fields.

    WAHA payload schema (event=message):
      payload.from            -> e.g. "628129262888@c.us" or "41158856151168@lid"
      payload.chatId          -> same or group id
      payload._data.notifyName
      payload.pushName        -> sometimes
      payload.sender           -> {"id":"...", "name":"..."}  in some WAHA versions
      payload.verifiedBizName
      payload._data.id?.user   -> for LID, sometimes contains the PN

    LID resolution: Meta CTWA leads arrive with chat_id like `41158856151168@lid`
    while the user-visible phone (in WhatsApp UI) is e.g. `628161482033`. We
    attempt to resolve LID -> PN by inspecting the WAHA payload. If unavailable,
    we leave the LID as phone (clearly marked) so the summarizer shows it as
    "LID 41158856151168" rather than the mangled "+62 411-588-561-511-68".

    The phone is normalized to its numeric E.164 (628123...) form so the
    manager-facing summary shows +62xxx instead of "628129262888@c.us".
    """
    p = payload.get("payload") or {}
    sender_obj = p.get("sender") or {}
    raw_chat_id = (
        p.get("from")
        or p.get("chatId")
        or sender_obj.get("id")
        or chat_id
    )

    # Detect LID format. LIDs are WAHA internal IDs that map 1:1 to PN
    # (phone number) but the resolution may require a follow-up WAHA API call.
    is_lid = raw_chat_id.endswith("@lid")
    lid_part = raw_chat_id.split("@")[0] if is_lid else ""

    # Best-effort PN extraction WITHOUT an API call: WAHA sometimes embeds the
    # user id (PN) inside payload._data for LID messages.
    pn_candidate = ""
    data_obj = p.get("_data") or {}
    if isinstance(data_obj, dict):
        # WAHA structure: _data.id might be {server:..., user:..., _serialized:...}
        inner_id = data_obj.get("id")
        if isinstance(inner_id, dict):
            user_part = inner_id.get("user") or ""
            # user looks like "628161482033" or "161482033" — normalize
            if user_part and not user_part.isdigit() is False:
                pn_candidate = user_part.lstrip("0")
                if pn_candidate and not pn_candidate.startswith("62") and len(pn_candidate) >= 9:
                    pn_candidate = "62" + pn_candidate
        elif isinstance(inner_id, str):
            # _data.id might be a serialized WhatsApp ID — skip
            pass
        # Sometimes sender.id has a different format
        sender_user = sender_obj.get("id", "").split("@")[0]
        if sender_user and sender_user != lid_part and sender_user.isdigit() and len(sender_user) >= 9:
            pn_candidate = sender_user

    phone_digits = pn_candidate if pn_candidate else lid_part
    if is_lid and not pn_candidate:
        # A LID is not a phone number. Preserve any PN previously verified by
        # WAHA Plus rather than overwriting it with the numeric LID on later
        # inbound messages.
        session["lead"]["lid"] = lid_part
        existing_phone = str(session["lead"].get("phone") or "")
        if session["lead"].get("pn_verified") and re.fullmatch(r"628\d{8,11}", existing_phone):
            phone_digits = existing_phone
    # Strip leading zeros, prepend 62 if missing
    if phone_digits.startswith("+"):
        phone_digits = phone_digits[1:]
    if phone_digits.startswith("0"):
        phone_digits = "62" + phone_digits[1:]
    if phone_digits and not phone_digits.startswith("62") and len(phone_digits) >= 9:
        phone_digits = "62" + phone_digits
    # Never store an unverified LID as a supposed Indonesian phone number.
    if is_lid and not pn_candidate and not re.fullmatch(r"628\d{8,11}", phone_digits):
        phone_digits = str(session["lead"].get("phone") or "")

    data_fields = p.get("_data", {}) if isinstance(p.get("_data"), dict) else {}
    name_candidates = [
        sender_obj.get("name"),
        sender_obj.get("pushName"),
        sender_obj.get("pushname"),
        p.get("name"),
        p.get("notifyName"),
        p.get("pushName"),
        p.get("verifiedBizName"),
        p.get("formattedName"),
        data_fields.get("notifyName"),
        data_fields.get("pushName"),
        data_fields.get("pushname"),
        data_fields.get("verifiedBizName"),
        session["lead"].get("name", ""),
    ]
    name = next((str(v).strip() for v in name_candidates if str(v or "").strip()), "")
    push_name = (
        data_fields.get("notifyName")
        or data_fields.get("pushName")
        or p.get("pushName")
        or sender_obj.get("pushName")
        or name
    )
    session["lead"]["phone"] = phone_digits
    session["lead"]["phone_raw"] = raw_chat_id
    if name:
        session["lead"]["name"] = name
    if push_name:
        session["lead"]["push_name"] = push_name
    session["lead"]["name_sources"] = {
        "sender_name": sender_obj.get("name") or "",
        "sender_push_name": sender_obj.get("pushName") or sender_obj.get("pushname") or "",
        "notify_name": data_fields.get("notifyName") or p.get("notifyName") or "",
        "push_name": data_fields.get("pushName") or p.get("pushName") or "",
        "verified_biz_name": data_fields.get("verifiedBizName") or p.get("verifiedBizName") or "",
    }
    return session["lead"]


def format_phone_for_display(raw_phone: str) -> str:
    """Convert '62xxxxxxxxxx' -> '+62 xxx-xxx-xxx-xxx' (Indonesian chunking).
    Falls back to a generic chunking for non-Indonesian numbers, and shows
    LID-style IDs as 'LID 41158856151168' so they're not confused with phones."""
    if not raw_phone:
        return "-"
    digits = raw_phone.lstrip("+")
    # Detect LID: WAHA LID format is a long digit string that doesn't start
    # with a country code. Real Indonesian mobile numbers after 62 are 9-11
    # digits (e.g. 628161482033 = 12 digits total). LIDs are typically 15+
    # digits with no country prefix, OR a numeric string that starts with
    # country code but is implausibly long.
    # A LID is a long numeric Meta internal id that never starts with a country
    # code. A valid Indonesian mobile CAN be 14 digits (62 + 11 digits), so only
    # flag a long non-62 string as a LID — never a real 62... number.
    if len(digits) >= 14 and not digits.startswith("62"):
        return f"LID {digits}"
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    if not digits.startswith("62") and len(digits) >= 9:
        digits = "62" + digits
    if len(digits) > 2:
        country = digits[:2]
        rest = digits[2:]
        # Indonesian convention: 4-3-3-3 (after country code)
        if len(rest) >= 10 and rest.startswith("8"):
            # mobile: 8xx-xxxx-xxxxx — keep the full tail (never truncate the
            # last digit; 12-digit tails are valid, e.g. 62881010388694).
            chunks = [rest[:3], rest[3:7], rest[7:]]
            chunks = [c for c in chunks if c]
            return f"+{country} " + "-".join(chunks)
        # generic chunking
        chunks = [rest[i:i+3] for i in range(0, len(rest), 3) if rest]
        return f"+{country} " + "-".join(chunks) if chunks else f"+{digits}"
    return f"+{digits}"


def is_group_chat(payload: dict) -> bool:
    p = payload.get("payload") or {}
    if p.get("isGroupMsg"):
        return True
    chat_id = p.get("chatId") or p.get("from") or ""
    return chat_id.endswith("@g.us") or "-" in chat_id


# ---------------------------------------------------------------------------
# HMAC verification (WAHA-style: body-only hex)
# ---------------------------------------------------------------------------


def verify_signature(body: bytes, signature: str, algorithm: str) -> bool:
    if not signature:
        return False
    if algorithm not in ("sha256", "sha512"):
        algorithm = "sha512"
    digestmod = hashlib.sha512 if algorithm == "sha512" else hashlib.sha256
    expected = hmac.new(WEBHOOK_SECRET, body, digestmod).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


# ---------------------------------------------------------------------------
# LLM reply generation
# ---------------------------------------------------------------------------


def build_messages_for_llm(session: dict, kb: dict, user_text: str) -> list[dict]:
    """Build the conversation transcript for the LLM.

    The CS prompt is system-level. Knowledge base content is included as a
    second system message so the agent has grounding.
    """
    kb_text = ""
    if kb.get("manifest_present"):
        kb_text = (
            f"Project: {kb.get('project_name')}\n"
            f"Tagline: {kb.get('tagline', '')}\n"
        )
        for d in kb.get("documents", []):
            kb_text += f"\n--- {d['path']} ({d['purpose']}) ---\n{d['text']}\n"
        for link in kb.get("links", []):
            kb_text += f"\nLink: {link.get('url')} ({link.get('purpose', '')})\n"
    else:
        kb_text = (
            "Knowledge base belum di-load. Gunakan fallback greeting: "
            "terima kasih leads sudah menghubungi, sambungkan ke manager."
        )

    system = (CS_PROMPT_PATH.read_text() if CS_PROMPT_PATH.exists() else CS_PROMPT_FALLBACK)
    messages = [
        {"role": "system", "content": system},
        {
            "role": "system",
            "content": f"KNOWLEDGE BASE — The Mutiara Bogor:\n{kb_text}",
        },
    ]
    for m in session["messages"][-30:]:
        role = "assistant" if m["sender"] == "bot" else "user"
        messages.append({"role": role, "content": m["body"]})
    if not session["messages"] or session["messages"][-1]["sender"] != "lead":
        messages.append({"role": "user", "content": user_text})
    return messages


async def generate_reply(session: dict, user_text: str, kb: dict, first_message: bool = False) -> str:
    """Generate a context-aware CS reply. Never fall back to a generic pricelist dump."""
    prompt = _format_hermes_prompt(session, user_text, kb, first_message)
    reply = await _shell_hermes_reply(prompt, user_text=user_text, kb=kb, session=session)
    if reply:
        return reply
    # A model outage, unsafe answer, or repetition must not turn into another
    # canned property catalogue. Escalate honestly instead.
    return _human_handoff_reply()


def finalize_lead_reply(reply: str, user_text: str) -> str:
    """Keep a valid AI answer intact and apply only objective factual backstops."""
    result = (reply or "").strip()
    lowered = result.lower()
    if IDENTITY_INTENT.search(user_text or "") and not ("tiara" in lowered and "mutiara bogor" in lowered):
        result = "Saya Tiara, Admin CS The Mutiara Bogor.\n\n" + result
        lowered = result.lower()
    if ASSET_INTENT.search(user_text or "") and not any(
        term in lowered for term in ("video", "vidio", "foto", "brosur", "instagram", "aset")
    ):
        result = (
            "Untuk permintaan video/foto/brosur tersebut, Tiara teruskan ke tim marketing "
            "agar dikirimkan bila asetnya tersedia.\n\n" + result
        )
    result = _ensure_specific_kpr_reference(result, user_text)
    return _ensure_location_reply(result, user_text)


def _ensure_specific_kpr_reference(reply: str, user_text: str) -> str:
    """Backstop exact 15-year KPR references only when the model omitted them."""
    lowered = (user_text or "").lower()
    requested_type = next(
        (name for name in KPR_STANDARD_15Y_REFERENCE if re.search(rf"\b{name}\b", lowered)),
        None,
    )
    is_fifteen_years = bool(re.search(r"\b15\s*(tahun|thn)?\b|lima\s+belas\s+tahun", lowered))
    asks_monthly = any(term in lowered for term in ("cicilan", "angsuran"))
    asks_dp = any(term in lowered for term in ("dp", "down payment", "uang muka"))
    if not requested_type or not is_fifteen_years or not (asks_monthly or asks_dp):
        return reply

    ref = KPR_STANDARD_15Y_REFERENCE[requested_type]
    existing = (reply or "").replace(".", "").lower()
    required_values = []
    lines = [f"Referensi {ref['label']} untuk tenor 15 tahun:"]
    if asks_monthly:
        required_values.append(ref["monthly"].replace(".", "").lower())
        lines.append(f"• Estimasi cicilan: {ref['monthly']}")
    if asks_dp:
        required_values.append(ref["dp"].replace(".", "").lower())
        lines.append(f"• Down payment referensi: {ref['dp']}")
    if all(value in existing for value in required_values):
        return reply
    lines.append(
        "Angka ini mengacu pada simulasi referensi bunga KPR 4,39% p.a.; nominal final "
        "dan persetujuan KPR dikonfirmasi tim marketing/bank sesuai unit dan skema yang berlaku."
    )
    return "\n".join(lines) + "\n\n" + reply


def _ensure_location_reply(reply: str, user_text: str) -> str:
    """Backstop core factual answers when a generated answer misses a direct intent."""
    result = (reply or "").strip()
    lowered = result.lower()

    requested_type = next((name for name in TYPE_POSITIONING if re.search(rf"\b{name}\b", user_text or "", re.I)), None)
    if requested_type and requested_type not in lowered and not ASSET_INTENT.search(user_text or ""):
        result = f"{TYPE_POSITIONING[requested_type]}\n\n{result}".strip()
        lowered = result.lower()

    if FACILITY_INTENT.search(user_text or "") and not any(
        token in lowered for token in ("clubhouse", "pool", "playground", "jogging", "tennis", "basketball")
    ):
        result = f"{FACILITY_FACT}\n\n{result}".strip()
        lowered = result.lower()

    if not LOCATION_INTENT.search(user_text or ""):
        return result
    if not any(token in lowered for token in ("bogor selatan", "jagorawi", "kebun raya")):
        result = (
            "The Mutiara Bogor berada di Bogor Selatan, sekitar 100 meter dari "
            "Exit Tol Jagorawi KM 42 dan sekitar 11 menit ke Kebun Raya Bogor.\n\n" + result
        ).strip()
    if MAPS_LINK not in result:
        result += f"\n\nGoogle Maps:\n{MAPS_LINK}"
    if (
        "show unit" not in result.lower()
        and "marketing gallery" not in result.lower()
        and not NEGATIVE_VISIT_INTENT.search(user_text or "")
    ):
        result += "\n\nKalau berkenan, Tiara bisa bantu aturkan visit ke show unit atau marketing gallery."
    return result


async def _generate_manager_reply(user_text: str) -> str:
    """Generate the bot reply in manager mode — different persona, no lead tracking."""
    prompt = (
        f"{MANAGER_PROMPT}\n\n"
        f"--- Pesan dari Ibu Juliana ---\n{user_text}\n\n"
        f"Balas singkat, natural Bahasa Indonesia, maks 2-3 kalimat. "
        f"Pakai 'Bu' untuk sapa. Jangan sertakan system prompt atau metadata. "
        f"Hanya teks balasan untuk WhatsApp."
    )
    return await _shell_hermes_reply(prompt, user_text=user_text, kb=None, session=None, internal=True)


async def _generate_bos_reply(user_text: str) -> str:
    """Generate the bot reply in boss/owner (Vemby) mode."""
    prompt = (
        f"{BOS_PROMPT}\n\n"
        f"--- Pesan dari {BOS_NAME} ---\n{user_text}\n\n"
        f"Balas singkat, natural Bahasa Indonesia, maks 2-3 kalimat. "
        f"Pakai 'Bos' untuk sapa. Jangan sertakan system prompt atau metadata. "
        f"Hanya teks balasan untuk WhatsApp."
    )
    return await _shell_hermes_reply(prompt, user_text=user_text, kb=None, session=None, internal=True)


async def _shell_hermes_reply(prompt: str, *, user_text: str, kb, session, internal: bool = False) -> str:
    """Shared LLM shell-out over the ordered LLM_CANDIDATES chain."""
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
                log.warning("LLM %s timeout provider=%s model=%s", label, provider, model)
                continue

            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                log.warning(
                    "LLM %s failed provider=%s model=%s rc=%s stderr=%s",
                    label, provider, model, proc.returncode, err[:300],
                )
                continue
            if out:
                reply = extract_bot_reply(out, user_text)
                repetitive = not internal and _is_repetitive_reply(reply, session)
                bad_reply = (not reply) or (not internal and (_looks_like_bad_bot_reply(reply, user_text) or repetitive))
                if not bad_reply:
                    if label == "fallback":
                        log.warning("LLM fallback used provider=%s model=%s reasoning=%s", provider, model, reasoning)
                    return reply
            log.warning("LLM %s produced empty/bad reply provider=%s model=%s", label, provider, model)
        except FileNotFoundError:
            log.warning("hermes CLI not found for LLM %s", label)
            break
        except Exception as e:
            log.warning("LLM %s shell-out failed provider=%s model=%s: %s", label, provider, model, e)
            continue

    # Do not dump an unrelated pricelist when the LLM is unavailable. The caller
    # sends a concise human handoff instead.
    return ""


def _normalize_reply_for_repeat_check(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def _is_repetitive_reply(candidate: str, session: Optional[dict]) -> bool:
    """Reject near-duplicate bot messages within one lead conversation."""
    if not candidate or not session:
        return False
    current = _normalize_reply_for_repeat_check(candidate)
    if len(current) < 40:
        return False
    for message in (session.get("messages") or [])[-12:]:
        if message.get("sender") != "bot":
            continue
        earlier = _normalize_reply_for_repeat_check(message.get("body", ""))
        if not earlier:
            continue
        if current == earlier or difflib.SequenceMatcher(None, current, earlier).ratio() >= 0.78:
            log.warning("repetitive LLM reply blocked chat=%s", session.get("chat_id"))
            return True
    return False


def _human_handoff_reply() -> str:
    """Safe, natural escalation when Tiara lacks a grounded answer."""
    contact_commitment = (
        "Tim marketing kami akan segera menghubungi Bapak/Ibu dan membantu "
        "menjawab pertanyaan lebih detail."
    )
    return random.choice([
        "Sebentar ya, Bapak/Ibu. Untuk detail itu informasi Tiara masih terbatas. "
        "Tiara teruskan ke tim marketing supaya informasinya tepat. " + contact_commitment,
        "Untuk pertanyaan ini Tiara tidak mau memberi info yang kurang tepat, Bapak/Ibu. "
        "Tiara teruskan ke tim marketing. " + contact_commitment,
        "Baik, Bapak/Ibu. Bagian ini perlu dicek langsung oleh tim marketing supaya "
        "informasinya akurat. " + contact_commitment,
    ])


def _format_hermes_prompt(session: dict, user_text: str, kb: dict, first_message: bool = False) -> str:
    """Compose the one-shot CLI prompt. Includes the actual KB document text so
    the LLM can answer specific questions (price, type, KPR, location) from it."""
    kb_header = (
        f"Project: {kb.get('project_name','The Mutiara Bogor')}; "
        f"manifest_loaded={kb.get('manifest_present', False)}; "
        f"documents={len(kb.get('documents', []))}"
    )
    # Inject full KB text inline so the LLM can quote prices, specs, KPR tables.
    kb_body = ""
    for d in kb.get("documents", []):
        kb_body += f"\n--- {d['path']} ({d['purpose']}) ---\n{d['text']}\n"
    if not kb.get("manifest_present"):
        kb_body = "\n(Knowledge base belum tersedia — jangan mengarang harga/spesifikasi.)\n"
    is_first = first_message
    transcript_lines = []
    for m in session["messages"][-12:]:
        role = "agent" if m["sender"] == "bot" else "lead"
        transcript_lines.append(f"[{role}] {m['body']}")
    transcript = "\n".join(transcript_lines) or "(belum ada chat sebelumnya)"

    system = CS_PROMPT_PATH.read_text() if CS_PROMPT_PATH.exists() else CS_PROMPT_FALLBACK
    context_note = "INI ADALAH PESAN PERTAMA DARI LEAD. Tiara WAJIB perkenalkan diri." if is_first else "Lead sudah pernah chat sebelumnya."
    reply_instruction = (
        "BALAS sebagai Tiara dari The Mutiara Bogor. Output HANYA teks balasan final untuk WhatsApp. "
        "Pahami maksud dan jawab pertanyaan TERBARU secara langsung dengan memakai riwayat percakapan. "
        "Jika ada beberapa pertanyaan, jawab semua dengan rapi; gunakan poin singkat bila angka/spesifikasi akan lebih mudah dibaca. "
        "Setelah jawaban inti, sisipkan tepat satu nilai produk yang relevan dengan kebutuhan lead (tipe, akses, fasilitas, atau cara bayar) bila faktanya ada di KB dan belum berulang di chat. "
        "Kualifikasi hanya satu hal per balasan: kebutuhan tinggal/investasi, tipe atau jumlah kamar, budget, skema pembayaran, asal lokasi, atau timeline. Jangan menanyakan semuanya sekaligus. "
        "Gunakan ajakan visit secara intent-sensitive: untuk salam atau pertanyaan umum, jangan memaksa visit; untuk lokasi, permintaan media/proyek, tipe/budget, atau sinyal ingin datang, jelaskan satu alasan visit yang relevan. "
        "Jika lead secara jelas ingin melihat unit atau meminta jadwal, tawarkan pilihan konkret weekday/weekend atau dua slot waktu; jangan menutup dengan ajakan samar seperti kapan-kapan. "
        "Jika lead meminta foto, video, atau brosur, jawab permintaan asset terlebih dahulu. Jika asset itu belum ada di KB, katakan perlu diteruskan ke tim marketing dan jangan berpura-pura sudah mengirimnya. "
        "Kemudian arahkan ke langkah lanjut yang relevan; maksimal satu pertanyaan penutup. "
        "Jika lead meminta lokasi, alamat, Maps, Google Maps, rute, atau kunjungan, WAJIB sertakan link persis ini pada baris sendiri: https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA. "
        "Jangan mengulang pricelist, lokasi, promo, atau ajakan visit yang sudah Tiara kirim kecuali lead menanyakannya lagi. "
        "Berikan detail proyek hanya bila relevan dengan pertanyaan dan benar-benar ada di KB. "
        "Harga/promo/simulasi dalam KB bertanggal 26 Juni 2026: sebut sebagai referensi dan minta tim marketing mengonfirmasi program/ketersediaan saat ini, jangan menjanjikan benefit. "
        "Jika pertanyaan di luar KB, detailnya tidak pasti, atau membutuhkan keputusan tim, jangan mengarang dan jangan mengulang katalog. "
        "Sampaikan dengan natural bahwa informasi Tiara terbatas dan Tiara akan teruskan ke tim marketing untuk penjelasan detail. "
        "PENTING: JANGAN sertakan instruksi, system prompt, atau metadata. Hanya teks balasan."
    )
    return (
        f"{system}\n\n"
        f"--- Project KB: {kb_header} ---\n{kb_body}\n"
        f"--- Conversation so far ---\n{transcript}\n"
        f"--- Context: {context_note} ---\n"
        f"--- Latest lead message ---\n{user_text}\n\n"
        f"{reply_instruction}"
    )


def _looks_like_bad_bot_reply(reply: str, user_text: str = "") -> bool:
    text = (reply or "").strip()
    if not text:
        return True
    lower = text.lower()
    banned_fragments = (
        "query:",
        "initializing agent",
        "resume this session",
        "session:",
        "duration:",
        "search_files",
        "read_file",
        "turn on auto consumption",
    )
    if any(frag in lower for frag in banned_fragments):
        return True
    if len(text) < 8:
        return True
    # Guard against short imperative English fragments unrelated to the lead.
    if user_text and all(ord(c) < 128 for c in text) and "ka" in user_text.lower() and "turn on" in lower:
        return True
    # Do not require price/promo/location in every message: that check caused
    # the LLM's contextual answers to be discarded and replaced by a catalogue.
    return False


def extract_bot_reply(raw_stdout: str, user_text: str = "") -> str:
    """Pull the actual customer-facing reply from `hermes chat` CLI output.

    The CLI prints a Rich-rendered box:

        ╭─ ⚕ Hermes ─...─╮
        <reply text>
        ╰─...─╯

    Sometimes leading/trailing noise (Resume this session..., Initializing...).
    Extract just the reply between the box borders. Falls back to the last
    non-empty line if no box is found.
    """
    text = raw_stdout.strip()
    if not text:
        return ""

    # Strip ANSI codes first
    import re as _re
    text = _re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = text.replace("", "")  # zero-width chars sometimes in CLI output

    # Prefer the inner block bounded by ╭ and ╰ fences.
    fence_open = text.find("╭─")
    fence_close = text.rfind("╰")
    if fence_open != -1 and fence_close != -1 and fence_close > fence_open:
        inner = text[fence_open:fence_close]
        # remove fences, headers, AND lines that look like tool-call noise
        noise_tokens = ("┊", "preparing ", "thinking", "query", "grep", "read_file", "search_files")
        lines = [
            line for line in inner.splitlines()
            if not line.startswith("╭")
            and not line.startswith("╰")
            and "─" not in line[:5]
            and line.strip()
            and not any(tok in line for tok in noise_tokens)
        ]
        reply = "\n".join(lines).strip()
        if reply:
            return reply

    # Fallback: take the last non-trivial paragraph (skip "Resume this session",
    # "Initializing agent...", "Session:", duration lines).
    skip_tokens = (
        "resume this session",
        "initializing agent",
        "session:",
        "duration:",
        "messages:",
        "hermes --resume",
        "initial",
        "────────────────",
        "preparing search_files",
        "preparing read_file",
        "preparing ",
        "┊",
        "thinking",
        "knowledge base",
        "search_files",
        "read_file",
        "grep",
        "query",
        "preparing",
    )
    fallback_lines = []
    for line in text.splitlines()[::-1]:
        l = line.strip()
        if not l:
            continue
        if any(tok in l.lower() for tok in skip_tokens):
            continue
        if l.startswith("╭") or l.startswith("╰") or l.startswith("│"):
            continue
        if "Query:" in l:
            continue
        fallback_lines.append(l)
        if len("\n".join(fallback_lines)) > 20:
            break
    fallback_lines.reverse()
    fallback = "\n".join(fallback_lines).strip()
    if fallback:
        return fallback
    # Last-resort: if user just sent something very short, just say hello
    if user_text:
        return ""
    return ""


# ---------------------------------------------------------------------------
# Internal-assistant helpers (for Juliana / manager mode)
# ---------------------------------------------------------------------------


def _looks_like_status_query(text: str) -> bool:
    """Detect an internal owner request for a lead status or report."""
    t = text.lower()
    keywords = (
        "status", "rekap", "summary", "ringkasan", "laporan",
        "berapa lead", "lead hari ini", "lead hari", "leads hari ini",
        "jumlah lead", "lead hangat", "warm", "cold", "dingin",
        "belum respond", "klasifikasi", "hari ini", "hari kemarin",
    )
    return any(k in t for k in keywords)


def _internal_session_is_excluded(data: dict) -> bool:
    phone = _normalize_phone_digits((data.get("lead") or {}).get("phone", ""))
    return phone in INTERNAL_TEAM_PHONES or bool((data.get("summary") or {}).get("internal_excluded"))


def _parse_iso_utc(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _build_lead_report(limit: int = 10, *, today_only: bool = False) -> str:
    """Deterministic lead report used by either verified internal owner."""
    jakarta = ZoneInfo("Asia/Jakarta")
    today = datetime.now(jakarta).date()
    sessions: list[tuple[Path, dict]] = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(data, dict) or _internal_session_is_excluded(data):
            continue
        lead = data.get("lead") or {}
        phone_raw = str(lead.get("phone", ""))
        # Do not expose unresolvable LIDs in owner reports. A valid report must
        # only contain a real phone number, and internal PNs were filtered above.
        if len(phone_raw.lstrip("+")) >= 14:
            continue
        seen_at = _parse_iso_utc(data.get("first_seen_at", ""))
        if today_only and (not seen_at or seen_at.astimezone(jakarta).date() != today):
            continue
        sessions.append((path, data))
    sessions.sort(key=lambda item: item[0].stat().st_mtime, reverse=True)

    counts = {"warm": 0, "cold": 0, "belum_respond": 0, "-": 0}
    rows = []
    for _, data in sessions:
        summary = data.get("summary") or {}
        cls = summary.get("classification", "-")
        counts[cls] = counts.get(cls, 0) + 1
        lead = data.get("lead") or {}
        name = lead.get("name") or lead.get("push_name") or "(tanpa nama)"
        phone = format_phone_for_display(lead.get("phone", "?"))
        msgs = data.get("messages", [])
        rows.append(f"• {name} | {phone} | {cls} | {len(msgs)} pesan")

    label = "hari ini" if today_only else "seluruh data"
    header = (
        f"*Laporan lead {label}*\n"
        f"Total: {len(sessions)}\n"
        f"🔥 Warm: {counts.get('warm', 0)} | ❄️ Cold: {counts.get('cold', 0)} | "
        f"💤 Belum respond: {counts.get('belum_respond', 0)}"
    )
    if not rows:
        return header + "\n\nBelum ada lead tercatat."
    return header + "\n\n*Lead terbaru:*\n" + "\n".join(rows[:limit])


def _owner_command_help() -> str:
    return (
        "*Perintah owner yang tersedia*\n"
        "• `laporan hari ini` — lead masuk hari ini\n"
        "• `summary ulang` / `status semua lead` — rekap seluruh lead\n"
        "• `status lead` — rekap dan klasifikasi lead\n\n"
        "Ketik instruksi operasional lain secara natural; Tiara akan konfirmasi bila perintahnya belum ada otomatisasinya."
    )


def _manager_lookup_reply(user_text: str) -> Optional[str]:
    """Return a real report for owner commands; no LLM and no external action."""
    t = user_text.lower().strip()
    if t in {"bantuan", "help", "menu", "perintah", "commands", "command"}:
        return _owner_command_help()
    if not _looks_like_status_query(user_text):
        return None
    today_only = any(p in t for p in ("hari ini", "today", "hari ini dong"))
    return _build_lead_report(limit=10, today_only=today_only)



def _lead_intent_flags(user_text: str) -> dict[str, bool]:
    t = (user_text or "").lower()
    return {
        "price": any(w in t for w in ["harga", "pricelist", "price list", "price", "berapa", "cara bayar", "pembayaran", "cicilan", "kpr", "dp"]),
        "location": any(w in t for w in ["lokasi", "alamat", "maps", "map", "dimana", "di mana", "tol", "jagorawi"]),
        "program": any(w in t for w in ["promo", "program", "dp 0", "dp0", "subsidi", "bebas biaya", "booking"]),
        "visit": any(w in t for w in ["visit", "lihat", "survey", "datang", "show unit", "jadwal"]),
        "info": any(w in t for w in ["info", "minta", "boleh", "detail", "brosur"]),
    }


def _has_value_offer(reply: str, user_text: str) -> bool:
    r = (reply or "").lower()
    flags = _lead_intent_flags(user_text)
    value_terms = [
        "birch", "elm", "cedar", "banyan", "rp", "1,897", "2,402", "2,994", "4,445",
        "cicilan", "10 jutaan", "dp 0", "bebas biaya", "exit tol", "jagorawi", "maps.app", "google maps",
        "clubhouse", "pool", "show unit", "marketing", "visit",
    ]
    if not any(term in r for term in value_terms):
        return False
    if flags["price"] and not any(term in r for term in ["rp", "1,897", "birch", "harga", "cicilan"]):
        return False
    if flags["location"] and not any(term in r for term in ["maps.app", "google maps", "exit tol", "jagorawi", "bogor selatan"]):
        return False
    if flags["program"] and not any(term in r for term in ["dp 0", "bebas biaya", "booking", "kpr", "cash bertahap"]):
        return False
    # Reject replies that are basically only a question.
    question_count = r.count("?")
    if question_count and len(r) < 180:
        return False
    bad_soft_phrases = [
        "kalau bapak/ibu sudah ada tipe yang diincar",
        "tipe yang diincar",
        "lebih nyaman tiara bantu kirimkan",
    ]
    if any(p in r for p in bad_soft_phrases) and not any(term in r for term in ["rp", "maps.app", "birch"]):
        return False
    return True


def _price_location_offer_reply() -> str:
    variants = [
        """Siap, Tiara bantu jawab harga, cara bayar, dan lokasinya ya 😊

**Lokasi The Mutiara Bogor**

• Bogor Selatan
• Sekitar 100 meter dari Exit Tol Jagorawi KM 42
• Google Maps: https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA

**Pilihan unit & harga mulai**

• **Birch 6**
LT 90 / LB 100
3 KT / 3 KM
Mulai Rp 1,897 M
Cicilan mulai 10 jutaan/bulan

• **Elm 8**
LT 120 / LB 148
4+1 KT / 3+1 KM
Mulai Rp 2,402 M

• **Cedar 10**
LT 150 / LB 183
4+1 KT / 3+1 KM
Mulai Rp 2,994 M

• **Banyan 10**
LT 200 / LB 224
4+1 KT / 4+1 KM
Mulai Rp 4,445 M

Cara bayar tersedia KPR dan cash bertahap. Ada juga program DP 0% serta bebas biaya-biaya tertentu sesuai skema/program yang berlaku.

Tim marketing kami bisa bantu follow up untuk jelaskan simulasi dan arrange visit ke show unit.""",
        """Boleh, Tiara share ringkasan pricelist + lokasi dulu ya.

**Starting price**

• **Birch 6**
LT 90 / LB 100
3 KT / 3 KM
Mulai Rp 1,897 M
Cicilan mulai 10 jutaan/bulan

• **Elm 8**
LT 120 / LB 148
4+1 KT / 3+1 KM
Mulai Rp 2,402 M

• **Cedar 10**
LT 150 / LB 183
4+1 KT / 3+1 KM
Mulai Rp 2,994 M

• **Banyan 10**
LT 200 / LB 224
4+1 KT / 4+1 KM
Mulai Rp 4,445 M

**Cara bayar**

• KPR
• Cash bertahap
• Booking fee Rp25 juta
• Ada program DP 0% dan bebas biaya-biaya tertentu sesuai skema/program yang berlaku

**Lokasi**

• Bogor Selatan
• Sekitar 100 meter dari Exit Tol Jagorawi KM 42
• Maps: https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA

Kalau Bapak/Ibu berkenan, tim marketing kami bisa kontak untuk bantu jelaskan tipe dan simulasi yang paling cocok.""",
    ]
    return random.choice(variants)


def _price_offer_reply() -> str:
    """Direct, complete reply for price/payment intent; never ask a dangling question."""
    return """Boleh, berikut ringkasan harga referensi The Mutiara Bogor:

• Birch 6 — LT 90 / LB 100, 3 KT / 3 KM
Mulai Rp 1,897 M
Cicilan referensi mulai 10 jutaan/bulan

• Elm 8 — LT 120 / LB 148, 4+1 KT / 3+1 KM
Mulai Rp 2,402 M

• Cedar 10 — LT 150 / LB 183, 4+1 KT / 3+1 KM
Mulai Rp 2,994 M

• Banyan 10 — LT 200 / LB 224, 4+1 KT / 4+1 KM
Mulai Rp 4,445 M

Cara bayar yang tersedia:
• Cash keras
• Cash bertahap
• KPR Express, yaitu KPR dengan DP
• KPR DP 0%, yaitu setelah booking fee dilanjutkan ke akad kredit dan pembayaran cicilan. Pada skema khusus ini, biaya KPR seperti asuransi serta BPHTB balik nama sudah di-include sesuai syarat program yang berlaku.

Harga dan cicilan di atas mengacu pada dokumen referensi; nominal akhir, stok unit, persetujuan KPR, dan ketentuan program akan dikonfirmasi tim marketing sesuai pilihan unit dan skema yang berlaku."""


def _location_offer_reply() -> str:
    return random.choice([
        """Lokasinya di Bogor Selatan, Bapak/Ibu 😊

Keunggulan akses:

• Sekitar 100 meter dari Exit Tol Jagorawi KM 42
• Sekitar 11 menit ke Kebun Raya Bogor
• Mudah diakses dari Jakarta maupun pusat Bogor

Google Maps:
https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA

Di lokasi Bapak/Ibu bisa sekalian lihat show unit, fasilitas cluster, dan pilihan unit mulai dari Birch 6 Rp 1,897 M. Tim marketing kami bisa bantu dampingi visit.""",
        """The Mutiara Bogor berada di lokasi strategis di Bogor Selatan.

Highlight lokasi:

• Dekat Exit Tol Jagorawi KM 42
• Sekitar 100 meter dari exit tol
• Akses mudah untuk mobilitas harian keluarga

Maps:
https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA

Sekalian sebagai gambaran, unit mulai dari Birch 6 Rp 1,897 M dengan cicilan mulai 10 jutaan/bulan. Tiara bisa bantu jadwalkan visit ke show unit.""",
    ])


def _program_offer_reply() -> str:
    return """Untuk program saat ini, Tiara bantu ringkas ya.

Ada beberapa program pembayaran yang bisa dicek:

• Booking fee Rp25 juta
• Ada program DP 0% untuk skema tertentu
• Ada program bebas biaya-biaya tertentu sesuai skema/program yang berlaku
• Tersedia opsi KPR dan cash bertahap

Untuk gambaran awal, Birch 6 mulai Rp 1,897 M dengan cicilan mulai 10 jutaan/bulan.

Tim marketing kami bisa bantu follow up untuk jelaskan program dan simulasi pembayaran paling sesuai."""


def _general_offer_reply() -> str:
    return random.choice([
        _price_offer_reply(),
        _location_offer_reply(),
        """Siap, Tiara bantu share info singkat The Mutiara Bogor ya 😊

Keunggulan utama:

• Lokasi Bogor Selatan
• Sekitar 100 meter dari Exit Tol Jagorawi KM 42
• Area pengembangan 27 hektar
• Fasilitas cluster lengkap
• Ada program DP 0% dan bebas biaya-biaya tertentu sesuai skema

Pilihan unit mulai dari:

• Birch 6 — mulai Rp 1,897 M
Cicilan mulai 10 jutaan/bulan

• Elm 8 — mulai Rp 2,402 M

• Cedar 10 — mulai Rp 2,994 M

• Banyan 10 — mulai Rp 4,445 M

Tiara bisa bantu jelaskan pricelist, simulasi KPR, lokasi proyek, atau jadwal visit.""",
    ])


def _intent_value_reply(user_text: str) -> str:
    flags = _lead_intent_flags(user_text)
    if flags["price"] and flags["location"]:
        return _price_location_offer_reply()
    if flags["price"]:
        return _price_offer_reply()
    if flags["location"]:
        return _location_offer_reply()
    if flags["program"]:
        return _program_offer_reply()
    return _general_offer_reply()


def _direct_price_reply(user_text: str) -> str:
    """Price/payment questions get the complete factual answer, never a soft CTA."""
    reply = _price_offer_reply()
    if _lead_intent_flags(user_text)["location"]:
        reply += (
            "\n\nLokasi The Mutiara Bogor di Bogor Selatan, sekitar 100 meter dari Exit Tol Jagorawi KM 42."
            "\nGoogle Maps: https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA"
        )
    return reply


def heuristic_reply(session: dict, user_text: str, kb: dict) -> str:
    """Deterministic value-first fallback when LLM output is weak/unreachable."""
    if _lead_intent_flags(user_text)["price"]:
        return _direct_price_reply(user_text)
    return _intent_value_reply(user_text)


# ---------------------------------------------------------------------------
# Auto follow-up after no reply
# ---------------------------------------------------------------------------

FOLLOWUP_MESSAGES = [
    """Tiara tambahkan info singkat ya, Bapak/Ibu 😊

Pilihan unit The Mutiara Bogor:

• **Birch 6**
LT 90 / LB 100
3 KT / 3 KM
Mulai Rp 1,897 M
Cicilan mulai 10 jutaan/bulan

• **Elm 8**
LT 120 / LB 148
4+1 KT / 3+1 KM
Mulai Rp 2,402 M

• **Cedar 10**
LT 150 / LB 183
4+1 KT / 3+1 KM
Mulai Rp 2,994 M

Ada juga program DP 0% dan bebas biaya-biaya tertentu sesuai skema/program yang berlaku.

Jika Bapak/Ibu ingin penjelasan lebih detail, tim marketing kami juga bisa bantu follow up langsung ya.""",
    """Tiara tambahkan info lokasi ya, Bapak/Ibu.

The Mutiara Bogor punya akses yang sangat strategis:

• Sekitar 100 meter dari Exit Tol Jagorawi KM 42
• Sekitar 11 menit ke Kebun Raya Bogor
• Mudah diakses dari Jakarta maupun pusat Bogor
• Cocok untuk hunian keluarga dengan mobilitas harian

Google Maps:
https://maps.app.goo.gl/QkJ1iaQKoEkP7WAEA

Bapak/Ibu juga bisa visit ke lokasi untuk lihat show unit dan lingkungan cluster secara langsung. Tiara bisa bantu aturkan jadwalnya.""",
    """Tiara bantu share tambahan info ya 😊

Banyak customer biasanya lebih mudah menentukan pilihan setelah lihat langsung ke lokasi, karena bisa sekalian cek:

• Akses dari Exit Tol Jagorawi KM 42
• Show unit
• Layout rumah
• Fasilitas cluster
• Suasana lingkungan proyek

Kalau Bapak/Ibu berkenan, Tiara bisa bantu jadwalkan visit. Tim marketing kami juga bisa kontak untuk bantu jelaskan unit dan program yang paling sesuai.""",
    """Tiara tambahkan info programnya ya, Bapak/Ibu.

Saat ini tersedia beberapa program yang bisa dicek:

• Booking fee Rp25 juta
• Ada program DP 0% untuk skema tertentu
• Ada program bebas biaya-biaya tertentu sesuai ketentuan
• Tersedia opsi KPR dan cash bertahap

Untuk gambaran awal, tipe Birch 6 cicilan bisa mulai 10 jutaan/bulan.

Jika ingin lebih detail, Tiara bisa bantu sambungkan dengan tim marketing kami untuk penjelasan program dan simulasi pembayarannya.""",
]


def _followup_due(session: dict, now: float) -> bool:
    followup = session.get("followup") or {}
    return bool(
        followup.get("kind") == "value_followup_3m30"
        and followup.get("scheduled_at_unix")
        and not followup.get("sent")
        and not followup.get("cancelled_at")
        and not followup.get("failed_at")
        and float(followup.get("scheduled_at_unix", 0)) <= now
    )


def _recontact_due(session: dict, now: float) -> bool:
    recontact = session.get("recontact") or {}
    return bool(
        recontact.get("kind") == "recontact_18h"
        and recontact.get("scheduled_at_unix")
        and not recontact.get("sent")
        and not recontact.get("cancelled_at")
        and not recontact.get("failed_at")
        and float(recontact.get("scheduled_at_unix", 0)) <= now
    )


def _lead_replied_after_initial(session: dict) -> bool:
    return sum(1 for m in session.get("messages", []) if m.get("sender") == "lead") > 1


def _recontact_message(now: float) -> str:
    hour = datetime.fromtimestamp(now, tz=JAKARTA_TZ).hour
    greeting = "Selamat pagi" if hour < 11 else "Selamat siang" if hour < 15 else "Selamat sore" if hour < 18 else "Selamat malam"
    return (
        f"{greeting}, Bapak/Ibu. Tiara ingin memastikan, apakah Bapak/Ibu sudah dihubungi oleh tim marketing kami dan sudah mendapatkan informasi yang dibutuhkan?\n\n"
        "Kalau belum, Tiara bisa bantu jawab pertanyaan di sini atau Tiara mintakan tim marketing untuk menghubungi Bapak/Ibu kembali."
    )


async def followup_loop(app: web.Application) -> None:
    """Run a 3m30 value follow-up and one separate 18h recontact per lead."""
    log.info("followup loop starting short_delay=%ss recontact_delay=%ss scan=%ss", FOLLOWUP_DELAY_SECONDS, RECONTACT_DELAY_SECONDS, FOLLOWUP_SCAN_INTERVAL)
    try:
        while True:
            now = time.time()
            for path in SESSIONS_DIR.glob("*.json"):
                try:
                    session = json.loads(path.read_text())
                    chat_id = session.get("chat_id")
                    event_session = session.get("waha_session")

                    if _followup_due(session, now):
                        followup = session.setdefault("followup", {})
                        if _lead_replied_after_initial(session):
                            followup["cancelled_at"] = _now_iso()
                            followup["cancel_reason"] = "lead_replied"
                        else:
                            msg = random.choice(FOLLOWUP_MESSAGES)
                            ok, info = await waha_send_text(chat_id, msg, session=event_session)
                            if ok:
                                append_message(session, "bot", msg, f"followup-{uuid.uuid4().hex[:8]}")
                                followup["sent"] = True
                                followup["sent_at"] = _now_iso()
                                followup["variant_chars"] = len(msg)
                                log.info("3m30 followup sent chat=%s session=%s", chat_id, event_session)
                            else:
                                followup["failed_at"] = _now_iso()
                                followup["error"] = info[:500]

                    if _recontact_due(session, now):
                        recontact = session.setdefault("recontact", {})
                        msg = _recontact_message(now)
                        ok, info = await waha_send_text(chat_id, msg, session=event_session)
                        if ok:
                            append_message(session, "bot", msg, f"recontact-{uuid.uuid4().hex[:8]}")
                            recontact["sent"] = True
                            recontact["sent_at"] = _now_iso()
                            log.info("18h recontact sent chat=%s session=%s", chat_id, event_session)
                        else:
                            recontact["failed_at"] = _now_iso()
                            recontact["error"] = info[:500]
                    save_session(session)
                except Exception as e:
                    log.exception("followup scan error for %s: %s", path, e)
            await asyncio.sleep(FOLLOWUP_SCAN_INTERVAL)
    except asyncio.CancelledError:
        log.info("followup loop stopped")
        raise


async def start_background_tasks(app: web.Application) -> None:
    app["followup_task"] = asyncio.create_task(followup_loop(app))


async def cleanup_background_tasks(app: web.Application) -> None:
    task = app.get("followup_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# WAHA sendText
# ---------------------------------------------------------------------------


async def waha_send_text(chat_id: str, text: str, session: Optional[str] = None) -> tuple[bool, str]:
    """Send a text message via WAHA. When `session` is provided (the typical
    case — derived from the inbound webhook event), we use it directly. For
    outbound manager handoff (no inbound event), falls back to the default
    session or, if chat_id matches one of our WAHA session phone numbers, the
    session that owns that number."""
    if not session:
        session = _infer_session_for_chat(chat_id) or WAHA_DEFAULT_SESSION
    url = f"{WAHA_BASE}/api/sendText"
    body = {
        "chatId": chat_id,
        "text": text,
        "session": session,
    }
    headers = {"Content-Type": "application/json"}
    if WAHA_API_KEY:
        headers["X-Api-Key"] = WAHA_API_KEY
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                text_resp = await r.text()
                ok = 200 <= r.status < 300
                # WAHA Plus may return 200 with error in body — check for error fields
                if ok:
                    try:
                        resp_data = json.loads(text_resp)
                        # WAHA error: {error: "...", statusCode: N}
                        if resp_data.get("error") or (resp_data.get("statusCode", 200) >= 400):
                            ok = False
                            text_resp = f"HTTP {resp_data.get('statusCode', r.status)}: {resp_data.get('error', text_resp)}"
                    except (json.JSONDecodeError, ValueError):
                        pass  # plain text response — keep ok as-is
                return ok, text_resp
    except Exception as e:
        log.exception("waha_send_text error: %s", e)
        return False, str(e)


# Phone numbers owned by each WAHA session (Tiara1=6282311125399,
# Tiara2-6388=6282311156388). Populated lazily and cached for the process
# lifetime. Used so outbound messages to a session-owned chat_id (e.g. Juliana
# on 6287770111700 if she pings a specific WAHA number) go to the right one.
_SESSION_PHONE_CACHE: dict[str, str] = {}


def _session_phone(session: str) -> Optional[str]:
    """Cached phone number for a WAHA session. Fetched once per process."""
    if session in _SESSION_PHONE_CACHE:
        return _SESSION_PHONE_CACHE[session]
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{WAHA_BASE}/api/sessions/{session}",
            headers={"X-Api-Key": WAHA_API_KEY} if WAHA_API_KEY else {},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        phone = data.get("me", {}).get("id", "").split("@")[0].lstrip("+")
        if phone:
            _SESSION_PHONE_CACHE[session] = phone
            return phone
    except Exception:
        pass
    return None


def _infer_session_for_chat(chat_id: str) -> Optional[str]:
    """If `chat_id` is one of OUR WAHA session numbers, return that session.
    Otherwise None (caller should fall back to default)."""
    digits = chat_id.split("@")[0].lstrip("+")
    for sess in WAHA_SESSIONS:
        phone = _session_phone(sess)
        if phone and phone == digits:
            return sess
    return None


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------


def extract_phone_from_payload(payload: dict) -> str:
    """Fast PN extraction from WAHA payload, no session lookup."""
    raw = (
        payload.get("from")
        or payload.get("chatId")
        or (payload.get("payload", {}).get("sender", {}).get("id", "")).split("@")[0]
        or ""
    )
    # Strip WhatsApp suffix
    digits = raw.split("@")[0].lstrip("+")
    # Normalize 08xx → 62xx
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    return digits


# HARD BLOCKLIST: internal staff/owners must never enter lead handling.
# Keep these canonical PNs in code (not LLM prompt) so they cannot be misclassified.
INTERNAL_TEAM_PHONES = frozenset({MANAGER_PHONE, BOS_PHONE})


def _normalize_phone_digits(raw: str) -> str:
    """Normalize +62/62/08 variants to a canonical Indonesian PN."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    elif digits.startswith("8"):
        digits = "62" + digits
    return digits


def _is_internal_number(phone_digits: str) -> bool:
    """True only for a canonical internal owner/manager PN."""
    return _normalize_phone_digits(phone_digits) in INTERNAL_TEAM_PHONES


async def _resolve_lid_to_pn_for_internal_check(chat_id: str, event_session: str | None) -> str:
    """Resolve a WAHA LID before lead routing, only to enforce the internal blocklist.

    A staff member can arrive as ``...@lid``.  Their LID is not stable enough
    to hardcode, so resolve it through WAHA Plus before a session/reply/report
    can be created. Failure returns an empty string and normal lead handling
    continues; it never sends a WhatsApp message.
    """
    if not chat_id.endswith("@lid"):
        return ""
    lid = chat_id.split("@", 1)[0]
    session_name = event_session or WAHA_DEFAULT_SESSION
    url = f"{WAHA_BASE}/api/{session_name}/lids/{lid}"
    headers = {"X-Api-Key": WAHA_API_KEY} if WAHA_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    log.warning("internal LID lookup failed lid=%s status=%s", lid, response.status)
                    return ""
                data = json.loads(await response.text())
                return _normalize_phone_digits((data.get("pn") or "").split("@", 1)[0])
    except Exception as exc:
        log.warning("internal LID lookup failed lid=%s error=%s", lid, exc)
        return ""


async def handle_webhook(request: web.Request) -> web.Response:
    body = await request.read()
    sig = request.headers.get("X-Webhook-Hmac", "")
    alg = request.headers.get("X-Webhook-Hmac-Algorithm", "sha512").lower()
    if not verify_signature(body, sig, alg):
        log.warning("invalid signature")
        return web.json_response({"error": "invalid signature"}, status=401)

    try:
        data = json.loads(body)
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    event = data.get("event")
    if event != "message":
        # Only message events drive the bot
        return web.json_response({"status": "ignored", "event": event}, status=202)

    payload = data.get("payload") or {}
    event_session = data.get("session")  # WAHA session name, e.g. "Tiara1" or "Tiara2-6388"
    chat_id = payload.get("from") or payload.get("chatId") or ""
    if not chat_id:
        return web.json_response({"error": "no chat_id"}, status=400)

    if is_group_chat(payload):
        log.info("group message ignored: %s (session=%s)", chat_id, event_session)
        return web.json_response({"status": "ignored", "reason": "group"}, status=202)

    # Skip WAHA internal channels (status@broadcast, etc.) — they are not leads.
    if chat_id.endswith("@broadcast") or chat_id.endswith("@newsletter") or chat_id == "status@broadcast":
        log.info("broadcast/status message ignored: %s", chat_id)
        return web.json_response({"status": "ignored", "reason": "broadcast"}, status=202)

    user_text = (payload.get("body") or "").strip()
    if not user_text:
        return web.json_response({"status": "ignored", "reason": "empty"}, status=202)

    # Resolve LID before any lead/session action: both internal staff may appear
    # as @lid in WhatsApp Business events.  The resolved PN is the source of
    # truth for the hard blocklist, not the opaque LID value.
    sender_phone = _normalize_phone_digits(extract_phone_from_payload(payload))
    resolved_lid_phone = await _resolve_lid_to_pn_for_internal_check(chat_id, event_session)
    effective_phone = resolved_lid_phone or sender_phone

    # ------------------------------------------------------------------
    # Internal branches: messages from Juliana (manager) or Vemby (owner)
    # are NEVER leads. Never create a lead session, CS reply, follow-up, or
    # manager report for these PNs — including when the incoming ID is a LID.
    # ------------------------------------------------------------------
    is_manager_chat = effective_phone == MANAGER_PHONE
    is_bos_chat = effective_phone == BOS_PHONE
    if effective_phone in INTERNAL_TEAM_PHONES:
        log.info("internal sender blocked from lead flow pn=%s chat=%s", effective_phone, chat_id)

    if is_bos_chat:
        log.info("bos inbound from %s (session=%s): %s", chat_id, event_session, user_text[:80])
        reply_text = _manager_lookup_reply(user_text) or ""  # same lookup, status-style queries
        if not reply_text:
            try:
                reply_text = await _generate_bos_reply(user_text)
            except Exception as e:
                log.exception("bos reply error: %s", e)
                reply_text = f"Siap Bos, dicatat 🙏"
        if not reply_text.strip():
            reply_text = "Siap Bos 🙏"
        ok, info = await waha_send_text(chat_id, reply_text, session=event_session)
        log.info("bos reply chat=%s ok=%s info_len=%s session=%s", chat_id, ok, len(info), event_session)
        return web.json_response(
            {"status": "ok", "bos_reply": ok, "reply_chars": len(reply_text), "session": event_session},
            status=200,
        )

    if is_manager_chat:
        log.info("manager inbound from %s (session=%s): %s", chat_id, event_session, user_text[:80])
        # Cheap reply first if it looks like a status query — avoids a 5-13s LLM round-trip.
        reply_text = _manager_lookup_reply(user_text) or ""
        if not reply_text:
            try:
                reply_text = await _generate_manager_reply(user_text)
            except Exception as e:
                log.exception("manager reply error: %s", e)
                reply_text = f"Siap Bu, dicatat 🙏"
        if not reply_text.strip():
            reply_text = "Siap Bu 🙏"
        ok, info = await waha_send_text(chat_id, reply_text, session=event_session)
        log.info("manager reply chat=%s ok=%s info_len=%s session=%s", chat_id, ok, len(info), event_session)
        return web.json_response(
            {"status": "ok", "manager_reply": ok, "reply_chars": len(reply_text), "session": event_session},
            status=200,
        )

    # ------------------------------------------------------------------
    # Lead branch (default)
    # ------------------------------------------------------------------
    session = load_session(chat_id)
    first_message = len(session["messages"]) == 0
    extract_lead_info(payload, session, chat_id)
    # Stamp which WAHA session this lead arrived through so the summarizer
    # can tell Juliana (and we can debug routing) per-lead.
    if event_session:
        session["waha_session"] = event_session
    append_message(session, "lead", user_text, payload.get("id", ""))

    # Schedule the Juliana handoff only on the first message. The first lead
    # message also gets a value follow-up in 3m30. Separately, every latest
    # lead message moves one 18h recontact window forward until it is sent.
    if first_message:
        schedule_at = time.time() + SUMMARY_DELAY_SECONDS
        session["summary"]["scheduled_at"] = datetime.fromtimestamp(
            schedule_at, tz=timezone.utc
        ).isoformat()
        session["summary"]["scheduled_at_unix"] = schedule_at

        followup_at = time.time() + FOLLOWUP_DELAY_SECONDS
        session["followup"] = {
            "kind": "value_followup_3m30",
            "scheduled_at": datetime.fromtimestamp(followup_at, tz=timezone.utc).isoformat(),
            "scheduled_at_unix": followup_at,
            "sent": False,
        }
    else:
        followup = session.get("followup") or {}
        if not followup.get("sent") and not followup.get("cancelled_at"):
            followup["cancelled_at"] = _now_iso()
            followup["cancel_reason"] = "lead_replied"
            session["followup"] = followup

    recontact = session.get("recontact") or {}
    if not recontact.get("sent"):
        recontact_at = time.time() + RECONTACT_DELAY_SECONDS
        session["recontact"] = {
            "kind": "recontact_18h",
            "scheduled_at": datetime.fromtimestamp(recontact_at, tz=timezone.utc).isoformat(),
            "scheduled_at_unix": recontact_at,
            "sent": False,
        }

    save_session(session)

    # Generate CS reply
    kb = load_kb()
    try:
        reply_text = await generate_reply(session, user_text, kb, first_message)
    except Exception as e:
        log.exception("generate_reply error: %s", e)
        reply_text = _human_handoff_reply()
    reply_text = finalize_lead_reply(reply_text, user_text)

    # Persist bot reply
    append_message(session, "bot", reply_text, f"bot-{uuid.uuid4().hex[:8]}")
    save_session(session)

    # Drop audit line
    try:
        (INBOX_DIR / f"{int(time.time())}_{session['lead']['phone']}.json").write_text(
            json.dumps(
                {
                    "received_at": _now_iso(),
                    "chat_id": chat_id,
                    "waha_session": event_session,
                    "lead": session["lead"],
                    "payload_name_probe": {
                        "sender": payload.get("sender") if isinstance(payload.get("sender"), dict) else {},
                        "name": payload.get("name") or "",
                        "notifyName": payload.get("notifyName") or "",
                        "pushName": payload.get("pushName") or payload.get("pushname") or "",
                        "verifiedBizName": payload.get("verifiedBizName") or "",
                        "formattedName": payload.get("formattedName") or "",
                        "_data": (payload.get("_data") if isinstance(payload.get("_data"), dict) else {}),
                    },
                    "user_text": user_text,
                    "bot_reply": reply_text,
                    "summary": session["summary"],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    except Exception:
        pass

    # Send back to lead via WAHA — use the same session the inbound event came from
    ok, info = await waha_send_text(chat_id, reply_text, session=event_session)
    log.info(
        "reply chat=%s ok=%s info_len=%s msgs=%d session=%s",
        chat_id, ok, len(info), len(session["messages"]) // 2, event_session,
    )

    return web.json_response(
        {
            "status": "ok",
            "replied": ok,
            "reply_chars": len(reply_text),
            "summarize_at": session["summary"].get("scheduled_at"),
            "session": event_session,
        },
        status=200,
    )


async def healthz(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "mutiara-bot"})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/webhook/mutiara", handle_webhook)
    app.router.add_get("/healthz", healthz)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("MUTIARA_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MUTIARA_PORT", "8645")))
    args = parser.parse_args()
    log.info("Mutiara Bogor bot starting on %s:%s", args.host, args.port)
    web.run_app(make_app(), host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
