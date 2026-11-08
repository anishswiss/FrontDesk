"""
Missed-call order bot: backend for Retell.

Flow:
  Customer calls restaurant -> busy/no answer -> forwarded to the Retell number
  -> Retell agent takes the order -> calls POST /retell/submit-order
  -> we save the order and SMS the restaurant owner via Twilio.

Run locally:
  pip install -r requirements.txt
  uvicorn main:app --reload --port 8000
  ngrok http 8000   # use the https URL in Retell
"""
import json
import logging
import os
import sqlite3
from contextlib import closing

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from twilio.rest import Client as TwilioClient

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("orderbot")

SMS_FROM = os.environ["TWILIO_SMS_FROM"]
DB_PATH = os.getenv("DB_PATH", "orders.db")

twilio = TwilioClient(os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"])
app = FastAPI()