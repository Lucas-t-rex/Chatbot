import json
from datetime import date, datetime
from decimal import Decimal

import pytz

from app.services import analista_relatorios as rel

TZ = pytz.timezone("America/Sao_Paulo")


def ts(dia, hora, minuto=0):
    return TZ.localize(datetime(2026, 10, dia, hora, minuto))


def script_base(**extra):
    base = {
        "titulo": "Script da academia", "fonte": "curso", "conversas": 10, "aderencia": 68,
        "feitas": 34, "oportunidades": 50, "seguiram": 4, "em_parte": 3, "normal": 3,
        "passos": [
            {"id": "S01", "rotulo": "Apresentação", "feitas": 8, "oportunidades": 10},
            {"id": "S02", "rotulo": "Propôs ligar", "feitas": 3, "oportunidades": 10},
            {"id": "S04", "rotulo": "Descobriu objetivo e dor", "feitas": 5, "oportunidades": 9},
            {"id": "S07", "rotulo": "Preço com valor", "feitas": 4, "oportunidades": 6},
            {"id": "S09", "rotulo": "Convite com dia e hora", "feitas": 4, "oportunidades": 8},
        ],
        "mais_seguido": {"id": "S01", "nome": "Acolhimento e apresentação", "feitas": 8, "oportunidades": 10},
        "mais_esquecido": {"id": "S02", "nome": "Propor ligação cedo", "feitas": 3, "oportunidades": 10},
        "termometro": [{"perfil": "comprador", "qtd": 2}, {"perfil": "comparador", "qtd": 3},
                       {"perfil": "pesquisador", "qtd": 4}, {"perfil": "incomodado", "qtd": 1}],
        "funil": {"acolhidos": 12, "descobriram": 8, "proximo_passo": 5, "agendaram": 4, "com_etapa": 13},
        "exemplo": None,
    }
    base.update(extra)
    return base


def agg_base(**extra):
    base = {
        "periodo": {}, "conversas": 14, "novas": 6, "retomadas": 8, "followups_da_equipe": 0,
        "msgs_cliente": 40, "msgs_equipe": 30,
        "com_primeira_resposta": 14, "respondidas_no_limite": 12,
        "primeira_resposta_media_util_seg": 240, "pior_espera_util_seg": 1800,
        "respondidas_por": {"atendente": 14, "bot": 0, "ambos": 0, "ninguem": 0},
        "sem_resposta": [],
        "analisaveis": 14, "analisadas": 14, "leads": 12,
        "sucesso": 4, "em_andamento": 6, "perdido": 1, "nao_lead": 2,
        "perdidos_por_motivo": {"preco": 1},
        "nota_media": 7.8, "nota_venda": 7.5, "propos_ligacao": 3,
        "script": script_base(),
        "atencao_total": 2,
        "pedem_atencao": [
            {"nome": "Maria", "telefone": "5544991234567", "motivo": "Pediu aula teste e ficou sem data",
             "temperatura": "comprador"},
            {"nome": "João", "telefone": "554488881111", "motivo": None, "temperatura": "comparador"},
        ],
        "objecoes": [{"tipo": "preco", "qtd": 9}, {"tipo": "horario", "qtd": 4}],
        "por_dia": {}, "em_aberto_anteriores": 3, "anterior": None, "sintese": None,
    }
    base.update(extra)
    return base


ONTEM = {"referencia": "2026-10-19", "rotulo": "ontem", "conversas": 10, "nota_media": 7.2, "aderencia": 55}

BLOCO_22H = """📘 *Script da academia hoje* (10 conversas de venda)
• Aderência: 68% (ontem 55%) · 4 seguiram o script · 3 em parte · 3 atendimento normal
• Apresentação: 8 de 10
• Propôs ligar: 3 de 10
• Descobriu objetivo e dor: 5 de 9
• Preço com valor: 4 de 6
• Convite com dia e hora: 4 de 8
✔️ Mais seguido: acolhimento e apresentação · 🔸 Mais esquecido: propor ligação cedo
🌡️ Termômetro: 2 compradores · 3 comparadores · 4 pesquisadores · 1 incomodado"""


