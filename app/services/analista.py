"""
ANALISTA — junta as métricas (analista_metricas.py) com o banco:
  - recalcular_contato(): atualiza a conversa do contato a cada mensagem gravada
  - resumo_periodo():     números de um período (app e WhatsApp do dono)
  - texto_resumo():       a mensagem que vai pro WhatsApp do dono
  - gerar_excel():        a planilha do período (é ela que vai pro agente externo)

Os nomes das colunas do Excel estão documentados em docs/AGENTE_ANALISTA.md (na raiz do projeto).
Se mudar aqui, mude lá também.
"""
from datetime import datetime, timedelta, timezone

from app.core.config import config
from app.core.db import db
from app.repositories import mensagens_repo, sessoes_repo
from app.services.analista_metricas import (
    calcular_sessao,
    esperando_ha,
    formatar_duracao,
    separar_sessoes,
)

TZ = config.FUSO_HORARIO
HORARIO = config.HORARIO_ATENDIMENTO


# ---------------------------------------------------------------------------
# Números: o mesmo celular pode aparecer com 12 ou 13 dígitos (com/sem o 9)
# ---------------------------------------------------------------------------
def variantes_numero(numero):
    n = "".join(c for c in str(numero or "") if c.isdigit())
    if not n:
        return []
    variantes = {n}
    if len(n) == 13 and n.startswith("55"):
        variantes.add(n[:4] + n[5:])           # tira o 9
    elif len(n) == 12 and n.startswith("55"):
        variantes.add(n[:4] + "9" + n[4:])     # põe o 9
    return sorted(variantes)


def numeros_responsaveis():
    """Todos os responsáveis (config.RESPONSAVEIS), com e sem o 9."""
    return sorted({v for n in config.RESPONSAVEIS for v in variantes_numero(n)})


def _ignorados():
    """Números que não são leads (os donos/responsáveis)."""
    return numeros_responsaveis()


# ---------------------------------------------------------------------------
# Recalcular a conversa de um contato
# ---------------------------------------------------------------------------
def recalcular_contato(telefone):
    """
    Recalcula as métricas da conversa atual do contato. Lê só as mensagens a
    partir do início da última conversa registrada, então é barato.
    """
    if not telefone or telefone in _ignorados():
        return
    try:
        desde = sessoes_repo.ultimo_inicio(telefone)
        mensagens = mensagens_repo.listar_brutas(telefone, desde)
        sessoes = separar_sessoes(mensagens, config.GAP_NOVA_CONVERSA_HORAS)
        if not sessoes:
            return
        resultado = []
        for s in sessoes:
            nova = not mensagens_repo.existe_conversa_antes(telefone, s[0]["ts"])
            resultado.append(calcular_sessao(s, nova=nova, horario=HORARIO, tz=TZ))
        sessoes_repo.substituir_a_partir_de(telefone, desde, resultado)
    except Exception as e:
        print(f"⚠️ [Analista] Erro ao recalcular {telefone}: {e}")


# ---------------------------------------------------------------------------
# Resumo de um período
# ---------------------------------------------------------------------------
def _media_ponderada(sessoes, campo):
    soma = sum((s[campo] or 0) * s["respostas"] for s in sessoes if s[campo] is not None)
    qtd = sum(s["respostas"] for s in sessoes if s[campo] is not None)
    return int(round(soma / qtd)) if qtd else None


def _media_simples(valores):
    valores = [v for v in valores if v is not None]
    return int(round(sum(valores) / len(valores))) if valores else None


def clientes_sem_resposta(agora=None, dias=7):
    """
    Quem está esperando resposta AGORA. Vale só a conversa mais recente de cada
    contato (uma conversa antiga sem resposta não conta se ele já voltou a falar)
    e ela sai da lista quando a IA leu até a última mensagem e viu que a conversa
    terminou (dados.aguarda_resposta = false).

    'esperando_seg' é o tempo de relógio desde a última mensagem do cliente, a
    mesma que aparece em 'ultima_msg'. Olha os últimos 'dias' dias, independente
    do período — quem ficou sem resposta ontem continua na lista.
    """
    agora = agora or datetime.now(timezone.utc)
    excluir = _ignorados()
    filtro_excluir = "and s.telefone <> all(%s)" if excluir else ""
    params = [agora - timedelta(days=dias)] + ([excluir] if excluir else [])
    linhas = db.todos(
        f"""
        select u.*, coalesce(c.customer_name, c.sender_name) as nome,
               m.text as ultima_msg, m.ts as ultima_msg_ts,
               a.analisado_ate, a.dados ->> 'aguarda_resposta' as aguarda_resposta
        from (
            select distinct on (s.telefone) s.telefone, s.inicio, s.fim, s.aguardando_desde, s.nova
            from sessoes_atendimento s
            where s.fim >= %s {filtro_excluir}
            order by s.telefone, s.inicio desc
        ) u
        join contatos c on c.telefone = u.telefone
        left join lateral (
            select m.text, m.ts from mensagens m
             where m.telefone = u.telefone and m.origem = 'cliente' and m.ts >= u.inicio
             order by m.ts desc, m.id desc limit 1
        ) m on true
        left join analises_conversa a on a.telefone = u.telefone and a.sessao_inicio = u.inicio
        where u.aguardando_desde is not null
        """,
        params,
    )
    limite = config.LIMITE_SEM_RESPOSTA_MIN * 60
    pendentes = []
    for l in linhas:
        if (l["aguarda_resposta"] == "false" and l["analisado_ate"] is not None
                and l["analisado_ate"] >= l["fim"]):
            continue   # a IA viu que a conversa acabou (despedida, recado, "vou pensar"...)
        _, util = esperando_ha(l["aguardando_desde"], agora, HORARIO, TZ)
        desde_ultima = int((agora - (l["ultima_msg_ts"] or l["aguardando_desde"])).total_seconds())
        if desde_ultima >= limite:
            pendentes.append({
                "telefone": l["telefone"],
                "nome": l["nome"] or "Sem nome",
                "aguardando_desde": l["aguardando_desde"].astimezone(TZ).isoformat(),
                "esperando_seg": desde_ultima,
                "esperando_util_seg": util,
                "nova": l["nova"],
                "ultima_msg": (l["ultima_msg"] or "")[:120],
            })
    pendentes.sort(key=lambda p: p["esperando_seg"], reverse=True)
    return pendentes


