# main.py
import logging
import asyncio

from fastapi import FastAPI, Form, BackgroundTasks, Response
from twilio.rest import Client

import agent
from config import get_settings

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="FinBot", version="1.0.0")
settings = get_settings()

_twilio = Client(settings.twilio_account_sid, settings.twilio_auth_token)


def _send_whatsapp(to: str, body: str) -> None:
    """Send a WhatsApp message via Twilio (runs in background)."""
    try:
        msg = _twilio.messages.create(
            from_=settings.twilio_whatsapp_number,
            to=to,
            body=body,
        )
        logger.info("Message sent", extra={"to": to, "sid": msg.sid})
    except Exception as exc:
        logger.error("Failed to send message", extra={"to": to, "error": str(exc)})


async def _process(user_phone: str, user_message: str) -> None:
    reply = await agent.run(user_phone, user_message)
    _send_whatsapp(user_phone, reply)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(...),
):
    """
    Twilio sends form-encoded POST with From and Body fields.
    We respond immediately with 200 and process async to avoid timeout.
    """
    user_phone = From.strip()
    user_message = Body.strip()

    logger.info("Incoming", extra={"from": user_phone, "text": user_message[:60]})

    background_tasks.add_task(_process, user_phone, user_message)

    # Empty TwiML response — we send the reply ourselves via Twilio API
    return Response(content="<?xml version='1.0'?><Response/>", media_type="text/xml")
