# app/main.py
import logging
import asyncio
import base64
import json
import os
from contextlib import asynccontextmanager
from collections import deque
import io
import time
import httpx
from pypdf import PdfReader, PdfWriter

from fastapi import FastAPI, Form, BackgroundTasks, Response
from twilio.rest import Client

import app.agent as agent
import app.database as db
import app.pdf_import as pdf_import
import app.ingestion as ingestion
from app.config import get_settings
from app.ofx_schema import OpenFinancePayload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

logging.getLogger("twilio.http_client").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    logger.info("HTTP Client inicializado no lifespan.")
    yield
    await http_client.aclose()
    logger.info("HTTP Client encerrado.")


app = FastAPI(title="FinBot", version="1.0.0", lifespan=lifespan)

http_client: httpx.AsyncClient = None
processed_messages = deque(maxlen=1000)
_twilio = Client(settings.twilio_account_sid, settings.twilio_auth_token)


def _send_whatsapp(to: str, body: str) -> None:
    if not body or not str(body).strip():
        logger.error(f"Erro: corpo de mensagem vazio para {to}. Abortando envio.")
        return

    for attempt in range(3):
        try:
            msg = _twilio.messages.create(
                from_=settings.twilio_whatsapp_number,
                to=to,
                body=body,
            )
            logger.info(f"Message sent to {to} | SID: {msg.sid}")
            return
        except Exception as exc:
            if attempt < 2:
                logger.warning(f"Retentativa de envio WhatsApp ({attempt+1}/3): {exc}")
                time.sleep(1)
                continue
            logger.error(f"Falha definitiva ao enviar WhatsApp para {to}: {exc}")


async def _process(user_phone: str, user_message: str) -> None:
    reply = await agent.run(user_phone, user_message)
    await asyncio.to_thread(_send_whatsapp, user_phone, reply)


async def async_process_pdf_extract(user_phone: str, media_url: str, senha_fornecida: str = None):
    try:
        start_time = time.perf_counter()
        auth_string = f"{settings.twilio_account_sid}:{settings.twilio_auth_token}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        headers = {"Authorization": f"Basic {encoded_auth}"}

        response = await http_client.get(media_url, headers=headers)
        response.raise_for_status()
        download_time = time.perf_counter() - start_time

        content_type = response.headers.get("Content-Type", "").lower()
        if "application/pdf" not in content_type:
            logger.error(f"Downloaded file is not a PDF. Content-Type: {content_type}")
            await asyncio.to_thread(_send_whatsapp, user_phone, "❌ O arquivo enviado não parece ser um PDF válido. Por favor, envie o extrato original do banco.")
            db.limpar_pdf_pendente(user_phone)
            return

        pdf_final_content = response.content
        reader = PdfReader(io.BytesIO(pdf_final_content))

        if reader.is_encrypted:
            if not senha_fornecida:
                db.salvar_pdf_aguardando_senha(user_phone, media_url, status="aguardando_senha")
                reply = (
                    "🏦 *Extrato recebido!* O arquivo está protegido.\n\n"
                    "Por favor, *digite a senha do PDF* para eu processar "
                    "(geralmente os 4 ou 6 primeiros dígitos do seu CPF)."
                )
                await asyncio.to_thread(_send_whatsapp, user_phone, reply)
                return

            if not reader.decrypt(senha_fornecida):
                await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Senha incorreta. Envie o PDF novamente para tentar outra vez.")
                db.limpar_pdf_pendente(user_phone)
                return

            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            with io.BytesIO() as output:
                writer.write(output)
                pdf_final_content = output.getvalue()

        db.limpar_pdf_pendente(user_phone)

        num_pages = len(reader.pages)
        logger.info(f"Iniciando extração de {num_pages} páginas para {user_phone}")
        await asyncio.to_thread(_send_whatsapp, user_phone, f"🔍 *PDF de {num_pages} páginas lido!* A IA está organizando seus gastos... Só mais um momento.")

        all_extracted_transactions = await pdf_import.converter_pdf_nativo_para_json(pdf_final_content, user_phone)
        llm_time = time.perf_counter() - start_time - download_time

        # ingestion agora retorna (mensagem, lista_outros_ou_None)
        mensagem, transacoes_outros = await ingestion.processar_ingestion_unificada(user_phone, all_extracted_transactions)

        total_time = time.perf_counter() - start_time
        logger.info(f"PDF Processed for {user_phone} in {total_time:.2f}s (DL: {download_time:.2f}s, LLM: {llm_time:.2f}s)")

        await asyncio.to_thread(_send_whatsapp, user_phone, mensagem)

    except ValueError as ve:
        logger.error(f"Falha na Extração IA para {user_phone}: {ve}")
        db.limpar_pdf_pendente(user_phone)
        await asyncio.to_thread(_send_whatsapp, user_phone, "⚠️ No momento, não consegui analisar seu extrato devido a alta demanda na IA. Tente novamente em alguns instantes.")
    except httpx.HTTPStatusError as hse:
        logger.error(f"Erro ao baixar arquivo do Twilio ({hse.response.status_code}) para {user_phone}")
        db.limpar_pdf_pendente(user_phone)
        await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Não consegui acessar o arquivo enviado. Por favor, reenvie o PDF.")
    except Exception as exc:
        logger.error(f"Erro crítico imprevisto no PDF ({user_phone}): {exc}", exc_info=True)
        db.limpar_pdf_pendente(user_phone)
        await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Tive um problema técnico ao processar seu extrato. Verifique se o arquivo está correto.")


