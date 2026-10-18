"""
Análises da IA (tabela analises_conversa): uma linha por conversa.

Uma conversa é "pendente" quando nunca foi analisada ou quando ganhou mensagem
depois do horário que a IA leu (analisado_ate < fim da sessão). É a "flag" de
lida: ela se desatualiza sozinha quando chega mensagem nova.
"""
from app.core.db import db, json_param

MAX_TENTATIVAS = 3

# Campos escalares gravados direto em colunas (o restante vai no JSON "dados").
CAMPOS = (
    "eh_lead", "tipo_contato", "genero", "interesse", "desfecho", "detalhe_desfecho",
    "motivo_perda", "temperatura", "precisa_atencao", "motivo_atencao",
    "nota_final", "nota_intuito_venda", "nota_ligacao", "propos_ligacao",
    "resumo", "ponto_forte", "pode_melhorar", "proxima_acao", "mensagem_sugerida",
)


def pendentes(desde, excluir=None, limite=80):
    """
    Conversas com atividade a partir de 'desde' que precisam (re)análise: têm
    mensagem do cliente e (nunca foram analisadas OU ganharam mensagem nova).
    Quem falhou 3 vezes só volta depois de 12h, para não gastar à toa.
    """
    excluir = [e for e in (excluir or []) if e]
    filtro_excluir = "and s.telefone <> all(%s)" if excluir else ""
    params = [desde] + ([excluir] if excluir else []) + [limite]
    return db.todos(
        f"""
        select s.*,
               coalesce(c.customer_name, c.sender_name) as nome,
               a.id as analise_id, a.versao, a.analisado_ate
        from sessoes_atendimento s
        join contatos c on c.telefone = s.telefone
        left join analises_conversa a
               on a.telefone = s.telefone and a.sessao_inicio = s.inicio
        where s.fim >= %s
          and s.msgs_cliente > 0
          {filtro_excluir}
          and (a.id is null or a.analisado_ate is null or a.analisado_ate < s.fim)
          and (a.id is null or a.tentativas < {MAX_TENTATIVAS}
               or a.analisado_em < now() - interval '12 hours')
        order by s.fim
        limit %s
        """,
        params,
    )


def salvar(telefone, sessao_inicio, analisado_ate, campos, dados, provedor, modelo, tokens_in, tokens_out):
    """Grava (ou regrava) a análise de uma conversa. Soma 1 em 'versao' a cada nova leitura."""
    colunas = ", ".join(CAMPOS)
    marcadores = ", ".join(["%s"] * len(CAMPOS))
    atualizacoes = ", ".join(f"{c} = excluded.{c}" for c in CAMPOS)
    valores = [campos.get(c) for c in CAMPOS]
    db.executar(
        f"""
        insert into analises_conversa
            (telefone, sessao_inicio, analisado_ate, analisado_em, status, erro, tentativas,
             provedor, modelo, tokens_entrada, tokens_saida, dados, {colunas})
        values (%s, %s, %s, now(), 'ok', null, 0, %s, %s, %s, %s, %s, {marcadores})
        on conflict (telefone, sessao_inicio) do update set
            analisado_ate = excluded.analisado_ate, analisado_em = now(),
            versao = analises_conversa.versao + 1,
            status = 'ok', erro = null, tentativas = 0,
            provedor = excluded.provedor, modelo = excluded.modelo,
            tokens_entrada = analises_conversa.tokens_entrada + excluded.tokens_entrada,
            tokens_saida = analises_conversa.tokens_saida + excluded.tokens_saida,
            dados = excluded.dados, {atualizacoes}
        """,
        [telefone, sessao_inicio, analisado_ate, provedor, modelo, tokens_in, tokens_out,
         json_param(dados), *valores],
    )


def registrar_erro(telefone, sessao_inicio, erro):
    """
    Anota a falha. Se já existe análise boa dessa conversa, ela é mantida (só soma
    a tentativa); se não existe, nasce com status 'erro'.
    """
    db.executar(
        """
        insert into analises_conversa (telefone, sessao_inicio, status, erro, tentativas, analisado_em, versao)
        values (%s, %s, 'erro', %s, 1, now(), 0)
        on conflict (telefone, sessao_inicio) do update set
            erro = excluded.erro, tentativas = analises_conversa.tentativas + 1, analisado_em = now()
        """,
        (telefone, sessao_inicio, (erro or "")[:500]),
    )


def da_sessao(telefone, sessao_inicio):
    return db.um(
        "select * from analises_conversa where telefone = %s and sessao_inicio = %s",
        (telefone, sessao_inicio),
    )


def anterior_do_contato(telefone, antes_de):
    """A análise da conversa anterior desse contato (dá continuidade: 'ontem pediu preço...')."""
    return db.um(
        """
        select * from analises_conversa
        where telefone = %s and sessao_inicio < %s and status = 'ok'
        order by sessao_inicio desc limit 1
        """,
        (telefone, antes_de),
    )


def do_periodo(inicio, fim, excluir=None):
    """
    Conversas com atividade em [inicio, fim) que já têm análise boa, junto com
    os números da sessão. Base dos resumos e do Excel.
    """
    excluir = [e for e in (excluir or []) if e]
    filtro_excluir = "and s.telefone <> all(%s)" if excluir else ""
    params = [fim, inicio] + ([excluir] if excluir else [])
    return db.todos(
        f"""
        select a.*, s.inicio, s.fim, s.nova, s.iniciada_por, s.msgs_cliente, s.msgs_atendente,
               s.primeira_resposta_util_seg, s.tempo_medio_resposta_util_seg,
               s.aguardando_desde, s.sinal_followup,
               coalesce(c.customer_name, c.sender_name) as nome
        from analises_conversa a
        join sessoes_atendimento s on s.telefone = a.telefone and s.inicio = a.sessao_inicio
        join contatos c on c.telefone = a.telefone
        where a.status = 'ok' and s.inicio < %s and s.fim >= %s {filtro_excluir}
        order by s.inicio
        """,
        params,
    )


def em_aberto_anteriores(inicio, dias=7, excluir=None):
    """Conversas de dias anteriores a 'inicio' (até 'dias' dias) ainda em andamento."""
    excluir = [e for e in (excluir or []) if e]
    filtro_excluir = "and a.telefone <> all(%s)" if excluir else ""
    params = [inicio, inicio, dias] + ([excluir] if excluir else [])
    linha = db.um(
        f"""
        select count(*) as total
        from analises_conversa a
        join sessoes_atendimento s on s.telefone = a.telefone and s.inicio = a.sessao_inicio
        where a.status = 'ok' and a.desfecho = 'em_andamento'
          and s.fim < %s and s.fim >= %s::timestamptz - make_interval(days => %s)
          {filtro_excluir}
        """,
        params,
    )
    return linha["total"] if linha else 0
