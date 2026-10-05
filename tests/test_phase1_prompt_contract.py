from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROMPT = (ROOT / "cs_prompt.md").read_text()


def test_prompt_defines_intent_sensitive_funnel():
    assert "Kualifikasi dan arah percakapan" in PROMPT
    assert "maksimal satu pertanyaan" in PROMPT
    assert "jangan memaksa visit" in PROMPT
    assert "Jawab pertanyaan spesifik yang ditanyakan terlebih dahulu" in PROMPT


def test_runtime_instruction_defines_asset_first_and_visit_choices():
    from bot import _format_hermes_prompt

    session = {"messages": []}
    kb = {"project_name": "The Mutiara Bogor", "manifest_present": True, "documents": [], "links": []}
    prompt = _format_hermes_prompt(session, "Bisa lihat show unit?", kb)

    assert "intent-sensitive" in prompt
    assert "asset terlebih dahulu" in prompt
    assert "pilihan konkret weekday/weekend" in prompt
    assert "Jangan menanyakan semuanya sekaligus" in prompt


def test_location_backstop_keeps_exact_maps_link_and_approved_visit_term():
    from bot import MAPS_LINK, _ensure_location_reply

    reply = _ensure_location_reply("Baik, saya bantu.", "Lokasinya di mana?")

    assert MAPS_LINK in reply
    assert "show unit" in reply.lower() or "marketing gallery" in reply.lower()
    assert "showroom" not in reply.lower()


def test_non_location_reply_is_not_forced_to_visit_by_backstop():
    from bot import _ensure_location_reply

    reply = _ensure_location_reply("Baik, Birch mulai Rp1,897 M.", "Berapa harga Birch?")

    assert "show unit" not in reply.lower()
    assert "marketing gallery" not in reply.lower()
