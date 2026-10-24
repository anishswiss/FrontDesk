from fastapi import APIRouter, FastAPI
from fastapi import Request
from main import app

router = APIRouter()

# ---------- endpoints ----------
@router.get("/health")
def health():
    return {"ok": True}





@router.post("/retell/webhook")
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

app.include_router(router)