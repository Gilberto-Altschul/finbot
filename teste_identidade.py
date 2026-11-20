"""Testes do apelido de telefone (app/identidade.py). Rode na pasta do projeto: python teste_identidade.py"""
import asyncio
import os

import app.demo as demo
import app.identidade as ident

ELA, ELE, OUTRO = "whatsapp:+5511999990000", "whatsapp:+5511888880000", "whatsapp:+5511777770000"


def com_alias(valor):
    if valor is None:
        os.environ.pop("FINFLOW_ALIAS", None)
    else:
        os.environ["FINFLOW_ALIAS"] = valor


def run():
    # 1) sem variável: igual a antes
    com_alias(None)
    assert ident.efetivo(ELA) == ELA and ident.efetivo(ELE) == ELE
    ident.definir_origem(ELA)
    assert ident.destino(ELE) == ELE

    # 2) com apelido: ela usa os dados dele; a resposta volta para ela
    com_alias(f"{ELA}={ELE}")
    assert ident.efetivo(ELA) == ELE and ident.efetivo(ELE) == ELE and ident.efetivo(OUTRO) == OUTRO
    ident.definir_origem(ELA)
    assert ident.destino(ELE) == ELA          # dados dele, escrito por ela -> responde a ela
    assert ident.destino(ELA) == ELA
    assert ident.destino(OUTRO) == OUTRO      # outro número nunca é redirecionado
    ident.definir_origem(ELE)
    assert ident.destino(ELE) == ELE          # o titular recebe no dele
    ident.definir_origem(OUTRO)
    assert ident.destino(ELE) == ELE          # quem não é apelido nunca desvia
    ident.definir_origem(None)
    assert ident.destino(ELE) == ELE          # sem contexto (cron, alertas): vai ao titular

    # 3) formatos livres e vários pares
    com_alias("+55 (11) 99999-0000 = 5511888880000 ; whatsapp:+5511555550000=whatsapp:+5511888880000")
    assert ident.efetivo(ELA) == ELE and ident.efetivo("whatsapp:+5511555550000") == ELE

    # 4) entradas inválidas, auto-referência e cadeia são ignoradas
    com_alias(f"lixo, {ELA}={ELA}, {ELA}={ELE}, {ELE}={OUTRO}")   # ELE->OUTRO torna ELE apelido: a cadeia inteira cai
    assert ident.efetivo(ELA) == ELA and ident.efetivo(ELE) == OUTRO

    # 5) demo tem prioridade sobre o apelido, e a mensagem do demo vai ao número real de quem ligou
    com_alias(f"{ELA}={ELE}")
    demo.ativar(ELA)
    try:
        assert demo.eh_telefone_demo(ident.efetivo(ELA))
        assert ident.destino(demo.telefone_demo(ELA)) == ELA
    finally:
        demo.desativar(ELA)
    assert ident.efetivo(ELA) == ELE

    # 6) cada requisição tem o seu contexto (duas pessoas ao mesmo tempo não se misturam), inclusive em threads
    async def atender(quem):
        ident.definir_origem(quem)
        await asyncio.sleep(0.01)
        return await asyncio.to_thread(ident.destino, ELE)

    async def dois():
        return await asyncio.gather(asyncio.create_task(atender(ELA)), asyncio.create_task(atender(ELE)))
    assert asyncio.run(dois()) == [ELA, ELE]
    com_alias(None)
    print("ok: identidade")


if __name__ == "__main__":
    run()
