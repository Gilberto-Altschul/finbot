# main.py
import logging
import asyncio

from fastapi import FastAPI, Form, BackgroundTasks, Response
from twilio.rest import Client

import agent
import database as db
from pluggy_service import PluggyService
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

# Cache simples para evitar processar a mesma mensagem vinda de retentativas do Twilio
processed_messages = set()

_twilio = Client(settings.twilio_account_sid, settings.twilio_auth_token)


def _send_whatsapp(to: str, body: str) -> None:
    """
    Send a WhatsApp message via Twilio.
    This call is synchronous but will be executed via asyncio.to_thread.
    """
    try:
        msg = _twilio.messages.create(
            from_=settings.twilio_whatsapp_number,
            to=to,
            body=body,
        )
        logger.info(f"Message sent to {to} | SID: {msg.sid}")
    except Exception as exc:
        logger.error(f"Failed to send message to {to}: {exc}")


async def _scheduled_sync_task() -> None:
    """Tarefa de segundo plano para sincronização periódica."""
    # Aguarda o servidor estabilizar antes da primeira execução
    await asyncio.sleep(30)
    
    while True:
        logger.info("Iniciando rotina de sincronização automática...")
        connections = db.get_all_user_connections()
        service = PluggyService()

        for conn in connections:
            phone = conn["user_phone"]
            try:
                # Executa a sincronização silenciosa
                res = service.sync_user_transactions(phone)
                
                # Só notifica o usuário se houver "Novas transações" ou alertas (🚨/⚠️)
                if "📌" in res or "🚨" in res or "⚠️" in res:
                    logger.info(f"Novidades para {phone}, enviando notificação.")
                    await asyncio.to_thread(_send_whatsapp, phone, res)
            except Exception as e:
                logger.error(f"Erro na sincronização automática para {phone}: {e}")

        wait_time = settings.sync_interval_hours * 3600
        logger.info(f"Sincronização concluída. Próxima rodada em {settings.sync_interval_hours} horas.")
        await asyncio.sleep(wait_time)


async def _process(user_phone: str, user_message: str) -> None:
    reply = await agent.run(user_phone, user_message)
    await asyncio.to_thread(_send_whatsapp, user_phone, reply)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(_scheduled_sync_task())


@app.post("/webhook/whatsapp")
async def webhook(
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form(...),
):
    """
    Twilio sends form-encoded POST with From and Body fields.
    We respond immediately with 200 and process async to avoid timeout.
    """
    # Proteção contra loop de retentativas do Twilio
    if MessageSid in processed_messages:
        logger.warning(f"Mensagem duplicada ignorada: {MessageSid}")
        return Response(content="<?xml version='1.0'?><Response/>", media_type="text/xml")

    processed_messages.add(MessageSid)
    
    user_phone = From.strip()
    user_message = Body.strip()

    logger.info(f"Incoming message from {user_phone} [SID: {MessageSid}]: {user_message[:60]}")

    background_tasks.add_task(_process, user_phone, user_message)

    # Empty TwiML response — we send the reply ourselves via Twilio API
    return Response(content="<?xml version='1.0'?><Response/>", media_type="text/xml")
