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
import app.titular as titular
import app.fechamento_cartao as fechamento_cartao
import app.seguranca as seguranca
import app.demo as demo_mod
import app.painel as painel
import app.audio_gasto as audio_gasto
import app.canal as canal
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
app.include_router(painel.router)   # painel web do FinFlow (/painel/...)
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


BANNER_DEMO = "🎭 _Modo demonstração · dados fictícios_\n\n"


def _enviar_quick_reply(to: str, cabeca: str, botoes: list[tuple[str, str]]) -> None:
    variaveis = {"1": cabeca.replace("\n", " ")}
    for i, (bid, titulo) in enumerate(botoes):
        variaveis[str(2 + 2 * i)], variaveis[str(3 + 2 * i)] = titulo, bid
    _twilio.messages.create(from_=settings.twilio_whatsapp_number, to=demo_mod.telefone_real(to),
                            content_sid=_content_sid(len(botoes)), content_variables=json.dumps(variaveis))


def _emitir_no_painel(to: str, body: str) -> None:
    """Chat do painel: a resposta vai para a tela, não para a Twilio. A pergunta de revisão vira botões, como no WhatsApp."""
    if not body or not str(body).strip():
        return
    if demo_mod.eh_telefone_demo(to):
        body = BANNER_DEMO + body
    try:
        est = revisao.ler_estado(to) if "ou digite uma palavra" in body else None
        bts = revisao.botoes(est) if est else []
        if bts and body.endswith(est["texto"]):
            prefixo = body[: -len(est["texto"])].strip()
            if prefixo:
                canal.emitir(prefixo)
            canal.emitir(est["cabeca"], bts)
            return
    except Exception as exc:
        logger.warning(f"Painel: falha ao preparar botões da revisão: {exc}")
    canal.emitir(body)


def _send_whatsapp(to: str, body: str) -> None:
    """Envia texto; se o final da mensagem for uma pergunta de revisão, troca por botões."""
    if canal.ativo():
        _emitir_no_painel(to, body)
        return
    if body and demo_mod.eh_telefone_demo(to):
        body = BANNER_DEMO + body   # modo demonstração: avisa em toda resposta que os dados são fictícios
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
    to = demo_mod.telefone_real(to)   # o usuário de demonstração é só uma chave: a mensagem vai ao número real
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


# ── Áudio: confirmação em um toque (quick-reply) ─────────────────────────────
_CONTENT_SIDS_CONFIRMA: dict[int, str] = {}


def _content_sid_confirma(n_botoes: int) -> str:
    """Template de quick-reply só com o texto (sem o 'digite outra categoria' da revisão). Criado uma vez e reaproveitado."""
    if n_botoes in _CONTENT_SIDS_CONFIRMA:
        return _CONTENT_SIDS_CONFIRMA[n_botoes]
    nome = f"finbot_confirma_{n_botoes}"
    auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    lista = requests.get("https://content.twilio.com/v1/Content", auth=auth, params={"PageSize": 200}, timeout=20)
    lista.raise_for_status()
    for c in lista.json().get("contents", []):
        if c.get("friendly_name") == nome:
            _CONTENT_SIDS_CONFIRMA[n_botoes] = c["sid"]
            return c["sid"]
    corpo = {
        "friendly_name": nome, "language": "pt_BR",
        "variables": {str(i): "x" for i in range(1, 2 + 2 * n_botoes)},
        "types": {"twilio/quick-reply": {
            "body": "🎙️ {{1}}",
            "actions": [{"title": "{{%d}}" % (2 + 2 * i), "id": "{{%d}}" % (3 + 2 * i)} for i in range(n_botoes)],
        }},
    }
    r = requests.post("https://content.twilio.com/v1/Content", auth=auth, json=corpo, timeout=20)
    r.raise_for_status()
    _CONTENT_SIDS_CONFIRMA[n_botoes] = r.json()["sid"]
    return _CONTENT_SIDS_CONFIRMA[n_botoes]


