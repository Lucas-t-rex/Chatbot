"""
ANALISTA — métricas de atendimento calculadas SEM IA.

Só funções puras: recebem mensagens, devolvem números. Não falam com banco,
WhatsApp nem Gemini — por isso dá pra testar tudo em tests/test_analista.py.

Uma mensagem aqui é qualquer objeto/dict com:
    ts      datetime COM fuso
    origem  'cliente' | 'atendente' | 'bot' | 'sistema'
    tipo    'texto' | 'audio' | 'midia' | 'ferramenta' | 'sistema'
    text    str
"""
import re
from datetime import datetime, time, timedelta

EQUIPE = ("atendente", "bot")

_RE_SAUDACAO = re.compile(
    r"\b(bom dia|boa tarde|boa noite|ol[aá]|oi+e?|seja bem[\s-]?vind[oa]|tudo bem|tudo bom|como vai)\b",
    re.IGNORECASE,
)
_RE_PEDIU_NOME = re.compile(
    r"(seu nome|qual (é |e )?o (seu )?nome|como (voc[eê]|vc) se chama|com quem (eu )?falo"
    r"|nome completo|me (diz|diga|fala|fale|informa|informe) (o )?seu nome)",
    re.IGNORECASE,
)
_RE_CTA = re.compile(
    r"(agend|marcar|aula experimental|aula gr[aá]tis|visita|vem conhecer|venha conhecer"
    r"|vem treinar|venha treinar|passa aqui|passe aqui|matr[ií]cula|matricular|link"
    r"|posso (te )?reservar|hor[aá]rio dispon)",
    re.IGNORECASE,
)


# Mensagens de encerramento do cliente: não esperam resposta, então não abrem
# "bloco de espera" (nem entram em "sem resposta"). Só vale para texto digitado.
_ENCERRAMENTO_NUCLEO = {
    "obrigado", "obrigada", "obrigadão", "obrigadao", "obg", "obgd", "brigado", "brigada",
    "valeu", "vlw", "vlww", "valew", "tchau", "xau", "flw", "falou", "fui", "tmj",
    "ok", "okk", "okay", "blz", "beleza", "certo", "combinado", "show", "joia", "joinha",
    "perfeito", "entendi", "abraço", "abraco", "abs", "nada", "disponha", "imagina",
}
# Palavras que acompanham o núcleo ("muito obrigado", "ok, valeu pela ajuda", "até mais").
# Sozinhas não encerram nada. "sim"/"isso" ficam de fora de propósito: respondem pergunta.
_ENCERRAMENTO_ENCHIMENTO = {
    "muito", "mto", "mt", "demais", "mesmo", "pela", "pelo", "ajuda", "atenção", "atencao",
    "a", "o", "e", "vc", "você", "voce", "pra", "para", "vocês", "voces", "de", "tá", "ta",
    "até", "ate", "logo", "mais", "amanhã", "amanha", "depois",
}
_RE_PALAVRA = re.compile(r"[a-zà-ú]+", re.IGNORECASE)


def eh_encerramento(texto):
    """True se a mensagem é só um agradecimento/despedida/'ok' (sem pergunta nova)."""
    t = (texto or "").strip().lower()
    if not t or "?" in t:
        return False
    palavras = _RE_PALAVRA.findall(t)
    if not palavras:
        # só emoji/pontuação (👍, 🙏, 😊...)
        return True
    if len(palavras) > 8:
        return False
    return (all(p in _ENCERRAMENTO_NUCLEO or p in _ENCERRAMENTO_ENCHIMENTO for p in palavras)
            and any(p in _ENCERRAMENTO_NUCLEO for p in palavras))


def _campo(m, nome):
    return m[nome] if isinstance(m, dict) else getattr(m, nome)


def eh_relevante(m):
    """Conta para a análise: fala de cliente, atendente ou bot (não log interno)."""
    return _campo(m, "origem") != "sistema" and _campo(m, "tipo") not in ("ferramenta", "sistema")


def separar_sessoes(mensagens, gap_horas):
    """
    Quebra as mensagens (já em ordem) em conversas. Uma pausa de 'gap_horas'
    ou mais começa uma conversa nova.
    """
    relevantes = [m for m in mensagens if eh_relevante(m)]
    sessoes, atual = [], []
    limite = timedelta(hours=gap_horas)
    for m in relevantes:
        if atual and _campo(m, "ts") - _campo(atual[-1], "ts") >= limite:
            sessoes.append(atual)
            atual = []
        atual.append(m)
    if atual:
        sessoes.append(atual)
    return sessoes


def _localizar(tz, dt_naive):
    # pytz precisa de localize(); zoneinfo aceita replace(tzinfo=...)
    if hasattr(tz, "localize"):
        return tz.localize(dt_naive)
    return dt_naive.replace(tzinfo=tz)


def _hora(hhmm):
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def segundos_uteis(inicio, fim, horario, tz):
    """
    Segundos entre 'inicio' e 'fim' contando só o horário de atendimento.
    horario = {0: [{"inicio": "05:00", "fim": "22:00"}], ..., 6: []} (0 = segunda)
    """
    if fim <= inicio:
        return 0
    a = inicio.astimezone(tz)
    b = fim.astimezone(tz)
    total = 0.0
    dia = a.date()
    for _ in range(400):  # trava de segurança: no máximo ~1 ano
        if dia > b.date():
            break
        for bloco in horario.get(dia.weekday(), []):
            ini = _localizar(tz, datetime.combine(dia, _hora(bloco["inicio"])))
            fim_bloco = _localizar(tz, datetime.combine(dia, _hora(bloco["fim"])))
            o_ini, o_fim = max(ini, a), min(fim_bloco, b)
            if o_fim > o_ini:
                total += (o_fim - o_ini).total_seconds()
        dia += timedelta(days=1)
    return int(total)


