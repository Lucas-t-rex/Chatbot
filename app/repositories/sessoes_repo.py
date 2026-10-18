"""
Sessões de atendimento: a saída do Analista (uma linha por conversa).
"""
from app.core.db import db

COLUNAS_METRICAS = (
    "inicio", "fim", "duracao_seg", "iniciada_por", "nova",
    "msgs_cliente", "msgs_atendente", "msgs_bot", "respostas",
    "primeira_resposta_seg", "primeira_resposta_util_seg",
    "tempo_medio_resposta_seg", "tempo_medio_resposta_util_seg", "tempo_max_resposta_util_seg",
    "respondida_por", "ultima_msg_de", "aguardando_desde",
    "sinal_saudacao", "sinal_pediu_nome", "sinal_cta", "sinal_followup",
    "media_caracteres_equipe", "maior_msg_equipe",
)


def ultimo_inicio(telefone):
    linha = db.um(
        "select max(inicio) as inicio from sessoes_atendimento where telefone = %s", (telefone,)
    )
    return linha["inicio"] if linha else None


def substituir_a_partir_de(telefone, desde, sessoes):
    """
    Regrava as sessões do contato que começam em 'desde' ou depois.
    Roda numa transação com trava por contato: duas mensagens chegando juntas
    pro mesmo número não geram sessão duplicada.
    """
    colunas = ", ".join(COLUNAS_METRICAS)
    marcadores = ", ".join(["%s"] * len(COLUNAS_METRICAS))
    atualizacoes = ", ".join(f"{c} = excluded.{c}" for c in COLUNAS_METRICAS if c != "inicio")
    with db.conexao() as conn:
        with conn.transaction():
            conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (telefone,))
            if desde is not None:
                conn.execute(
                    "delete from sessoes_atendimento where telefone = %s and inicio >= %s",
                    (telefone, desde),
                )
            for s in sessoes:
                conn.execute(
                    f"""
                    insert into sessoes_atendimento (telefone, {colunas}, atualizado_em)
                    values (%s, {marcadores}, now())
                    on conflict (telefone, inicio) do update set {atualizacoes}, atualizado_em = now()
                    """,
                    [telefone, *[s[c] for c in COLUNAS_METRICAS]],
                )


def do_periodo(inicio, fim, excluir=None):
    """
    Sessões que tiveram atividade dentro de [inicio, fim), com o nome do contato.
    """
    excluir = [e for e in (excluir or []) if e]
    filtro_excluir = "and s.telefone <> all(%s)" if excluir else ""
    params = [fim, inicio] + ([excluir] if excluir else [])
    return db.todos(
        f"""
        select s.*, c.customer_name, c.sender_name,
               coalesce(c.customer_name, c.sender_name) as nome
        from sessoes_atendimento s join contatos c on c.telefone = s.telefone
        where s.inicio < %s and s.fim >= %s {filtro_excluir}
        order by s.inicio
        """,
        params,
    )