def resumo_periodo(inicio, fim, agora=None):
    """Números das conversas com atividade entre inicio e fim."""
    agora = agora or datetime.now(timezone.utc)
    sessoes = sessoes_repo.do_periodo(inicio, fim, excluir=_ignorados())
    novas = [s for s in sessoes if s["nova"] and s["inicio"] >= inicio]
    respondidas_por = {"atendente": 0, "bot": 0, "ambos": 0, "ninguem": 0}
    for s in sessoes:
        respondidas_por[s["respondida_por"]] = respondidas_por.get(s["respondida_por"], 0) + 1
    pendentes = clientes_sem_resposta(agora)
    limite_seg = config.LIMITE_SEM_RESPOSTA_MIN * 60
    com_1a_resposta = [s for s in sessoes if s["primeira_resposta_util_seg"] is not None]

    return {
        "periodo": {
            "inicio": inicio.astimezone(TZ).isoformat(),
            "fim": fim.astimezone(TZ).isoformat(),
        },
        "conversas": len(sessoes),
        "msgs_cliente": sum(s["msgs_cliente"] for s in sessoes),
        "msgs_equipe": sum(s["msgs_atendente"] for s in sessoes),
        "com_primeira_resposta": len(com_1a_resposta),
        "respondidas_no_limite": sum(1 for s in com_1a_resposta if s["primeira_resposta_util_seg"] <= limite_seg),
        "novas": len(novas),
        "retomadas": len(sessoes) - len(novas),
        "iniciadas_pelo_cliente": sum(1 for s in sessoes if s["iniciada_por"] == "cliente"),
        "followups_da_equipe": sum(1 for s in sessoes if s["sinal_followup"]),
        "primeira_resposta_media_util_seg": _media_simples(s["primeira_resposta_util_seg"] for s in sessoes),
        "primeira_resposta_media_seg": _media_simples(s["primeira_resposta_seg"] for s in sessoes),
        "tempo_medio_resposta_util_seg": _media_ponderada(sessoes, "tempo_medio_resposta_util_seg"),
        "tempo_medio_resposta_seg": _media_ponderada(sessoes, "tempo_medio_resposta_seg"),
        "pior_espera_util_seg": max((s["tempo_max_resposta_util_seg"] or 0 for s in sessoes), default=None) or None,
        "terminaram_com_cliente": sum(1 for s in sessoes if s["ultima_msg_de"] == "cliente"),
        "respondidas_por": respondidas_por,
        "sem_resposta_total": len(pendentes),
        "sem_resposta": pendentes,
    }


# ---------------------------------------------------------------------------
# Mensagem do WhatsApp do dono
# ---------------------------------------------------------------------------
def janela_do_dia(agora=None):
    """De 00h de hoje (horário de SP) até agora."""
    agora = agora or datetime.now(timezone.utc)
    local = agora.astimezone(TZ)
    meia_noite = TZ.localize(datetime(local.year, local.month, local.day))
    return meia_noite, agora


def _telefone_legivel(telefone):
    """554491586345 / 5544991586345 -> (44) 99158-6345 (se não reconhecer, devolve como veio)."""
    n = "".join(c for c in str(telefone or "") if c.isdigit())
    if n.startswith("55") and len(n) in (12, 13):
        ddd, numero = n[2:4], n[4:]
        return f"({ddd}) {numero[:-4]}-{numero[-4:]}"
    return str(telefone or "")


