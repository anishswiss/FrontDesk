"""
stt_only_bot.py - test ONLY Deepgram speech-to-text over a real phone call.

No LLM, no voice, no database. The bot answers the call, stays silent, and prints
what Deepgram hears while you talk:

    hearing: 'masala'                 <- live guess while you are still speaking
    FINAL:   'one masala dosa please' <- the finished sentence

Run it the same way as the real bot:
    python stt_only_bot.py -t twilio -x YOUR-NAME.ngrok.io
Then call your Twilio number (webhook already points at https://YOUR-NAME.ngrok.io/).

Optional settings in .env (or set in the terminal before running):
    STT_KEYTERMS=Masala Dosa|Medu Vada|Filter Coffee    words to boost, separated by |
    STT_LANGUAGE=en-IN                                   try en, en-IN, multi
Compare runs with and without STT_KEYTERMS to see the difference.
"""
import os

from dotenv import load_dotenv
from loguru import logger
import asyncio
from twilio.rest import Client as TwilioClient

from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.workers.runner import WorkerRunner

load_dotenv(override=True)


class ShowTranscripts(FrameProcessor):
    """Prints what the speech-to-text heard."""

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterimTranscriptionFrame):
            logger.info(f"hearing: {frame.text!r}")
        elif isinstance(frame, TranscriptionFrame):
            logger.info(f"FINAL:   {frame.text!r}")
        await self.push_frame(frame, direction)


async def bot(runner_args: RunnerArguments):
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    if transport_type != "twilio":
        logger.error(f"Unexpected transport: {transport_type}")
        return   

    call_sid = call_data["call_id"]
    stream_sid = call_data["stream_id"]

    twilio_client = TwilioClient(
    os.getenv("TWILIO_ACCOUNT_SID") or os.environ["TWILIO_SID"],
    os.getenv("TWILIO_AUTH_TOKEN") or os.environ["TWILIO_TOKEN"])

    # Twilio's stream start message has no To/From, so ask Twilio's REST API.
    call_info = await asyncio.to_thread(lambda: twilio_client.calls(call_sid).fetch())
    inbound_number = call_info.to          # the number the call was forwarded to
    print(f" inbound_number {inbound_number}")

    keyterms = [t.strip() for t in os.getenv("STT_KEYTERMS", "").split("|") if t.strip()]
    settings = {}
    if keyterms:
        settings["keyterm"] = keyterms
    if os.getenv("STT_LANGUAGE"):
        settings["language"] = os.getenv("STT_LANGUAGE")
    logger.info(f"Deepgram settings: {settings or 'defaults (nova-3, English)'}")

    menu_keywords = [
        "dosa:3", "masala:3", "podi:3", "uthappam:3", 
        "mysore:2", "andhra karam:2", "paneer:2"
    ]

    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"),
                              settings=DeepgramSTTService.Settings(
                                 model="nova-3",
                                language="en",
                                smart_format=True,
                                keyterm=menu_keywords
                              ))

    serializer = TwilioFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data["call_id"],
        account_sid=os.getenv("TWILIO_ACCOUNT_SID") or os.environ["TWILIO_SID"],
        auth_token=os.getenv("TWILIO_AUTH_TOKEN") or os.environ["TWILIO_TOKEN"],
    )
    transport = FastAPIWebsocketTransport(
        websocket=runner_args.websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,   # keeps the Twilio stream open; the bot says nothing
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    pipeline = Pipeline([transport.input(), stt, ShowTranscripts(), transport.output()])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000),
    )

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, client):
        await worker.cancel()

    logger.info("Call connected. Speak now; the bot will stay silent and print what it hears.")
    await WorkerRunner(handle_sigint=False).run(worker)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
