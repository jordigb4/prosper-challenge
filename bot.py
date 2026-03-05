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
from pipecat.turns.user_turn_strategies import UserTurnStrategies

logger.info("✅ All components loaded successfully!")

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Healthie tool definitions (OpenAI function-calling schema)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_patient",
            "description": (
                "Search Healthie for a patient by their full name and date of birth. "
                "Call this once the caller has provided both pieces of information. "
                "Returns patient details on success, or an error message if not found."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Patient's full name, e.g. 'Jane Smith'",
                    },
                    "date_of_birth": {
                        "type": "string",
                        "description": "Patient's date of birth in YYYY-MM-DD format",
                    },
                },
                "required": ["name", "date_of_birth"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_appointment",
            "description": (
                "Book an appointment in Healthie for a verified patient. "
                "Call this once you have the patient_id, desired date and time. "
                "Returns appointment details on success."
            ),
            "parameters": {
                "type": "object",
                "properties": {
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
                "required": ["patient_id", "date", "time"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a friendly appointment scheduling assistant at Prosper Health clinic.

Your job is to schedule appointments for patients. Follow these steps in order:

STEP 1 — Greet the caller warmly and ask for their full name.
STEP 2 — Ask for their date of birth to verify their identity.
STEP 3 — Call find_patient with the name and date_of_birth you collected.
  • If the patient is NOT found, apologise and ask them to call the clinic directly.
  • If the patient IS found, confirm their name back and move to STEP 4.
STEP 4 — Ask what date they would like their appointment.
STEP 5 — Ask what time they would prefer.
STEP 6 — Call create_appointment with the patient_id, date, and time.
  • If booking succeeds, confirm the appointment details clearly and thank them.
  • If booking fails, apologise and offer to try a different date or time.

Guidelines:
- Be warm, concise and professional. This is a voice conversation so keep sentences short.
- Always confirm back what you have heard before making a function call.
- Format dates as YYYY-MM-DD and times as HH:MM (24-hour) before calling functions.
  When the user says something like "March 15th" convert it to "2026-03-15".
  When they say "2 PM" convert it to "14:00".
- Never reveal internal patient IDs or raw API responses to the caller.
- If at any point you are unsure, ask the caller to repeat themselves.
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
    from healthie import find_patient  # local import avoids circular deps

    name = args.get("name", "")
    dob = args.get("date_of_birth", "")
    logger.info(f"[tool] find_patient name={name!r} dob={dob!r}")

    try:
        patient = await find_patient(name=name, date_of_birth=dob)
        if patient:
            logger.info(f"[tool] Patient found: {patient}")
            await result_callback(json.dumps({"status": "found", "patient": patient}))
        else:
            logger.warning(f"[tool] Patient not found")
            await result_callback(
                json.dumps({"status": "not_found", "message": "No patient matching that name and date of birth was found in the system."})
            )
    except Exception as exc:
        logger.exception(f"[tool] find_patient error: {exc}")
        await result_callback(
            json.dumps({"status": "error", "message": f"There was a problem searching for the patient: {exc}"})
        )


async def handle_create_appointment(
    function_name: str,
    tool_call_id: str,
    args: dict,
    llm,
    context: LLMContext,
    result_callback,
):
    """Bridge between the LLM function call and healthie.create_appointment."""
    from healthie import create_appointment  # local import avoids circular deps

    patient_id = args.get("patient_id", "")
    date = args.get("date", "")
    time = args.get("time", "")
    logger.info(f"[tool] create_appointment patient_id={patient_id!r} date={date!r} time={time!r}")

    try:
        appointment = await create_appointment(patient_id=patient_id, date=date, time=time)
        if appointment:
            logger.info(f"[tool] Appointment created: {appointment}")
            await result_callback(json.dumps({"status": "created", "appointment": appointment}))
        else:
            logger.warning(f"[tool] Appointment creation returned None")
            await result_callback(
                json.dumps({"status": "failed", "message": "The appointment could not be created. The time slot may be unavailable."})
            )
    except Exception as exc:
        logger.exception(f"[tool] create_appointment error: {exc}")
        await result_callback(
            json.dumps({"status": "error", "message": f"There was a problem creating the appointment: {exc}"})
        )

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
    llm.register_function("find_patient", handle_find_patient)
    llm.register_function("create_appointment", handle_create_appointment)

    messages = [
        {"role":"system","content":SYSTEM_PROMPT}
    ]

    context = LLMContext(messages, tools=TOOLS)
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
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
        ),
    }

    transport = await create_transport(runner_args, transport_params)

    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
