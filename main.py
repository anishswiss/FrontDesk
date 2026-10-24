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


# ---------- database ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS restaurants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                inbound_number TEXT UNIQUE NOT NULL,  -- the number customers get forwarded to
                owner_phone TEXT NOT NULL            -- where order SMS is sent
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT UNIQUE,                 -- prevents duplicate orders
                restaurant_id INTEGER NOT NULL,
                customer_phone TEXT,
                customer_name TEXT,
                order_type TEXT,
                items_json TEXT NOT NULL,
                notes TEXT,
                sms_sent INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS calls (
                call_id TEXT PRIMARY KEY,
                transcript TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        # Seed one restaurant for testing, from env vars
        num = os.getenv("SEED_INBOUND_NUMBER")
        if num:
            c.execute(
                "INSERT OR IGNORE INTO restaurants (name, inbound_number, owner_phone) VALUES (?,?,?)",
                (os.getenv("SEED_NAME", "Test Restaurant"), num, os.environ["SEED_OWNER_PHONE"]),
            )
        c.commit()


init_db()





def format_sms(restaurant_name, customer_name, phone, order_type, items, notes):
    lines = [f"New order for {restaurant_name}"]
    lines.append(f"Customer: {customer_name or 'Unknown'} ({phone or 'no number'})")
    lines.append(f"Type: {order_type or 'not specified'}")
    for it in items:
        extra = f" ({it['notes']})" if it.get("notes") else ""
        lines.append(f"- {it.get('quantity', 1)} x {it.get('name', '?')}{extra}")
    if notes:
        lines.append(f"Notes: {notes}")
    return "\n".join(lines)


