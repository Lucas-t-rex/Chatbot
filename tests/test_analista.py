"""
Testes das métricas do Analista (sem banco, sem IA).
Rodar: python -m pytest   (dentro de chatbot-python/)
"""
from datetime import datetime

import pytz

from app.services.analista_metricas import (
    calcular_sessao,
    esperando_ha,
    formatar_duracao,
    segundos_uteis,
    separar_sessoes,
)

TZ = pytz.timezone("America/Sao_Paulo")
# Seg-qui 05h-22h, sex 05h-21h, fim de semana fechado (igual ao config)
HORARIO = {
    0: [{"inicio": "05:00", "fim": "22:00"}],
    1: [{"inicio": "05:00", "fim": "22:00"}],
    2: [{"inicio": "05:00", "fim": "22:00"}],
    3: [{"inicio": "05:00", "fim": "22:00"}],
    4: [{"inicio": "05:00", "fim": "21:00"}],
    5: [],
    6: [],
}


def ts(dia, hora, minuto, segundo=0):
    """Outubro/2026: dia 5 = segunda, 9 = sexta, 12 = segunda."""
    return TZ.localize(datetime(2026, 10, dia, hora, minuto, segundo))


def msg(dia, hora, minuto, origem, text="ok", tipo="texto"):
    return {"ts": ts(dia, hora, minuto), "origem": origem, "tipo": tipo, "text": text}


def calcular(mensagens, nova=True):
    return calcular_sessao(mensagens, nova=nova, horario=HORARIO, tz=TZ)


# ---------------------------------------------------------------------------
# Tempo de resposta
# ---------------------------------------------------------------------------
def test_primeira_resposta_media_e_maximo():
    s = calcular([
        msg(5, 10, 0, "cliente", "Oi, quanto custa?"),
        msg(5, 10, 1, "cliente", "É pra musculação"),
        msg(5, 10, 5, "atendente", "Bom dia! Te passo os valores"),   # 5 min (conta do 1º msg do bloco)
        msg(5, 10, 10, "cliente", "E o horário?"),
        msg(5, 10, 12, "atendente", "Das 5h às 22h"),                  # 2 min
    ])
    assert s["primeira_resposta_seg"] == 300
    assert s["primeira_resposta_util_seg"] == 300
    assert s["respostas"] == 2
    assert s["tempo_medio_resposta_seg"] == 210
    assert s["tempo_max_resposta_util_seg"] == 300
    assert s["ultima_msg_de"] == "atendente"
    assert s["aguardando_desde"] is None
    assert s["respondida_por"] == "atendente"
    assert s["msgs_cliente"] == 3 and s["msgs_atendente"] == 2 and s["msgs_bot"] == 0
    assert s["duracao_seg"] == 12 * 60
    assert s["iniciada_por"] == "cliente"


def test_cliente_sem_resposta_marca_aguardando_desde_o_inicio_do_bloco():
    s = calcular([
        msg(5, 9, 0, "cliente", "Oi, quero saber dos planos"),
        msg(5, 9, 2, "atendente"),
        msg(5, 9, 30, "cliente", "Ainda tem vaga?"),
        msg(5, 9, 31, "cliente", "Alguém aí?"),
    ])
    assert s["ultima_msg_de"] == "cliente"
    assert s["aguardando_desde"] == ts(5, 9, 30)
    assert s["respostas"] == 1


def test_ninguem_respondeu():
    s = calcular([msg(5, 9, 0, "cliente")])
    assert s["respondida_por"] == "ninguem"
    assert s["primeira_resposta_seg"] is None
    assert s["tempo_medio_resposta_seg"] is None
    assert s["sinal_saudacao"] is None and s["sinal_cta"] is None


def test_bot_e_atendente_na_mesma_conversa():
    s = calcular([
        msg(5, 9, 0, "cliente"),
        msg(5, 9, 0, "bot"),
        msg(5, 9, 10, "cliente"),
        msg(5, 9, 20, "atendente"),
    ])
    assert s["respondida_por"] == "ambos"
    assert s["msgs_bot"] == 1 and s["msgs_atendente"] == 1


# ---------------------------------------------------------------------------
# Horário de atendimento (tempo útil)
# ---------------------------------------------------------------------------
def test_tempo_util_atravessando_a_noite():
    # seg 21:50 -> ter 05:10 = 10 min (até 22h) + 10 min (a partir das 5h)
    assert segundos_uteis(ts(5, 21, 50), ts(6, 5, 10), HORARIO, TZ) == 20 * 60


def test_tempo_util_atravessando_o_fim_de_semana():
    # sex 20:50 -> seg 05:05 = 10 min (sex fecha 21h) + 5 min (seg)
    assert segundos_uteis(ts(9, 20, 50), ts(12, 5, 5), HORARIO, TZ) == 15 * 60


def test_mensagem_da_madrugada_respondida_cedo_nao_destroi_a_media():
    s = calcular([
        msg(5, 23, 30, "cliente", "Vocês abrem amanhã?"),
        msg(6, 5, 3, "atendente", "Bom dia! Abrimos às 5h"),
    ])
    assert s["primeira_resposta_seg"] == (5 * 60 + 33) * 60   # bruto: 5h33
    assert s["primeira_resposta_util_seg"] == 3 * 60            # útil: 3 min


def test_esperando_ha():
    bruto, util = esperando_ha(ts(5, 21, 50), ts(6, 5, 10), HORARIO, TZ)
    assert bruto == (7 * 60 + 20) * 60
    assert util == 20 * 60
    assert esperando_ha(None, ts(5, 10, 0), HORARIO, TZ) == (None, None)


