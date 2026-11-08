"""
Pipecat voice bot for missed-call ordering (Twilio inbound).

Run locally (uses Pipecat's development runner, which serves the TwiML + WebSocket):
    python bot.py -t twilio -x YOUR-NAME.ngrok.io
Then point your Twilio number's voice webhook at https://YOUR-NAME.ngrok.io/

Written against pipecat-ai 1.15.0. Pipecat's API changes between releases, so keep the
version pinned in requirements.txt. If you upgrade and something breaks, compare with the
official example: github.com/pipecat-ai/pipecat-examples (twilio-chatbot/inbound).
"""
import asyncio
import os

from dotenv import load_dotenv
from loguru import logger
from twilio.rest import Client as TwilioClient

from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame,TTSSpeakFrame, EndFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import UserTurnStoppedMessage
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.workers.runner import WorkerRunner

from business.orders import get_menu_text, get_restaurant, get_restaurant_with_menu_text, save_order

load_dotenv(override=True)

twilio_client = TwilioClient(
    os.getenv("TWILIO_ACCOUNT_SID") or os.environ["TWILIO_SID"],
    os.getenv("TWILIO_AUTH_TOKEN") or os.environ["TWILIO_TOKEN"],
)

# What the AI is allowed to submit once the customer confirms the order.
ORDER_TOOL = FunctionSchema(
    name="submit_order",
    description="Submit the customer's confirmed order to the restaurant. "
                "Call this only after reading the order back and the customer confirmed.",
    properties={
        "customer_name": {"type": "string"},
        "order_type": {"type": "string", "enum": ["pickup", "delivery"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "notes": {"type": "string"},
                },
                "required": ["name", "quantity"],
            },
        },
        "special_instructions": {"type": "string"},
    },
    required=["customer_name", "order_type", "items"],
)


def build_prompt(restaurant, menu_text) -> str:
    if menu_text is None and restaurant["menu"] is None:  # unknown number: fail politely
        return (
            "You are an automated phone assistant. Apologize that you cannot take "
            "orders right now, ask the caller to try again later, and say goodbye."
        )
    menu_text = menu_text or restaurant["menu"]
    return f"""You are the automated phone assistant for an Indian Restaurant : {restaurant['name']}.
The restaurant is busy and cannot answer, so you take orders for them.
Your words are spoken aloud: use short sentences, no lists, no special characters.

The caller has already heard your greeting and has not given their name yet. Do not repeat the greeting.

MENU (Only describe a dish using words from the menu above. Never guess what a dish is.
If the caller asks for something that is not on the menu, say it is not on the menu,
and name the closest items exactly as listed, without calling them something else.):

{menu_text}

Collect: the customer's name, pickup or delivery, and each item with quantity and changes.
If an item is not on the menu, say so and suggest something similar.
Before submitting, read the full order back and ask the customer to confirm.
Only after they confirm, call submit_order, then tell them the restaurant received it.
If asked something you do not know (hours, address, allergies), say the restaurant
will follow up. Keep every reply to one or two short sentences."""


async def run_bot(transport, restaurant, menu_text, inbound_number, customer_number, call_sid):

    menu_keyterms = [
        "dosa:3", "masala:3", "podi:3", "uthappam:3", 
        "mysore:2", "andhra karam:2", "paneer:2"
    ]

    menu_keyterms = restaurant.__getitem__("speech_hints")
    
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"),
                              settings=DeepgramSTTService.Settings(
                                 model="nova-3",
                                language="en",
                                smart_format=True,
                                keyterm=menu_keyterms
                              ))
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id=os.getenv("CARTESIA_VOICE_ID", "79a125e8-cd45-4c13-8a67-188112f4dd22"),
    )
    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAILLMService.Settings(model="gpt-4.1-mini"),
    )

    async def submit_order(params: FunctionCallParams):
        result = await save_order(inbound_number, customer_number, call_sid, params.arguments)
        await params.result_callback({"result": result})

    llm.register_function("submit_order", submit_order)

    messages = [
        {"role": "system", "content": build_prompt(restaurant, menu_text)}
    ]
    context = LLMContext(messages, ToolsSchema(standard_tools=[ORDER_TOOL]))
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineWorker(
        pipeline,
        params=PipelineParams(          # phone audio is 8 kHz
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            enable_metrics=True,
        ),
    )

    if restaurant: # number found in your database
        short_name = restaurant["name"].split("|")[0].strip()
        greeting = (
            f"Hello, this is the automated assistant for {short_name}. "
            "The restaurant is busy right now, but I can take your order. "
            "May I have your name, please?"
        )
    else:  # number NOT in your database
        greeting = (
            "Hello. I'm sorry, I can't take orders right now. "
            "Please try again later. Goodbye."
        )

    @transport.event_handler("on_client_connected")
    async def on_connected(transport, client):        
        if restaurant:
            await task.queue_frames([TTSSpeakFrame(greeting, append_to_context=True)])
        else:
            # unknown number: apologize, then end the call
            await task.queue_frames([TTSSpeakFrame(greeting), EndFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, client):
        await task.cancel()

    await WorkerRunner(handle_sigint=False).run(task)


async def bot(runner_args: RunnerArguments):
    """Entry point called by Pipecat's runner for each incoming call."""
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    if transport_type != "twilio":
        logger.error(f"Unexpected transport: {transport_type}")
        return

    call_sid = call_data["call_id"]
    stream_sid = call_data["stream_id"]

    # Twilio's stream start message has no To/From, so ask Twilio's REST API.
    call_info = await asyncio.to_thread(lambda: twilio_client.calls(call_sid).fetch())
    inbound_number = call_info.to          # the number the call was forwarded to
    print('f inbound_number {inbound_number}')
    customer_number = getattr(call_info, "_from", None) or getattr(call_info, "from_", None)
    logger.info(f"Call {call_sid}: to={inbound_number} from={customer_number}")

    # Database calls are blocking, so run them in a thread to keep call audio smooth.
    '''
    restaurant = await asyncio.to_thread(get_restaurant, inbound_number)
    menu_text = ""
    if restaurant is None:
        logger.error(f"No restaurant configured for {inbound_number}")
    else:
        menu_text = await asyncio.to_thread(get_menu_text, restaurant["id"])
    '''
    restaurant, menu_text = get_restaurant_with_menu_text(inbound_number)

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=twilio_client.username,
        auth_token=twilio_client.password,
    )
    transport = FastAPIWebsocketTransport(
        websocket=runner_args.websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )
    await run_bot(transport, restaurant, menu_text, inbound_number, customer_number, call_sid)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()