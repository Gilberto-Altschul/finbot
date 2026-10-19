# app/main.py
import logging
import asyncio
import base64
import hmac
import json
import os
from contextlib import asynccontextmanager
from collections import deque
import io
import time
import httpx
import requests
from pypdf import PdfReader, PdfWriter

from fastapi import FastAPI, Form, Header, HTTPException, BackgroundTasks, Request, Response
from twilio.rest import Client

import app.agent as agent
import app.database as db
import app.pdf_import as pdf_import
import app.ingestion as ingestion
import app.revisao as revisao
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


# O WhatsApp/Twilio rejeita mensagens com mais de 1600 caracteres (erro 400
# "exceeds the 1600 character limit"). Usamos uma margem de segurança abaixo
# disso — sobra espaço pro indicador de página "(1/3)" que adicionamos quando
# a mensagem precisa ser dividida em várias partes.
WHATSAPP_MAX_CHARS = 1500


def _dividir_mensagem(body: str, limite: int = WHATSAPP_MAX_CHARS) -> list[str]:
    """Divide uma mensagem longa em pedaços que cabem no limite do WhatsApp,
    tentando quebrar em fronteiras de parágrafo/linha (preserva formatação e
    emojis de cabeçalho de seção) antes de recorrer a corte por palavra."""
    if len(body) <= limite:
        return [body]

    partes: list[str] = []
    atual = ""
    # Primeiro tenta respeitar parágrafos (separados por linha em branco ou \n)
    for linha in body.split("\n"):
        candidato = f"{atual}\n{linha}" if atual else linha
        if len(candidato) <= limite:
            atual = candidato
            continue

        if atual:
            partes.append(atual)
            atual = ""

        # A própria linha é maior que o limite — corta por palavra
        if len(linha) > limite:
            palavras = linha.split(" ")
            pedaco = ""
            for palavra in palavras:
                cand = f"{pedaco} {palavra}" if pedaco else palavra
                if len(cand) > limite:
                    partes.append(pedaco)
                    pedaco = palavra
                else:
                    pedaco = cand
            atual = pedaco
        else:
            atual = linha

    if atual:
        partes.append(atual)

    total = len(partes)
    if total > 1:
        return [f"{p}\n\n_(parte {i+1}/{total})_" for i, p in enumerate(partes)]
    return partes


# ── Botões de resposta rápida (Twilio Content API) ───────────────────────────
# Na janela de 24h o WhatsApp aceita quick-reply (até 3 botões) sem aprovação de template.
# O template é criado uma vez (por nº de botões) e reaproveitado pelo friendly_name.
USAR_BOTOES = os.getenv("FINBOT_BOTOES", "1") != "0"
_CONTENT_SIDS: dict[int, str] = {}


def _content_sid(n_botoes: int) -> str:
    if n_botoes in _CONTENT_SIDS:
        return _CONTENT_SIDS[n_botoes]
    nome = f"finbot_revisao_{n_botoes}"
    auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    lista = requests.get("https://content.twilio.com/v1/Content", auth=auth, params={"PageSize": 200}, timeout=20)
    lista.raise_for_status()
    for c in lista.json().get("contents", []):
        if c.get("friendly_name") == nome:
            _CONTENT_SIDS[n_botoes] = c["sid"]
            return c["sid"]
    corpo = {
        "friendly_name": nome, "language": "pt_BR",
        "variables": {str(i): "x" for i in range(1, 2 + 2 * n_botoes)},
        "types": {"twilio/quick-reply": {
            "body": "🤔 {{1}}\n\n✏️ Ou digite outra categoria.",
            "actions": [{"title": "{{%d}}" % (2 + 2 * i), "id": "{{%d}}" % (3 + 2 * i)} for i in range(n_botoes)],
        }},
    }
    r = requests.post("https://content.twilio.com/v1/Content", auth=auth, json=corpo, timeout=20)
    r.raise_for_status()
    _CONTENT_SIDS[n_botoes] = r.json()["sid"]
    return _CONTENT_SIDS[n_botoes]


def _enviar_quick_reply(to: str, cabeca: str, botoes: list[tuple[str, str]]) -> None:
    variaveis = {"1": cabeca.replace("\n", " ")}
    for i, (bid, titulo) in enumerate(botoes):
        variaveis[str(2 + 2 * i)], variaveis[str(3 + 2 * i)] = titulo, bid
    _twilio.messages.create(from_=settings.twilio_whatsapp_number, to=to,
                            content_sid=_content_sid(len(botoes)), content_variables=json.dumps(variaveis))


