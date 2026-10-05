# Mutiara Conversation Behavior Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Tiara answer the lead's immediate question first, qualify with one useful question, and guide high-intent property prospects toward a concrete show-unit visit without over-selling or repeating catalog text.

**Architecture:** Keep the current LLM shell-out and knowledge-base loading unchanged. Improve the behavior contract in `cs_prompt.md` and the runtime reply instruction in `bot.py` so both the system prompt and per-request prompt enforce the same conversation stages. Add deterministic reply guardrails only where they preserve direct-answer, location-link, and visit behavior; leave lead classification and manager status tracking for Phase 2.

**Tech Stack:** Python 3.12, aiohttp service, Markdown prompt, pytest/unittest-compatible local tests.

**Spec:** Approved chat design on 2026-09-19; Phase 1 only.

## Global Constraints

- Do not change WAHA routing, credentials, internal-number blocklists, or LLM provider chain.
- Do not invent prices, promotions, stock, schedules, or legal details.
- Keep `show unit` and `marketing gallery`; do not use `showroom`.
- Maximum one question at the end of a customer reply.
- Price/payment requests must receive a direct answer before qualification.
- Location/Maps requests must retain the exact approved Google Maps URL.
- Vendor/spam classification is deferred to Phase 2; Phase 1 must not claim to solve it.

---

### Task 1: Update the conversation behavior contract

**Files:**
- Modify: `cs_prompt.md`
- Test: `tests/test_phase1_prompt_contract.py`

**Interfaces:**
- Produces the customer-facing behavior contract consumed by `bot.py` and the LLM.

- [ ] Add explicit stages: direct answer, one relevant value, one qualification question, and visit invitation only when the lead has a matching intent signal.
- [ ] Add intent-specific rules for price/payment, location, photo/video, and show-unit requests.
- [ ] Add visit micro-commitment rules: offer two concrete time windows when the lead already asks to visit; do not end with vague “kapan-kapan”.
- [ ] Add a compact list of questions the bot may ask over multiple turns: intended use, preferred type/room count, budget/payment, timeline, location/origin, and visit timing. Never ask all at once.
- [ ] Remove or rewrite prompt examples that imply a visit should be offered after every generic information request.
- [ ] Keep the current factual guardrails and asset links intact.
- [ ] Add tests that assert the prompt contains the new rules and forbidden vague-visit language is absent from active examples.

### Task 2: Align runtime prompt instructions

**Files:**
- Modify: `bot.py:_format_hermes_prompt`
- Test: `tests/test_phase1_prompt_contract.py`

**Interfaces:**
- Consumes: existing `session`, `user_text`, and `kb` arguments.
- Produces: the same list of system/user messages with stronger per-request behavior instructions.

- [ ] Replace the generic “offer detail/simulation/visit” instruction with intent-sensitive guidance: answer first, ask one qualification question, and use a visit CTA only for location, media, show-unit, or qualified buying signals.
- [ ] Require an actual next step for explicit visit intent: ask for weekday/weekend or offer two time options.
- [ ] Require the bot to acknowledge a media request before recommending a visit; if the asset is absent from KB, use honest handoff language.
- [ ] Preserve first-message identity, exact Maps URL, KB-only facts, no metadata, and no repeated catalog dump.
- [ ] Add unit tests by calling `_format_hermes_prompt` with representative latest messages and asserting the generated instruction includes the relevant behavior branch.

### Task 3: Add regression tests for factual and formatting guardrails

**Files:**
- Test: `tests/test_phase1_reply_guardrails.py`
- Modify only if needed: `bot.py:_ensure_location_reply`

**Interfaces:**
- Consumes: existing `_ensure_location_reply` behavior.
- Produces: regression coverage for direct location handling and approved visit wording.

- [ ] Test that a location request adds the exact Maps URL when missing.
- [ ] Test that a location request uses `show unit` or `marketing gallery`, never `showroom`.
- [ ] Test that unrelated messages are not automatically appended with a visit CTA by `_ensure_location_reply`.
- [ ] Run the focused tests and then the available project test suite.

### Task 4: Verify and report

**Files:**
- No production file changes beyond Tasks 1–3.

- [ ] Run syntax checks for `bot.py`.
- [ ] Run focused Phase 1 tests.
- [ ] Exercise representative prompt/reply cases offline: price, KPR, location, video request, explicit show-unit request, generic greeting, and unknown detail.
- [ ] Confirm no WAHA/API side effects are triggered by tests.
- [ ] Report changed files, test output, and remaining Phase 2 work.
