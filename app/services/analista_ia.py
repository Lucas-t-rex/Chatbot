"""
ANALISTA COM IA — lê UMA conversa por vez e grava a análise no banco.

Fluxo (analisar_pendentes):
  1. busca as conversas pendentes (nunca analisadas, ou com mensagem nova depois
     do horário que a IA leu);
  2. para cada uma, monta a entrada (transcrição + números do sistema + análise
     anterior), chama a IA, VALIDA a resposta e grava em analises_conversa;
  3. o que dá para calcular com código (notas de tempo, nota final) é do código,
     não da IA — assim os números saem iguais toda vez.

Sem ANALISTA_IA_API_KEY nada disso roda (a IA fica desligada).
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import config
from app.repositories import analises_repo, mensagens_repo
from app.services import ia_cliente
from app.services.analista import _ignorados
from app.services.analista_metricas import eh_relevante, esperando_ha, separar_sessoes

TZ = config.FUSO_HORARIO
PASTA_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

# Critérios que a IA avalia (chave do JSON -> nome para o Excel)
CRITERIOS_IA = {
    "cordialidade": "Cordialidade inicial",
    "contexto": "Retenção de contexto",
    "descoberta": "Descoberta da necessidade",
    "qualificacao": "Qualificação",
    "clareza": "Clareza e objetividade",
    "ortografia": "Ortografia e gramática",
    "empatia": "Empatia e tom",
    "valor": "Apresentação de valor",
    "objecoes": "Contorno de objeções",
    "chamada_acao": "Chamada para ação",
    "resolucao": "Resolução no 1º contato",
    "followup": "Follow-up",
    "fechamento": "Fechamento",
    "intuito_venda": "Intuito de venda",
    "intuito_ligacao": "Intuito de ligação",
}
# Critérios calculados pelo código a partir das métricas
CRITERIOS_TEMPO = {
    "primeira_resposta": "Tempo da 1ª resposta",
    "tempo_medio": "Tempo médio de resposta",
}
TODOS_CRITERIOS = {**CRITERIOS_TEMPO, **CRITERIOS_IA}

DESFECHOS = ("sucesso", "em_andamento", "perdido", "nao_lead")
TIPOS_CONTATO = ("lead", "aluno", "fornecedor", "engano", "outro")
MOTIVOS_PERDA = ("preco", "horario", "distancia", "sumiu", "outro")
TEMPERATURAS = ("quente", "morno", "frio")

LIMITES_TEXTO = {
    "interesse": 120, "detalhe_desfecho": 200, "motivo_atencao": 200, "resumo": 300,
    "ponto_forte": 400, "pode_melhorar": 400, "proxima_acao": 250, "mensagem_sugerida": 500,
    "como_poderia_ser": 600, "trecho_ligacao": 200,
}
MAX_MSG_CHARS = 1000
SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
_QUEM = {"cliente": "Cliente", "atendente": "Atendente", "bot": "Bot"}


class RespostaInvalida(Exception):
    """A IA respondeu, mas sem os campos mínimos para uma análise."""


# ---------------------------------------------------------------------------
# Método de vendas (técnicas do curso)
# ---------------------------------------------------------------------------
def carregar_metodo():
    """
    Lê as técnicas de app/prompts/metodo_vendas_<fonte>.md. Se a fonte "curso"
    for pedida mas o arquivo ainda não existir, cai nas boas práticas gerais.
    Devolve {"fonte", "titulo", "tecnicas": {"T01": {"nome", "como"}}}.
    """
    fonte = config.METODO_FONTE
    arquivo = PASTA_PROMPTS / f"metodo_vendas_{fonte}.md"
    if not arquivo.exists():
        fonte, arquivo = "geral", PASTA_PROMPTS / "metodo_vendas_geral.md"
    titulo, tecnicas = "Boas práticas de venda", {}
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        m_titulo = re.match(r"^TITULO\s*\|\s*(.+)$", linha.strip())
        if m_titulo:
            titulo = m_titulo.group(1).strip()
            continue
        m = re.match(r"^(T\d+)\s*\|\s*([^|]+?)\s*\|\s*(.+)$", linha.strip())
        if m:
            tecnicas[m.group(1)] = {"nome": m.group(2).strip(), "como": m.group(3).strip()}
    return {"fonte": fonte, "titulo": titulo, "tecnicas": tecnicas}


def montar_instrucoes(metodo=None):
    """O texto fixo que vai como 'sistema' em toda chamada (com as técnicas no lugar)."""
    metodo = metodo or carregar_metodo()
    texto = (PASTA_PROMPTS / "analista_conversa.md").read_text(encoding="utf-8")
    tecnicas = "\n".join(
        f"- {tid} — {t['nome']}: {t['como']}" for tid, t in metodo["tecnicas"].items()
    ) or "(nenhuma técnica cadastrada)"
    return texto.replace("{{TECNICAS}}", tecnicas)


# ---------------------------------------------------------------------------
# Notas calculadas pelo código
# ---------------------------------------------------------------------------
def nota_de_tempo(segundos_uteis):
    """Régua do tempo de resposta (minutos de horário útil): quanto menor, melhor."""
    if segundos_uteis is None:
        return None
    minutos = segundos_uteis / 60
    for limite, nota in ((5, 10), (15, 8), (30, 6), (60, 4), (180, 2)):
        if minutos <= limite:
            return float(nota)
    return 0.0


def notas_de_tempo(sessao, agora=None):
    """
    Notas de 1ª resposta e tempo médio. Quando a equipe ainda não respondeu: se o
    cliente já espera além do limite, nota 0; se ainda está dentro do limite (ou
    nada esperava resposta), o critério não se aplica (None).
    """
    agora = agora or datetime.now(timezone.utc)
    primeira = sessao.get("primeira_resposta_util_seg")
    medio = sessao.get("tempo_medio_resposta_util_seg")
    if primeira is None:
        desde = sessao.get("aguardando_desde")
        if desde is None:
            return {"primeira_resposta": None, "tempo_medio": None}
        _, util = esperando_ha(desde, agora, config.HORARIO_ATENDIMENTO, TZ)
        estourou = util >= config.LIMITE_SEM_RESPOSTA_MIN * 60
        return {"primeira_resposta": 0.0 if estourou else None,
                "tempo_medio": 0.0 if estourou else None}
    return {"primeira_resposta": nota_de_tempo(primeira), "tempo_medio": nota_de_tempo(medio)}


def media_das_notas(notas):
    """Média (1 casa) dos critérios que se aplicam; None se nenhum se aplica."""
    validas = [n for n in notas.values() if n is not None]
    return round(sum(validas) / len(validas), 1) if validas else None


# ---------------------------------------------------------------------------
# Entrada para a IA
# ---------------------------------------------------------------------------
def _linhas_da_transcricao(mensagens):
    linhas, dia_atual = [], None
    for m in mensagens:
        if not eh_relevante(m):
            continue
        local = m["ts"].astimezone(TZ)
        dia = local.date()
        if dia != dia_atual:
            linhas.append(f"--- {SEMANA[local.weekday()]} {local.strftime('%d/%m')} ---")
            dia_atual = dia
        texto = " ".join((m["text"] or "").split())
        if len(texto) > MAX_MSG_CHARS:
            texto = texto[:MAX_MSG_CHARS] + "..."
        linhas.append(f"{local.strftime('%H:%M')} {_QUEM.get(m['origem'], m['origem'])}: {texto}")
    return linhas


def cortar_transcricao(linhas, max_chars):
    """Conversa grande demais: fica o começo (40%) e o fim (60%), com um aviso no meio."""
    if sum(len(l) + 1 for l in linhas) <= max_chars:
        return linhas
    inicio, fim, usado = [], [], 0
    for l in linhas:
        if usado + len(l) > max_chars * 0.4:
            break
        inicio.append(l)
        usado += len(l) + 1
    usado = 0
    for l in reversed(linhas[len(inicio):]):
        if usado + len(l) > max_chars * 0.6:
            break
        fim.append(l)
        usado += len(l) + 1
    fim.reverse()
    omitidas = len(linhas) - len(inicio) - len(fim)
    return inicio + [f"[... {omitidas} linhas do meio omitidas por serem muitas ...]"] + fim


def _duracao(seg):
    if seg is None:
        return "sem dado"
    return f"{round(seg / 60, 1)} min".replace(".0 ", " ")


def _resumo_anterior(analise):
    """Versão curta de uma análise antiga, para a IA ter contexto."""
    if not analise:
        return None
    return json.dumps({
        "desfecho": analise.get("desfecho"), "detalhe_desfecho": analise.get("detalhe_desfecho"),
        "interesse": analise.get("interesse"), "resumo": analise.get("resumo"),
        "nota_final": float(analise["nota_final"]) if analise.get("nota_final") is not None else None,
    }, ensure_ascii=False)


def montar_entrada(sessao, mensagens, mesma_conversa=None, conversa_anterior=None, agora=None):
    """O texto que vai como 'usuario': contexto + números do sistema + transcrição."""
    agora = agora or datetime.now(timezone.utc)
    linhas = [
        f"Contato: {sessao.get('nome') or 'Sem nome'}",
        "Cliente: " + ("NOVO (primeiro contato com a academia)" if sessao.get("nova")
                       else "QUE VOLTOU (já tinha conversado antes)"),
        f"Conversa iniciada por: {'o cliente' if sessao.get('iniciada_por') == 'cliente' else 'a equipe'}",
        "",
        "NÚMEROS MEDIDOS PELO SISTEMA (fatos; horário útil = só o horário de atendimento):",
        f"- 1ª resposta da equipe: {_duracao(sessao.get('primeira_resposta_util_seg'))}",
        f"- Tempo médio de resposta: {_duracao(sessao.get('tempo_medio_resposta_util_seg'))}",
        f"- Maior espera: {_duracao(sessao.get('tempo_max_resposta_util_seg'))}",
        f"- Respondida por: {sessao.get('respondida_por')}",
    ]
    if sessao.get("aguardando_desde"):
        _, util = esperando_ha(sessao["aguardando_desde"], agora, config.HORARIO_ATENDIMENTO, TZ)
        linhas.append(f"- AGORA: o cliente falou por último e espera resposta há {_duracao(util)} (horário útil)")
    else:
        linhas.append("- AGORA: ninguém aguardando resposta")
    if mesma_conversa:
        linhas += ["", "SUA ANÁLISE ANTERIOR DESTA MESMA CONVERSA (chegaram mensagens novas; atualize): "
                   + _resumo_anterior(mesma_conversa)]
    if conversa_anterior:
        dia = conversa_anterior["sessao_inicio"].astimezone(TZ).strftime("%d/%m")
        linhas += ["", f"CONVERSA ANTERIOR DESTE CONTATO (dia {dia}), só para contexto: "
                   + _resumo_anterior(conversa_anterior)]
    transcricao = cortar_transcricao(_linhas_da_transcricao(mensagens), config.ANALISTA_IA_MAX_CHARS_CONVERSA)
    linhas += ["", "TRANSCRIÇÃO:"] + transcricao
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Validação da resposta da IA
# ---------------------------------------------------------------------------
def _texto(valor, limite):
    if valor is None:
        return None
    t = " ".join(str(valor).split())
    if not t or t.lower() in ("null", "none", "n/a"):
        return None
    return t if len(t) <= limite else t[: limite - 3].rstrip() + "..."


def _nota(valor):
    if valor is None or isinstance(valor, bool):
        return None
    try:
        n = float(str(valor).replace(",", "."))
    except ValueError:
        return None
    return round(min(10.0, max(0.0, n)), 1)


def _booleano(valor):
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str):
        return valor.strip().lower() in ("true", "sim", "yes", "1")
    return None


def _genero(valor):
    t = str(valor or "").strip().lower()
    if t.startswith("masc"):
        return "Masculino"
    if t.startswith("fem"):
        return "Feminino"
    return "Indefinido"


def validar_resposta(bruto, metodo, notas_tempo):
    """
    Transforma o JSON da IA em (campos, dados) prontos para gravar. Corrige o que
    vem torto (nota fora de 0-10, opção desconhecida, texto grande) e levanta
    RespostaInvalida se faltar o essencial.
    """
    if not isinstance(bruto, dict) or not (bruto.get("notas") or bruto.get("desfecho")):
        raise RespostaInvalida("resposta sem 'notas' nem 'desfecho'")

    tipo = str(bruto.get("tipo_contato") or "").strip().lower()
    tipo = tipo if tipo in TIPOS_CONTATO else ("lead" if bruto.get("eh_lead") is not False else "outro")
    eh_lead = _booleano(bruto.get("eh_lead"))
    if eh_lead is None:
        eh_lead = tipo == "lead"
    desfecho = str(bruto.get("desfecho") or "").strip().lower()
    desfecho = desfecho if desfecho in DESFECHOS else "em_andamento"
    if not eh_lead:
        desfecho = "nao_lead"
    elif desfecho == "nao_lead":
        desfecho = "em_andamento"

    notas_ia = {}
    bruto_notas = bruto.get("notas") if isinstance(bruto.get("notas"), dict) else {}
    for chave in CRITERIOS_IA:
        notas_ia[chave] = _nota(bruto_notas.get(chave)) if eh_lead else None
    notas = {**{k: (notas_tempo.get(k) if eh_lead else None) for k in CRITERIOS_TEMPO}, **notas_ia}

    motivo_perda = str(bruto.get("motivo_perda") or "").strip().lower()
    motivo_perda = motivo_perda if (desfecho == "perdido" and motivo_perda in MOTIVOS_PERDA) else (
        "outro" if desfecho == "perdido" else None)
    temperatura = str(bruto.get("temperatura") or "").strip().lower()
    temperatura = temperatura if (eh_lead and temperatura in TEMPERATURAS) else None

    precisa = bool(_booleano(bruto.get("precisa_atencao"))) and eh_lead and desfecho != "sucesso"
    propos = _booleano(bruto.get("propos_ligacao")) if eh_lead else None

    campos = {
        "eh_lead": eh_lead, "tipo_contato": tipo, "genero": _genero(bruto.get("genero")),
        "interesse": _texto(bruto.get("interesse"), LIMITES_TEXTO["interesse"]),
        "desfecho": desfecho,
        "detalhe_desfecho": _texto(bruto.get("detalhe_desfecho"), LIMITES_TEXTO["detalhe_desfecho"]),
        "motivo_perda": motivo_perda, "temperatura": temperatura,
        "precisa_atencao": precisa,
        "motivo_atencao": _texto(bruto.get("motivo_atencao"), LIMITES_TEXTO["motivo_atencao"]) if precisa else None,
        "nota_final": media_das_notas(notas) if eh_lead else None,
        "nota_intuito_venda": notas_ia["intuito_venda"],
        "nota_ligacao": notas_ia["intuito_ligacao"],
        "propos_ligacao": propos,
        "resumo": _texto(bruto.get("resumo"), LIMITES_TEXTO["resumo"]),
        "ponto_forte": _texto(bruto.get("ponto_forte"), LIMITES_TEXTO["ponto_forte"]) if eh_lead else None,
        "pode_melhorar": _texto(bruto.get("pode_melhorar"), LIMITES_TEXTO["pode_melhorar"]) if eh_lead else None,
        "proxima_acao": _texto(bruto.get("proxima_acao"), LIMITES_TEXTO["proxima_acao"]),
        "mensagem_sugerida": _texto(bruto.get("mensagem_sugerida"), LIMITES_TEXTO["mensagem_sugerida"]),
    }

    conhecidas = set(metodo["tecnicas"])
    def _ids(valor):
        itens = valor if isinstance(valor, list) else []
        return [i for i in dict.fromkeys(str(x).strip().upper() for x in itens) if i in conhecidas]

    objecoes = []
    for o in (bruto.get("objecoes") if isinstance(bruto.get("objecoes"), list) else [])[:5]:
        if isinstance(o, dict) and _texto(o.get("tipo"), 40):
            objecoes.append({"tipo": _texto(o.get("tipo"), 40).lower(), "trecho": _texto(o.get("trecho"), 200)})

    dados = {
        "notas": notas,
        "objecoes": objecoes if eh_lead else [],
        "tecnicas_aplicadas": _ids(bruto.get("tecnicas_aplicadas")) if eh_lead else [],
        "tecnicas_a_reforcar": _ids(bruto.get("tecnicas_a_reforcar")) if eh_lead else [],
        "como_poderia_ser": _texto(bruto.get("como_poderia_ser"), LIMITES_TEXTO["como_poderia_ser"]) if eh_lead else None,
        "trecho_ligacao": _texto(bruto.get("trecho_ligacao"), LIMITES_TEXTO["trecho_ligacao"]) if eh_lead else None,
        "metodo_fonte": metodo["fonte"],
    }
    return campos, dados


# ---------------------------------------------------------------------------
# Uma conversa e uma rodada
# ---------------------------------------------------------------------------
def analisar_sessao(sessao, instrucoes, metodo, agora=None, ia=None):
    """Analisa e grava UMA conversa. Levanta IAErro / RespostaInvalida se falhar."""
    ia = ia or ia_cliente
    agora = agora or datetime.now(timezone.utc)
    telefone, inicio = sessao["telefone"], sessao["inicio"]
    # só as mensagens DESTA conversa (para antes da pausa que abre a próxima)
    sessoes = separar_sessoes(mensagens_repo.listar_brutas(telefone, inicio), config.GAP_NOVA_CONVERSA_HORAS)
    mensagens = sessoes[0] if sessoes else []
    if not any(m["origem"] == "cliente" for m in mensagens):
        return None
    mesma = analises_repo.da_sessao(telefone, inicio) if sessao.get("analise_id") else None
    if mesma and mesma.get("status") != "ok":
        mesma = None
    anterior = analises_repo.anterior_do_contato(telefone, inicio) if not sessao.get("nova") else None

    entrada = montar_entrada(sessao, mensagens, mesma, anterior, agora)
    bruto, uso = ia.gerar_json(instrucoes, entrada, max_tokens=3000)
    campos, dados = validar_resposta(bruto, metodo, notas_de_tempo(sessao, agora))
    analises_repo.salvar(
        telefone, inicio, mensagens[-1]["ts"], campos, dados,
        config.ANALISTA_IA_PROVEDOR, config.ANALISTA_IA_MODELO, uso["entrada"], uso["saida"],
    )
    return campos


def analisar_pendentes(agora=None, ia=None, dias=None):
    """
    Uma rodada: analisa as conversas pendentes (dos últimos "dias" dias), uma de cada vez, respeitando os
    limites de quantidade e de tempo. Nunca levanta exceção (o resumo precisa sair
    mesmo se a IA estiver fora do ar).
    Devolve {"ligada", "analisadas", "erros", "restantes", "parou_por"}.
    """
    ia = ia or ia_cliente
    resultado = {"ligada": ia.ia_ligada(), "analisadas": 0, "erros": 0, "restantes": 0, "parou_por": None}
    if not resultado["ligada"]:
        return resultado
    agora = agora or datetime.now(timezone.utc)
    try:
        metodo = carregar_metodo()
        instrucoes = montar_instrucoes(metodo)
        desde = agora - timedelta(days=dias or config.ANALISTA_IA_DIAS_PARA_TRAS)
        fila = analises_repo.pendentes(desde, excluir=_ignorados(), limite=config.ANALISTA_IA_MAX_CONVERSAS_RODADA)
    except Exception as e:
        print(f"❌ [Analista IA] Não consegui montar a fila: {e}")
        resultado["parou_por"] = "erro"
        return resultado

    limite_seg = config.ANALISTA_IA_MAX_MINUTOS_RODADA * 60
    comeco, erros_seguidos = time.monotonic(), 0
    for i, sessao in enumerate(fila):
        if time.monotonic() - comeco >= limite_seg:
            resultado["parou_por"] = "tempo"
            resultado["restantes"] = len(fila) - i
            break
        try:
            analisar_sessao(sessao, instrucoes, metodo, agora=agora, ia=ia)
            resultado["analisadas"] += 1
            erros_seguidos = 0
        except (ia_cliente.IAErro, RespostaInvalida) as e:
            resultado["erros"] += 1
            erros_seguidos += 1
            print(f"⚠️ [Analista IA] Falha em {sessao['telefone']}: {e}")
            try:
                analises_repo.registrar_erro(sessao["telefone"], sessao["inicio"], str(e))
            except Exception as e2:
                print(f"⚠️ [Analista IA] Nem o erro consegui gravar: {e2}")
            if erros_seguidos >= 3:   # API provavelmente fora do ar: não adianta insistir
                resultado["parou_por"] = "erros"
                resultado["restantes"] = len(fila) - i - 1
                break
        except Exception as e:
            resultado["erros"] += 1
            print(f"❌ [Analista IA] Erro inesperado em {sessao['telefone']}: {e}")
    return resultado
