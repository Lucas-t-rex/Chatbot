"""
RELATÓRIOS DO DONO — juntam os números do sistema (analista.py) com o que a IA
concluiu de cada conversa (analises_conversa) e viram:
  - o resumo do dia (um só, às 22h; compara com o de ontem) -> montar_diario()
  - o resumo da semana (sábado 18h)                         -> montar_semanal()

O bloco "📘 Script da academia" (aderência, "X de Y" por passo, termômetro,
funil) é calculado aqui em agregar_script(), a partir do que a IA marcou em
cada conversa (tecnicas_aplicadas = fez; tecnicas_a_reforcar = tinha a chance
e faltou; o que não teve chance não entra na conta).

Regras de ouro:
  - o dono NUNCA fica sem mensagem: se a IA falhar ou estiver desligada, sai o
    resumo só com números (analista.texto_resumo);
  - tom de coach: começa pelo que foi bem, sem críticas duras;
  - tudo que vai pro texto é calculado aqui, no código; a IA só escreve o
    "destaque" e a "dica" (e a narrativa da semana).
"""
import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from app.core.config import config
from app.repositories import analises_repo, relatorios_repo, sessoes_repo
from app.services import analista, ia_cliente
from app.services.analista_ia import ETAPAS_SCRIPT, PASTA_PROMPTS, _temperatura, _texto, carregar_metodo

TZ = config.FUSO_HORARIO
DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
MOTIVOS_PT = {"preco": "preço", "horario": "horário", "distancia": "distância", "sumiu": "sumiu", "outro": "outro motivo"}
OBJECOES_PT = {"preco": "Preço", "horario": "Horário", "distancia": "Distância", "tempo": "Falta de tempo",
               "saude": "Receio com a saúde", "vou_pensar": "\"Vou pensar\"", "outro": "Outras"}

# Termômetro do lead, do mais quente ao mais frio (é a ordem de "pedem atenção").
TERMOMETRO = ("comprador", "comparador", "pesquisador", "incomodado", "tranquilo")
TERMOMETRO_PLURAL = {"comprador": "compradores", "comparador": "comparadores", "pesquisador": "pesquisadores",
                     "incomodado": "incomodados", "tranquilo": "tranquilos"}
# Faixas de aderência ao script de UMA conversa (feitas / oportunidades).
FAIXA_SEGUIU, FAIXA_EM_PARTE = 0.70, 0.40
# Funil do script: quem chegou a uma etapa também passou pelas anteriores.
FUNIL = (
    ("acolhidos", ("acolhimento", "descoberta", "foco", "agendamento"), "acolhido", "acolhidos"),
    ("descobriram", ("descoberta", "foco", "agendamento"), "descobriu o objetivo", "descobriram o objetivo"),
    ("proximo_passo", ("foco", "agendamento"), "com próximo passo", "com próximo passo"),
    ("agendaram", ("agendamento",), "agendou", "agendaram"),
)
MIN_OPORTUNIDADES_RANKING = 2


# ---------------------------------------------------------------------------
# Pequenos formatadores
# ---------------------------------------------------------------------------
def _n(valor):
    """7.8 -> '7,8'"""
    return f"{valor:.1f}".replace(".", ",")


def _pl(n, singular, plural):
    return singular if n == 1 else plural


def _media(valores):
    valores = [float(v) for v in valores if v is not None]
    return round(sum(valores) / len(valores), 1) if valores else None


def _tendencia(atual, antes):
    if atual is None or antes is None:
        return ""
    if atual - antes >= 0.3:
        return " 📈"
    if atual - antes <= -0.3:
        return " 📉"
    return ""


def _primeiro_nome(nome):
    partes = (nome or "").split()
    return partes[0] if partes else "cliente"


def _minusculo(texto):
    return texto[:1].lower() + texto[1:] if texto else texto


def _pct(parte, total):
    return round(100 * parte / total) if total else None


# ---------------------------------------------------------------------------
# Script da academia (aderência, X de Y, termômetro, funil)
# ---------------------------------------------------------------------------
def aderencia_da_conversa(dados, conhecidas=None):
    """
    (feitas, faltaram) de UMA conversa, como conjuntos de IDs. Oportunidades =
    feitas + faltaram; o passo que não teve chance não está em nenhuma das duas.
    """
    dados = dados or {}
    feitas = set(dados.get("tecnicas_aplicadas") or [])
    faltaram = set(dados.get("tecnicas_a_reforcar") or []) - feitas
    if conhecidas is not None:
        feitas &= set(conhecidas)
        faltaram &= set(conhecidas)
    return feitas, faltaram