async def async_process_categorizacao(user_phone: str, resposta_usuario: str, transactions_json: str):
    """Aplica a categorização do usuário e grava as transações confirmadas."""
    try:
        transactions = ingestion.aplicar_categorizacao_usuario(transactions_json, resposta_usuario)
        resultado = await ingestion.gravar_transacoes_confirmadas(user_phone, transactions)
        await asyncio.to_thread(_send_whatsapp, user_phone, resultado)
    except Exception as exc:
        logger.error(f"Erro ao processar categorização de {user_phone}: {exc}", exc_info=True)
        db.limpar_transacoes_pendentes(user_phone)
        await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Erro ao aplicar as categorias. As transações foram salvas como *Outros*.")


# ── Routes ────────────────────────────────────────────────────────────────────

def _parece_categorizacao(mensagem: str) -> bool:
    """
    Retorna True se a mensagem parece uma resposta de categorização.
    Padrões aceitos: começa com número ("1 roupa"), é "ok", ou lista de itens ("1 x, 2 y").
    """
    msg = mensagem.strip().lower()
    if msg == "ok":
        return True
    # Verifica se começa com número seguido de espaço e texto
    import re
    return bool(re.match(r"^[0-9]+\s+[a-zA-Z]", msg))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook/whatsapp")
async def webhook(
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form(...),
    NumMedia: int = Form(0),
    MediaUrl0: str = Form(None),
    MediaContentType0: str = Form(None)
):
    if MessageSid in processed_messages:
        logger.warning(f"Mensagem duplicada ignorada: {MessageSid}")
        return Response(content="<Response/>", media_type="text/xml")

    processed_messages.append(MessageSid)
    user_phone = From.strip()
    user_message = Body.strip()

    try:
        db._get_or_create_user_connection(user_phone)
    except Exception as exc:
        logger.error(f"Falha ao criar conexão para {user_phone}: {exc}")
        await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Tive um problema técnico ao iniciar sua sessão. Por favor, tente novamente.")
        return Response(content="<Response/>", media_type="text/xml")

    # ── CASO 1: PDF ENVIADO ───────────────────────────────────────────────────
    if NumMedia > 0 and MediaUrl0 and "pdf" in str(MediaContentType0).lower():
        logger.info(f"PDF recebido de {user_phone}.")
        db.salvar_pdf_aguardando_senha(user_phone, MediaUrl0, status="processando")
        await asyncio.to_thread(_send_whatsapp, user_phone, "🏦 *Extrato recebido!* Verificando o arquivo...")
        background_tasks.add_task(async_process_pdf_extract, user_phone, MediaUrl0)
        return Response(content="<Response/>", media_type="text/xml")

    # ── CASO 2: RESPOSTA DE CATEGORIZAÇÃO PENDENTE ───────────────────────────
    transactions_json = db.obter_transacoes_pendentes(user_phone)
    if transactions_json and _parece_categorizacao(user_message):
        logger.info(f"Resposta de categorização recebida de {user_phone}: {user_message}")
        background_tasks.add_task(async_process_categorizacao, user_phone, user_message, transactions_json)
        return Response(content="<Response/>", media_type="text/xml")

    # ── CASO 3: SENHA DE PDF PENDENTE ────────────────────────────────────────
    pending_pdf_info = db.obter_pdf_pendente(user_phone)
    if pending_pdf_info:
        pdf_pendente_url, pdf_status = pending_pdf_info
        if pdf_status == "aguardando_senha":
            logger.info(f"Senha recebida de {user_phone}.")
            await asyncio.to_thread(_send_whatsapp, user_phone, "🔑 Senha recebida! Descriptografando e processando...")
            background_tasks.add_task(async_process_pdf_extract, user_phone, pdf_pendente_url, senha_fornecida=user_message)
            return Response(content="<Response/>", media_type="text/xml")
        elif pdf_status == "processando":
            logger.info(f"PDF ainda processando para {user_phone}. Mensagem tratada como conversa normal.")
            background_tasks.add_task(_process, user_phone, user_message)
            return Response(content="<Response/>", media_type="text/xml")

    # ── CASO 4: MENSAGEM NORMAL ───────────────────────────────────────────────
    logger.info(f"Incoming message from {user_phone}: {user_message}")
    background_tasks.add_task(_process, user_phone, user_message)
    return Response(content="<Response/>", media_type="text/xml")
