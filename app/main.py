# app/main.py
import logging
import asyncio
import base64
from collections import deque
import io
import requests
from pypdf import PdfReader

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
    try:
        msg = _twilio.messages.create(
            from_=settings.twilio_whatsapp_number,
            to=to,
            body=body,
        )
        logger.info(f"Message sent to {to} | SID: {msg.sid}")
    except Exception as exc:
        logger.error(f"Failed to send WhatsApp to {to}: {exc}")

async def _process(user_phone: str, user_message: str) -> None:
    """Orquestra a conversa padrão delegando para o Agente Gemini."""
    reply = await agent.run(user_phone, user_message)
    await asyncio.to_thread(_send_whatsapp, user_phone, reply)

async def async_process_pdf_extract(user_phone: str, media_url: str, senha_fornecida: str = None):
    """Executado em segundo plano para descriptografar, ler e processar as metas do PDF."""
    try:
        # Adiciona autenticação básica para evitar Erro 401 ao baixar mídia protegida do Twilio.
        # Usando headers explícitos para maior robustez na autenticação.
        auth_string = f"{settings.twilio_account_sid}:{settings.twilio_auth_token}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        headers = {"Authorization": f"Basic {encoded_auth}"}
        response = requests.get(
            media_url, 
            headers=headers,
            timeout=15
        )
        response.raise_for_status() # Levanta um HTTPError para respostas de erro (4xx ou 5xx)

        # Verifica se o Content-Type é realmente um PDF
        content_type = response.headers.get("Content-Type", "").lower()
        if "application/pdf" not in content_type:
            logger.error(f"Downloaded file is not a PDF. Content-Type: {content_type}")
            _send_whatsapp(user_phone, "❌ O arquivo que você enviou não parece ser um PDF válido. Por favor, tente novamente com um arquivo PDF.")
            db.limpar_pdf_pendente(user_phone) # Limpa o estado pendente
            return
        pdf_file = io.BytesIO(response.content)
        
        reader = PdfReader(pdf_file)
        if reader.is_encrypted:
            if not senha_fornecida:
                # PDF protegido e ainda não temos a senha: pede agora
                db.salvar_pdf_aguardando_senha(user_phone, media_url, status="aguardando_senha")
                reply = (
                    "🏦 *Extrato do C6 Bank recebido!* O arquivo está protegido.\n\n"
                    "Por favor, *digite a senha do PDF* por aqui para eu processar "
                    "(geralmente os 4 ou 6 primeiros dígitos do seu CPF)."
                )
                _send_whatsapp(user_phone, reply)
                return

            if not reader.decrypt(senha_fornecida):
                _send_whatsapp(user_phone, "❌ Senha incorreta. O arquivo continua protegido. Envie o PDF novamente no chat para tentar outra vez.")
                db.limpar_pdf_pendente(user_phone)
                return

        # Desbloqueou o arquivo com sucesso? Reseta imediatamente o estado pendente no banco
        db.limpar_pdf_pendente(user_phone)
        
        # Junta o texto bruto extraído de todas as páginas do extrato
        texto_completo = "".join([page.extract_text() + "\n" for page in reader.pages])
            
        # Executa a tradução assíncrona da LLM de forma segura dentro da BackgroundTask
        json_padrao_contrato = await pdf_import.converter_texto_c6_para_json_padrao(texto_completo)
        
        # Roda a esteira de ingestão e calcula os estouros de limite (Budgets)
        diagnostico_final = ingestion.processar_ingestion_unificada(user_phone, json_padrao_contrato)
        _send_whatsapp(user_phone, diagnostico_final)
        
    except Exception as e:
        logger.error(f"Erro no processamento do PDF: {e}")
        db.limpar_pdf_pendente(user_phone)
        _send_whatsapp(user_phone, "❌ Tive um problema técnico ao tentar abrir seu PDF.")

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
        _send_whatsapp(user_phone, "❌ Tive um problema técnico ao iniciar sua sessão. Por favor, tente novamente.")
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