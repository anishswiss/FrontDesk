# ---------- endpoints ----------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/retell/submit-order")
async def submit_order(request: Request):
    """Retell custom function. Called by the agent once the customer confirms."""
    body = await verified_body(request)
    args = body.get("args", {})
    call = body.get("call", {})
    call_id = call.get("call_id")
    to_number = call.get("to_number")      # the Retell number that was called
    from_number = call.get("from_number")  # the customer

    with closing(db()) as c:
        rest = c.execute(
            "SELECT * FROM restaurants WHERE retell_number = ?", (to_number,)
        ).fetchone()
        if not rest:
            log.error("No restaurant for number %s", to_number)
            return {"result": "Sorry, I could not save the order. Please call back later."}

        items = args.get("items") or []
        try:
            cur = c.execute(
                """INSERT INTO orders
                   (call_id, restaurant_id, customer_phone, customer_name, order_type, items_json, notes)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    call_id, rest["id"], from_number, args.get("customer_name"),
                    args.get("order_type"), json.dumps(items), args.get("special_instructions"),
                ),
            )
            c.commit()
            order_id = cur.lastrowid
        except sqlite3.IntegrityError:
            return {"result": "This order was already submitted."}

    # Order is saved before we try the SMS, so a Twilio failure never loses it.
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

    return {"result": "Order saved. Tell the customer the restaurant has received it."}


@app.post("/retell/webhook")
async def retell_webhook(request: Request):
    """Retell call events. We store the transcript so owners can double-check orders."""
    body = await verified_body(request)
    if body.get("event") in ("call_ended", "call_analyzed"):
        call = body.get("call", {})
        with closing(db()) as c:
            c.execute(
                "INSERT OR REPLACE INTO calls (call_id, transcript) VALUES (?,?)",
                (call.get("call_id"), call.get("transcript")),
            )
            c.commit()
    return {"ok": True}