# ---------------------------------------------------------------------------
# Resumo do dia (22h)
# ---------------------------------------------------------------------------
def test_diario_completo():
    agg = agg_base(
        anterior=dict(ONTEM),
        sintese={"destaque": "A Maria foi acolhida e saiu com aula marcada.", "dica": "Convide com dia e hora."},
        sem_resposta=[{"telefone": "5544977772222", "nome": "Carla", "esperando_seg": 2400, "esperando_util_seg": 2400,
                       "nova": True, "ultima_msg": "Tem vaga à noite?"}],
    )
    texto = rel.texto_diario(agg, "dia 20/10 (00h às 22h)")
    assert texto.startswith("📊 *Atendimento - dia 20/10 (00h às 22h)*")
    assert "• 14 conversas (ontem 10)" in texto
    assert "6 clientes novos · 8 clientes que voltaram" in texto
    assert "Também em aberto de dias anteriores: 3" in texto
    assert "✅ 4 com sucesso (agendaram ou fecharam)" in texto
    assert "💬 6 em andamento" in texto
    assert "↩️ 1 desistiu (preço)" in texto
    assert "➖ 2 não eram venda" in texto
    assert "Dentro de 15 min: 12 de 14 (86%)" in texto
    assert "Nota média: 7,8 (ontem 7,2) 📈" in texto
    assert "Intuito de venda: 7,5 · Propôs ligação: 3 de 12" in texto
    assert "🌟 Muito bom: A Maria foi acolhida" in texto and "💡 Dica: Convide" in texto
    assert BLOCO_22H in texto
    assert "Boas práticas" not in texto and "desde as" not in texto
    assert "⚠️ *Pedem atenção (2)*" in texto
    assert "🔥 Maria (44) 99123-4567 — Pediu aula teste e ficou sem data" in texto
    assert "• João (44) 8888-1111" in texto                      # comparador: sem 🔥
    assert "🆕 Carla (44) 97777-2222 - há 40 min" in texto
    assert "ℹ️" not in texto          # IA analisou tudo: sem aviso
    # ordem dos blocos: qualidade -> script -> pedem atenção
    assert texto.index("*Qualidade do atendimento*") < texto.index("📘") < texto.index("⚠️ *Pedem atenção")


def test_bloco_do_script_sem_comparacao_e_com_singulares():
    sc = script_base(conversas=1, aderencia=100, seguiram=1, em_parte=0, normal=0,
                     passos=[{"id": "S01", "rotulo": "Apresentação", "feitas": 1, "oportunidades": 1}],
                     mais_seguido=None, mais_esquecido=None,
                     termometro=[{"perfil": "comprador", "qtd": 1}, {"perfil": "tranquilo", "qtd": 2}])
    texto = rel.texto_diario(agg_base(script=sc), "x")
    assert ("📘 *Script da academia hoje* (1 conversa de venda)\n"
            "• Aderência: 100% · 1 seguiu o script\n"
            "• Apresentação: 1 de 1\n"
            "🌡️ Termômetro: 1 comprador · 2 tranquilos") in texto
    assert "Mais seguido" not in texto and "em parte" not in texto


def test_diario_enxuto_quando_nao_tem_o_opcional():
    agg = agg_base(
        conversas=1, novas=1, retomadas=0, sucesso=1, em_andamento=0, perdido=0, nao_lead=0, leads=1,
        perdidos_por_motivo={}, em_aberto_anteriores=0, pedem_atencao=[], atencao_total=0,
        script=script_base(conversas=0, aderencia=None, passos=[], termometro=[], mais_seguido=None,
                           mais_esquecido=None),
        analisaveis=2, analisadas=1,
    )
    texto = rel.texto_diario(agg, "dia 20/10 (00h às 22h)")
    assert "• 1 conversa\n" in texto and "1 cliente novo · 0 clientes que voltaram" in texto
    assert "✅ 1 com sucesso (agendou ou fechou)" in texto
    assert "desistiu" not in texto and "Também em aberto" not in texto
    assert "Pedem atenção" not in texto and "📘" not in texto
    assert "✅ Nenhum cliente esperando resposta agora." in texto
    assert "ℹ️ A IA analisou 1 de 2 conversas; as outras entram no próximo resumo." in texto
    assert "Nota média: 7,8\n" in texto            # sem resumo anterior: sem comparação