# ---------------------------------------------------------------------------
# Separação em conversas
# ---------------------------------------------------------------------------
def test_pausa_longa_comeca_conversa_nova_e_ignora_logs_internos():
    mensagens = [
        msg(5, 9, 0, "cliente"),
        msg(5, 9, 1, "bot", "Chamando função: fn_x()", tipo="ferramenta"),
        msg(5, 9, 2, "atendente"),
        msg(5, 20, 0, "cliente"),             # 11h depois: mesma conversa
        msg(6, 10, 0, "cliente"),             # 14h depois: conversa nova
        msg(6, 10, 1, "sistema", "log", tipo="sistema"),
    ]
    sessoes = separar_sessoes(mensagens, gap_horas=12)
    assert [len(s) for s in sessoes] == [3, 1]


def test_followup_e_quando_a_equipe_retoma_um_contato_antigo():
    s = calcular([
        msg(6, 10, 0, "atendente", "Oi Ana! Conseguiu ver os horários?"),
        msg(6, 10, 30, "cliente", "Ainda não"),
    ], nova=False)
    assert s["iniciada_por"] == "equipe"
    assert s["sinal_followup"] is True
    # 1º contato puxado pela equipe com alguém novo NÃO é follow-up
    s2 = calcular([msg(6, 10, 0, "atendente")], nova=True)
    assert s2["sinal_followup"] is False


# ---------------------------------------------------------------------------
# Sinais
# ---------------------------------------------------------------------------
def test_sinais_de_saudacao_nome_e_cta():
    s = calcular([
        msg(5, 9, 0, "cliente", "quanto é a mensalidade"),
        msg(5, 9, 1, "atendente", "Boa tarde! Com quem eu falo?"),
        msg(5, 9, 2, "cliente", "Carla"),
        msg(5, 9, 3, "atendente", "Carla, quer agendar uma aula experimental amanhã?"),
    ])
    assert s["sinal_saudacao"] is True
    assert s["sinal_pediu_nome"] is True
    assert s["sinal_cta"] is True


def test_resposta_seca_sem_sinais():
    s = calcular([
        msg(5, 9, 0, "cliente", "valor?"),
        msg(5, 9, 1, "atendente", "R$ 99,90"),
    ])
    assert s["sinal_saudacao"] is False
    assert s["sinal_pediu_nome"] is False
    assert s["sinal_cta"] is False
    assert s["media_caracteres_equipe"] == len("R$ 99,90")


def test_formatar_duracao():
    assert formatar_duracao(None) == "-"
    assert formatar_duracao(45) == "45 s"
    assert formatar_duracao(360) == "6 min"
    assert formatar_duracao(3600) == "1 h"
    assert formatar_duracao(4800) == "1 h 20 min"
    assert formatar_duracao(90000) == "1 d 1 h"


# ---------------------------------------------------------------------------
# Encerramento ("obrigado", "ok", "👍") não conta como cliente esperando
# ---------------------------------------------------------------------------
def test_eh_encerramento():
    from app.services.analista_metricas import eh_encerramento
    for texto in ["Obrigado", "obg!", "Vlw", "valeu, tchau", "Ok", "joinha", "👍", "🙏",
                  "muito obrigado pela ajuda", "Falou", "FLW", "até mais, obrigado"]:
        assert eh_encerramento(texto), texto
    for texto in ["Sim", "isso", "Quanto custa?", "ok mas qual o horário?", "obrigado, e a matrícula?",
                  "Boa tarde", "Quero agendar uma aula", "", "?"]:
        assert not eh_encerramento(texto), texto


def test_obrigado_no_final_nao_deixa_cliente_esperando():
    s = calcular([
        msg(5, 10, 0, "cliente", "Quanto custa?"),
        msg(5, 10, 2, "atendente", "R$ 120 por mês"),
        msg(5, 10, 3, "cliente", "Obrigado!"),
    ])
    assert s["aguardando_desde"] is None
    assert s["respostas"] == 1


def test_ok_depois_de_so_um_momento_nao_conta_espera():
    s = calcular([
        msg(5, 10, 0, "cliente", "Quero falar com um professor"),
        msg(5, 10, 1, "atendente", "Só um momento que já chamo ele"),
        msg(5, 10, 2, "cliente", "ok"),
    ])
    assert s["aguardando_desde"] is None


def test_pergunta_depois_do_obrigado_continua_esperando():
    s = calcular([
        msg(5, 10, 0, "cliente", "Quanto custa?"),
        msg(5, 10, 2, "atendente", "R$ 120"),
        msg(5, 10, 3, "cliente", "Obrigado, e tem aula experimental?"),
    ])
    assert s["aguardando_desde"] == ts(5, 10, 3)


def test_cliente_que_se_despede_depois_de_ficar_sem_resposta_nao_esta_esperando():
    s = calcular([
        msg(5, 10, 0, "cliente", "Vocês abrem sábado?"),
        msg(5, 14, 0, "cliente", "Ok, obrigado"),
    ])
    assert s["aguardando_desde"] is None


def test_despedida_por_audio_continua_para_a_ia_decidir():
    s = calcular([
        msg(5, 10, 0, "cliente", "Vocês abrem sábado?"),
        msg(5, 14, 0, "cliente", "[Áudio do cliente]: valeu", tipo="audio"),
    ])
    assert s["aguardando_desde"] == ts(5, 10, 0)


# ---------------------------------------------------------------------------
# Responsáveis (quem recebe os resumos)
# ---------------------------------------------------------------------------
def test_lista_de_numeros_junta_e_tira_repetidos():
    from app.core.config import _lista_de_numeros
    assert _lista_de_numeros("5544999059174, 554491216103", "554499059174,554498314311") == [
        "554499059174", "554491216103", "554498314311"]
    assert _lista_de_numeros("", None) == []
