# The Mutiara Bogor — Sales Agent Backend

This directory hosts the WhatsApp sales pipeline that fronts the WAHA `Tiara1`
session attached to `+62 823-1112-5399`. The pipeline:

1. Receives inbound WhatsApp messages via WAHA webhook (HMAC verified)
2. Parses lead contact data (name, pushName, phone)
3. Replies in real time as a polite Indonesian sales CS using the project
   knowledge base at `~/mutiara/knowledge/`
4. Persists the conversation to `~/mutiara/sessions/<chat_id>.json`
5. Schedules a 5-minute summary-and-handoff task per lead that:
   - Sends a concise summary + warm/cold/no-respond classification to
     Juliana (`+62 877-7011-1700`, WhatsApp via WAHA)
   - Marks the lead as handled in the sessions store

## Files

- `bot.py`                 — aiohttp webhook server (port 8645) handling inbound messages
- `summarizer.py`          — 5-minute scheduler + summary dispatcher
- `cs_prompt.md`           — system prompt for the sales CS agent personality
- `knowledge/`             — your project KB (PDF/text/links) — populate this
- `inbox/`                 — dropped/parsed leads audit log (auto-written)
- `sessions/`              — live conversation state per chat_id (JSON)
- `logs/`                  — runtime logs

## Run

    cd ~/mutiara
    python3 bot.py &             # port 8645, the WAHA webhook target
    python3 summarizer.py &      # background scheduler

WAHA webhook URL: `http://192.168.50.25:8645/webhook/mutiara`

## Knowledge Base

Drop files in `~/mutiara/knowledge/` (PDF, .txt, .md, etc.) plus a manifest
file `manifest.json` describing them. The agent reads the manifest first then
loads files referenced by it.

Example manifest:

```json
{
  "project_name": "The Mutiara Bogor",
  "tagline": "...",
  "documents": [
    {"path": "brosur.pdf", "purpose": "main brochure"},
    {"path": "price-list.txt", "purpose": "unit pricing"}
  ],
  "links": [
    {"url": "https://...", "purpose": "showroom virtual tour"}
  ]
}
```

Until populated, the bot uses a placeholder greeting and asks clarifying
questions to qualify the lead.