def faixa_aderencia(feitas, oportunidades):
    """'seguiu' (70% ou mais), 'em_parte' (40% a 69%), 'normal' (abaixo de 40%) ou None (sem oportunidade)."""
    if not oportunidades:
        return None
    proporcao = feitas / oportunidades
    if proporcao >= FAIXA_SEGUIU:
        return "seguiu"
    return "em_parte" if proporcao >= FAIXA_EM_PARTE else "normal"


def texto_termometro(termometro):
    """[{"perfil", "qtd"}] -> '2 compradores · 1 incomodado'."""
    return " · ".join(f"{t['qtd']} {t['perfil'] if t['qtd'] == 1 else TERMOMETRO_PLURAL[t['perfil']]}"
                      for t in termometro)


def agregar_script(leads, metodo):
    """Números do script num período (tudo em tipos JSON, para guardar em relatorios_dono)."""
    tecnicas = metodo["tecnicas"]
    feitas_t, oport_t, faixas, termometro, etapas = Counter(), Counter(), Counter(), Counter(), Counter()
    soma_feitas = soma_oport = conversas = 0
    momentos = []
    for a in leads:
        dados = a.get("dados") or {}
        feitas, faltaram = aderencia_da_conversa(dados, tecnicas)
        oport = len(feitas) + len(faltaram)
        if oport:
            conversas += 1
            soma_feitas += len(feitas)
            soma_oport += oport
            faixas[faixa_aderencia(len(feitas), oport)] += 1
            feitas_t.update(feitas)
            oport_t.update(feitas | faltaram)
        temp = _temperatura(a.get("temperatura"))
        if temp:
            termometro[temp] += 1
        if dados.get("etapa_script") in ETAPAS_SCRIPT:
            etapas[dados["etapa_script"]] += 1
        for m in dados.get("momentos") or []:
            if isinstance(m, dict) and m.get("tecnica") in tecnicas and m.get("pelo_script"):
                momentos.append(({**m, "cliente": _primeiro_nome(a.get("nome"))}, a.get("desfecho") == "sucesso"))

    passos = [{"id": tid, "rotulo": t["rotulo_diario"], "feitas": feitas_t[tid], "oportunidades": oport_t[tid]}
              for tid, t in tecnicas.items() if t.get("rotulo_diario") and oport_t[tid]]
    candidatos = [tid for tid in tecnicas if oport_t[tid] >= MIN_OPORTUNIDADES_RANKING]
    proporcao = lambda tid: feitas_t[tid] / oport_t[tid]
    seguidos = [tid for tid in candidatos if feitas_t[tid]]
    seguido = max(seguidos, key=lambda tid: (proporcao(tid), oport_t[tid]), default=None)
    esquecidos = [tid for tid in candidatos if feitas_t[tid] < oport_t[tid] and tid != seguido]
    esquecido = min(esquecidos, key=lambda tid: (proporcao(tid), -oport_t[tid]), default=None)
    resumo_tecnica = lambda tid: ({"id": tid, "nome": tecnicas[tid]["nome"], "feitas": feitas_t[tid],
                                   "oportunidades": oport_t[tid]} if tid else None)

    # um exemplo real: de preferência do passo mais esquecido, de uma venda que ainda não fechou
    # (é onde o script mais teria ajudado) e com a fala do cliente e da equipe
    def _ordem_exemplo(item):
        m, sucesso = item
        return (m["tecnica"] != esquecido, sucesso, not (m.get("cliente_disse") and m.get("equipe_respondeu")))
    escolhido = min(momentos, key=_ordem_exemplo, default=None)
    exemplo = {**escolhido[0], "nome_tecnica": tecnicas[escolhido[0]["tecnica"]]["nome"]} if escolhido else None

    funil = {chave: sum(etapas[e] for e in incluidas) for chave, incluidas, _, _ in FUNIL}
    funil["com_etapa"] = sum(etapas.values())
    return {
        "titulo": metodo["titulo"], "fonte": metodo["fonte"],
        "conversas": conversas, "aderencia": _pct(soma_feitas, soma_oport),
        "feitas": soma_feitas, "oportunidades": soma_oport,
        "seguiram": faixas["seguiu"], "em_parte": faixas["em_parte"], "normal": faixas["normal"],
        "passos": passos,
        "mais_seguido": resumo_tecnica(seguido), "mais_esquecido": resumo_tecnica(esquecido),
        "termometro": [{"perfil": p, "qtd": termometro[p]} for p in TERMOMETRO if termometro[p]],
        "funil": funil,
        "exemplo": exemplo,
    }