def _media(valores):
    return int(round(sum(valores) / len(valores))) if valores else None


def calcular_sessao(mensagens, *, nova, horario, tz):
    """
    Métricas de UMA conversa (lista de mensagens relevantes, em ordem).

    Tempo de resposta: do início de cada bloco de mensagens do cliente até a
    primeira mensagem da equipe (atendente ou bot) depois dele.
    """
    if not mensagens:
        raise ValueError("Sessão vazia")

    primeira, ultima = mensagens[0], mensagens[-1]
    inicio, fim = _campo(primeira, "ts"), _campo(ultima, "ts")

    msgs_cliente = sum(1 for m in mensagens if _campo(m, "origem") == "cliente")
    msgs_atendente = sum(1 for m in mensagens if _campo(m, "origem") == "atendente")
    msgs_bot = sum(1 for m in mensagens if _campo(m, "origem") == "bot")

    # --- pares (cliente perguntou -> equipe respondeu) ---
    brutos, uteis, quem_respondeu = [], [], set()
    bloco_inicio = None
    for m in mensagens:
        origem = _campo(m, "origem")
        if origem == "cliente":
            # "obrigado", "ok", "👍" sozinhos não esperam resposta: não abrem bloco de espera
            encerramento = _campo(m, "tipo") == "texto" and eh_encerramento(_campo(m, "text"))
            if bloco_inicio is None and not encerramento:
                bloco_inicio = _campo(m, "ts")
        elif origem in EQUIPE:
            quem_respondeu.add(origem)
            if bloco_inicio is not None:
                brutos.append(int((_campo(m, "ts") - bloco_inicio).total_seconds()))
                uteis.append(segundos_uteis(bloco_inicio, _campo(m, "ts"), horario, tz))
                bloco_inicio = None

    if quem_respondeu == {"atendente", "bot"}:
        respondida_por = "ambos"
    elif quem_respondeu:
        respondida_por = quem_respondeu.pop()
    else:
        respondida_por = "ninguem"

    iniciada_por = "cliente" if _campo(primeira, "origem") == "cliente" else "equipe"

    # --- sinais simples (não são nota; a nota é do agente externo) ---
    equipe = [m for m in mensagens if _campo(m, "origem") in EQUIPE]
    textos_equipe = [(_campo(m, "text") or "") for m in equipe if _campo(m, "tipo") == "texto"]

    # Cordialidade inicial: a primeira fala da equipe na conversa tem saudação?
    primeira_resposta_txt = (_campo(equipe[0], "text") or "") if equipe else None

    tem_equipe = bool(equipe)
    todo_texto_equipe = "\n".join(textos_equipe)

    return {
        "inicio": inicio,
        "fim": fim,
        "duracao_seg": int((fim - inicio).total_seconds()),
        "iniciada_por": iniciada_por,
        "nova": bool(nova),
        "msgs_cliente": msgs_cliente,
        "msgs_atendente": msgs_atendente,
        "msgs_bot": msgs_bot,
        "respostas": len(brutos),
        "primeira_resposta_seg": brutos[0] if brutos else None,
        "primeira_resposta_util_seg": uteis[0] if uteis else None,
        "tempo_medio_resposta_seg": _media(brutos),
        "tempo_medio_resposta_util_seg": _media(uteis),
        "tempo_max_resposta_util_seg": max(uteis) if uteis else None,
        "respondida_por": respondida_por,
        "ultima_msg_de": _campo(ultima, "origem"),
        "aguardando_desde": bloco_inicio,  # != None: o cliente está sem resposta
        "sinal_saudacao": bool(_RE_SAUDACAO.search(primeira_resposta_txt)) if primeira_resposta_txt is not None else None,
        "sinal_pediu_nome": bool(_RE_PEDIU_NOME.search(todo_texto_equipe)) if tem_equipe else None,
        "sinal_cta": bool(_RE_CTA.search(todo_texto_equipe)) if tem_equipe else None,
        # Equipe puxou a conversa de novo com alguém que já tinha conversado antes
        "sinal_followup": iniciada_por == "equipe" and not nova,
        "media_caracteres_equipe": _media([len(t) for t in textos_equipe]),
        "maior_msg_equipe": max((len(t) for t in textos_equipe), default=None),
    }


def esperando_ha(aguardando_desde, agora, horario, tz):
    """(segundos brutos, segundos úteis) que o cliente está esperando."""
    if aguardando_desde is None:
        return None, None
    return (
        int((agora - aguardando_desde).total_seconds()),
        segundos_uteis(aguardando_desde, agora, horario, tz),
    )


def formatar_duracao(segundos):
    """45 -> '45 s' | 360 -> '6 min' | 4800 -> '1 h 20 min' | 90000 -> '1 d 1 h'"""
    if segundos is None:
        return "-"
    segundos = int(segundos)
    if segundos < 60:
        return f"{segundos} s"
    minutos = segundos // 60
    if minutos < 60:
        return f"{minutos} min"
    horas, resto = divmod(minutos, 60)
    if horas < 24:
        return f"{horas} h {resto} min" if resto else f"{horas} h"
    dias, horas = divmod(horas, 24)
    return f"{dias} d {horas} h" if horas else f"{dias} d"
