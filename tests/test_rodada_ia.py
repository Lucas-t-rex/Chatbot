import base64
from datetime import date, datetime

import pytest
import pytz

from app.core.config import config
from app.services import analista_rodada as rod
from app.services.evolution_service import EvolutionService

TZ = pytz.timezone("America/Sao_Paulo")
DONO = "554491586345@s.whatsapp.net"


def ts(dia, hora, minuto=0):
    """Outubro/2026: 12 = segunda, 16 = sexta, 17 = sábado, 18 = domingo."""
    return TZ.localize(datetime(2026, 10, dia, hora, minuto))


class Registro:
    def __init__(self):
        self.eventos, self.salvos = [], []


@pytest.fixture
def reg(monkeypatch):
    r = Registro()
    analise = {"ligada": True, "analisadas": 3, "erros": 0, "restantes": 0, "parou_por": None}

    def analisar(agora=None, ia=None, dias=None):
        r.eventos.append(("analisar", dias))
        return analise

    def diario(hora, agora=None, ia=None):
        r.eventos.append(("montar_diario", hora))
        return {"texto": f"DIARIO {hora}h", "dados": {"conversas": 5}, "referencia": date(2026, 10, 12),
                "hora": hora, "conversas": 5}

    def semanal(agora=None, ia=None):
        r.eventos.append(("montar_semanal",))
        return {"texto": "SEMANAL", "dados": {"conversas": 30}, "referencia": date(2026, 10, 12),
                "inicio": ts(12, 0), "fim": ts(17, 18), "conversas": 30}

    monkeypatch.setattr(rod.analista_ia, "analisar_pendentes", analisar)
    monkeypatch.setattr(rod.analista_relatorios, "montar_diario", diario)
    monkeypatch.setattr(rod.analista_relatorios, "montar_semanal", semanal)
    monkeypatch.setattr(rod.analista_excel, "gerar", lambda i, f, a=None: b"PLANILHA")
    monkeypatch.setattr(rod.relatorios_repo, "salvar",
                        lambda tipo, ref, hora, dados, texto, enviado: r.salvos.append((tipo, hora, texto, enviado)))
    return r


def enviadores(reg, texto_ok=True, doc_ok=True):
    def texto(destino, t):
        reg.eventos.append(("texto", destino, t))
        return texto_ok

    def doc(destino, conteudo, nome, legenda):
        reg.eventos.append(("documento", nome, conteudo, legenda))
        return doc_ok

    return texto, doc


def test_diario_le_as_conversas_antes_de_montar_e_enviar(reg):
    texto, doc = enviadores(reg)
    out = rod.executar(22, DONO, texto, doc, agora=ts(14, 22))               # quarta 22h
    assert [e[0] for e in reg.eventos] == ["analisar", "montar_diario", "texto"]
    assert reg.eventos[2][1:] == (DONO, "DIARIO 22h")
    assert out["enviado"] is True
    assert reg.salvos == [("diario", 22, "DIARIO 22h", True)]


def test_envio_que_falha_fica_registrado_como_nao_enviado(reg):
    texto, doc = enviadores(reg, texto_ok=False)
    out = rod.executar(22, DONO, texto, doc, agora=ts(14, 22))
    assert out["enviado"] is False and reg.salvos[0][3] is False


def test_sabado_18h_manda_o_semanal_com_a_planilha_no_lugar_do_diario(reg):
    texto, doc = enviadores(reg)
    out = rod.executar(18, DONO, texto, doc, agora=ts(17, 18))               # sábado 18h
    assert ("analisar", 7) in reg.eventos                                    # olha a semana toda
    assert [e[0] for e in reg.eventos if e[0] != "analisar"] == ["montar_semanal", "texto", "documento"]
    nome_doc = [e for e in reg.eventos if e[0] == "documento"][0]
    assert nome_doc[1] == "atendimento_semana_12-10_a_17-10-2026.xlsx" and nome_doc[2] == b"PLANILHA"
    assert out["planilha"] is True and reg.salvos == [("semanal", 18, "SEMANAL", True)]


def test_planilha_que_falha_nao_impede_o_texto(reg, monkeypatch):
    monkeypatch.setattr(rod.analista_excel, "gerar", lambda i, f, a=None: (_ for _ in ()).throw(RuntimeError("x")))
    texto, doc = enviadores(reg)
    out = rod.executar(18, DONO, texto, doc, agora=ts(17, 18))
    assert out["enviado"] is True and out["planilha"] is False


def test_semanal_que_quebra_cai_no_diario(reg, monkeypatch):
    monkeypatch.setattr(rod.analista_relatorios, "montar_semanal",
                        lambda agora=None, ia=None: (_ for _ in ()).throw(RuntimeError("quebrou")))
    texto, doc = enviadores(reg)
    out = rod.executar(18, DONO, texto, doc, agora=ts(17, 18))
    assert out["enviado"] is True and reg.salvos[0][:2] == ("diario", 18)


def test_fim_de_semana_sem_conversa_nao_manda_nada(reg, monkeypatch):
    monkeypatch.setattr(rod.analista_relatorios, "montar_diario", lambda hora, agora=None, ia=None: {
        "texto": "vazio", "dados": {}, "referencia": date(2026, 10, 18), "hora": hora, "conversas": 0})
    texto, doc = enviadores(reg)
    out = rod.executar(22, DONO, texto, doc, agora=ts(18, 22))               # domingo
    assert out["enviado"] is False and not [e for e in reg.eventos if e[0] == "texto"] and reg.salvos == []
    # mas se houve conversa no domingo, o dono recebe
    monkeypatch.setattr(rod.analista_relatorios, "montar_diario", lambda hora, agora=None, ia=None: {
        "texto": "teve", "dados": {}, "referencia": date(2026, 10, 18), "hora": hora, "conversas": 2})
    assert rod.executar(22, DONO, texto, doc, agora=ts(18, 22))["enviado"] is True