def linhas_velocidade(r):
    """Bloco 'Velocidade da equipe' (números do sistema, sem IA)."""
    limite = config.LIMITE_SEM_RESPOSTA_MIN
    linhas = ["*Velocidade da equipe*"]
    if r["com_primeira_resposta"]:
        pct = round(100 * r["respondidas_no_limite"] / r["com_primeira_resposta"])
        linhas += [
            f"• 1ª resposta em média: {formatar_duracao(r['primeira_resposta_media_util_seg'])}",
            f"• Dentro de {limite} min: {r['respondidas_no_limite']} de {r['com_primeira_resposta']} ({pct}%)",
        ]
        if r["pior_espera_util_seg"]:
            linhas.append(f"• Maior espera: {formatar_duracao(r['pior_espera_util_seg'])}")
    sem_nenhuma = r["respondidas_por"].get("ninguem", 0)
    if sem_nenhuma:
        linhas.append(f"• {sem_nenhuma} conversa(s) sem nenhuma resposta da equipe")
    return linhas


def linhas_esperando(pendentes):
    """Bloco 'Esperando resposta agora' (ou o aviso de que não tem ninguém)."""
    if not pendentes:
        return ["✅ Nenhum cliente esperando resposta agora."]
    novos = sum(1 for p in pendentes if p["nova"])
    titulo = f"⚠️ *Esperando resposta agora ({len(pendentes)})*"
    if novos:
        titulo += f" - {novos} cliente(s) novo(s)"
    linhas = [titulo]
    for p in pendentes[: config.MAX_SEM_RESPOSTA_NO_RESUMO]:
        trecho = p["ultima_msg"].replace("\n", " ")
        if len(trecho) > 60:
            trecho = trecho[:57] + "..."
        etiqueta = "🆕 " if p["nova"] else ""
        linhas.append(
            f"• {etiqueta}{p['nome']} {_telefone_legivel(p['telefone'])} - "
            f"há {formatar_duracao(p['esperando_seg'])}"
            + (f': "{trecho}"' if trecho else "")
        )
    if len(pendentes) > config.MAX_SEM_RESPOSTA_NO_RESUMO:
        linhas.append(f"... e mais {len(pendentes) - config.MAX_SEM_RESPOSTA_NO_RESUMO}. Veja no app.")
    return linhas


def texto_resumo(resumo, rotulo):
    r = resumo
    linhas = [f"📊 *Atendimento - {rotulo}*", ""]

    if not r["conversas"]:
        linhas.append("Nenhuma conversa com clientes neste período.")
    else:
        linhas += [
            "*Movimento*",
            f"• {r['conversas']} {'conversa' if r['conversas'] == 1 else 'conversas'} "
            f"({r['novas']} {'cliente novo' if r['novas'] == 1 else 'clientes novos'} · "
            f"{r['retomadas']} {'cliente que voltou' if r['retomadas'] == 1 else 'clientes que voltaram'})",
            f"• {r['msgs_cliente'] + r['msgs_equipe']} mensagens: "
            f"{r['msgs_cliente']} dos clientes · {r['msgs_equipe']} da equipe",
        ]
        if r["followups_da_equipe"]:
            linhas.append(f"• A equipe chamou {r['followups_da_equipe']} cliente(s) antigo(s) de volta")

        linhas += [""] + linhas_velocidade(r)

    linhas.append("")
    linhas += linhas_esperando(r["sem_resposta"])
    return "\n".join(linhas)


def rotulo_do_horario(hora, agora=None):
    agora = agora or datetime.now(timezone.utc)
    data = agora.astimezone(TZ).strftime("%d/%m")
    if hora <= 12:
        return f"manhã de {data} (00h às {hora}h)"
    return f"dia {data} (00h às {hora}h)"


# ---------------------------------------------------------------------------
# Excel do período
# ---------------------------------------------------------------------------
COLUNAS_LEADS = [
    "Telefone", "Nome", "Início", "Fim", "Duração (min)", "Nova conversa", "Iniciada por",
    "Msgs cliente", "Msgs atendente", "Msgs bot",
    "1ª resposta (min, horário útil)", "1ª resposta (min, relógio)",
    "Tempo médio resposta (min, horário útil)", "Tempo médio resposta (min, relógio)",
    "Maior espera (min, horário útil)", "Respondida por", "Última msg de",
    "Sem resposta desde", "Sinal: saudação", "Sinal: pediu nome", "Sinal: chamou para ação",
    "Sinal: follow-up", "Média de caracteres da equipe", "Maior msg da equipe",
]

COLUNAS_CONVERSAS = ["Telefone", "Nome", "Data", "Hora", "Quem", "Tipo", "Mensagem"]

_QUEM = {"cliente": "Cliente", "atendente": "Atendente", "bot": "Bot", "sistema": "Sistema"}


def _min(seg):
    return round(seg / 60, 1) if seg is not None else None


def _sim_nao(v):
    if v is None:
        return "-"
    return "Sim" if v else "Não"


def _data_hora(dt):
    return dt.astimezone(TZ).strftime("%d/%m/%Y %H:%M") if dt else ""


def gerar_excel(inicio, fim, agora=None):
    """Planilha do período (bytes do .xlsx). O desenho fica em analista_excel.py."""
    from app.services import analista_excel   # import aqui: analista_excel importa este módulo
    return analista_excel.gerar(inicio, fim, agora)