def _enviar_confirmacao_audio(to: str, pend: dict) -> None:
    """Uma mensagem só: texto + botões. Se os botões falharem, a mesma pergunta sai em texto ('sim'/'não')."""
    linha, botoes = audio_gasto.texto_confirmacao(pend)
    if canal.ativo():
        canal.emitir(f"🎙️ {linha}", botoes)
        return
    if USAR_BOTOES:
        try:
            variaveis = {"1": linha.replace("\n", " ")}
            for i, (bid, titulo) in enumerate(botoes):
                variaveis[str(2 + 2 * i)], variaveis[str(3 + 2 * i)] = titulo, bid
            _twilio.messages.create(from_=settings.twilio_whatsapp_number, to=demo_mod.telefone_real(to),
                                    content_sid=_content_sid_confirma(len(botoes)), content_variables=json.dumps(variaveis))
            return
        except Exception as exc:
            logger.warning(f"Botões de confirmação indisponíveis ({exc}); enviando em texto.")
    _send_whatsapp(to, audio_gasto.texto_confirmacao_sem_botoes(pend))


async def _processar_audio_bytes(user_phone: str, audio: bytes, content_type: str | None) -> None:
    """Transcreve e: gasto -> pede confirmação; outra coisa -> segue para o agente como texto. Levanta AudioIlegivel/erros: quem chama trata."""
    dados = await audio_gasto.extrair(audio, content_type)
    if audio_gasto.eh_gasto(dados):
        pend = await audio_gasto.preparar(user_phone, dados)
        await asyncio.to_thread(_enviar_confirmacao_audio, user_phone, pend)
        return
    # Não é um gasto (pergunta, comando...): segue como se tivesse sido digitado, mostrando o que foi ouvido.
    reply = await agent.run(user_phone, dados["transcricao"])
    await asyncio.to_thread(_send_whatsapp, user_phone, f"🎙️ _{dados['transcricao']}_\n\n{reply}")


async def async_process_audio(user_phone: str, media_url: str, content_type: str | None) -> None:
    """Baixa o áudio, transcreve e: gasto -> pede confirmação; outra coisa -> segue para o agente como texto."""
    try:
        if not seguranca.midia_twilio_ok(media_url):
            logger.error("Áudio ignorado: o endereço da mídia não é da Twilio.")
            await asyncio.to_thread(_send_whatsapp, user_phone, "⚠️ Não consegui abrir esse áudio. Mande de novo ou digite o gasto.")
            return
        auth_string = f"{settings.twilio_account_sid}:{settings.twilio_auth_token}"
        headers = {"Authorization": f"Basic {base64.b64encode(auth_string.encode()).decode()}"}
        resp = await http_client.get(media_url, headers=headers)
        resp.raise_for_status()
        audio = resp.content
        if len(audio) > audio_gasto.MAX_BYTES:
            await asyncio.to_thread(_send_whatsapp, user_phone, "🎙️ Esse áudio ficou longo demais. Mande um de até 1 minuto ou digite o gasto.")
            return
        logger.info(f"Áudio de {user_phone}: {len(audio)} bytes.")
        await _processar_audio_bytes(user_phone, audio, content_type or resp.headers.get("Content-Type"))
    except audio_gasto.AudioIlegivel as exc:
        logger.warning(f"Áudio não entendido para {user_phone}: {exc}")
        await asyncio.to_thread(_send_whatsapp, user_phone, "🎙️ Não consegui entender esse áudio. Pode repetir ou digitar o gasto?")
    except Exception as exc:
        logger.error(f"Erro ao processar áudio de {user_phone}: {exc}", exc_info=True)
        await asyncio.to_thread(_send_whatsapp, user_phone, "⚠️ Tive um problema com esse áudio. Pode digitar o gasto?")


