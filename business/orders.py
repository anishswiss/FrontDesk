"""
Core order logic, independent of Retell or Pipecat.
Both voice layers call save_order(); only the way they get the inputs differs.
"""
import json
import logging
import os
import sqlite3
from contextlib import closing

from starlette.concurrency import run_in_threadpool
from twilio.rest import Client as TwilioClient

log = logging.getLogger("orderbot")

DB_PATH = os.getenv("DB_PATH", "orders.db")
SMS_FROM = os.environ["TWILIO_SMS_FROM"]
twilio = TwilioClient(os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"])


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


async def save_order(to_number: str, from_number: str, call_id: str, args: dict) -> str:
    """
    to_number:   the number the customer reached (identifies the restaurant)
    from_number: the customer's number
    call_id:     unique per call, prevents duplicate orders
    args:        {customer_name, order_type, items[], special_instructions}
    Returns a short message the AI can use in its reply.
    """
    items = args.get("items") or []

    with closing(db()) as c:
        rest = c.execute(
            "SELECT * FROM restaurants WHERE inbound_number = ?", (to_number,)
        ).fetchone()
        if not rest:
            log.error("No restaurant for number %s", to_number)
            return "Sorry, I could not save the order. Please call back later."

        try:
            cur = c.execute(
                """INSERT INTO orders
                   (call_id, restaurant_id, customer_phone, customer_name,
                    order_type, items_json, notes)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    call_id, rest["id"], from_number, args.get("customer_name"),
                    args.get("order_type"), json.dumps(items),
                    args.get("special_instructions"),
                ),
            )
            c.commit()
            order_id = cur.lastrowid
        except sqlite3.IntegrityError:
            return "This order was already submitted."

    # Order is saved first, so an SMS failure never loses it.
    sms = format_sms(
        rest["name"], args.get("customer_name"), from_number,
        args.get("order_type"), items, args.get("special_instructions"),
    )
    try:
        await run_in_threadpool(
            twilio.messages.create, from_=SMS_FROM, to=rest["owner_phone"], body=sms
        )
        with closing(db()) as c:
            c.execute("UPDATE orders SET sms_sent = 1 WHERE id = ?", (order_id,))
            c.commit()
    except Exception:
        log.exception("SMS failed for order %s", order_id)  # TODO: alert yourself

    return "Order saved. Tell the customer the restaurant has received it."