def test_diario_motivos_de_perda_e_sem_queda_de_nota():
    agg = agg_base(perdido=3, perdidos_por_motivo={"preco": 2, "horario": 1},
                   anterior={**ONTEM, "conversas": 14, "nota_media": 7.7, "aderencia": None})
    texto = rel.texto_diario(agg, "x")
    assert "↩️ 3 desistiram (preço 2, horário 1)" in texto
    assert "(ontem 7,7)\n" in texto and "📈" not in texto and "📉" not in texto
    assert "• 14 conversas (ontem 14)" in texto
    assert "• Aderência: 68% · 4 seguiram" in texto             # ontem sem aderência: não compara


def test_diario_depois_do_fim_de_semana_compara_com_a_sexta():
    texto = rel.texto_diario(agg_base(anterior={**ONTEM, "rotulo": "sexta", "nota_media": 8.5}), "x")
    assert "• 14 conversas (sexta 10)" in texto and "Nota média: 7,8 (sexta 8,5) 📉" in texto
    assert "Aderência: 68% (sexta 55%)" in texto


# ---------------------------------------------------------------------------
# Resumo da semana
# ---------------------------------------------------------------------------
EXEMPLO = {"tecnica": "S09", "nome_tecnica": "Convite com dia e hora", "cliente": "Maria",
           "cliente_disse": "Posso fazer uma aula teste?", "equipe_respondeu": "Pode sim, quando quiser passar aqui",
           "pelo_script": "Pode sim, Maria! Tenho amanhã às 18h ou quinta às 9h. Qual fica melhor?"}


def test_semanal_completo():
    por_dia = {
        "2026-10-12": {"conversas": 8, "nota_media": 6.9}, "2026-10-14": {"conversas": 16, "nota_media": 7.4},
        "2026-10-16": {"conversas": 10, "nota_media": 7.8},
    }
    sintese = {
        "destaques": ["A Maria fechou aula teste.", "O João foi muito bem atendido."],
        "treino": {"tema": "Convidar com dia e hora", "exemplo": "\"Quer vir amanhã às 18h?\""},
        "objecoes": [{"tipo": "preco", "como_responder": "Mostrar o plano trimestral."}],
        "metodo": {"ja_faz": "A equipe já se apresenta bem.", "reforcar": None},
    }
    agg = agg_base(conversas=63, novas=28, retomadas=35, sucesso=14, leads=50, por_dia=por_dia, sintese=sintese,
                   script=script_base(exemplo=EXEMPLO))
    texto = rel.texto_semanal(agg, date(2026, 10, 12), date(2026, 10, 17),
                              anterior={"sucesso": 11, "nota_media": 7.0, "script": {"aderencia": 60}})
    assert texto.startswith("📈 *Resumo da semana - 12/10 a 17/10*")
    assert "63 conversas (28 clientes novos · 35 clientes que voltaram)" in texto
    assert "Melhor dia: quarta (16)" in texto
    assert "Taxa de sucesso: 28%" in texto
    assert "Comparado com a semana passada: +3 sucessos" in texto
    assert "Nota média: 7,8 📈 (segunda 6,9 → sexta 7,8)" in texto
    assert "🌟 *Destaques da semana*\n• A Maria fechou aula teste." in texto
    assert "🎯 *Para treinar na próxima semana*\n• Convidar com dia e hora - \"Quer vir amanhã às 18h?\"" in texto
    assert "• Preço (9×) - Mostrar o plano trimestral." in texto
    assert "• Horário (4×)\n" in texto + "\n"
    assert ("📘 *Script da academia na semana* (10 conversas de venda)\n"
            "• Aderência: 68% (semana passada 60%) · 4 seguiram o script") in texto
    assert "• Convite com dia e hora: 4 de 8" in texto and "🌡️ Termômetro: 2 compradores" in texto
    assert ("🪜 Funil: 12 acolhidos → 8 descobriram o objetivo → 5 com próximo passo → 4 agendaram\n"
            "• A equipe já se apresenta bem.") in texto
    assert ("🔎 *Um exemplo da semana* (Convite com dia e hora · Maria)\n"
            "Cliente: \"Posso fazer uma aula teste?\"\n"
            "Foi respondido: \"Pode sim, quando quiser passar aqui\"\n"
            "Pelo script: \"Pode sim, Maria! Tenho amanhã às 18h ou quinta às 9h. Qual fica melhor?\"") in texto
    assert texto.rstrip().endswith("📎 A planilha da semana está em anexo.")