async def _processar_pdf_conteudo(user_phone: str, conteudo: bytes, senha_fornecida: str | None, media_url: str | None,
                                  start_time: float, download_time: float) -> bool:
    """Lê o PDF (já baixado), pede/usa a senha e importa os lançamentos. Devolve True se ficou aguardando a senha.
    `media_url=None` = veio do chat do painel (sem arquivo guardado). Levanta erros: quem chama trata."""
    pdf_final_content = conteudo
    reader = PdfReader(io.BytesIO(pdf_final_content))

    if reader.is_encrypted:
        if not senha_fornecida:
            if media_url is None:      # chat do painel: o arquivo não fica guardado; o painel pede a senha e reenvia
                canal.emitir("🏦 *Extrato recebido!* O arquivo está protegido. Informe a senha do PDF "
                             "(geralmente os 4 ou 6 primeiros dígitos do seu CPF).", acao="senha_pdf")
                return False
            db.salvar_pdf_aguardando_senha(user_phone, media_url, status="aguardando_senha")
            reply = (
                "🏦 *Extrato recebido!* O arquivo está protegido.\n\n"
                "Por favor, *digite a senha do PDF* para eu processar "
                "(geralmente os 4 ou 6 primeiros dígitos do seu CPF)."
            )
            await asyncio.to_thread(_send_whatsapp, user_phone, reply)
            return True

        if not reader.decrypt(senha_fornecida):
            await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Senha incorreta. Envie o PDF novamente para tentar outra vez.")
            db.limpar_pdf_pendente(user_phone)
            return False

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
    try:   # o vencimento impresso na fatura vale mais que o dia padrão (12) usado quando o banco não informa
        dia_venc = pdf_import.dia_vencimento_do_pdf(await asyncio.to_thread(pdf_import._extrair_texto_pdf, pdf_final_content))
        if dia_venc:
            cfg = db.get_user_settings(user_phone) or {}
            if cfg.get("cartao_dia_vencimento") != dia_venc:
                db.save_user_settings(user_phone, dia_venc, None)   # só o vencimento; o corte não é alterado
                logger.info(f"Vencimento do cartão ajustado para o dia {dia_venc} (lido do PDF).")
    except Exception as e:
        logger.warning(f"Não consegui ler o vencimento do PDF ({e}); mantendo o dia configurado.")
    llm_time = time.perf_counter() - start_time - download_time

    # ingestion agora retorna (mensagem, lista_outros_ou_None)
    async def _andamento(feitos: int, total: int, n_novos: int):
        if feitos == 0:
            texto = f"📄 Encontrei *{n_novos}* lançamentos novos. Estou classificando, isso pode levar um ou dois minutos."
        else:
            texto = f"⏳ Classificando… {feitos} de {total} estabelecimentos."
        await asyncio.to_thread(_send_whatsapp, user_phone, texto)

    mensagem, transacoes_outros = await ingestion.processar_ingestion_unificada(
        user_phone, all_extracted_transactions, _andamento)

    total_time = time.perf_counter() - start_time
    logger.info(f"PDF Processed for {user_phone} in {total_time:.2f}s (DL: {download_time:.2f}s, LLM: {llm_time:.2f}s)")

    await asyncio.to_thread(_send_whatsapp, user_phone, mensagem)
    return False


async def async_process_pdf_extract(user_phone: str, media_url: str, senha_fornecida: str = None):
    pediu_senha = False      # só nesse caso o estado "aguardando senha" deve sobreviver ao fim do processamento
    try:
        if not seguranca.midia_twilio_ok(media_url):
            # Nunca manda as credenciais da Twilio para um endereço que não seja da Twilio.
            logger.error("PDF ignorado: o endereço da mídia não é da Twilio.")
            await asyncio.to_thread(_send_whatsapp, user_phone, "⚠️ Não consegui abrir esse arquivo. Envie o PDF de novo.")
            return
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

        pediu_senha = await _processar_pdf_conteudo(user_phone, response.content, senha_fornecida, media_url, start_time, download_time)
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
        detalhe = f"\n\n🔧 {type(exc).__name__}: {str(exc)[:300]}" if os.getenv("FINBOT_DEBUG") == "1" else ""
        await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Tive um problema técnico ao processar seu extrato. O arquivo parece correto: tente de novo em instantes." + detalhe)
    finally:
        # Qualquer saída (sucesso, erro, "já sincronizado") libera o estado; senão a próxima mensagem do cliente
        # (ex.: "Resumo") seria lida como senha do PDF e o extrato rodaria de novo, em loop.
        if not pediu_senha:
            db.limpar_pdf_pendente(user_phone)


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


@app.post("/cron/sync", status_code=202)
async def cron_sync(background_tasks: BackgroundTasks, x_cron_secret: str = Header(None)):
    """Sincronização agendada de todos os usuários (silenciosa). Chamada pelo cron do Railway.
    Exige o cabeçalho X-Cron-Secret igual à variável FINBOT_CRON_SECRET."""
    import app.cron_sync as cron_sync_mod
    segredo = os.getenv("FINBOT_CRON_SECRET")
    if not segredo:
        raise HTTPException(status_code=503, detail="FINBOT_CRON_SECRET não configurado.")
    if not x_cron_secret or not hmac.compare_digest(x_cron_secret.encode(), segredo.encode()):
        raise HTTPException(status_code=401, detail="Segredo inválido.")
    if cron_sync_mod.em_andamento():
        return {"status": "ja_em_andamento"}
    background_tasks.add_task(cron_sync_mod.executar_async)
    return {"status": "iniciado"}


