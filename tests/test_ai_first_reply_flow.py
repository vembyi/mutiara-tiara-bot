from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROMPT = (ROOT / "cs_prompt.md").read_text()


def test_ai_reply_for_price_is_not_replaced_by_a_catalogue_template():
    from bot import finalize_lead_reply

    ai_reply = "Untuk tenor 15 tahun, estimasi cicilan Cedar standar Rp29.700.733/bulan; angka final mengikuti unit dan approval KPR."

    assert finalize_lead_reply(ai_reply, "Cicilan Cedar 15 tahun berapa?") == ai_reply


def test_ai_reply_for_video_request_is_not_replaced_by_a_visit_template():
    from bot import finalize_lead_reply

    ai_reply = "Untuk video Cedar 10, Tiara teruskan ke tim marketing agar dikirimkan bila asetnya tersedia."

    assert finalize_lead_reply(ai_reply, "Bisa kirim vidio Cedar 10?") == ai_reply


def test_video_request_gets_honest_asset_handoff_when_ai_skips_it():
    from bot import finalize_lead_reply

    reply = finalize_lead_reply("Apakah Bapak/Ibu tertarik untuk visit?", "Bisa kirim vidio Cedar 10?")

    assert "permintaan video" in reply.lower()
    assert "tim marketing" in reply.lower()


def test_identity_question_gets_tiara_identity_when_ai_skips_it():
    from bot import finalize_lead_reply

    reply = finalize_lead_reply("Proyek kami berada di Bogor Selatan.", "Ini siapa ya?")

    assert "Saya Tiara, Admin CS The Mutiara Bogor." in reply


def test_cedar_15_year_kpr_question_gets_grounded_standard_reference_when_ai_skips_numbers():
    from bot import finalize_lead_reply

    reply = finalize_lead_reply(
        "Cedar 10 cocok untuk keluarga yang membutuhkan ruang lebih besar.",
        "Cicilan Cedar 15 tahun berapa dan DP-nya berapa?",
    )

    assert "Rp29.700.733/bulan" in reply
    assert "Rp409.573.791" in reply
    assert "Cedar 10 standar" in reply


def test_abbreviated_location_question_gets_maps_backstop():
    from bot import MAPS_LINK, _ensure_location_reply

    reply = _ensure_location_reply("Baik, Tiara bantu jelaskan.", "Dmn ini?")

    assert MAPS_LINK in reply
    assert "Bogor Selatan" in reply
    assert "showroom" not in reply.lower()


def test_region_location_question_gets_maps_backstop():
    from bot import MAPS_LINK, _ensure_location_reply

    reply = _ensure_location_reply("Baik, Tiara bantu jelaskan.", "Daerah mana persisnya Mutiara di Bogor ini?")

    assert MAPS_LINK in reply
    assert "Bogor Selatan" in reply


def test_negative_visit_intent_does_not_get_forced_visit_cta():
    from bot import MAPS_LINK, finalize_lead_reply

    reply = finalize_lead_reply("Lokasinya di Bogor Selatan.", "Saya cuma minta Maps, belum mau visit.")

    assert MAPS_LINK in reply
    assert "Tiara bisa bantu aturkan visit" not in reply


def test_persona_prompt_does_not_embed_copyable_reply_templates():
    assert "## Initial Greeting" not in PROMPT
    assert "Variant Price" not in PROMPT
    assert "Variant Location" not in PROMPT


def test_persona_prompt_requires_asset_request_to_be_answered_before_visit():
    assert "Jawab permintaan asset terlebih dahulu" in PROMPT
    assert "jangan berpura-pura aset sudah dikirim" in PROMPT.lower()


def test_persona_prompt_requires_marketing_contact_commitment_for_unanswerable_questions():
    assert "akan segera menghubungi" in PROMPT.lower()
    assert "menjawab pertanyaan lebih detail" in PROMPT.lower()


def test_human_handoff_promises_marketing_will_contact_for_more_detail():
    from bot import _human_handoff_reply

    reply = _human_handoff_reply()

    assert "tim marketing" in reply.lower()
    assert "akan segera menghubungi" in reply.lower()
    assert "lebih detail" in reply.lower()


def test_complete_ai_answer_is_not_given_a_marketing_handoff():
    from bot import finalize_lead_reply

    ai_reply = "The Mutiara Bogor berada di Bogor Selatan, sekitar 100 meter dari Exit Tol Jagorawi KM 42."

    reply = finalize_lead_reply(ai_reply, "Lokasinya di mana?")

    assert "akan segera menghubungi" not in reply.lower()
