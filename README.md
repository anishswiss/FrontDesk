# Missed-call order bot (Retell + FastAPI)

## 1. Run locally
```
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # fill in your keys
uvicorn main:app --reload --port 8000
ngrok http 8000          # copy the https URL
```
Check it works: open http://localhost:8000/health

## 2. Set up Retell (dashboard)
1. Create an agent and buy/import a phone number; assign the agent to it.
2. Paste your prompt (menu, prices, greeting, rules). See "Prompt starter" below.
3. Add a **Custom Function** named `submit_order`:
   - URL: `https://YOUR-NGROK-URL/retell/submit-order`
   - Parameters (JSON schema):
```json
{
  "type": "object",
  "properties": {
    "customer_name": {"type": "string"},
    "order_type": {"type": "string", "enum": ["pickup", "delivery"]},
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "name": {"type": "string"},
          "quantity": {"type": "integer"},
          "notes": {"type": "string"}
        },
        "required": ["name", "quantity"]
      }
    },
    "special_instructions": {"type": "string"}
  },
  "required": ["customer_name", "order_type", "items"]
}
```
4. Leave "Payload: args only" **off** (the code expects `name`, `args`, `call`).
5. Set the agent's webhook URL to `https://YOUR-NGROK-URL/retell/webhook`.

## 3. Prompt starter
```
You are the phone assistant for [Restaurant]. The restaurant is busy, so you take orders.
Say at the start that you are an automated assistant.
Menu (only offer these items): ...
Collect: name, pickup or delivery, items with quantities and changes.
Always read the full order back and ask the customer to confirm.
Only after they confirm, call submit_order. Never invent items or prices.
If asked something you don't know, say the restaurant will call back.
```

## 4. Test
Call your Retell number directly first. Once orders and SMS work, set up
busy/no-answer forwarding on your phone to the Retell number and test that.

## Notes
- Verify field names against Retell's current docs (docs.retellai.com) if anything differs.
- The SQLite file (orders.db) is fine for testing; move to Postgres/Supabase for production.
- Twilio trial accounts can only text verified numbers; US production SMS needs 10DLC registration.