# ---------------------------------------------------------------------------
# Agregação (banco -> números)
# ---------------------------------------------------------------------------
def agregar(inicio, fim, agora=None, metodo=None):
    """
    Junta sistema + IA de um período. Devolve (agg, analises): 'agg' é um dict
    pronto para virar texto e para ser guardado em relatorios_dono (só tipos JSON);
    'analises' são as linhas cruas (usadas pela síntese da IA e pelo Excel).
    """
    agora = agora or datetime.now(timezone.utc)
    metodo = metodo or carregar_metodo()
    excluir = analista._ignorados()
    r = analista.resumo_periodo(inicio, fim, agora)
    sessoes = sessoes_repo.do_periodo(inicio, fim, excluir=excluir)
    analises = analises_repo.do_periodo(inicio, fim, excluir=excluir)

    leads = [a for a in analises if a["eh_lead"]]
    por_desfecho = Counter(a["desfecho"] for a in leads)
    perdidos_motivo = Counter(a["motivo_perda"] or "outro" for a in leads if a["desfecho"] == "perdido")

    objecoes = Counter()
    for a in leads:
        dados = a["dados"] or {}
        objecoes.update(o.get("tipo") for o in (dados.get("objecoes") or []) if o.get("tipo"))
    script = agregar_script(leads, metodo)

    # quem pede atenção (sem repetir quem já está na lista "esperando resposta"),
    # do mais quente no termômetro (comprador) ao mais frio
    esperando = {p["telefone"] for p in r["sem_resposta"]}
    ordem_temp = {t: i for i, t in enumerate(TERMOMETRO)}
    atencao = sorted(
        (a for a in leads if a["precisa_atencao"] and a["telefone"] not in esperando),
        key=lambda a: ordem_temp.get(_temperatura(a["temperatura"]), len(TERMOMETRO)),
    )

    # dia a dia
    chave = lambda tel, ini: (tel, ini)
    analise_de = {chave(a["telefone"], a["sessao_inicio"]): a for a in analises}
    por_dia = {}
    for s in sessoes:
        dia = s["inicio"].astimezone(TZ).date().isoformat()
        d = por_dia.setdefault(dia, {"conversas": 0, "novas": 0, "retomadas": 0, "sucesso": 0,
                                     "em_andamento": 0, "perdido": 0, "_notas": [], "_primeiras": []})
        d["conversas"] += 1
        d["novas" if s["nova"] else "retomadas"] += 1
        if s["primeira_resposta_util_seg"] is not None:
            d["_primeiras"].append(s["primeira_resposta_util_seg"])
        a = analise_de.get(chave(s["telefone"], s["inicio"]))
        if a and a["eh_lead"]:
            if a["desfecho"] in ("sucesso", "em_andamento", "perdido"):
                d[a["desfecho"]] += 1
            if a["nota_final"] is not None:
                d["_notas"].append(a["nota_final"])
    for d in por_dia.values():
        d["nota_media"] = _media(d.pop("_notas"))
        primeiras = d.pop("_primeiras")
        d["primeira_resposta_media_util_seg"] = int(round(sum(primeiras) / len(primeiras))) if primeiras else None

    analisaveis = sum(1 for s in sessoes if s["msgs_cliente"] > 0)
    agg = {
        "periodo": r["periodo"],
        "conversas": r["conversas"], "novas": r["novas"], "retomadas": r["retomadas"],
        "followups_da_equipe": r["followups_da_equipe"],
        "msgs_cliente": r["msgs_cliente"], "msgs_equipe": r["msgs_equipe"],
        "com_primeira_resposta": r["com_primeira_resposta"], "respondidas_no_limite": r["respondidas_no_limite"],
        "primeira_resposta_media_util_seg": r["primeira_resposta_media_util_seg"],
        "pior_espera_util_seg": r["pior_espera_util_seg"], "respondidas_por": r["respondidas_por"],
        "sem_resposta": r["sem_resposta"],
        "analisaveis": analisaveis, "analisadas": len(analises), "leads": len(leads),
        "sucesso": por_desfecho["sucesso"], "em_andamento": por_desfecho["em_andamento"],
        "perdido": por_desfecho["perdido"], "nao_lead": len(analises) - len(leads),
        "perdidos_por_motivo": dict(perdidos_motivo),
        "nota_media": _media(a["nota_final"] for a in leads),
        "nota_venda": _media(a["nota_intuito_venda"] for a in leads),
        "propos_ligacao": sum(1 for a in leads if a["propos_ligacao"]),
        "script": script,
        "atencao_total": len(atencao),
        "pedem_atencao": [
            {"nome": a["nome"] or "Sem nome", "telefone": a["telefone"], "motivo": a["motivo_atencao"],
             "temperatura": _temperatura(a["temperatura"])}
            for a in atencao[: config.MAX_SEM_RESPOSTA_NO_RESUMO]
        ],
        "objecoes": [{"tipo": t, "qtd": q} for t, q in objecoes.most_common(5)],
        "por_dia": por_dia,
        "em_aberto_anteriores": analises_repo.em_aberto_anteriores(inicio, excluir=excluir),
        "anterior": None, "sintese": None,
    }
    return agg, analises


