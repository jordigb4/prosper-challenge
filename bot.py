#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Pipecat Quickstart Example.

The example runs a simple voice AI bot that you can connect to using your
browser and speak with it. You can also deploy this bot to Pipecat Cloud.

Required AI services:
- ElevenLabs (Speech-to-Text and Text-to-Speech)
- OpenAI (LLM)

Run the bot using::

    uv run bot.py
"""

import os

from dotenv import load_dotenv
import json
from loguru import logger
from datetime import datetime, timezone

print("🚀 Starting Pipecat bot...")
print("⏳ Loading models and imports (20 seconds, first run only)\n")

logger.info("Loading Local Smart Turn Analyzer V3...")
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

logger.info("✅ Local Smart Turn Analyzer V3 loaded")
logger.info("Loading Silero VAD model...")
from pipecat.audio.vad.silero import SileroVADAnalyzer

logger.info("✅ Silero VAD model loaded")

from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMRunFrame

logger.info("Loading pipeline components...")
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.turns.user_turn_strategies import UserTurnStrategies

logger.info("✅ All components loaded successfully!")

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Healthie tool definitions
# ---------------------------------------------------------------------------

find_patient_function = FunctionSchema(
    name = 'find_patient',
    description="Search Healthie for a patient by their full name and date of birth. Call this once the caller has provided both pieces of information. Returns patient details on success, or an informative error message.",
    properties={
                    "name": {
                        "type": "string",
                        "description": "Patient's full name, e.g. 'Jane Smith Smith'",
                    },
                    "date_of_birth": {
                        "type": "string",
                        "description": "Patient's date of birth in YYYY-MM-DD format",
                    },
    },
    required=["name","date_of_birth"]
)

create_appointment_function = FunctionSchema(
    name = 'create_appointment',
    description=  "Book an appointment in Healthie for a verified patient. Call this once you have the patient_id, desired date and time. Returns appointment details on success.",
    properties={
                    "patient_id": {
                        "type": "string",
                        "description": "The Healthie patient ID returned by find_patient",
                    },
                    "date": {
                        "type": "string",
                        "description": "Appointment date in YYYY-MM-DD format",
                    },
                    "time": {
                        "type": "string",
                        "description": "Appointment time in HH:MM (24-hour) format",
                    },
    },
    required=["patient_id", "date", "time"]
)

tools = ToolsSchema(standard_tools=[find_patient_function,create_appointment_function],)
# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
def _build_system_prompt() -> str:
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%A, %B %-d, %Y")          
    earliest_hour = (now.hour + 1) % 24                  
    return f"""Today is {today_str} (UTC). The current time is {now.strftime("%H:%M")} UTC.

""" + _SYSTEM_PROMPT_BODY


_SYSTEM_PROMPT_BODY = """
You are a friendly and professional appointment scheduling assistant at Prosper Health clinic.
You are speaking with patients on a live phone call.
Keep responses short, natural, and easy to understand.
Your goal is to identify the patient and schedule an appointment.

## IDENTITY VERIFICATION

STEP 1 — Ask for the caller's full name.
- If the name is unclear or may have been misheard, politely ask them to repeat or spell it.
  Examples:
  - "Could you please repeat your name?"
  - "Could you spell your last name for me?"
- Do not proceed until you are confident you heard the name correctly.

STEP 2 — Ask for their date of birth.
  Example: "Could you tell me your date of birth?"
- If the date is unclear, ask them to repeat it.
- Always repeat the date back to confirm before proceeding.
  Example: "Just to confirm, your date of birth is March 15th, 1985 — is that right?"
- Convert the confirmed date to YYYY-MM-DD format before calling any function.

## PATIENT LOOKUP

Tell the caller you're pulling up their profile before calling the function.
  Example: "Just a moment while I pull up your profile."

STEP 3 — Call `find_patient` with:
  • name
  • date_of_birth

The function returns:
  • success (boolean)
  • patient (object or null)
  • reason (string or null)

Handle the result as follows:

  IF success = true
    → Confirm the match and proceed to scheduling.
      Example: "Great, I found your profile. Let's get your appointment scheduled."

  IF success = false
    → Check the reason field and respond once, then retry:

    reason = "no_results_for_name"
      → Ask the caller to confirm or spell their name.

    reason = "dob_mismatch"
      → Explain that the date of birth didn't match what's on file.
      → Ask the caller to confirm their date of birth.

    reason = "system_error"
      → Apologize briefly.
      → Retry immediately with the same details, without asking the caller anything.

## PATIENT LOOKUP RETRY LIMIT
You may call `find_patient` at most 3 times total.
Every call counts toward this limit, regardless of the failure reason.
After 3 failed attempts, do not retry. Say:
"I'm sorry, I wasn't able to locate your account. Please contact the clinic directly and they'll be happy to help."
Then end the call politely.

## APPOINTMENT SCHEDULING

When a caller gives a date without a year, assume the year is {now.year}.

STEP 4 — Ask what date they would like their appointment.
STEP 5 — Ask what time they prefer.

Appointments must be scheduled for today or a future date.
If the requested date is today, the time must be {earliest_hour:02d}:00 or later (UTC).
If either constraint is violated, politely tell the caller the requested date or time is in the past and ask them to choose another.

Convert before calling functions:
  • Dates → YYYY-MM-DD       (e.g. "March 15th" → "2026-03-15")
  • Times → HH:MM 24-hour    (e.g. "2 PM" → "14:00")