async def _tratar_mensagem(real_phone: str, user_message: str, agenda, *, ButtonPayload: str | None = None,
                           NumMedia: int = 0, MediaUrl0: str | None = None, MediaContentType0: str | None = None) -> None:
    """Núcleo do chat: decide o que fazer com UMA mensagem (comandos, confirmações, revisão, agente). É chamado pelo webhook do
    WhatsApp (agenda = BackgroundTasks) e pelo chat do painel (agenda = _Agenda, que roda as tarefas na hora). As respostas saem por
    _send_whatsapp, que no painel vão para a tela (app.canal)."""
    # Modo demonstração: "modo demo" / "sair do demo" / "limpar demo" valem sempre, ligado ou não.
    cmd_demo = demo_mod.comando(user_message)
    if cmd_demo:
        resposta = await asyncio.to_thread(demo_mod.executar, real_phone, cmd_demo)
        await asyncio.to_thread(_send_whatsapp, real_phone, resposta)
        return
    await asyncio.to_thread(demo_mod.limpar_se_expirou, real_phone)   # demo vencido: desliga e apaga os dados fictícios
    user_phone = demo_mod.efetivo(real_phone)   # = real_phone, ou o usuário sintético se o modo demo estiver ligado

    # Toque em botão da revisão: o id vem em ButtonPayload ("r:<qid>:<n>"), o Body é só o título.
    if ButtonPayload and ButtonPayload.startswith("r:"):
        escolha = await asyncio.to_thread(revisao.resolver_payload, user_phone, ButtonPayload)
        if escolha is None:
            await asyncio.to_thread(_send_whatsapp, user_phone, "Essa pergunta já foi respondida 😉 Se quiser rever o que sobrou, digite *revisar*.")
            return
        user_message = escolha

    try:
        db._get_or_create_user_connection(user_phone)
    except Exception as exc:
        logger.error(f"Falha ao criar conexão para {user_phone}: {exc}")
        await asyncio.to_thread(_send_whatsapp, user_phone, "❌ Tive um problema técnico ao iniciar sua sessão. Por favor, tente novamente.")
        return

    # ── CASO 0.9: TOQUE NOS BOTÕES DA CONFIRMAÇÃO DE ÁUDIO ("g:ok", "g:no", "g:d", "g:c") ──
    if ButtonPayload and ButtonPayload.startswith("g:"):
        acao = audio_gasto.acao_do_botao(ButtonPayload)
        if acao:
            resposta = await audio_gasto.confirmar(user_phone, acao)
            await asyncio.to_thread(_send_whatsapp, user_phone, resposta)
            return

    # ── CASO 0.95: ÁUDIO — transcreve e pede confirmação (ou segue como pergunta) ──
    if NumMedia > 0 and MediaUrl0 and str(MediaContentType0).lower().startswith("audio/"):
        logger.info(f"Áudio recebido de {user_phone}.")
        agenda.add_task(async_process_audio, user_phone, MediaUrl0, MediaContentType0)
        return

    # ── CASO 1: PDF ENVIADO ───────────────────────────────────────────────────
    if NumMedia > 0 and MediaUrl0 and "pdf" in str(MediaContentType0).lower():
        logger.info(f"PDF recebido de {user_phone}.")
        # "processando": só vira "aguardando_senha" se o processamento descobrir que o PDF tem senha.
        db.salvar_pdf_aguardando_senha(user_phone, MediaUrl0, status="processando")
        await asyncio.to_thread(_send_whatsapp, user_phone, "🏦 *Extrato recebido!* Verificando o arquivo...")
        agenda.add_task(async_process_pdf_extract, user_phone, MediaUrl0)
        return

    # ── CASO 1.5: OUTRA MÍDIA SEM TEXTO (imagem, figurinha, vídeo) — avisa em vez de ficar mudo ──
    if NumMedia > 0 and MediaUrl0 and not user_message:
        await asyncio.to_thread(_send_whatsapp, user_phone, "Por enquanto eu entendo *texto*, *áudio* e *PDF* do extrato. 🙂 Conte o gasto por áudio ou digite.")
        return

    # ── CASO 2: SENHA DE PDF PENDENTE (prioridade alta) ─────────────────────
    pending_pdf_info = db.obter_pdf_pendente(user_phone)
    if pending_pdf_info:
        pdf_pendente_url, pdf_status = pending_pdf_info
        if pdf_status == "aguardando_senha":
            logger.info(f"Senha recebida de {user_phone}.")
            await asyncio.to_thread(_send_whatsapp, user_phone, "🔑 Senha recebida! Descriptografando e processando...")
            agenda.add_task(async_process_pdf_extract, user_phone, pdf_pendente_url, senha_fornecida=user_message)
            return
        elif pdf_status == "processando":
            # Se parece senha (só números), guarda para usar depois
            if user_message.strip().isdigit() and len(user_message.strip()) >= 4:
                logger.info(f"Possível senha recebida durante processamento para {user_phone}. Guardando.")
                db.salvar_pdf_aguardando_senha(user_phone, pdf_pendente_url, status="aguardando_senha")
                await asyncio.to_thread(_send_whatsapp, user_phone, "🔑 Senha recebida! Processando seu extrato...")
                agenda.add_task(async_process_pdf_extract, user_phone, pdf_pendente_url, senha_fornecida=user_message)
            else:
                logger.info(f"PDF ainda processando para {user_phone}. Mensagem tratada como conversa normal.")
                agenda.add_task(_process, user_phone, user_message)
            return

    # ── CASO 2.3: RESPOSTA DIGITADA À CONFIRMAÇÃO DE ÁUDIO ("sim", "não", "crédito"...) ──
    # Só vale se há confirmação pendente E a mensagem é exatamente uma dessas palavras; o resto segue o fluxo normal.
    if audio_gasto.tem_pendente(user_phone):
        acao = audio_gasto.interpretar_texto(user_message, audio_gasto.obter(user_phone))
        if acao:
            resposta = await audio_gasto.confirmar(user_phone, acao)
            await asyncio.to_thread(_send_whatsapp, user_phone, resposta)
            return

    # ── CASO 2.35: RESPOSTA AO AJUSTE DE FECHAMENTO DO CARTÃO ("sim", "só ajuste", "não") ──
    if fechamento_cartao.tem_pendente(user_phone):
        acao = fechamento_cartao.interpretar_resposta(user_message)
        if acao:
            resposta = await asyncio.to_thread(fechamento_cartao.confirmar, user_phone, acao)
            await asyncio.to_thread(_send_whatsapp, user_phone, resposta)
            return

    # ── CASO 2.36: COMANDO "o Itaú fecha dia 20 em dezembro" — ajusta o fechamento de uma fatura ──
    if fechamento_cartao.eh_comando(user_message):
        reply = await asyncio.to_thread(fechamento_cartao.responder, user_phone, user_message)
        await asyncio.to_thread(_send_whatsapp, user_phone, reply)
        return

    # ── CASO 2.4: COMANDO "meu nome é ..." — cadastra o nome do titular (Pix entre contas próprias) ──
    if titular.eh_comando(user_message):
        reply = await asyncio.to_thread(titular.responder, user_phone, user_message)
        await asyncio.to_thread(_send_whatsapp, user_phone, reply)
        return

    # ── CASO 2.45: COMANDO "painel" — manda o link de uso único do painel web ──
    if painel.eh_comando(user_message):
        await asyncio.to_thread(_send_whatsapp, real_phone, painel.mensagem_link(real_phone))
        return

    # ── CASO 2.5: COMANDO "revisar" — retoma a fila de pendentes quando o usuário quiser ──
    if revisao.eh_comando_revisar(user_message):
        agenda.add_task(async_process_retomar_revisao, user_phone)
        return

    # ── CASO 3: RESPOSTA À PERGUNTA DE REVISÃO ───────────────────────────────
    # As transações já estão salvas; isto só reclassifica. Conversa normal passa direto (CASO 4).
    if revisao.ler_estado(user_phone) and revisao.eh_resposta_de_revisao(user_message, ingestion._resolver_categoria):
        logger.info(f"Resposta de revisão recebida de {user_phone}: {user_message}")
        agenda.add_task(async_process_revisao, user_phone, user_message)
        return

    # ── CASO 4: MENSAGEM NORMAL ───────────────────────────────────────────────
    logger.info(f"Incoming message from {user_phone}: {user_message}")
    agenda.add_task(_process, user_phone, user_message)
    return


