# Solution Overview


## Architecture

```
Caller (phone/WebRTC)
        │
        ▼
  Pipecat Pipeline
  ┌─────────────────────────────────────────┐
  │  ElevenLabs STT                         │
  │       ↓                                 │
  │  LLM Context Aggregator (user)          │
  │       ↓                                 │
  │  OpenAI GPT (function calling)          │
  │       ↓          ↘                      │
  │  LLM Context    Tool Handlers           │
  │  Aggregator      ↓         ↓            │
  │  (assistant)  find_    create_          │
  │               patient  appointment      │
  │       ↓          ↘         ↙            │
  │  ElevenLabs TTS   healthie.py           │
  └─────────────────────────────────────────┘
        │                  │
        ▼                  ▼
  Caller hears       Playwright scrapes
  response           Healthie UI
```

### Key components

| Component | Choice | Why |
|-----------|--------|-----|
| Voice pipeline | Pipecat | Handles STT→LLM→TTS orchestration, VAD, turn detection |
| STT | ElevenLabs Realtime | Low-latency streaming transcription |
| LLM | OpenAI (GPT-4o) | Reliable function calling, handles ambiguous speech well |
| TTS | ElevenLabs | Natural-sounding voice, low latency |
| Turn detection | LocalSmartTurnAnalyzerV3 + SileroVAD | Avoids cutting the caller off mid-sentence |
| EHR integration | Playwright (browser automation) | Healthie has no public API — scraping the UI was the only option |

---

## Conversation Flow

The system prompt drives a strict, linear flow:

1. **Greet** — introduce the bot as a Prosper Health assistant
2. **Collect name** — ask for full name; ask to repeat/spell if unclear
3. **Collect DOB** — ask for date of birth; confirm it back before proceeding
4. **Look up patient** — call `find_patient`; handle `no_results_for_name`, `dob_mismatch`, and `system_error` with appropriate retry logic
5. **Collect appointment date** — ask preferred date
6. **Collect appointment time** — ask preferred time
7. **Book appointment** — call `create_appointment`; handle `unavailable_time_slot` and `system_error`
8. **Confirm** — read back the confirmed appointment details

The prompt enforces one question at a time and instructs the LLM never to surface internal error codes or patient IDs to the caller.

---

## Healthie Integration — Decisions & Trade-offs

### Why Playwright (browser scraping)?

Healthie does not expose a public REST or GraphQL API for the operations needed (patient search, appointment creation). The only available surface was the web UI. Playwright with `playwright-stealth` was chosen to:

- Automate login and navigate the authenticated session
- Search patients via the existing header search bar
- Create appointments through the "Add Appointment" modal

**Trade-off:** This is inherently brittle. Any UI change in Healthie (CSS class rename, DOM restructure, selector change) can break the integration silently. A proper API or webhook integration would be far more robust.

### Session management

The current implementation calls `login_to_healthie()` on every tool invocation, which means a full browser launch and login sequence (~10–20 seconds) per tool call. This is the most significant latency issue in the system.

The fix is straightforward — restore the session reuse guard that was present in the earlier commented-out version:

```python
if _page is not None:
    return _page
```

This was intentionally left as a documented improvement rather than silently patched, to keep the code honest about its current state.

---

## Known Issues & Documented Improvements

### 1. Session reuse (critical latency)
As above — login on every call adds 10–20s of dead air per tool invocation. Fix: cache the browser session and only re-login on session expiry or page error.

### 2. Schema mismatch between handler and system prompt
`handle_find_patient` in `bot.py` returns `{"status": "found", ...}` but the system prompt instructs the LLM to check `result.success` (boolean). This means the LLM's conditional logic may not behave as written. Fix: align the callback payload to `{"success": true, "patient": ..., "reason": null}`.

### 3. Silent appointment type selection
`create_appointment` always picks the first option in the appointment type dropdown. If the clinic has multiple types (new patient, follow-up, telehealth), wrong type may be booked silently. Fix: either pass appointment type as a parameter or assert the expected type name.

### 4. No session reset on crash
If Playwright throws an unhandled exception, `_page` remains set to a broken object. The next call will attempt to reuse it and also fail. Fix: reset `_browser`, `_page`, and `_playwright_ctx` to `None` inside the `except` block.

### 5. No retry cap for identity resolution loops
The prompt retries on `no_results_for_name` and `dob_mismatch` but sets no hard cap. A confused caller could loop indefinitely. Fix: after 2 failed attempts, escalate to "please contact the clinic directly."

---

## Latency

The dominant latency sources, ranked:

| Source | Estimate | Mitigation |
|--------|----------|------------|
| Playwright login (per call) | 10–20s | Session reuse (see above) |
| Playwright page interaction | 3–8s | Unavoidable given scraping approach |
| OpenAI LLM response | 0.5–2s | Streaming already enabled via Pipecat |
| ElevenLabs STT | ~200ms | Realtime streaming |
| ElevenLabs TTS | ~300ms | Streaming |

Once session reuse is implemented, the tool call overhead drops to 3–8s (page navigation + DOM interaction), which is acceptable for a phone conversation where the bot says "just a moment while I pull up your profile."

For further latency reduction, the ideal path is replacing Playwright entirely with a native Healthie API or webhooks, which would bring tool call latency under 500ms.

---

## Reliability

Current reliability risks and mitigations:

**Healthie UI changes** — No mitigation currently. Selectors are hardcoded. Mitigation: add a CI smoke test that runs `find_patient` and `create_appointment` against a test patient daily.

**OpenAI unavailability** — No fallback LLM configured. Mitigation: add a fallback to Anthropic Claude or a local model (e.g. via Ollama) in the `OpenAILLMService` config.

**ElevenLabs unavailability** — No STT/TTS fallback. Mitigation: Pipecat supports Deepgram for STT and Cartesia for TTS as drop-in alternatives.

**Browser process crash** — Playwright can crash under memory pressure or after extended uptime. Mitigation: implement the session reset on exception (issue #4 above) and add a watchdog that re-initializes the browser if `_page.is_closed()`.

---

## Evaluation

To verify the agent behaves correctly, three levels of testing are useful:

### Unit tests — `healthie.py`
Test `find_patient` and `create_appointment` in isolation against a Healthie staging environment (or a mock using `pytest-playwright`). Assert correct return shapes for all outcome paths: `success=True`, `no_results_for_name`, `dob_mismatch`, `system_error`, `unavailable_time_slot`.

### Conversation flow tests
Use Pipecat's test runner or a mock transport to inject canned caller utterances and assert the LLM calls the right tool with the right arguments. Key scenarios:

- Happy path: valid name → valid DOB → available slot → confirmed
- Name not found: retry once, then escalate
- DOB mismatch: re-ask DOB, retry
- Slot unavailable: offer alternative time
- System error: retry once, then escalate

### End-to-end call simulation
Record a set of synthetic phone calls (using ElevenLabs or a TTS script to generate caller audio) and replay them through the full pipeline. Evaluate:
- Was the appointment booked in Healthie? (check via UI or API)
- Did the bot confirm the correct details?
- Was total call duration within acceptable bounds (target: <3 minutes for a standard booking)?

A simple evaluation dashboard could track these metrics per call using the `enable_metrics=True` flag already set in `PipelineTask`.