def test_funil_e_exemplo_sem_dados_ou_no_singular():
    assert rel.texto_funil({"acolhidos": 0, "descobriram": 0, "proximo_passo": 0, "agendaram": 0,
                            "com_etapa": 0}) == ""
    assert rel.texto_funil(None) == ""
    assert rel.texto_funil({"acolhidos": 1, "descobriram": 1, "proximo_passo": 1, "agendaram": 1, "com_etapa": 1}) == \
        "1 acolhido → 1 descobriu o objetivo → 1 com próximo passo → 1 agendou"
    linhas = rel._linhas_exemplo({**EXEMPLO, "cliente_disse": None, "equipe_respondeu": None})
    assert linhas[1:] == ["Foi respondido: (ficou sem resposta)", f"Pelo script: \"{EXEMPLO['pelo_script']}\""]
    assert rel._linhas_exemplo(None) == []


def test_semanal_sem_ia_e_sem_conversas():
    sem_ia = rel.texto_semanal(agg_base(conversas=5, analisadas=0, analisaveis=5, leads=0, sucesso=0,
                                        em_andamento=0, perdido=0, nao_lead=0, nota_media=None, nota_venda=None,
                                        pedem_atencao=[], atencao_total=0, objecoes=[],
                                        script=script_base(conversas=0, aderencia=None, termometro=[],
                                                           funil={"com_etapa": 0})),
                               date(2026, 10, 12), date(2026, 10, 17))
    assert "*Volume*" in sem_ia and "*Velocidade da equipe*" in sem_ia
    assert "Qualidade" not in sem_ia and "Destaques" not in sem_ia and "📘" not in sem_ia and "Funil" not in sem_ia
    assert "ℹ️" not in sem_ia                 # IA desligada: não fala em "analisou 0 de 5"
    assert "📎 A planilha" in sem_ia
    vazio = rel.texto_semanal(agg_base(conversas=0), date(2026, 10, 12), date(2026, 10, 17))
    assert "Nenhuma conversa com clientes nesta semana." in vazio


# ---------------------------------------------------------------------------
# Síntese da IA
# ---------------------------------------------------------------------------
class IAFalsa:
    def __init__(self, resposta, ligada=True):
        self.resposta, self._ligada, self.entradas = resposta, ligada, []

    def ia_ligada(self):
        return self._ligada

    def gerar_json(self, sistema, usuario, max_tokens=0):
        self.entradas.append(usuario)
        if isinstance(self.resposta, Exception):
            raise self.resposta
        return self.resposta, {"entrada": 1, "saida": 1}


def _analise(nome="Maria Souza", eh_lead=True, **extra):
    base = {"nome": nome, "eh_lead": eh_lead, "desfecho": "sucesso", "nota_final": Decimal("8.5"),
            "resumo": "quer muay thai", "ponto_forte": "simpática", "pode_melhorar": "convidar"}
    base.update(extra)
    return base


def test_sintese_do_dia_usa_so_o_primeiro_nome_e_ignora_nao_leads():
    ia = IAFalsa({"destaque": "A Maria foi acolhida.", "dica": "Convide com dia e hora."})
    s = rel.sintetizar_dia([_analise(), _analise("Fornecedor X", eh_lead=False)], ia)
    assert s == {"destaque": "A Maria foi acolhida.", "dica": "Convide com dia e hora."}
    assert '"cliente": "Maria"' in ia.entradas[0] and "Souza" not in ia.entradas[0]
    assert "Fornecedor" not in ia.entradas[0]


def test_sintese_do_dia_falha_sem_derrubar():
    assert rel.sintetizar_dia([_analise()], IAFalsa(RuntimeError("fora do ar"))) is None
    assert rel.sintetizar_dia([_analise()], IAFalsa({}, ligada=False)) is None
    assert rel.sintetizar_dia([], IAFalsa({"destaque": "x"})) is None
    assert rel.sintetizar_dia([_analise()], IAFalsa({"destaque": None, "dica": ""})) is None