# ── Chat do painel: mesma lógica do WhatsApp, sem Twilio ─────────────────────
class _Agenda:
    """Faz o papel do BackgroundTasks no painel: guarda as tarefas e roda na hora, na mesma sessão do usuário."""
    def __init__(self) -> None:
        self.tarefas: list = []

    def add_task(self, fn, *args, **kwargs) -> None:
        self.tarefas.append((fn, args, kwargs))


MAX_PDF_PAINEL = 10 * 1024 * 1024


async def _painel_texto(real_phone: str, texto: str, payload: str | None = None) -> None:
    c = canal.caixa(real_phone)
    async with c.trava:
        with canal.usando(c):
            try:
                if painel.eh_comando(texto):
                    canal.emitir("Você já está no painel 🙂 Escreva aqui o que quiser: gastos, perguntas, comandos.")
                    return
                agenda = _Agenda()
                await _tratar_mensagem(real_phone, texto, agenda, ButtonPayload=payload)
                for fn, args, kwargs in agenda.tarefas:
                    await fn(*args, **kwargs)
            except Exception as exc:
                logger.error(f"Erro no chat do painel ({real_phone}): {exc}", exc_info=True)
                canal.emitir("⚠️ Tive um problema para responder agora. Tente de novo.")


async def _painel_audio(real_phone: str, audio: bytes, content_type: str | None) -> None:
    c = canal.caixa(real_phone)
    async with c.trava:
        with canal.usando(c):
            user_phone = demo_mod.efetivo(real_phone)
            try:
                db._get_or_create_user_connection(user_phone)
                await _processar_audio_bytes(user_phone, audio, content_type)
            except audio_gasto.AudioIlegivel as exc:
                logger.warning(f"Áudio do painel não entendido ({exc})")
                canal.emitir("🎙️ Não consegui entender esse áudio. Pode repetir ou digitar o gasto?")
            except Exception as exc:
                logger.error(f"Erro ao processar áudio do painel: {exc}", exc_info=True)
                canal.emitir("⚠️ Tive um problema com esse áudio. Pode digitar o gasto?")


