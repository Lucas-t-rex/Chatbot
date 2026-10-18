from app.services.analista import texto_resumo, _telefone_legivel


def _resumo(**extra):
    base = {
        "conversas": 8, "novas": 5, "retomadas": 3, "followups_da_equipe": 0,
        "msgs_cliente": 25, "msgs_equipe": 17,
        "com_primeira_resposta": 8, "respondidas_no_limite": 7,
        "primeira_resposta_media_util_seg": 240, "pior_espera_util_seg": 2280,
        "respondidas_por": {"atendente": 8, "bot": 0, "ambos": 0, "ninguem": 0},
        "sem_resposta": [],
    }
    base.update(extra)
    return base


def test_telefone_legivel():
    assert _telefone_legivel("5544991586345") == "(44) 99158-6345"
    assert _telefone_legivel("554491586345") == "(44) 9158-6345"
    assert _telefone_legivel("abc") == "abc"


def test_resumo_completo():
    texto = texto_resumo(_resumo(), "dia 17/10 (00h às 18h)")
    assert "8 conversas (5 clientes novos · 3 clientes que voltaram)" in texto
    assert "42 mensagens: 25 dos clientes · 17 da equipe" in texto
    assert "Dentro de 15 min: 7 de 8 (88%)" in texto
    assert "Nenhum cliente esperando resposta agora" in texto


def test_resumo_lista_quem_esta_esperando():
    pendentes = [{
        "telefone": "5544991586345", "nome": "Maria", "esperando_util_seg": 2400,
        "nova": True, "ultima_msg": "Quanto custa a mensalidade?",
    }]
    texto = texto_resumo(_resumo(sem_resposta=pendentes), "manhã")
    assert "Esperando resposta agora (1)" in texto
    assert "1 cliente(s) novo(s)" in texto
    assert "🆕 Maria (44) 99158-6345 - há 40 min" in texto


def test_resumo_sem_conversas():
    texto = texto_resumo(_resumo(conversas=0, novas=0, retomadas=0), "manhã")
    assert "Nenhuma conversa com clientes neste período." in texto
    assert "Velocidade" not in texto
