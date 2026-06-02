# app/main.py
import logging
import asyncio
import base64
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="FinBot", version="1.0.0")
settings = get_settings()

# Cache simples para evitar processar a mesma mensagem vinda de retentativas do Twilio
processed_messages = deque(maxlen=1000)
_twilio = Client(settings.twilio_account_sid, settings.twilio_auth_token)

def _send_whatsapp(to: str, body: str) -> None:
    """Dispara mensagens síncronas usando thread do asyncio para não travar a API."""
    if not body or not str(body).strip():
        logger.error(f"Erro: Tentativa de enviar corpo de mensagem vazio para {to}. Abortando envio.")
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
                logger.warning(f"Retentativa de envio WhatsApp ({attempt+1}/3) por erro: {exc}")
                time.sleep(1)
                continue
            logger.error(f"Falha definitiva ao enviar WhatsApp para {to}: {exc}")

async def _process(user_phone: str, user_message: str) -> None:
    """Orquestra a conversa padrão delegando para o Agente Gemini."""
    reply = await agent.run(user_phone, user_message)
    await asyncio.to_thread(_send_whatsapp, user_phone, reply)

async def async_process_pdf_extract(user_phone: str, media_url: str, senha_fornecida: str = None):
    """Executado em segundo plano para descriptografar, ler e processar as metas do PDF."""
    try:
        start_time = time.perf_counter()
        # Adiciona autenticação básica para evitar Erro 401 ao baixar mídia protegida do Twilio.
        # Usando headers explícitos para maior robustez na autenticação.
        auth_string = f"{settings.twilio_account_sid}:{settings.twilio_auth_token}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        headers = {"Authorization": f"Basic {encoded_auth}"}

        async with httpx.AsyncClient() as client:
            response = await client.get(
                media_url, 
                headers=headers,
                timeout=15.0,
                follow_redirects=True
            )
            response.raise_for_status()
        download_time = time.perf_counter() - start_time

        # Verifica se o Content-Type é realmente um PDF
        content_type = response.headers.get("Content-Type", "").lower()
        if "application/pdf" not in content_type:
            logger.error(f"Downloaded file is not a PDF. Content-Type: {content_type}")
            await asyncio.to_thread(_send_whatsapp, user_phone, "❌ O arquivo que você enviou não parece ser um PDF válido. Por favor, tente novamente com um arquivo PDF.")
            db.limpar_pdf_pendente(user_phone) # Limpa o estado pendente
            return
        pdf_file = io.BytesIO(response.content)
        pdf_final_content = response.content
        
        reader = PdfReader(pdf_file)
        if reader.is_encrypted:
            if not senha_fornecida:
                # PDF protegido e ainda não temos a senha: pede agora
                db.salvar_pdf_aguardando_senha(user_phone, media_url, status="aguardando_senha")
                reply = (
                    "🏦 *Extrato do Bank recebido!* O arquivo está protegido.\n\n"
                    "Por favor, *digite a senha do PDF* por aqui para eu processar "
                    "(geralmente os 4 ou 6 primeiros dígitos do seu CPF)."
                )
                await asyncio.to_thread(_send_whatsapp, user_phone, reply)
                return

            if not reader.decrypt(senha_fornecida):
                await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Senha incorreta. O arquivo continua protegido. Envie o PDF novamente no chat para tentar outra vez.")
                db.limpar_pdf_pendente(user_phone)
                return
            
            # Se estava criptografado, geramos uma versão limpa (bytes) para o Gemini
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            with io.BytesIO() as output:
                writer.write(output)
                pdf_final_content = output.getvalue()

        # Desbloqueou o arquivo com sucesso? Reseta imediatamente o estado pendente no banco
        db.limpar_pdf_pendente(user_phone)
        
        # Mensagem intermediária para manter o usuário engajado durante o processamento da IA
        await asyncio.to_thread(_send_whatsapp, user_phone, "🔍 *Leitura concluída!* Agora a inteligência artificial está organizando seus gastos... Só mais um momento.")
            
        # AGORA ENVIAMOS OS BYTES DIRETAMENTE (Abordagem Nativa/Multimodal)
        json_padrao_contrato = await pdf_import.converter_pdf_nativo_para_json(pdf_final_content, user_phone)
        llm_time = time.perf_counter() - start_time - download_time
        
        # Roda a esteira de ingestão e calcula os estouros de limite (Budgets)
        diagnostico_final = await ingestion.processar_ingestion_unificada(user_phone, json_padrao_contrato)
        total_time = time.perf_counter() - start_time
        
        logger.info(f"PDF Processed for {user_phone} in {total_time:.2f}s (DL: {download_time:.2f}s, LLM: {llm_time:.2f}s)")
        await asyncio.to_thread(_send_whatsapp, user_phone, diagnostico_final)
        
    except Exception as e:
        logger.error(f"Erro no processamento do PDF: {e}")
        db.limpar_pdf_pendente(user_phone)
        await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Tive um problema técnico ao tentar abrir seu PDF.")

# ── Routes ────────────────────────────────────────────────────────────────────

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
    # Proteção de idempotência contra loops e retentativas rápidas do Twilio
    if MessageSid in processed_messages:
        logger.warning(f"Mensagem duplicada ignorada: {MessageSid}")
        return Response(content="<Response/>", media_type="text/xml")

    processed_messages.append(MessageSid)
    user_phone = From.strip()
    user_message = Body.strip()

    # Garante que a entrada do usuário em finbot_user_connections exista
    try:
        db._get_or_create_user_connection(user_phone)
    except Exception as exc:
        logger.error(f"Falha ao obter ou criar conexão de usuário para {user_phone}: {exc}")
        await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Tive um problema técnico ao iniciar sua sessão. Por favor, tente novamente.")
        return Response(content="<Response/>", media_type="text/xml")

    # ── CASO 1: O USUÁRIO ENVIOU UM ARQUIVO PDF (EXTRATO) ────────────────────
    if NumMedia > 0 and MediaUrl0 and "pdf" in str(MediaContentType0).lower():
        logger.info(f"PDF recebido de {user_phone}. Alterando estado para aguardando_senha.")
        db.salvar_pdf_aguardando_senha(user_phone, MediaUrl0, status="processando")
        
        await asyncio.to_thread(_send_whatsapp, user_phone, "🏦 *Extrato recebido!* Verificando o arquivo...")
        background_tasks.add_task(async_process_pdf_extract, user_phone, MediaUrl0)
        return Response(content="<Response/>", media_type="text/xml")

    # ── CASO 2: MENSAGEM DE TEXTO ORDINÁRIA (PODE SER UMA SENHA OU CONVERSA) ──
    # Verifica se o banco de dados tem um link de extrato retido aguardando validação
    pdf_pendente_url = db.obter_pdf_pendente(user_phone)
    
    if pdf_pendente_url:
        logger.info(f"Senha recebida de {user_phone}. Iniciando descriptografia em segundo plano.")
        await asyncio.to_thread(_send_whatsapp, user_phone, "🔑 Senha recebida! Descriptografando e processando suas transações...")
        
        # Dispara o processamento pesado em segundo plano liberando o HTTP do Twilio na hora
        background_tasks.add_task(async_process_pdf_extract, user_phone, pdf_pendente_url, senha_fornecida=user_message)
        return Response(content="<Response/>", media_type="text/xml")

    # Caso padrão: O usuário enviou uma mensagem de texto normal, delega para a IA reativa
    logger.info(f"Incoming message from {user_phone}: {user_message}")
    background_tasks.add_task(_process, user_phone, user_message)
    return Response(content="<Response/>", media_type="text/xml")