# ---------------------------------------------------------------------------
# Síntese da IA (opcional: se falhar, o resumo sai sem ela)
# ---------------------------------------------------------------------------
def _compactar(analises, limite=60):
    itens = []
    for a in analises:
        if not a["eh_lead"]:
            continue
        itens.append({
            "cliente": _primeiro_nome(a["nome"]), "desfecho": a["desfecho"],
            "nota": float(a["nota_final"]) if a["nota_final"] is not None else None,
            "resumo": a["resumo"], "ponto_forte": a["ponto_forte"], "pode_melhorar": a["pode_melhorar"],
        })
    return itens[:limite]


def _chamar(prompt_arquivo, conteudo, ia):
    instrucoes = (PASTA_PROMPTS / prompt_arquivo).read_text(encoding="utf-8")
    dado, _ = ia.gerar_json(instrucoes, json.dumps(conteudo, ensure_ascii=False), max_tokens=1500)
    return dado


def sintetizar_dia(analises, ia=None):
    """{"destaque", "dica"} escritos pela IA, ou None (IA desligada, sem dados ou falha)."""
    ia = ia or ia_cliente
    itens = _compactar(analises)
    if not itens or not ia.ia_ligada():
        return None
    try:
        dado = _chamar("analista_sintese.md", itens, ia)
    except Exception as e:
        print(f"⚠️ [Analista IA] Síntese do dia falhou (segue sem ela): {e}")
        return None
    sintese = {"destaque": _texto(dado.get("destaque"), 250), "dica": _texto(dado.get("dica"), 300)}
    return sintese if any(sintese.values()) else None


def _script_para_ia(script):
    """O que a narrativa da semana precisa saber do script (números já calculados aqui)."""
    if not script or not script.get("conversas"):
        return None
    nome = lambda t: t["nome"] if t else None
    return {
        "titulo": script["titulo"], "aderencia_pct": script["aderencia"],
        "conversas_de_venda": script["conversas"],
        "seguiram": script["seguiram"], "em_parte": script["em_parte"], "atendimento_normal": script["normal"],
        "passos": [f"{p['rotulo']}: {p['feitas']} de {p['oportunidades']}" for p in script["passos"]],
        "mais_seguido": nome(script["mais_seguido"]), "mais_esquecido": nome(script["mais_esquecido"]),
        "funil": script["funil"],
    }


def sintetizar_semana(agg, analises, ia=None):
    """Narrativa da semana (destaques, treino, objeções, método) ou None."""
    ia = ia or ia_cliente
    itens = _compactar(analises)
    if not itens or not ia.ia_ligada():
        return None
    numeros = {k: agg[k] for k in ("conversas", "novas", "retomadas", "sucesso", "em_andamento", "perdido",
                                   "nao_lead", "nota_media", "nota_venda", "propos_ligacao", "leads")}
    conteudo = {
        "numeros": numeros,
        "objecoes_mais_ouvidas": [{"tipo": OBJECOES_PT.get(o["tipo"], o["tipo"]), "id": o["tipo"], "qtd": o["qtd"]}
                                  for o in agg["objecoes"][:3]],
        "script": _script_para_ia(agg.get("script")),
        "conversas": itens,
    }
    try:
        dado = _chamar("analista_semanal.md", conteudo, ia)
    except Exception as e:
        print(f"⚠️ [Analista IA] Síntese da semana falhou (segue sem ela): {e}")
        return None
    destaques = [t for t in (_texto(x, 250) for x in (dado.get("destaques") or [])[:2]) if t]
    treino = dado.get("treino") if isinstance(dado.get("treino"), dict) else {}
    treino = {"tema": _texto(treino.get("tema"), 150), "exemplo": _texto(treino.get("exemplo"), 300)}
    objecoes = []
    for o in (dado.get("objecoes") or [])[:3]:
        if isinstance(o, dict) and _texto(o.get("tipo"), 40) and _texto(o.get("como_responder"), 250):
            objecoes.append({"tipo": _texto(o["tipo"], 40).lower().replace(" ", "_"),
                             "como_responder": _texto(o["como_responder"], 250)})
    metodo = dado.get("metodo") if isinstance(dado.get("metodo"), dict) else {}
    metodo = {"ja_faz": _texto(metodo.get("ja_faz"), 250), "reforcar": _texto(metodo.get("reforcar"), 250)}
    sintese = {"destaques": destaques, "treino": treino if treino["tema"] else None,
               "objecoes": objecoes, "metodo": metodo if any(metodo.values()) else None}
    return sintese if (destaques or sintese["treino"] or objecoes or sintese["metodo"]) else None


