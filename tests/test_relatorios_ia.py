import json
from datetime import date, datetime
from decimal import Decimal

import pytz

from app.services import analista_relatorios as rel

TZ = pytz.timezone("America/Sao_Paulo")


def ts(dia, hora, minuto=0):
    return TZ.localize(datetime(2026, 10, dia, hora, minuto))


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
        "tecnicas": {"titulo": "Boas práticas de venda", "fonte": "geral",
                     "mais_usadas": [{"id": "T03", "nome": "Descobrir o objetivo antes do preço", "qtd": 8}],
                     "a_reforcar": [{"id": "T05", "nome": "Convite com dia e hora", "qtd": 3}]},
        "atencao_total": 2,
        "pedem_atencao": [
            {"nome": "Maria", "telefone": "5544991234567", "motivo": "Pediu aula teste e ficou sem data", "temperatura": "quente"},
            {"nome": "João", "telefone": "554488881111", "motivo": None, "temperatura": "morno"},
        ],
        "objecoes": [{"tipo": "preco", "qtd": 9}, {"tipo": "horario", "qtd": 4}],
        "por_dia": {}, "em_aberto_anteriores": 3, "anterior": None, "sintese": None,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Resumo do dia
# ---------------------------------------------------------------------------
def test_diario_completo():
    agg = agg_base(
        anterior={"hora": 12, "conversas": 10, "nota_media": 7.2},
        sintese={"destaque": "A Maria foi acolhida e saiu com aula marcada.", "dica": "Convide com dia e hora."},
        sem_resposta=[{"telefone": "5544977772222", "nome": "Carla", "esperando_util_seg": 2400,
                       "nova": True, "ultima_msg": "Tem vaga à noite?"}],
    )
    texto = rel.texto_diario(agg, "dia 20/10 (00h às 18h)")
    assert texto.startswith("📊 *Atendimento - dia 20/10 (00h às 18h)*")
    assert "• 14 conversas (+4 desde as 12h)" in texto
    assert "6 clientes novos · 8 clientes que voltaram" in texto
    assert "Também em aberto de dias anteriores: 3" in texto
    assert "✅ 4 com sucesso (agendaram ou fecharam)" in texto
    assert "💬 6 em andamento" in texto
    assert "↩️ 1 desistiu (preço)" in texto
    assert "➖ 2 não eram venda" in texto
    assert "Dentro de 15 min: 12 de 14 (86%)" in texto
    assert "Nota média: 7,8 (às 12h: 7,2) 📈" in texto
    assert "Intuito de venda: 7,5 · Propôs ligação: 3 de 12" in texto
    assert "🌟 Muito bom: A Maria foi acolhida" in texto and "💡 Dica: Convide" in texto
    assert "📘 *Boas práticas de venda hoje*" in texto
    assert "Mais usadas: Descobrir o objetivo antes do preço (8×)" in texto
    assert "Pode reforçar: Convite com dia e hora (em 3 conversas)" in texto
    assert "⚠️ *Pedem atenção (2)*" in texto
    assert "🔥 Maria (44) 99123-4567 — Pediu aula teste e ficou sem data" in texto
    assert "• João (44) 8888-1111" in texto
    assert "🆕 Carla (44) 97777-2222 - há 40 min" in texto
    assert "ℹ️" not in texto          # IA analisou tudo: sem aviso


def test_diario_enxuto_quando_nao_tem_o_opcional():
    agg = agg_base(
        conversas=1, novas=1, retomadas=0, sucesso=1, em_andamento=0, perdido=0, nao_lead=0, leads=1,
        perdidos_por_motivo={}, em_aberto_anteriores=0, pedem_atencao=[], atencao_total=0,
        tecnicas={"titulo": "Método do curso", "fonte": "curso", "mais_usadas": [], "a_reforcar": []},
        analisaveis=2, analisadas=1,
    )
    texto = rel.texto_diario(agg, "manhã de 20/10 (00h às 12h)")
    assert "• 1 conversa\n" in texto and "1 cliente novo · 0 clientes que voltaram" in texto
    assert "✅ 1 com sucesso (agendou ou fechou)" in texto
    assert "desistiu" not in texto and "Também em aberto" not in texto
    assert "Pedem atenção" not in texto and "Método do curso" not in texto
    assert "✅ Nenhum cliente esperando resposta agora." in texto
    assert "ℹ️ A IA analisou 1 de 2 conversas; as outras entram no próximo resumo." in texto
    assert "Nota média: 7,8\n" in texto            # sem resumo anterior: sem comparação


def test_diario_motivos_de_perda_e_sem_queda_de_nota():
    agg = agg_base(perdido=3, perdidos_por_motivo={"preco": 2, "horario": 1},
                   anterior={"hora": 12, "conversas": 14, "nota_media": 7.7})
    texto = rel.texto_diario(agg, "x")
    assert "↩️ 3 desistiram (preço 2, horário 1)" in texto
    assert "(às 12h: 7,7)\n" in texto and "📈" not in texto and "📉" not in texto
    assert "desde as 12h" not in texto          # não aumentou o total de conversas