def test_dia_de_semana_sem_conversa_manda_mesmo_assim(reg, monkeypatch):
    monkeypatch.setattr(rod.analista_relatorios, "montar_diario", lambda hora, agora=None, ia=None: {
        "texto": "nenhuma conversa", "dados": {}, "referencia": date(2026, 10, 14), "hora": hora, "conversas": 0})
    texto, doc = enviadores(reg)
    assert rod.executar(22, DONO, texto, doc, agora=ts(14, 22))["enviado"] is True


def test_relatorio_novo_quebrado_cai_no_resumo_so_com_numeros(reg, monkeypatch):
    monkeypatch.setattr(rod.analista_relatorios, "montar_diario",
                        lambda hora, agora=None, ia=None: (_ for _ in ()).throw(RuntimeError("bug")))
    monkeypatch.setattr(rod, "_resumo_so_com_numeros", lambda hora, agora: ("RESUMO ANTIGO", 4))
    texto, doc = enviadores(reg)
    out = rod.executar(22, DONO, texto, doc, agora=ts(14, 22))
    assert out["enviado"] is True and [e for e in reg.eventos if e[0] == "texto"][0][2] == "RESUMO ANTIGO"


def test_erro_no_registro_do_banco_nao_impede_o_envio(reg, monkeypatch):
    monkeypatch.setattr(rod.relatorios_repo, "salvar",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("banco caiu")))
    texto, doc = enviadores(reg)
    assert rod.executar(22, DONO, texto, doc, agora=ts(14, 22))["enviado"] is True


def test_duas_rodadas_nao_andam_juntas(reg):
    texto, doc = enviadores(reg)
    assert rod._trava.acquire(blocking=False)
    try:
        out = rod.executar(22, DONO, texto, doc, agora=ts(14, 22))
    finally:
        rod._trava.release()
    assert out["enviado"] is False and out["motivo"] == "rodada em andamento" and reg.eventos == []
    assert rod.executar(22, DONO, texto, doc, agora=ts(14, 22))["enviado"] is True      # liberou


def test_um_resumo_por_dia_as_22h_e_o_semanal_no_sabado_18h():
    assert config.HORARIOS_RESUMO == [22]
    # 18h fora da lista: o main.py registra o job do semanal (sábado 18h) à parte
    assert config.RESUMO_SEMANAL_HORA not in config.HORARIOS_RESUMO
    assert (config.RESUMO_SEMANAL_DIA, config.RESUMO_SEMANAL_HORA) == (5, 18)


def test_regras_de_horario():
    assert rod.eh_hora_do_semanal(18, ts(17, 18)) and not rod.eh_hora_do_semanal(18, ts(16, 18))
    assert not rod.eh_hora_do_semanal(22, ts(17, 22))
    assert rod.dia_fechado(ts(17, 12)) and rod.dia_fechado(ts(18, 12)) and not rod.dia_fechado(ts(16, 12))


# ---------------------------------------------------------------------------
# Evolution: emojis e documento
# ---------------------------------------------------------------------------
class SessaoFalsa:
    def __init__(self):
        self.chamadas = []

    def post(self, url, json=None, timeout=None):
        self.chamadas.append((url, json))

        class R:
            status_code = 201

            @staticmethod
            def json():
                return {"key": {"id": "ABC123"}}

        return R()


@pytest.fixture
def evo(monkeypatch):
    e = EvolutionService()
    sessao = SessaoFalsa()
    monkeypatch.setattr(e, "_session", sessao)
    ids = []
    monkeypatch.setattr(e, "_sent_ids_callback", ids.append)
    e.sessao_falsa, e.ids = sessao, ids
    return e


def test_texto_normal_continua_sem_emoji_e_o_do_dono_mantem(evo):
    assert evo.send_whatsapp_message("5544@s.whatsapp.net", "Oi 😊 tudo bem ✅") is True
    assert evo.sessao_falsa.chamadas[0][1]["textMessage"]["text"] == "Oi  tudo bem"
    assert evo.send_whatsapp_message("5544", "📊 *Resumo* ⚠️ ✅", manter_emojis=True) is True
    assert evo.sessao_falsa.chamadas[1][1]["textMessage"]["text"] == "📊 *Resumo* ⚠️ ✅"


def test_documento_vai_em_base64_e_registra_o_id(evo):
    ok = evo.send_whatsapp_document("554491586345@s.whatsapp.net", b"conteudo xlsx", "semana.xlsx", "Planilha")
    assert ok is True
    url, corpo = evo.sessao_falsa.chamadas[0]
    assert url.endswith("/message/sendMedia/chatbot")
    assert corpo["number"] == "554491586345"
    m = corpo["mediaMessage"]
    assert m["mediatype"] == "document" and m["fileName"] == "semana.xlsx" and m["caption"] == "Planilha"
    assert base64.b64decode(m["media"]) == b"conteudo xlsx"
    assert evo.ids == ["ABC123"]               # o eco do webhook não vira "atendente"