def test_sintese_da_semana_valida_o_formato_e_recebe_o_script():
    agg = agg_base()
    ia = IAFalsa({
        "destaques": ["Ótimo!", "Muito bem!", "terceiro (deve ser cortado)"],
        "treino": {"tema": "Convidar com dia e hora", "exemplo": "Quer vir amanhã?"},
        "objecoes": [{"tipo": "Preço", "como_responder": "Plano trimestral"}, {"tipo": "x"}, "lixo"],
        "metodo": "texto solto",
    })
    s = rel.sintetizar_semana(agg, [_analise()], ia)
    assert s["destaques"] == ["Ótimo!", "Muito bem!"]
    assert s["treino"]["tema"] == "Convidar com dia e hora"
    assert s["objecoes"] == [{"tipo": "preço", "como_responder": "Plano trimestral"}]
    assert s["metodo"] is None
    enviado = json.loads(ia.entradas[0])
    assert enviado["script"]["aderencia_pct"] == 68 and enviado["script"]["mais_esquecido"] == "Propor ligação cedo"
    assert "Convite com dia e hora: 4 de 8" in enviado["script"]["passos"]


# ---------------------------------------------------------------------------
# Montagem (banco simulado)
# ---------------------------------------------------------------------------
def test_diario_sem_nenhuma_analise_cai_no_resumo_so_com_numeros(monkeypatch):
    agg = agg_base(analisadas=0, leads=0)
    monkeypatch.setattr(rel, "agregar", lambda inicio, fim, agora=None, metodo=None: (agg, []))
    out = rel.montar_diario(22, agora=ts(20, 22))
    assert "*Movimento*" in out["texto"] and "Qualidade" not in out["texto"]    # formato antigo
    assert out["referencia"] == date(2026, 10, 20) and out["hora"] == 22


def test_diario_com_analises_compara_com_o_de_ontem(monkeypatch):
    agg = agg_base()
    pedidos = []
    monkeypatch.setattr(rel, "agregar", lambda inicio, fim, agora=None, metodo=None: (agg, [_analise()]))

    def anterior(ref):
        pedidos.append(ref)
        return {"referencia": date(2026, 10, 19), "hora": 22,
                "dados": {"conversas": 10, "nota_media": 7.2, "script": {"aderencia": 55}}}

    monkeypatch.setattr(rel.relatorios_repo, "diario_anterior", anterior)
    out = rel.montar_diario(22, agora=ts(20, 22), ia=IAFalsa(RuntimeError("fora")))
    assert pedidos == [date(2026, 10, 20)]                          # "antes de hoje"
    assert "(ontem 10)" in out["texto"] and "(ontem 7,2) 📈" in out["texto"] and "(ontem 55%)" in out["texto"]
    assert "🌟" not in out["texto"]                  # IA da síntese caiu: sai sem ela
    assert out["dados"]["anterior"]["rotulo"] == "ontem"
    json.dumps(out["dados"])


def test_comparacao_usa_o_dia_da_semana_e_ignora_resumo_muito_antigo(monkeypatch):
    def com(ref_anterior):
        monkeypatch.setattr(rel.relatorios_repo, "diario_anterior",
                            lambda ref: {"referencia": ref_anterior, "dados": {"conversas": 3, "nota_media": 6.0}})
        return rel._comparacao_com_ontem(date(2026, 10, 19))              # segunda-feira

    assert com(date(2026, 10, 16))["rotulo"] == "sexta"
    assert com(datetime(2026, 10, 18, 0, 0))["rotulo"] == "ontem"
    assert com("2026-10-18")["rotulo"] == "ontem"
    assert com(date(2026, 10, 9)) is None                                 # mais de 7 dias: não compara
    monkeypatch.setattr(rel.relatorios_repo, "diario_anterior", lambda ref: None)
    assert rel._comparacao_com_ontem(date(2026, 10, 19)) is None


def test_janela_da_semana_comeca_na_segunda():
    inicio, fim = rel.janela_da_semana(ts(17, 18))       # sábado 17/10/2026
    assert inicio == ts(12, 0) and fim == ts(17, 18)
    assert rel.janela_da_semana(ts(12, 7))[0] == ts(12, 0)   # a própria segunda