# ---------------------------------------------------------------------------
# Resumo da semana
# ---------------------------------------------------------------------------
def test_semanal_completo():
    por_dia = {
        "2026-10-12": {"conversas": 8, "nota_media": 6.9}, "2026-10-14": {"conversas": 16, "nota_media": 7.4},
        "2026-10-16": {"conversas": 10, "nota_media": 7.8},
    }
    sintese = {
        "destaques": ["A Maria fechou aula teste.", "O João foi muito bem atendido."],
        "treino": {"tema": "Convidar com dia e hora", "exemplo": "\"Quer vir amanhã às 18h?\""},
        "objecoes": [{"tipo": "preco", "como_responder": "Mostrar o plano trimestral."}],
        "metodo": {"ja_faz": "A equipe já descobre o objetivo antes do preço.", "reforcar": None},
    }
    agg = agg_base(conversas=63, novas=28, retomadas=35, sucesso=14, leads=50, por_dia=por_dia, sintese=sintese)
    texto = rel.texto_semanal(agg, date(2026, 10, 12), date(2026, 10, 17),
                              anterior={"sucesso": 11, "nota_media": 7.0})
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
    assert "• A equipe já descobre o objetivo antes do preço." in texto
    assert texto.rstrip().endswith("📎 A planilha da semana está em anexo.")


def test_semanal_sem_ia_e_sem_conversas():
    sem_ia = rel.texto_semanal(agg_base(conversas=5, analisadas=0, analisaveis=5, leads=0, sucesso=0,
                                        em_andamento=0, perdido=0, nao_lead=0, nota_media=None, nota_venda=None,
                                        pedem_atencao=[], atencao_total=0, objecoes=[]),
                               date(2026, 10, 12), date(2026, 10, 17))
    assert "*Volume*" in sem_ia and "*Velocidade da equipe*" in sem_ia
    assert "Qualidade" not in sem_ia and "Destaques" not in sem_ia
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


def test_sintese_da_semana_valida_o_formato():
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


# ---------------------------------------------------------------------------
# Montagem (banco simulado)
# ---------------------------------------------------------------------------
def test_diario_sem_nenhuma_analise_cai_no_resumo_so_com_numeros(monkeypatch):
    agg = agg_base(analisadas=0, leads=0)
    monkeypatch.setattr(rel, "agregar", lambda inicio, fim, agora=None, metodo=None: (agg, []))
    out = rel.montar_diario(18, agora=ts(20, 18))
    assert "*Movimento*" in out["texto"] and "Qualidade" not in out["texto"]    # formato antigo
    assert out["referencia"] == date(2026, 10, 20) and out["hora"] == 18


def test_diario_com_analises_compara_com_o_resumo_anterior(monkeypatch):
    agg = agg_base()
    monkeypatch.setattr(rel, "agregar", lambda inicio, fim, agora=None, metodo=None: (agg, [_analise()]))
    monkeypatch.setattr(rel.relatorios_repo, "diario_anterior",
                        lambda ref, hora: {"hora": 12, "dados": {"conversas": 10, "nota_media": 7.2}})
    out = rel.montar_diario(18, agora=ts(20, 18), ia=IAFalsa(RuntimeError("fora")))
    assert "(+4 desde as 12h)" in out["texto"] and "(às 12h: 7,2) 📈" in out["texto"]
    assert "🌟" not in out["texto"]                  # IA da síntese caiu: sai sem ela
    assert out["dados"]["anterior"]["hora"] == 12


def test_janela_da_semana_comeca_na_segunda():
    inicio, fim = rel.janela_da_semana(ts(17, 18))       # sábado 17/10/2026
    assert inicio == ts(12, 0) and fim == ts(17, 18)
    assert rel.janela_da_semana(ts(12, 7))[0] == ts(12, 0)   # a própria segunda


# ---------------------------------------------------------------------------
# Agregação (banco simulado)
# ---------------------------------------------------------------------------
def _sessao_db(tel, dia, nova=True, primeira=300, msgs_cliente=2):
    return {"telefone": tel, "inicio": ts(dia, 10), "nova": nova, "msgs_cliente": msgs_cliente,
            "primeira_resposta_util_seg": primeira}


def _analise_db(tel, dia, desfecho="em_andamento", eh_lead=True, nota="8.0", atencao=False, temp="morno",
                aplicadas=(), reforcar=(), objecoes=(), motivo_perda=None, propos=False):
    return {
        "telefone": tel, "sessao_inicio": ts(dia, 10), "nome": tel[-4:], "eh_lead": eh_lead, "desfecho": desfecho,
        "motivo_perda": motivo_perda, "nota_final": Decimal(nota) if eh_lead else None,
        "nota_intuito_venda": Decimal("7.0") if eh_lead else None, "propos_ligacao": propos,
        "precisa_atencao": atencao, "motivo_atencao": "precisa de retorno" if atencao else None, "temperatura": temp,
        "dados": {"tecnicas_aplicadas": list(aplicadas), "tecnicas_a_reforcar": list(reforcar),
                  "objecoes": [{"tipo": o} for o in objecoes]},
    }


