"""Sincronização agendada (cron): importa os lançamentos novos de TODOS os usuários, em silêncio.

- Não envia nenhuma mensagem de WhatsApp (fora da janela de 24h o WhatsApp recusaria, e no sandbox do
  Twilio nem há template). As dúvidas de categoria ficam na fila: o usuário vê com o comando "revisar".
- Roda numa thread própria, com loop de eventos próprio, para não travar o webhook do WhatsApp
  (o cliente da Pluggy usa `requests`, que é bloqueante).
- Variáveis de ambiente:
    FINBOT_CRON_SECRET            obrigatória; sem ela a rota /cron/sync responde 503
    FINBOT_CRON_ATUALIZAR_ITEM=1  opcional; pede à Pluggy para atualizar cada banco ANTES de ler
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

import app.database as db
import app.faturas as fat
import app.itens as itens

logger = logging.getLogger(__name__)

_trava = threading.Lock()
ESPERA_ITEM_SEGUNDOS = 90          # tempo máximo esperando a Pluggy terminar de atualizar um banco
SEM_DADOS = ("LOGIN_ERROR", "WAITING_USER_INPUT", "WAITING_USER_ACTION", "OUTDATED")   # precisa de ação humana


def em_andamento() -> bool:
    return _trava.locked()


def _mascara(telefone: str) -> str:
    return "…" + "".join(c for c in telefone if c.isdigit())[-4:]


def telefones_com_banco() -> list[str]:
    """Usuários que têm banco vinculado (ou que são o dono, com descoberta automática)."""
    from app.utils import descriptografar_telefone

    vistos: dict[str, None] = {}
    linhas = db.get_db().table("finbot_user_connections").select("user_phone").execute().data or []
    for r in linhas:
        bruto = r.get("user_phone")
        if not bruto:
            continue
        try:
            fone = descriptografar_telefone(bruto)
        except Exception as e:
            logger.warning(f"Cron: telefone ilegível, ignorado ({e}).")
            continue
        if fone in vistos:
            continue
        if fat._pode_descobrir_itens(fone) or itens.ler_ids(fone):
            vistos[fone] = None
    return list(vistos)


async def _atualizar_itens(pluggy, item_ids: list[str]) -> None:
    """Pede à Pluggy para buscar dados novos no banco e espera terminar (ou desiste após o prazo)."""
    for item_id in item_ids:
        try:
            await pluggy.atualizar_item(item_id)
        except Exception as e:
            logger.warning(f"Cron: não consegui pedir atualização do item {item_id[:8]}… ({e}).")
            continue
        for _ in range(ESPERA_ITEM_SEGUNDOS // 5):
            await asyncio.sleep(5)
            try:
                status = await pluggy.verificar_status_sincronizacao(item_id)
            except Exception:
                break
            if status == "UPDATED" or status in SEM_DADOS:
                if status in SEM_DADOS:
                    logger.warning(f"Cron: item {item_id[:8]}… precisa de ação do titular (status {status}).")
                break


async def sincronizar_usuario(pluggy, telefone: str) -> tuple[int, int]:
    """Devolve (novos, para_revisar). Erros de um banco não derrubam os outros."""
    import app.dados_banco as dbk

    if os.getenv("FINBOT_CRON_ATUALIZAR_ITEM") == "1":
        ids = list(dict.fromkeys(itens.ler_ids(telefone)))
        if ids:
            await _atualizar_itens(pluggy, ids)
    try:
        await dbk.atualizar(lambda: pluggy, telefone)       # saldos, cartões e faturas
    except Exception as e:
        logger.warning(f"Cron {_mascara(telefone)}: não atualizei contas/faturas ({e}).")
    contas = dbk.ler_contas(telefone)
    novos = revisar = 0
    for c in contas:
        try:
            _, rows = await pluggy.sync_user_transactions(telefone, c["id"], c["item_id"], iniciar_revisao=False)
        except Exception as e:
            logger.error(f"Cron {_mascara(telefone)}: falha em {c.get('nome')} ({e}).")
            continue
        if rows:
            novos += len(rows)
            revisar += sum(1 for x in rows if x.get("review_status") == "revisar")
    return novos, revisar


async def _rodar() -> dict:
    from app.pluggy_service import PluggyService

    pluggy = PluggyService()                                # autentica uma vez
    resumo = {"usuarios": 0, "novos": 0, "revisar": 0, "falhas": 0}
    for fone in telefones_com_banco():
        resumo["usuarios"] += 1
        try:
            n, r = await sincronizar_usuario(pluggy, fone)
        except Exception as e:
            resumo["falhas"] += 1
            logger.error(f"Cron {_mascara(fone)}: falhou ({e}).", exc_info=True)
            continue
        resumo["novos"] += n
        resumo["revisar"] += r
        logger.info(f"Cron {_mascara(fone)}: {n} novos, {r} para revisar.")
    return resumo


def executar() -> dict | None:
    """Ponto de entrada (síncrono, próprio para BackgroundTasks/thread). Só uma execução por vez."""
    if not _trava.acquire(blocking=False):
        logger.info("Cron: já há uma sincronização em andamento; ignorando.")
        return None
    try:
        logger.info("Cron: iniciando sincronização de todos os usuários.")
        resumo = asyncio.run(_rodar())
        logger.info(f"Cron: concluído — {resumo}.")
        return resumo
    except Exception as e:
        logger.error(f"Cron: erro geral ({e}).", exc_info=True)
        return None
    finally:
        _trava.release()


async def executar_async() -> dict | None:
    """Versão para rodar NO MESMO event loop do servidor (BackgroundTasks com função async).
    Necessário porque o cliente do Gemini guarda a sessão HTTP do loop onde foi criado;
    asyncio.run() em outra thread cria um loop novo e quebra com 'attached to a different loop'."""
    if not _trava.acquire(blocking=False):
        logger.info("Cron: já há uma sincronização em andamento; ignorando.")
        return None
    try:
        logger.info("Cron: iniciando sincronização de todos os usuários.")
        resumo = await _rodar()
        logger.info(f"Cron: concluído — {resumo}.")
        return resumo
    except Exception as e:
        logger.error(f"Cron: erro geral ({e}).", exc_info=True)
        return None
    finally:
        _trava.release()