# ---------------------------------------------------------------------------
# Script da academia: aderência, X de Y, faixas, termômetro e funil
# ---------------------------------------------------------------------------
METODO = {"fonte": "curso", "titulo": "Script da academia", "modelos": [], "tecnicas": {
    "S01": {"nome": "Acolhimento e apresentação", "como": "", "rotulo_diario": "Apresentação"},
    "S02": {"nome": "Propor ligação cedo", "como": "", "rotulo_diario": "Propôs ligar"},
    "S07": {"nome": "Preço com valor", "como": "", "rotulo_diario": "Preço com valor"},
    "S08": {"nome": "Foco no próximo passo", "como": "", "rotulo_diario": None},
    "S10": {"nome": "Retomar quem sumiu", "como": "", "rotulo_diario": None},
}}


def _lead(nome, feitas=(), faltaram=(), temp=None, etapa=None, momentos=()):
    return {"nome": nome, "eh_lead": True, "temperatura": temp,
            "dados": {"tecnicas_aplicadas": list(feitas), "tecnicas_a_reforcar": list(faltaram),
                      "etapa_script": etapa, "momentos": list(momentos)}}


def test_faixas_de_aderencia():
    assert rel.faixa_aderencia(7, 10) == "seguiu" and rel.faixa_aderencia(1, 1) == "seguiu"
    assert rel.faixa_aderencia(69, 100) == "em_parte" and rel.faixa_aderencia(2, 5) == "em_parte"
    assert rel.faixa_aderencia(39, 100) == "normal" and rel.faixa_aderencia(0, 3) == "normal"
    assert rel.faixa_aderencia(0, 0) is None


def test_aderencia_da_conversa_so_conta_o_que_teve_oportunidade():
    feitas, faltaram = rel.aderencia_da_conversa(
        {"tecnicas_aplicadas": ["S01", "S07"], "tecnicas_a_reforcar": ["S02", "S07", "X99"]}, METODO["tecnicas"])
    assert feitas == {"S01", "S07"} and faltaram == {"S02"}            # S07 feito não conta como faltou
    assert rel.aderencia_da_conversa(None) == (set(), set())


def test_agregar_script_x_de_y_e_aderencia_do_dia():
    leads = [
        # 3 feitas de 3 (100%: seguiu). Preço não foi assunto: S07 fica fora do denominador
        _lead("Ana Paula", feitas=["S01", "S02", "S08"], temp="comprador", etapa="agendamento"),
        # 1 de 2 (50%: em parte)
        _lead("Bruno", feitas=["S01"], faltaram=["S07"], temp="comparador", etapa="descoberta"),
        # 0 de 3 (normal)
        _lead("Carla", faltaram=["S01", "S02", "S07"], temp="Pesquisador", etapa="acolhimento",
              momentos=[{"tecnica": "S07", "cliente_disse": "Quanto custa?", "equipe_respondeu": "R$ 120",
                         "pelo_script": "Te passo sim! Qual o seu objetivo?"}]),
        # sem nenhuma oportunidade: fora da conta (mas entra no termômetro e no funil)
        _lead("Davi", temp="quente", etapa="nenhuma"),
    ]
    sc = rel.agregar_script(leads, METODO)
    assert sc["conversas"] == 3 and (sc["feitas"], sc["oportunidades"]) == (4, 8) and sc["aderencia"] == 50
    assert (sc["seguiram"], sc["em_parte"], sc["normal"]) == (1, 1, 1)
    # só os passos com rótulo, e só os que tiveram oportunidade; o denominador exclui quem não teve chance
    assert sc["passos"] == [
        {"id": "S01", "rotulo": "Apresentação", "feitas": 2, "oportunidades": 3},
        {"id": "S02", "rotulo": "Propôs ligar", "feitas": 1, "oportunidades": 2},
        {"id": "S07", "rotulo": "Preço com valor", "feitas": 0, "oportunidades": 2},
    ]
    # S08 tem 1 oportunidade só: fica fora do ranking (mínimo 2)
    assert sc["mais_seguido"]["id"] == "S01" and sc["mais_esquecido"]["id"] == "S07"
    assert sc["termometro"] == [{"perfil": "comprador", "qtd": 2}, {"perfil": "comparador", "qtd": 1},
                                {"perfil": "pesquisador", "qtd": 1}]
    assert sc["funil"] == {"acolhidos": 3, "descobriram": 2, "proximo_passo": 1, "agendaram": 1, "com_etapa": 4}
    assert sc["exemplo"]["tecnica"] == "S07" and sc["exemplo"]["cliente"] == "Carla"
    assert sc["exemplo"]["nome_tecnica"] == "Preço com valor"
    json.dumps(sc)