# ---------------------------------------------------------------------------
# Texto do resumo do dia
# ---------------------------------------------------------------------------
def _linhas_resultados(agg):
    linhas = []
    s, a, p, nl = agg["sucesso"], agg["em_andamento"], agg["perdido"], agg["nao_lead"]
    if s:
        linhas.append(f"✅ {s} com sucesso ({_pl(s, 'agendou ou fechou', 'agendaram ou fecharam')})")
    if a:
        linhas.append(f"💬 {a} em andamento")
    if p:
        motivos = ", ".join(
            f"{MOTIVOS_PT.get(m, m)} {q}" if len(agg["perdidos_por_motivo"]) > 1 else MOTIVOS_PT.get(m, m)
            for m, q in sorted(agg["perdidos_por_motivo"].items(), key=lambda x: -x[1]))
        linhas.append(f"↩️ {p} {_pl(p, 'desistiu', 'desistiram')}" + (f" ({motivos})" if motivos else ""))
    if nl:
        linhas.append(f"➖ {nl} {_pl(nl, 'não era venda', 'não eram venda')}")
    return linhas


def _linhas_script(agg, quando, antes=None):
    """
    Bloco "📘 Script da academia hoje / na semana".
    antes: (rótulo, aderência anterior em %) para comparar, ex.: ("ontem", 55).
    """
    sc = agg.get("script") or {}
    if not sc or not (sc.get("conversas") or sc.get("termometro")):
        return []
    titulo = f"📘 *{sc['titulo']} {quando}*"
    if sc["conversas"]:
        titulo += f" ({sc['conversas']} {_pl(sc['conversas'], 'conversa de venda', 'conversas de venda')})"
    linhas = [titulo]
    if sc["aderencia"] is not None:
        ader = f"Aderência: {sc['aderencia']}%"
        if antes and antes[1] is not None:
            ader += f" ({antes[0]} {antes[1]}%)"
        partes = [ader]
        if sc["seguiram"]:
            partes.append(f"{sc['seguiram']} {_pl(sc['seguiram'], 'seguiu o script', 'seguiram o script')}")
        if sc["em_parte"]:
            partes.append(f"{sc['em_parte']} em parte")
        if sc["normal"]:
            partes.append(f"{sc['normal']} atendimento normal")
        linhas.append("• " + " · ".join(partes))
    for p in sc["passos"]:
        linhas.append(f"• {p['rotulo']}: {p['feitas']} de {p['oportunidades']}")
    destaques = []
    if sc.get("mais_seguido"):
        destaques.append(f"✔️ Mais seguido: {_minusculo(sc['mais_seguido']['nome'])}")
    if sc.get("mais_esquecido"):
        destaques.append(f"🔸 Mais esquecido: {_minusculo(sc['mais_esquecido']['nome'])}")
    if destaques:
        linhas.append(" · ".join(destaques))
    if sc["termometro"]:
        linhas.append(f"🌡️ Termômetro: {texto_termometro(sc['termometro'])}")
    return linhas


def texto_funil(funil):
    """'12 acolhidos → 8 descobriram o objetivo → 5 com próximo passo → 4 agendaram' (ou '' sem dados)."""
    if not funil or not funil.get("com_etapa"):
        return ""
    return " → ".join(f"{funil[chave]} {_pl(funil[chave], um, varios)}" for chave, _, um, varios in FUNIL)