Tell the caller you're setting up their appointment before calling the function.
  Example: "Just a moment while I set up your appointment."

STEP 6 — Call `create_appointment` with:
  • patient_id
  • date
  • time

The function returns:
  • success (boolean)
  • appointment (object or null) — contains patient_id, date, and time if successful
  • reason (string or null)

Handle the result as follows:

  IF success = true
    → Confirm the appointment using the details in the returned appointment object.
      Example: "You're all set! Your appointment is confirmed for [date] at [time]."

  IF success = false
    → Check the reason field and respond once, then retry:

    reason = "invalid_date"
      → Tell the caller the requested date or time is in the past.
      → Ask them to choose a future date or time.
        Example: "That date and time have already passed. Could you give me a future date or time?"

    reason = "unavailable_time_slot"
      → Let the caller know that slot isn't available.
      → Ask if they'd like to try a different time or date.
        Example: "It looks like that time slot isn't available. Would you like to try a different time or another day?"

    reason = "system_error"
      → Apologize briefly.
      → Retry immediately with the same details, without asking the caller anything.

## APPOINTMENT SCHEDULING RETRY LIMIT
You may call `create_appointment` at most 3 times total.
Every call counts toward this limit, regardless of the failure reason.
After 3 failed attempts, do not retry. Say:
"I'm sorry, I'm having trouble completing the booking right now. Please contact the clinic directly and they'll get you scheduled."
Then end the call politely.

## CONVERSATION GUIDELINES

- Keep sentences short and natural — this is a phone call.
- Ask only one question at a time.
- Always confirm key details (name, date of birth, appointment date and time) before calling functions.
- Never mention internal system details, error codes, or technical reasons for failures.
- Never reveal patient IDs or raw API responses.
- If you didn't catch something, ask the caller to repeat it.
"""


# ---------------------------------------------------------------------------
# Function call handlers
# ---------------------------------------------------------------------------

async def handle_find_patient(
    function_name: str,
    tool_call_id: str,
    args: dict,
    llm,
    context: LLMContext,
    result_callback,
):
    """Bridge between the LLM function call and healthie.find_patient."""
    from healthie import find_patient 
    name = args.get("name", "")
    dob = args.get("date_of_birth", "")
    logger.info(f"[tool] find_patient name={name!r} dob={dob!r}")

    try:
        result = await find_patient(name=name, date_of_birth=dob)
        logger.info(f"[tool] find_patient result: {result}")
        await result_callback(json.dumps(result))  # always call
    except Exception as exc:
        logger.exception(f"[tool] find_patient error: {exc}")
        await result_callback(json.dumps({"success": False, "reason": "system_error"}))

async def handle_create_appointment(
    function_name: str,
    tool_call_id: str,
    args: dict,
    llm,
    context: LLMContext,
    result_callback,
):
    """Bridge between the LLM function call and healthie.create_appointment."""
    from healthie import create_appointment  

    patient_id = args.get("patient_id", "")
    date = args.get("date", "")
    time = args.get("time", "")
    logger.info(f"[tool] create_appointment patient_id={patient_id!r} date={date!r} time={time!r}")

    try:
        appt_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        now_floored = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        if appt_dt < now_floored:
            logger.warning(f"[tool] create_appointment rejected: {appt_dt} is in the past")
            await result_callback(json.dumps({"success": False, "appointment": None, "reason": "invalid_date"}))
            return
    except ValueError:
        pass  # malformed input - system_error later


    try:
        result = await create_appointment(patient_id=patient_id, date=date, time=time)
        logger.info(f"[tool] create_appointment result: {result}")
        await result_callback(json.dumps(result))  
    except Exception as exc:
        logger.exception(f"[tool] create_appointment error: {exc}")
        await result_callback(json.dumps({"success": False, "reason": "system_error"}))

# ---------------------------------------------------------------------------
# Main bot entrypoint
# ---------------------------------------------------------------------------

async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    logger.info(f"Starting bot")

    elevenlabs_key = os.environ["ELEVENLABS_API_KEY"]
    stt = ElevenLabsRealtimeSTTService(api_key=elevenlabs_key)
    tts = ElevenLabsTTSService(
        api_key=elevenlabs_key,
        voice_id="SAz9YHcvj6GT2YYXdXww",
    )

    llm = OpenAILLMService(api_key=os.environ["OPENAI_API_KEY"])

    # Register function call handlers
    llm.register_function("find_patient", handle_find_patient,cancel_on_interruption=False)
    llm.register_function("create_appointment", handle_create_appointment,cancel_on_interruption=False)

    messages = [
        {"role":"system","content":_build_system_prompt()}
    ]

    context = LLMContext(messages, tools=tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())]
            ),
        ),
    )

    rtvi = RTVIProcessor()

    pipeline = Pipeline(
        [
            transport.input(),  # Transport user input
            rtvi,  # RTVI processor
            stt,
            user_aggregator,  # User responses
            llm,  # LLM
            tts,  # TTS
            transport.output(),  # Transport bot output
            assistant_aggregator,  # Assistant spoken responses
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[RTVIObserver(rtvi)],
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Client connected")
        # Kick off the conversation.
        messages.append({"role": "system", "content": "Say hello and briefly introduce yourself as a digital assistant from the Prosper Health clinic, then begin STEP 1."})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"Client disconnected")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)

    await runner.run(task)


async def bot(runner_args: RunnerArguments):
    """Main bot entry point for the bot starter."""

    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.8)),
        ),
    }

    transport = await create_transport(runner_args, transport_params)

    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
