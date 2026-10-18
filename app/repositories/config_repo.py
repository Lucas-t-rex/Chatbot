"""
Configurações que mudam em tempo de execução (tabela "configuracoes").

- modo_operacao: "analista" | "chat" | "ambos"  — trocado pelo app
- bot_ativo:     true | false                   — comando "bot on/off" do responsável
"""
import threading
import time

from app.core.config import config
from app.core.db import db, json_param

MODOS_VALIDOS = ("analista", "chat", "ambos")

# Nomes antigos continuam funcionando
_APELIDOS = {"auditor": "analista", "atendente": "chat"}

_CACHE_SEGUNDOS = 10
_cache = {"modo": None, "lido_em": 0.0}
_trava = threading.Lock()


def normalizar_modo(valor):
    v = (valor or "").strip().lower()
    v = _APELIDOS.get(v, v)
    return v if v in MODOS_VALIDOS else None


def obter(chave, padrao=None):
    linha = db.um("select valor from configuracoes where chave = %s", (chave,))
    return linha["valor"] if linha else padrao


def salvar(chave, valor):
    db.executar(
        """
        insert into configuracoes (chave, valor, atualizado_em) values (%s, %s, now())
        on conflict (chave) do update set valor = excluded.valor, atualizado_em = now()
        """,
        (chave, json_param(valor)),
    )


def obter_modo():
    """
    Modo atual, com cache de alguns segundos (é consultado a cada mensagem).
    Se o banco nunca teve modo salvo, usa o MODO_OPERACAO do config.
    Se o banco estiver fora do ar, usa o último valor conhecido.
    """
    agora = time.monotonic()
    with _trava:
        if _cache["modo"] and agora - _cache["lido_em"] < _CACHE_SEGUNDOS:
            return _cache["modo"]
    try:
        modo = normalizar_modo(obter("modo_operacao"))
        if modo is None:
            modo = normalizar_modo(config.MODO_OPERACAO) or "analista"
            salvar("modo_operacao", modo)
    except Exception as e:
        print(f"⚠️ [Modo] Não consegui ler o modo do banco ({e}). Mantendo o último conhecido.")
        modo = _cache["modo"] or normalizar_modo(config.MODO_OPERACAO) or "analista"
    with _trava:
        _cache["modo"] = modo
        _cache["lido_em"] = agora
    return modo


def definir_modo(modo):
    modo_ok = normalizar_modo(modo)
    if modo_ok is None:
        raise ValueError(f"Modo inválido: {modo}. Use: {', '.join(MODOS_VALIDOS)}")
    salvar("modo_operacao", modo_ok)
    with _trava:
        _cache["modo"] = modo_ok
        _cache["lido_em"] = time.monotonic()
    return modo_ok


def analista_ligado():
    return obter_modo() in ("analista", "ambos")


def chat_ligado():
    return obter_modo() in ("chat", "ambos")


def bot_ativo():
    try:
        return bool(obter("bot_ativo", True))
    except Exception:
        return True


def definir_bot_ativo(ativo):
    salvar("bot_ativo", bool(ativo))