def test_agregar_script_sem_oportunidade_nenhuma():
    sc = rel.agregar_script([_lead("A"), _lead("B", temp="tranquilo")], METODO)
    assert sc["conversas"] == 0 and sc["aderencia"] is None and sc["passos"] == []
    assert sc["mais_seguido"] is None and sc["mais_esquecido"] is None and sc["exemplo"] is None
    assert sc["termometro"] == [{"perfil": "tranquilo", "qtd": 1}]
    texto = rel.texto_diario(agg_base(script=sc), "x")
    assert "📘 *Script da academia hoje*\n🌡️ Termômetro: 1 tranquilo" in texto


def test_exemplo_prefere_o_passo_mais_esquecido_de_venda_que_nao_fechou():
    momento = lambda tecnica, quem: {"tecnica": tecnica, "cliente_disse": f"fala de {quem}",
                                     "equipe_respondeu": "resposta", "pelo_script": "ideal"}
    leads = [
        {**_lead("Ana", feitas=["S01"], faltaram=["S02"], momentos=[momento("S01", "Ana")]), "desfecho": "sucesso"},
        {**_lead("Bia", feitas=["S01"], faltaram=["S02"], momentos=[momento("S02", "Bia")]), "desfecho": "sucesso"},
        {**_lead("Cris", faltaram=["S02"], momentos=[momento("S02", "Cris")]), "desfecho": "em_andamento"},
    ]
    sc = rel.agregar_script(leads, METODO)
    assert sc["mais_esquecido"]["id"] == "S02"
    assert sc["exemplo"]["cliente"] == "Cris" and sc["exemplo"]["cliente_disse"] == "fala de Cris"


def test_tudo_seguido_nao_tem_mais_esquecido():
    leads = [_lead("A", feitas=["S01", "S02"]), _lead("B", feitas=["S01", "S02"])]
    sc = rel.agregar_script(leads, METODO)
    assert sc["aderencia"] == 100 and sc["mais_seguido"]["id"] == "S01" and sc["mais_esquecido"] is None


# ---------------------------------------------------------------------------
# Agregação (banco simulado)
# ---------------------------------------------------------------------------
def _sessao_db(tel, dia, nova=True, primeira=300, msgs_cliente=2):
    return {"telefone": tel, "inicio": ts(dia, 10), "nova": nova, "msgs_cliente": msgs_cliente,
            "primeira_resposta_util_seg": primeira}


def _analise_db(tel, dia, desfecho="em_andamento", eh_lead=True, nota="8.0", atencao=False, temp="pesquisador",
                aplicadas=(), reforcar=(), objecoes=(), motivo_perda=None, propos=False):
    return {
        "telefone": tel, "sessao_inicio": ts(dia, 10), "nome": tel[-4:], "eh_lead": eh_lead, "desfecho": desfecho,
        "motivo_perda": motivo_perda, "nota_final": Decimal(nota) if eh_lead else None,
        "nota_intuito_venda": Decimal("7.0") if eh_lead else None, "propos_ligacao": propos,
        "precisa_atencao": atencao, "motivo_atencao": "precisa de retorno" if atencao else None, "temperatura": temp,
        "dados": {"tecnicas_aplicadas": list(aplicadas), "tecnicas_a_reforcar": list(reforcar),
                  "objecoes": [{"tipo": o} for o in objecoes]},
    }


def _banco(monkeypatch, sessoes, analises, sem_resposta=()):
    resumo = {"periodo": {}, "conversas": len(sessoes), "novas": 4, "retomadas": 1, "followups_da_equipe": 0,
              "msgs_cliente": 8, "msgs_equipe": 6, "com_primeira_resposta": 4, "respondidas_no_limite": 4,
              "primeira_resposta_media_util_seg": 300, "pior_espera_util_seg": 600,
              "respondidas_por": {"atendente": 4, "ninguem": 1}, "sem_resposta": list(sem_resposta)}
    monkeypatch.setattr(rel.analista, "resumo_periodo", lambda i, f, a=None: resumo)
    monkeypatch.setattr(rel.analista, "_ignorados", lambda: [])
    monkeypatch.setattr(rel.sessoes_repo, "do_periodo", lambda i, f, excluir=None: sessoes)
    monkeypatch.setattr(rel.analises_repo, "do_periodo", lambda i, f, excluir=None: analises)
    monkeypatch.setattr(rel.analises_repo, "em_aberto_anteriores", lambda i, dias=7, excluir=None: 2)