def _linhas_exemplo(exemplo):
    """O 'dito × ideal' de um momento real, em 3 linhas."""
    if not exemplo:
        return []
    linhas = [f"🔎 *Um exemplo da semana* ({exemplo['nome_tecnica']} · {exemplo.get('cliente') or 'cliente'})"]
    if exemplo.get("cliente_disse"):
        linhas.append(f"Cliente: \"{exemplo['cliente_disse']}\"")
    linhas.append(f"Foi respondido: \"{exemplo['equipe_respondeu']}\"" if exemplo.get("equipe_respondeu")
                  else "Foi respondido: (ficou sem resposta)")
    linhas.append(f"Pelo script: \"{exemplo['pelo_script']}\"")
    return linhas


def _linhas_atencao(agg):
    if not agg["pedem_atencao"]:
        return []
    linhas = [f"⚠️ *Pedem atenção ({agg['atencao_total']})*"]
    for p in agg["pedem_atencao"]:
        fogo = "🔥 " if _temperatura(p.get("temperatura")) == "comprador" else ""
        motivo = f" — {p['motivo']}" if p.get("motivo") else ""
        linhas.append(f"• {fogo}{p['nome']} {analista._telefone_legivel(p['telefone'])}{motivo}")
    if agg["atencao_total"] > len(agg["pedem_atencao"]):
        linhas.append(f"... e mais {agg['atencao_total'] - len(agg['pedem_atencao'])}. Veja no app.")
    return linhas


def texto_diario(agg, rotulo):
    """
    Mensagem do dono (versão com IA). Seções vazias não aparecem.
    agg["anterior"] (opcional) = o último diário: {"rotulo": "ontem"|"sexta"..., "conversas",
    "nota_media", "aderencia"}; vira "(ontem 10)", "(ontem 7,2) 📈" e "(ontem 55%)".
    """
    blocos = [[f"📊 *Atendimento - {rotulo}*"]]
    ant = agg.get("anterior")

    mov = ["*Movimento*"]
    total = f"• {agg['conversas']} {_pl(agg['conversas'], 'conversa', 'conversas')}"
    if ant and ant.get("conversas") is not None:
        total += f" ({ant['rotulo']} {ant['conversas']})"
    mov.append(total)
    mov.append(f"• {agg['novas']} {_pl(agg['novas'], 'cliente novo', 'clientes novos')} · "
               f"{agg['retomadas']} {_pl(agg['retomadas'], 'cliente que voltou', 'clientes que voltaram')}")
    if agg["em_aberto_anteriores"]:
        mov.append(f"• Também em aberto de dias anteriores: {agg['em_aberto_anteriores']}")
    if agg["followups_da_equipe"]:
        mov.append(f"• A equipe chamou {agg['followups_da_equipe']} cliente(s) antigo(s) de volta")
    blocos.append(mov)

    resultados = _linhas_resultados(agg)
    if resultados:
        blocos.append(["*Como estão as conversas*"] + resultados)

    blocos.append(analista.linhas_velocidade(agg))

    qualidade = []
    if agg["nota_media"] is not None:
        nota = f"• Nota média: {_n(agg['nota_media'])}"
        if ant and ant.get("nota_media") is not None:
            nota += f" ({ant['rotulo']} {_n(ant['nota_media'])}){_tendencia(agg['nota_media'], ant['nota_media'])}"
        qualidade.append(nota)
        extra = []
        if agg["nota_venda"] is not None:
            extra.append(f"Intuito de venda: {_n(agg['nota_venda'])}")
        if agg["leads"]:
            extra.append(f"Propôs ligação: {agg['propos_ligacao']} de {agg['leads']}")
        if extra:
            qualidade.append("• " + " · ".join(extra))
    sintese = agg.get("sintese") or {}
    if sintese.get("destaque"):
        qualidade.append(f"🌟 Muito bom: {sintese['destaque']}")
    if sintese.get("dica"):
        qualidade.append(f"💡 Dica: {sintese['dica']}")
    if qualidade:
        blocos.append(["*Qualidade do atendimento*"] + qualidade)

    script = _linhas_script(agg, "hoje", (ant["rotulo"], ant.get("aderencia")) if ant else None)
    if script:
        blocos.append(script)
    atencao = _linhas_atencao(agg)
    if atencao:
        blocos.append(atencao)
    blocos.append(analista.linhas_esperando(agg["sem_resposta"]))
    if agg["analisadas"] < agg["analisaveis"]:
        blocos.append([f"ℹ️ A IA analisou {agg['analisadas']} de {agg['analisaveis']} conversas; "
                       f"as outras entram no próximo resumo."])
    return "\n\n".join("\n".join(b) for b in blocos)