async def _painel_pdf(real_phone: str, conteudo: bytes, senha: str | None) -> None:
    c = canal.caixa(real_phone)
    async with c.trava:
        with canal.usando(c):
            user_phone = demo_mod.efetivo(real_phone)
            if not conteudo.startswith(b"%PDF"):
                canal.emitir("❌ O arquivo enviado não parece ser um PDF válido. Envie o extrato original do banco.")
                return
            try:
                db._get_or_create_user_connection(user_phone)
                inicio = time.perf_counter()
                await _processar_pdf_conteudo(user_phone, conteudo, senha or None, None, inicio, 0.0)
            except ValueError as ve:
                logger.error(f"Falha na extração IA (painel): {ve}")
                canal.emitir("⚠️ No momento, não consegui analisar seu extrato devido a alta demanda na IA. Tente novamente em alguns instantes.")
            except Exception as exc:
                logger.error(f"Erro crítico no PDF do painel: {exc}", exc_info=True)
                canal.emitir("❌ Tive um problema técnico ao processar seu extrato. O arquivo parece correto: tente de novo em instantes.")


painel.registrar_chat(texto=_painel_texto, audio=_painel_audio, pdf=_painel_pdf)



@app.post("/webhook/whatsapp")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_twilio_signature: str = Header(default=""),
    From: str = Form(...),
    Body: str = Form(""),   # áudio/imagem sem legenda chegam com Body vazio; "Form(...)" recusaria (422) em vez de responder
    MessageSid: str = Form(...),
    NumMedia: int = Form(0),
    MediaUrl0: str = Form(None),
    MediaContentType0: str = Form(None),
    ButtonPayload: str = Form(None)
):
    # Só a Twilio pode chamar este endereço: confere a assinatura antes de qualquer coisa (inclusive antes de
    # registrar o MessageSid). Desligar só em emergência: FINBOT_VALIDAR_TWILIO=0.
    if seguranca.validacao_ligada():
        form = await request.form()
        url = seguranca.url_publica(str(request.url), dict(request.headers), "/webhook/whatsapp")
        if not seguranca.assinatura_valida(url, {k: v for k, v in form.items()}, x_twilio_signature, settings.twilio_auth_token):
            logger.warning(f"Webhook do WhatsApp rejeitado: assinatura inválida (url usada: {url}).")
            raise HTTPException(status_code=403, detail="assinatura inválida")

    if MessageSid in processed_messages:
        logger.warning(f"Mensagem duplicada ignorada: {MessageSid}")
        return Response(content="<Response/>", media_type="text/xml")

    processed_messages.append(MessageSid)
    real_phone = From.strip()
    user_message = Body.strip()

    await _tratar_mensagem(real_phone, user_message, background_tasks, ButtonPayload=ButtonPayload,
                           NumMedia=NumMedia, MediaUrl0=MediaUrl0, MediaContentType0=MediaContentType0)
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