def test_agregar_conta_tudo_certo(monkeypatch):
    sessoes = [_sessao_db("A", 12), _sessao_db("B", 12), _sessao_db("C", 13, nova=False, primeira=None),
               _sessao_db("D", 13), _sessao_db("E", 14, msgs_cliente=0)]
    analises = [
        _analise_db("A", 12, "sucesso", nota="9.0", aplicadas=["S04", "S09"], propos=True),
        _analise_db("B", 12, "perdido", nota="6.0", motivo_perda="preco", objecoes=["preco", "preco"],
                    reforcar=["S09"]),
        _analise_db("C", 13, "em_andamento", nota="7.0", atencao=True, temp="comprador", aplicadas=["S04"]),
        _analise_db("D", 13, "nao_lead", eh_lead=False, aplicadas=["S01"]),
    ]
    _banco(monkeypatch, sessoes, analises, sem_resposta=[
        {"telefone": "C", "nome": "C", "esperando_seg": 1200, "esperando_util_seg": 1200, "nova": False, "ultima_msg": "oi"}])

    agg, _ = rel.agregar(ts(12, 0), ts(17, 18), agora=ts(17, 18))
    assert (agg["sucesso"], agg["perdido"], agg["em_andamento"], agg["nao_lead"]) == (1, 1, 1, 1)
    assert agg["leads"] == 3 and agg["analisadas"] == 4 and agg["analisaveis"] == 4   # E não tem msg do cliente
    assert agg["nota_media"] == 7.3 and agg["nota_venda"] == 7.0 and agg["propos_ligacao"] == 1
    assert agg["perdidos_por_motivo"] == {"preco": 1}
    assert agg["objecoes"] == [{"tipo": "preco", "qtd": 2}]
    sc = agg["script"]                                     # D não é lead: fica fora do script
    assert sc["titulo"] == "Script da academia" and sc["conversas"] == 3 and sc["aderencia"] == 75
    assert {p["id"]: (p["feitas"], p["oportunidades"]) for p in sc["passos"]} == {"S04": (2, 2), "S09": (1, 2)}
    assert sc["mais_seguido"]["id"] == "S04" and sc["mais_esquecido"]["id"] == "S09"
    assert sc["termometro"] == [{"perfil": "comprador", "qtd": 1}, {"perfil": "pesquisador", "qtd": 2}]
    # C precisa de atenção, mas já está na lista "esperando resposta": não repete
    assert agg["atencao_total"] == 0 and agg["pedem_atencao"] == []
    assert agg["em_aberto_anteriores"] == 2
    assert agg["por_dia"]["2026-10-12"] == {"conversas": 2, "novas": 2, "retomadas": 0, "sucesso": 1,
                                            "em_andamento": 0, "perdido": 1, "nota_media": 7.5,
                                            "primeira_resposta_media_util_seg": 300}
    assert agg["por_dia"]["2026-10-13"]["retomadas"] == 1 and agg["por_dia"]["2026-10-13"]["nota_media"] == 7.0
    json.dumps(agg)          # tem que ser gravável em relatorios_dono (nada de Decimal/datetime)


def test_agregar_lista_quem_pede_atencao_pelo_termometro(monkeypatch):
    sessoes = [_sessao_db(t, 12) for t in "ABCD"]
    analises = [_analise_db("A", 12, atencao=True, temp="pesquisador"),
                _analise_db("B", 12, atencao=True, temp="comprador"),
                _analise_db("C", 12, atencao=True, temp=None),
                _analise_db("D", 12, atencao=True, temp="morno")]      # valor antigo = comparador
    _banco(monkeypatch, sessoes, analises)
    agg, _ = rel.agregar(ts(12, 0), ts(12, 18), agora=ts(12, 18))
    assert [p["telefone"] for p in agg["pedem_atencao"]] == ["B", "D", "A", "C"] and agg["atencao_total"] == 4
    assert agg["pedem_atencao"][1]["temperatura"] == "comparador"