# ---------------------------------------------------------------------------
# Texto do resumo da semana
# ---------------------------------------------------------------------------
def texto_semanal(agg, primeiro_dia, ultimo_dia, anterior=None):
    """
    primeiro_dia / ultimo_dia: datas (segunda e o dia do envio).
    anterior: os dados do semanal da semana passada (ou None).
    """
    blocos = [[f"📈 *Resumo da semana - {primeiro_dia.strftime('%d/%m')} a {ultimo_dia.strftime('%d/%m')}*"]]
    if not agg["conversas"]:
        blocos.append(["Nenhuma conversa com clientes nesta semana."])
        blocos.append(analista.linhas_esperando(agg["sem_resposta"]))
        return "\n\n".join("\n".join(b) for b in blocos)

    vol = ["*Volume*",
           f"• {agg['conversas']} {_pl(agg['conversas'], 'conversa', 'conversas')} "
           f"({agg['novas']} {_pl(agg['novas'], 'cliente novo', 'clientes novos')} · "
           f"{agg['retomadas']} {_pl(agg['retomadas'], 'cliente que voltou', 'clientes que voltaram')})"]
    por_dia = agg["por_dia"]
    if por_dia:
        melhor = max(por_dia.items(), key=lambda kv: kv[1]["conversas"])
        dia = datetime.fromisoformat(melhor[0]).weekday()
        vol.append(f"• Melhor dia: {DIAS[dia]} ({melhor[1]['conversas']})")
    if agg["followups_da_equipe"]:
        vol.append(f"• A equipe chamou {agg['followups_da_equipe']} cliente(s) antigo(s) de volta")
    blocos.append(vol)

    resultados = _linhas_resultados(agg)
    if resultados:
        bloco = ["*Resultados*"] + resultados
        if agg["leads"]:
            bloco.append(f"• Taxa de sucesso: {round(100 * agg['sucesso'] / agg['leads'])}% das conversas de venda")
        if anterior and anterior.get("sucesso") is not None:
            delta = agg["sucesso"] - anterior["sucesso"]
            if delta:
                bloco.append(f"• Comparado com a semana passada: {'+' if delta > 0 else ''}{delta} "
                             f"{_pl(abs(delta), 'sucesso', 'sucessos')}")
        blocos.append(bloco)

    blocos.append(analista.linhas_velocidade(agg))

    qualidade = []
    if agg["nota_media"] is not None:
        nota = f"• Nota média: {_n(agg['nota_media'])}"
        if anterior and anterior.get("nota_media") is not None:
            nota += _tendencia(agg["nota_media"], anterior["nota_media"])
        com_nota = [(d, v["nota_media"]) for d, v in sorted(por_dia.items()) if v.get("nota_media") is not None]
        if len(com_nota) >= 2:
            ini, fim = com_nota[0], com_nota[-1]
            nota += (f" ({DIAS[datetime.fromisoformat(ini[0]).weekday()]} {_n(ini[1])} → "
                     f"{DIAS[datetime.fromisoformat(fim[0]).weekday()]} {_n(fim[1])})")
        qualidade.append(nota)
        extra = []
        if agg["nota_venda"] is not None:
            extra.append(f"Intuito de venda: {_n(agg['nota_venda'])}")
        if agg["leads"]:
            extra.append(f"Propôs ligação: {agg['propos_ligacao']} de {agg['leads']}")
        if extra:
            qualidade.append("• " + " · ".join(extra))
    if qualidade:
        blocos.append(["*Qualidade do atendimento*"] + qualidade)

    sintese = agg.get("sintese") or {}
    if sintese.get("destaques"):
        blocos.append(["🌟 *Destaques da semana*"] + [f"• {d}" for d in sintese["destaques"]])
    if sintese.get("treino"):
        t = sintese["treino"]
        blocos.append(["🎯 *Para treinar na próxima semana*",
                       f"• {t['tema']}" + (f" - {t['exemplo']}" if t.get("exemplo") else "")])
    if agg["objecoes"]:
        como = {o["tipo"]: o["como_responder"] for o in sintese.get("objecoes") or []}
        linhas = ["💬 *Objeções mais ouvidas*"]
        for o in agg["objecoes"][:3]:
            linha = f"• {OBJECOES_PT.get(o['tipo'], o['tipo'])} ({o['qtd']}×)"
            if como.get(o["tipo"]):
                linha += f" - {como[o['tipo']]}"
            linhas.append(linha)
        blocos.append(linhas)
    ader_antes = ((anterior or {}).get("script") or {}).get("aderencia")
    script = _linhas_script(agg, "na semana", ("semana passada", ader_antes) if ader_antes is not None else None)
    funil = texto_funil((agg.get("script") or {}).get("funil"))
    if script and funil:
        script.append(f"🪜 Funil: {funil}")
    if script and sintese.get("metodo"):
        m = sintese["metodo"]
        script += [f"• {m['ja_faz']}"] if m.get("ja_faz") else []
        script += [f"• {m['reforcar']}"] if m.get("reforcar") else []
    if script:
        blocos.append(script)
    exemplo = _linhas_exemplo((agg.get("script") or {}).get("exemplo"))
    if exemplo:
        blocos.append(exemplo)
    atencao = _linhas_atencao(agg)
    if atencao:
        blocos.append(atencao)
    blocos.append(analista.linhas_esperando(agg["sem_resposta"]))
    if 0 < agg["analisadas"] < agg["analisaveis"]:
        blocos.append([f"ℹ️ A IA analisou {agg['analisadas']} de {agg['analisaveis']} conversas da semana."])
    blocos.append(["📎 A planilha da semana está em anexo."])
    return "\n\n".join("\n".join(b) for b in blocos)


