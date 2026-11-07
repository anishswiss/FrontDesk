"""
Business logic, independent of the voice layer (Postgres version).
Pipecat's submit_order tool calls save_order(); database setup, restaurant lookup,
menu items and owner SMS live here too.

Set DATABASE_URL in .env, e.g. postgresql://user:password@host:5432/dbname
"""
import asyncio
import logging
import os
from contextlib import closing

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from twilio.rest import Client as TwilioClient

load_dotenv()
log = logging.getLogger("orderbot")

DATABASE_URL = os.environ["DATABASE_URL"]
SMS_FROM = os.environ["TWILIO_SMS_FROM"]
# Accept either naming scheme for the Twilio credentials
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID") or os.environ["TWILIO_SID"]
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN") or os.environ["TWILIO_TOKEN"]
twilio = TwilioClient(TWILIO_SID, TWILIO_TOKEN)

SAMPLE_MENU = """\
Chicken Biryani - $14
Veg Biryani - $12
Butter Chicken - $15
Paneer Tikka Masala - $13
Garlic Naan - $3
Mango Lassi - $4
"""

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS restaurants (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        inbound_number TEXT UNIQUE NOT NULL,   -- Twilio number calls are forwarded to
        owner_phone TEXT NOT NULL,             -- where order SMS is sent
        menu TEXT                              -- old plain-text menu (fallback)
    )""",
    """CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        call_id TEXT UNIQUE,                   -- prevents duplicate orders
        restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
        customer_phone TEXT,
        customer_name TEXT,
        order_type TEXT,
        items_json JSONB NOT NULL,
        notes TEXT,
        sms_sent BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS menu_items (
        id SERIAL PRIMARY KEY,
        restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
        category TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        price_cents INTEGER NOT NULL,
        available BOOLEAN NOT NULL DEFAULT TRUE,   -- FALSE = sold out / hidden from the bot
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (restaurant_id, category, name)
    )""",
]


def db():
    """Open a connection. Rows come back as dicts. Callers must commit() writes."""
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with closing(db()) as c:
        for stmt in SCHEMA:
            c.execute(stmt)
        num = os.getenv("SEED_INBOUND_NUMBER")
        if num:  # seed one test restaurant from env
            c.execute(
                """INSERT INTO restaurants (name, inbound_number, owner_phone, menu)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (inbound_number) DO NOTHING""",
                (os.getenv("SEED_NAME", "Test Restaurant"), num,
                 os.environ["SEED_OWNER_PHONE"], SAMPLE_MENU),
            )
        c.commit()


init_db()


# ---------- reads (blocking: from async code call them with asyncio.to_thread) ----------
def get_restaurant(inbound_number: str):
    with closing(db()) as c:
        return c.execute(
            "SELECT * FROM restaurants WHERE inbound_number = %s", (inbound_number,)
        ).fetchone()


def get_menu_rows(restaurant_id: int):
    with closing(db()) as c:
        return c.execute(
            "SELECT * FROM menu_items WHERE restaurant_id = %s ORDER BY id", (restaurant_id,)
        ).fetchall()


def get_menu_text(restaurant_id: int) -> str:
    """Available items as plain text for the AI prompt ('' if the restaurant has none)."""
    groups = {}
    for r in get_menu_rows(restaurant_id):
        if r["available"]:
            groups.setdefault(r["category"] or "Menu", []).append(r)
    lines = []
    for cat, rows in groups.items():
        lines.append(f"{cat}:")
        for r in rows:
            line = f"- {r['name']} - ${r['price_cents'] / 100:.2f}"
            if r["description"]:
                line += f" ({r['description'][:100]})"
            lines.append(line)
        lines.append("")
    return "\n".join(lines).strip()


# ---------- menu writes ----------
def save_menu_items(restaurant_id: int, items: list, deactivate_missing: bool = False) -> dict:
    """
    Upsert items: {category, name, description, price_cents, available(optional)}.
    Existing rows keep their 'available' flag unless the item sets it explicitly.
    deactivate_missing=True hides items that are not in this import (instead of deleting).
    """
    added = updated = hidden = 0
    seen = set()
    with closing(db()) as c:
        for it in items:
            key = (it.get("category") or "", it["name"])
            seen.add(key)
            row = c.execute(
                """INSERT INTO menu_items
                   (restaurant_id, category, name, description, price_cents, available)
                   VALUES (%(rid)s, %(cat)s, %(name)s, %(desc)s, %(price)s,
                           COALESCE(%(avail)s::boolean, TRUE))
                   ON CONFLICT (restaurant_id, category, name) DO UPDATE SET
                     description = EXCLUDED.description,
                     price_cents = EXCLUDED.price_cents,
                     available   = COALESCE(%(avail)s::boolean, menu_items.available),
                     updated_at  = now()
                   RETURNING (xmax = 0) AS inserted""",
                {"rid": restaurant_id, "cat": key[0], "name": key[1],
                 "desc": it.get("description") or "", "price": int(it["price_cents"]),
                 "avail": it.get("available")},
            ).fetchone()
            if row["inserted"]:
                added += 1
            else:
                updated += 1
        if deactivate_missing:
            for r in c.execute(
                "SELECT id, category, name FROM menu_items WHERE restaurant_id = %s AND available",
                (restaurant_id,),
            ).fetchall():
                if (r["category"], r["name"]) not in seen:
                    c.execute("UPDATE menu_items SET available = FALSE WHERE id = %s", (r["id"],))
                    hidden += 1
        c.commit()
    return {"added": added, "updated": updated, "hidden": hidden}


# ---------- orders ----------
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


def _insert_order(inbound_number, customer_number, call_id, args):
    """Blocking part of save_order. Returns (status, restaurant, order_id)."""
    rest = get_restaurant(inbound_number)
    if not rest:
        return "no_restaurant", None, None
    try:
        with closing(db()) as c:
            row = c.execute(
                """INSERT INTO orders
                   (call_id, restaurant_id, customer_phone, customer_name,
                    order_type, items_json, notes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (call_id, rest["id"], customer_number, args.get("customer_name"),
                 args.get("order_type"), Jsonb(args.get("items") or []),
                 args.get("special_instructions")),
            ).fetchone()
            c.commit()
    except psycopg.errors.UniqueViolation:
        return "duplicate", rest, None
    return "ok", rest, row["id"]


def _mark_sms_sent(order_id):
    with closing(db()) as c:
        c.execute("UPDATE orders SET sms_sent = TRUE WHERE id = %s", (order_id,))
        c.commit()


async def save_order(inbound_number: str, customer_number: str, call_id: str, args: dict) -> str:
    """Save the order, then text the owner. Returns a short message for the AI.
    Database work runs in a thread so it never blocks live call audio."""
    status, rest, order_id = await asyncio.to_thread(
        _insert_order, inbound_number, customer_number, call_id, args
    )
    if status == "no_restaurant":
        log.error("No restaurant for number %s", inbound_number)
        return "Sorry, I could not save the order. Please call back later."
    if status == "duplicate":
        return "This order was already submitted."

    # Saved first, so an SMS failure never loses the order.
    sms = format_sms(rest["name"], args.get("customer_name"), customer_number,
                     args.get("order_type"), args.get("items") or [],
                     args.get("special_instructions"))
    try:
        await asyncio.to_thread(
            twilio.messages.create, from_=SMS_FROM, to=rest["owner_phone"], body=sms
        )
        await asyncio.to_thread(_mark_sms_sent, order_id)
    except Exception:
        log.exception("SMS failed for order %s", order_id)  # TODO: alert yourself

    return "Order saved. Tell the customer the restaurant has received it."