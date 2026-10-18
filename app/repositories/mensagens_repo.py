"""
Histórico de mensagens (era o array "history" dentro do documento do Mongo).

Cada mensagem guarda:
  - role:   'user' | 'assistant' | 'system'  (o formato que a IA e o app já usam)
  - origem: 'cliente' | 'atendente' | 'bot' | 'sistema'  (quem falou de verdade)
  - tipo:   'texto' | 'audio' | 'midia' | 'ferramenta' | 'sistema'
"""
from datetime import datetime, timezone

from app.core.config import config
from app.core.db import db

_PREFIXOS_AUDIO = ("[áudio", "[audio", "[transcrição de áudio", "[transcricao de audio")
_PREFIXOS_MIDIA = (
    "[imagem", "[vídeo", "[video", "[documento", "[figurinha", "[localização",
    "[localizacao", "[contato enviado",
)


def inferir_tipo(texto, role="assistant"):
    """Classifica a mensagem pelo conteúdo, para o histórico e para o app."""
    if role == "system":
        return "sistema"
    t = (texto or "").strip().lower()
    if t.startswith("chamando função") or t.startswith("chamando funcao"):
        return "ferramenta"
    if t.startswith("[human_intervention]") or t.startswith("sistema:"):
        return "sistema"
    if t.startswith(_PREFIXOS_AUDIO):
        return "audio"
    if t.startswith(_PREFIXOS_MIDIA):
        return "midia"
    return "texto"


def origem_padrao(role):
    """Origem quando quem chama não informa: user = cliente, assistant = bot."""
    return {"user": "cliente", "assistant": "bot", "system": "sistema"}.get(role, "sistema")


def adicionar(telefone, role, texto, origem=None, tipo=None, msg_id=None, ts=None):
    """
    Grava uma mensagem (e cria o contato se for o primeiro contato).
    Devolve True se gravou, False se o msg_id já existia (evento duplicado).
    """
    origem = origem or origem_padrao(role)
    tipo = tipo or inferir_tipo(texto, role)
    ts = ts or datetime.now(timezone.utc)
    with db.conexao() as conn:
        conn.execute(
            "insert into contatos (telefone) values (%s) on conflict (telefone) do nothing",
            (telefone,),
        )
        linha = conn.execute(
            """
            insert into mensagens (telefone, role, origem, tipo, text, msg_id, ts)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (msg_id) do nothing
            returning id
            """,
            (telefone, role, origem, tipo, texto, msg_id, ts),
        ).fetchone()
    return linha is not None


def msg_id_existe(msg_id):
    if not msg_id:
        return False
    return db.um("select 1 from mensagens where msg_id = %s", (msg_id,)) is not None


def _como_historico(linha):
    """Converte a linha no formato de dict que o código antigo usava no Mongo."""
    return {
        "role": linha["role"],
        "text": linha["text"],
        "ts": linha["ts"].astimezone(config.FUSO_HORARIO).isoformat(),
        "msg_id": linha["msg_id"],
        "origem": linha["origem"],
        "tipo": linha["tipo"],
    }


def historico(telefone, ultimas=None):
    """Histórico em ordem cronológica, no formato antigo ({role, text, ts, ...})."""
    if ultimas:
        linhas = db.todos(
            """
            select * from (
                select * from mensagens where telefone = %s order by ts desc, id desc limit %s
            ) t order by ts, id
            """,
            (telefone, ultimas),
        )
    else:
        linhas = db.todos(
            "select * from mensagens where telefone = %s order by ts, id",
            (telefone,),
        )
    return [_como_historico(l) for l in linhas]


def historico_desde(telefone, depois_de):
    """Mensagens com ts estritamente depois de 'depois_de' (datetime ou None = todas)."""
    if depois_de is None:
        return historico(telefone)
    linhas = db.todos(
        "select * from mensagens where telefone = %s and ts > %s order by ts, id",
        (telefone, depois_de),
    )
    return [_como_historico(l) for l in linhas]


# ---------------------------------------------------------------------------
# Leituras "cruas" (datetime de verdade) para o Analista
# ---------------------------------------------------------------------------
def listar_brutas(telefone, desde=None):
    if desde is None:
        return db.todos(
            "select * from mensagens where telefone = %s order by ts, id", (telefone,)
        )
    return db.todos(
        "select * from mensagens where telefone = %s and ts >= %s order by ts, id",
        (telefone, desde),
    )


def existe_conversa_antes(telefone, ts):
    """O contato já tinha conversado (cliente/equipe) antes deste momento?"""
    return db.um(
        """
        select 1 from mensagens
        where telefone = %s and ts < %s
          and origem <> 'sistema' and tipo not in ('ferramenta', 'sistema')
        limit 1
        """,
        (telefone, ts),
    ) is not None


def do_periodo(inicio, fim, excluir=None):
    """Todas as mensagens do período (para o Excel), com o nome do contato."""
    excluir = [e for e in (excluir or []) if e]
    filtro_excluir = "and m.telefone <> all(%s)" if excluir else ""
    params = [inicio, fim] + ([excluir] if excluir else [])
    return db.todos(
        f"""
        select m.*, coalesce(c.customer_name, c.sender_name) as nome
        from mensagens m join contatos c on c.telefone = m.telefone
        where m.ts >= %s and m.ts < %s {filtro_excluir}
        order by m.telefone, m.ts, m.id
        """,
        params,
    )