# ---------------------------------------------------------------------------
# Montagem completa (usada pela rodada)
# ---------------------------------------------------------------------------
def janela_da_semana(agora=None):
    """Da segunda 00h (SP) da semana de 'agora' até 'agora'."""
    agora = agora or datetime.now(timezone.utc)
    local = agora.astimezone(TZ)
    segunda = (local - timedelta(days=local.weekday())).date()
    return TZ.localize(datetime(segunda.year, segunda.month, segunda.day)), agora


def _comparacao_com_ontem(referencia):
    """
    Os números do último diário enviado antes de hoje, para "(ontem 7,2)". Depois
    de um fim de semana sem resumo vira "(sexta 7,2)"; mais de 7 dias atrás, não compara.
    """
    anterior = relatorios_repo.diario_anterior(referencia)
    if not anterior:
        return None
    ref = anterior["referencia"]
    if isinstance(ref, datetime):
        ref = ref.date()
    elif isinstance(ref, str):
        ref = date.fromisoformat(ref[:10])
    dias = (referencia - ref).days
    if not 1 <= dias <= 7:
        return None
    d = anterior["dados"] or {}
    return {"referencia": ref.isoformat(), "rotulo": "ontem" if dias == 1 else DIAS[ref.weekday()],
            "conversas": d.get("conversas"), "nota_media": d.get("nota_media"),
            "aderencia": (d.get("script") or {}).get("aderencia")}


def montar_diario(hora, agora=None, ia=None):
    """
    Devolve {"texto", "dados", "referencia", "hora", "conversas"}. Se não há nenhuma
    análise da IA (desligada ou toda com erro), o texto é o resumo só com números.
    """
    agora = agora or datetime.now(timezone.utc)
    inicio, fim = analista.janela_do_dia(agora)
    agg, analises = agregar(inicio, fim, agora)
    referencia = inicio.date()
    rotulo = analista.rotulo_do_horario(hora, agora)

    if agg["analisadas"] == 0:
        texto = analista.texto_resumo(agg, rotulo)
    else:
        agg["anterior"] = _comparacao_com_ontem(referencia)
        agg["sintese"] = sintetizar_dia(analises, ia)
        texto = texto_diario(agg, rotulo)
    return {"texto": texto, "dados": agg, "referencia": referencia, "hora": hora, "conversas": agg["conversas"]}


def montar_semanal(agora=None, ia=None):
    """Devolve {"texto", "dados", "referencia", "inicio", "fim", "conversas"}."""
    agora = agora or datetime.now(timezone.utc)
    inicio, fim = janela_da_semana(agora)
    agg, analises = agregar(inicio, fim, agora)
    segunda = inicio.date()
    anterior = relatorios_repo.semanal_da_semana(segunda - timedelta(days=7))
    dados_anterior = (anterior["dados"] or {}) if anterior else None
    agg["sintese"] = sintetizar_semana(agg, analises, ia)
    texto = texto_semanal(agg, segunda, fim.astimezone(TZ).date(), dados_anterior)
    return {"texto": texto, "dados": agg, "referencia": segunda, "inicio": inicio, "fim": fim,
            "conversas": agg["conversas"]}