def test_agregar_conta_tudo_certo(monkeypatch):
    sessoes = [_sessao_db("A", 12), _sessao_db("B", 12), _sessao_db("C", 13, nova=False, primeira=None),
               _sessao_db("D", 13), _sessao_db("E", 14, msgs_cliente=0)]
    analises = [
        _analise_db("A", 12, "sucesso", nota="9.0", aplicadas=["T03", "T05"], propos=True),
        _analise_db("B", 12, "perdido", nota="6.0", motivo_perda="preco", objecoes=["preco", "preco"],
                    reforcar=["T05"]),
        _analise_db("C", 13, "em_andamento", nota="7.0", atencao=True, temp="quente", aplicadas=["T03"]),
        _analise_db("D", 13, "nao_lead", eh_lead=False),
    ]
    resumo = {"periodo": {}, "conversas": 5, "novas": 4, "retomadas": 1, "followups_da_equipe": 0,
              "msgs_cliente": 8, "msgs_equipe": 6, "com_primeira_resposta": 4, "respondidas_no_limite": 4,
              "primeira_resposta_media_util_seg": 300, "pior_espera_util_seg": 600,
              "respondidas_por": {"atendente": 4, "ninguem": 1},
              "sem_resposta": [{"telefone": "C", "nome": "C", "esperando_util_seg": 1200, "nova": False,
                                "ultima_msg": "oi"}]}
    monkeypatch.setattr(rel.analista, "resumo_periodo", lambda i, f, a=None: resumo)
    monkeypatch.setattr(rel.analista, "_ignorados", lambda: [])
    monkeypatch.setattr(rel.sessoes_repo, "do_periodo", lambda i, f, excluir=None: sessoes)
    monkeypatch.setattr(rel.analises_repo, "do_periodo", lambda i, f, excluir=None: analises)
    monkeypatch.setattr(rel.analises_repo, "em_aberto_anteriores", lambda i, dias=7, excluir=None: 2)

    agg, _ = rel.agregar(ts(12, 0), ts(17, 18), agora=ts(17, 18))
    assert (agg["sucesso"], agg["perdido"], agg["em_andamento"], agg["nao_lead"]) == (1, 1, 1, 1)
    assert agg["leads"] == 3 and agg["analisadas"] == 4 and agg["analisaveis"] == 4   # E não tem msg do cliente
    assert agg["nota_media"] == 7.3 and agg["nota_venda"] == 7.0 and agg["propos_ligacao"] == 1
    assert agg["perdidos_por_motivo"] == {"preco": 1}
    assert agg["objecoes"] == [{"tipo": "preco", "qtd": 2}]
    assert [t["id"] for t in agg["tecnicas"]["mais_usadas"]] == ["T03", "T05"]
    assert agg["tecnicas"]["mais_usadas"][0]["qtd"] == 2 and agg["tecnicas"]["a_reforcar"][0]["id"] == "T05"
    # C precisa de atenção, mas já está na lista "esperando resposta": não repete
    assert agg["atencao_total"] == 0 and agg["pedem_atencao"] == []
    assert agg["em_aberto_anteriores"] == 2
    assert agg["por_dia"]["2026-10-12"] == {"conversas": 2, "novas": 2, "retomadas": 0, "sucesso": 1,
                                            "em_andamento": 0, "perdido": 1, "nota_media": 7.5,
                                            "primeira_resposta_media_util_seg": 300}
    assert agg["por_dia"]["2026-10-13"]["retomadas"] == 1 and agg["por_dia"]["2026-10-13"]["nota_media"] == 7.0
    json.dumps(agg)          # tem que ser gravável em relatorios_dono (nada de Decimal/datetime)


def test_agregar_lista_quem_pede_atencao_com_o_quente_primeiro(monkeypatch):
    sessoes = [_sessao_db("A", 12), _sessao_db("B", 12)]
    analises = [_analise_db("A", 12, atencao=True, temp="morno"), _analise_db("B", 12, atencao=True, temp="quente")]
    monkeypatch.setattr(rel.analista, "resumo_periodo", lambda i, f, a=None: {
        "periodo": {}, "conversas": 2, "novas": 2, "retomadas": 0, "followups_da_equipe": 0, "msgs_cliente": 4,
        "msgs_equipe": 2, "com_primeira_resposta": 2, "respondidas_no_limite": 2,
        "primeira_resposta_media_util_seg": 1, "pior_espera_util_seg": None,
        "respondidas_por": {}, "sem_resposta": []})
    monkeypatch.setattr(rel.analista, "_ignorados", lambda: [])
    monkeypatch.setattr(rel.sessoes_repo, "do_periodo", lambda i, f, excluir=None: sessoes)
    monkeypatch.setattr(rel.analises_repo, "do_periodo", lambda i, f, excluir=None: analises)
    monkeypatch.setattr(rel.analises_repo, "em_aberto_anteriores", lambda i, dias=7, excluir=None: 0)
    agg, _ = rel.agregar(ts(12, 0), ts(12, 18), agora=ts(12, 18))
    assert [p["telefone"] for p in agg["pedem_atencao"]] == ["B", "A"] and agg["atencao_total"] == 2
