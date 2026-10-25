# app/rotina.py
# Aprende, por usuário, quando um restaurante é "rotina" (Alimentação) sem ninguém configurar nada.
# Parte de um padrão conservador e só AMPLIA a rotina onde o usuário, ao responder, foi consistente.
import logging
import time
from dataclasses import dataclass
from datetime import date

import app.database as db

logger = logging.getLogger(__name__)

LIMITE_PADRAO = 90.0                  # ponto de partida invisível, R$ por refeição
DIAS_PADRAO = frozenset({0, 1, 2, 3})  # segunda a quinta
DIAS_CANDIDATOS = (4, 5, 6)           # sexta, sábado, domingo: o que ele pode "ensinar"
MIN_EVIDENCIAS = 4                    # respostas consistentes antes de mudar qualquer coisa
SUB_ROTINA, SUB_LAZER = "Restaurante", "Restaurante lazer"
_NOMES = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


@dataclass(frozen=True)
class Perfil:
    limite: float = LIMITE_PADRAO
    dias: frozenset = DIAS_PADRAO

    def descricao(self) -> str:
        d = sorted(self.dias)
        if d and d == list(range(d[0], d[-1] + 1)) and len(d) > 2:
            dias = f"{_NOMES[d[0]]} a {_NOMES[d[-1]]}"
        else:
            dias = ", ".join(_NOMES[i] for i in d)
        return f"refeições de {dias} até R$ {self.limite:,.0f}".replace(",", ".") + " são rotina para você"


def calcular_perfil(respostas: list[tuple[float, date, bool]]) -> Perfil:
    """Função pura. respostas = (valor, data, foi_rotina) das perguntas que o usuário respondeu."""
    abaixo = [r for r in respostas if r[0] <= LIMITE_PADRAO]
    dias = set(DIAS_PADRAO)
    for d in DIAS_CANDIDATOS:   # dia inteiro consistente em "rotina" (valores dentro do limite)
        doDia = [r for r in abaixo if r[1].weekday() == d]
        if len(doDia) >= MIN_EVIDENCIAS and all(r[2] for r in doDia):
            dias.add(d)

    limite = LIMITE_PADRAO
    acima = sorted((r for r in respostas if r[0] > LIMITE_PADRAO and r[1].weekday() in dias), key=lambda r: r[0])
    prefixo = []
    for r in acima:             # sobe enquanto TODAS as respostas forem rotina; para no 1º "Restaurante lazer"
        if not r[2]:
            break
        prefixo.append(r)
    if len(prefixo) >= MIN_EVIDENCIAS:
        limite = max(r[0] for r in prefixo)
    return Perfil(limite=limite, dias=frozenset(dias))


def _ler_respostas(user_phone: str) -> list[tuple[float, date, bool]]:
    res = (db.get_db().table("finbot_expenses").select("amount, purchase_date, subcategory")
           .ilike("user_phone", db._q(user_phone)).eq("review_status", "confirmado")
           .in_("subcategory", [SUB_ROTINA, SUB_LAZER]).limit(1000).execute())
    out = []
    for r in res.data or []:
        try:
            out.append((abs(float(r["amount"])), date.fromisoformat(str(r["purchase_date"])[:10]),
                        r["subcategory"] == SUB_ROTINA))
        except (TypeError, ValueError, KeyError):
            continue
    return out


_CACHE: dict[str, tuple[float, Perfil]] = {}
_TTL = 300.0


def invalidar(user_phone: str) -> None:
    _CACHE.pop(user_phone, None)


def perfil(user_phone: str) -> Perfil:
    """Perfil aprendido do usuário (cache curto). Qualquer falha cai no padrão: nunca bloqueia a importação."""
    agora = time.monotonic()
    hit = _CACHE.get(user_phone)
    if hit and agora - hit[0] < _TTL:
        return hit[1]
    try:
        p = calcular_perfil(_ler_respostas(user_phone))
    except Exception as e:
        logger.warning(f"Perfil de rotina indisponível, usando o padrão: {e}")
        p = Perfil()
    _CACHE[user_phone] = (agora, p)
    return p