def _send_whatsapp(to: str, body: str) -> None:
    """Envia texto; se o final da mensagem for uma pergunta de revisão, troca por botões."""
    if USAR_BOTOES and body and "ou digite uma palavra" in body:
        try:
            est = revisao.ler_estado(to)
            bts = revisao.botoes(est) if est else []
            if bts and body.endswith(est["texto"]):
                prefixo = body[: -len(est["texto"])].strip()
                if prefixo:
                    _send_whatsapp_texto(to, prefixo)
                try:
                    _enviar_quick_reply(to, est["cabeca"], bts)
                except Exception as exc:  # botão falhou: a pergunta em texto continua funcionando
                    logger.warning(f"Botões indisponíveis ({exc}); enviando pergunta em texto.")
                    _send_whatsapp_texto(to, est["texto"])
                return
        except Exception as exc:
            logger.warning(f"Falha ao preparar botões: {exc}")
    _send_whatsapp_texto(to, body)


def _send_whatsapp_texto(to: str, body: str) -> None:
    if not body or not str(body).strip():
        logger.error(f"Erro: corpo de mensagem vazio para {to}. Abortando envio.")
        return

    partes = _dividir_mensagem(body)
    if len(partes) > 1:
        logger.info(f"Mensagem para {to} excede {WHATSAPP_MAX_CHARS} chars ({len(body)} chars) — dividindo em {len(partes)} envios.")

    for indice, parte in enumerate(partes):
        enviado = False
        for attempt in range(3):
            try:
                msg = _twilio.messages.create(
                    from_=settings.twilio_whatsapp_number,
                    to=to,
                    body=parte,
                )
                logger.info(f"Message sent to {to} | SID: {msg.sid}" + (f" | parte {indice+1}/{len(partes)}" if len(partes) > 1 else ""))
                enviado = True
                break
            except Exception as exc:
                if attempt < 2:
                    logger.warning(f"Retentativa de envio WhatsApp ({attempt+1}/3): {exc}")
                    time.sleep(1)
                    continue
                logger.error(f"Falha definitiva ao enviar WhatsApp para {to}: {exc}")

        # Pequena pausa entre partes (só se a parte anterior saiu) para reduzir
        # o risco de o WhatsApp entregar/renderizar as mensagens fora de ordem.
        if enviado and indice < len(partes) - 1:
            time.sleep(0.4)


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


async def async_process_revisao(user_phone: str, resposta_usuario: str):
    """Aplica a resposta à pergunta atual da fila de revisão e envia a próxima."""
    try:
        reply = await asyncio.to_thread(revisao.responder, user_phone, resposta_usuario)
        await asyncio.to_thread(_send_whatsapp, user_phone, reply)
    except Exception as exc:
        logger.error(f"Erro ao processar revisão de {user_phone}: {exc}", exc_info=True)
        detalhe = f"\n\n🔧 {type(exc).__name__}: {str(exc)[:300]}" if os.getenv("FINBOT_DEBUG") == "1" else ""
        await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Não consegui aplicar essa categoria. Suas transações continuam salvas — tente de novo." + detalhe)


async def async_process_retomar_revisao(user_phone: str):
    """Comando "revisar": reabre a fila de pendentes quando o usuário quiser."""
    try:
        reply = await asyncio.to_thread(revisao.retomar, user_phone)
        await asyncio.to_thread(_send_whatsapp, user_phone, reply)
    except Exception as exc:
        logger.error(f"Erro ao retomar revisão de {user_phone}: {exc}", exc_info=True)


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
    MediaContentType0: str = Form(None),
    ButtonPayload: str = Form(None)
):
    if MessageSid in processed_messages:
        logger.warning(f"Mensagem duplicada ignorada: {MessageSid}")
        return Response(content="<Response/>", media_type="text/xml")

    processed_messages.append(MessageSid)
    user_phone = From.strip()
    user_message = Body.strip()

    # Toque em botão da revisão: o id vem em ButtonPayload ("r:<qid>:<n>"), o Body é só o título.
    if ButtonPayload and ButtonPayload.startswith("r:"):
        escolha = await asyncio.to_thread(revisao.resolver_payload, user_phone, ButtonPayload)
        if escolha is None:
            await asyncio.to_thread(_send_whatsapp, user_phone, "Essa pergunta já foi respondida 😉 Se quiser rever o que sobrou, digite *revisar*.")
            return Response(content="<Response/>", media_type="text/xml")
        user_message = escolha

    try:
        db._get_or_create_user_connection(user_phone)
    except Exception as exc:
        logger.error(f"Falha ao criar conexão para {user_phone}: {exc}")
        await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Tive um problema técnico ao iniciar sua sessão. Por favor, tente novamente.")
        return Response(content="<Response/>", media_type="text/xml")

    # ── CASO 1: PDF ENVIADO ───────────────────────────────────────────────────
    if NumMedia > 0 and MediaUrl0 and "pdf" in str(MediaContentType0).lower():
        logger.info(f"PDF recebido de {user_phone}.")
        # Inicia como aguardando_senha — se não for protegido, o processamento ignora a senha
        db.salvar_pdf_aguardando_senha(user_phone, MediaUrl0, status="aguardando_senha")
        await asyncio.to_thread(_send_whatsapp, user_phone, "🏦 *Extrato recebido!* Verificando o arquivo...")
        background_tasks.add_task(async_process_pdf_extract, user_phone, MediaUrl0)
        return Response(content="<Response/>", media_type="text/xml")

    # ── CASO 2: SENHA DE PDF PENDENTE (prioridade alta) ─────────────────────
    pending_pdf_info = db.obter_pdf_pendente(user_phone)
    if pending_pdf_info:
        pdf_pendente_url, pdf_status = pending_pdf_info
        if pdf_status == "aguardando_senha":
            logger.info(f"Senha recebida de {user_phone}.")
            await asyncio.to_thread(_send_whatsapp, user_phone, "🔑 Senha recebida! Descriptografando e processando...")
            background_tasks.add_task(async_process_pdf_extract, user_phone, pdf_pendente_url, senha_fornecida=user_message)
            return Response(content="<Response/>", media_type="text/xml")
        elif pdf_status == "processando":
            # Se parece senha (só números), guarda para usar depois
            if user_message.strip().isdigit() and len(user_message.strip()) >= 4:
                logger.info(f"Possível senha recebida durante processamento para {user_phone}. Guardando.")
                db.salvar_pdf_aguardando_senha(user_phone, pdf_pendente_url, status="aguardando_senha")
                await asyncio.to_thread(_send_whatsapp, user_phone, "🔑 Senha recebida! Processando seu extrato...")
                background_tasks.add_task(async_process_pdf_extract, user_phone, pdf_pendente_url, senha_fornecida=user_message)
            else:
                logger.info(f"PDF ainda processando para {user_phone}. Mensagem tratada como conversa normal.")
                background_tasks.add_task(_process, user_phone, user_message)
            return Response(content="<Response/>", media_type="text/xml")

    # ── CASO 2.5: COMANDO "revisar" — retoma a fila de pendentes quando o usuário quiser ──
    if revisao.eh_comando_revisar(user_message):
        background_tasks.add_task(async_process_retomar_revisao, user_phone)
        return Response(content="<Response/>", media_type="text/xml")

    # ── CASO 3: RESPOSTA À PERGUNTA DE REVISÃO ───────────────────────────────
    # As transações já estão salvas; isto só reclassifica. Conversa normal passa direto (CASO 4).
    if revisao.ler_estado(user_phone) and revisao.eh_resposta_de_revisao(user_message, ingestion._resolver_categoria):
        logger.info(f"Resposta de revisão recebida de {user_phone}: {user_message}")
        background_tasks.add_task(async_process_revisao, user_phone, user_message)
        return Response(content="<Response/>", media_type="text/xml")

    # ── CASO 4: MENSAGEM NORMAL ───────────────────────────────────────────────
    logger.info(f"Incoming message from {user_phone}: {user_message}")
    background_tasks.add_task(_process, user_phone, user_message)
    return Response(content="<Response/>", media_type="text/xml")

@app.post("/webhook/pluggy")
async def webhook_pluggy(request: Request, x_webhook_secret: str = Header(default="")):
    """Recebe eventos da Pluggy. Exige o header X-Webhook-Secret, cadastrado ao
    registrar o webhook na Pluggy (campo `headers` do POST /webhooks)."""
    segredo = os.environ.get("PLUGGY_WEBHOOK_SECRET", "")
    if not segredo:
        logger.error("PLUGGY_WEBHOOK_SECRET não configurado — webhook da Pluggy desativado.")
        raise HTTPException(status_code=503, detail="webhook não configurado")
    if not hmac.compare_digest(x_webhook_secret.encode(), segredo.encode()):
        logger.warning("Webhook da Pluggy rejeitado: segredo inválido.")
        raise HTTPException(status_code=401, detail="unauthorized")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="payload inválido")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload inválido")

    logger.info(f"Webhook da Pluggy recebido: {str(payload.get('event', 'desconhecido'))[:60]}")
    # TODO (etapa 1, passo 2): gravar o evento em pluggy_events e processar em segundo plano.
    return {"status": "success"}