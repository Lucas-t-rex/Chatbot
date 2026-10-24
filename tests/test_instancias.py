"""Duas instâncias Evolution: 'chatbot' (grava/atende) e 'relatorio' (só envia relatórios)."""
import pytest

from app.core.config import config
from app.services.evolution_service import EvolutionService, evento_da_instancia


class RespostaFalsa:
    def __init__(self, status=200, corpo=None):
        self.status_code = status
        self._corpo = corpo if corpo is not None else {}
        self.text = str(self._corpo)

    def json(self):
        return self._corpo


class SessaoFalsa:
    def __init__(self, estados=None):
        self.estados = estados or {}      # {"relatorio": "open"}
        self.chamadas = []

    def get(self, url, **kw):
        self.chamadas.append(("GET", url, None))
        if "/instance/connectionState/" in url:
            nome = url.rsplit("/", 1)[1]
            return RespostaFalsa(200, {"instance": {"instanceName": nome, "state": self.estados.get(nome, "close")}})
        return RespostaFalsa(200, {"qrcode": {"base64": "x"}})

    def post(self, url, json=None, **kw):
        self.chamadas.append(("POST", url, json))
        return RespostaFalsa(201, {"key": {"id": "ID1"}})


@pytest.fixture
def evo(monkeypatch):
    e = EvolutionService()
    sessao = SessaoFalsa()
    monkeypatch.setattr(e, "_session", sessao)
    monkeypatch.setattr(e, "_sent_ids_callback", None)
    e.sessao_falsa = sessao
    return e


def test_padrao_continua_na_instancia_chatbot(evo):
    evo.send_whatsapp_message("5544@s.whatsapp.net", "oi")
    assert evo.sessao_falsa.chamadas[-1][1].endswith("/message/sendText/chatbot")


def test_envio_com_instancia_explicita(evo):
    evo.send_whatsapp_message("5544@s.whatsapp.net", "oi", instance="relatorio")
    evo.send_whatsapp_document("5544", b"x", "a.xlsx", instance="relatorio")
    urls = [c[1] for c in evo.sessao_falsa.chamadas]
    assert urls[0].endswith("/message/sendText/relatorio")
    assert urls[1].endswith("/message/sendMedia/relatorio")


def test_relatorios_usam_relatorio_quando_conectada(evo):
    evo.sessao_falsa.estados["relatorio"] = "open"
    assert evo.instancia_para_relatorios() == "relatorio"


@pytest.mark.parametrize("estado", ["close", "connecting"])
def test_relatorios_nao_saem_se_relatorio_nao_conectada(evo, estado):
    # Sem fallback: a 'chatbot' é o número das atendentes e nunca recebe relatório.
    evo.sessao_falsa.estados["relatorio"] = estado
    assert evo.instancia_para_relatorios() is None


def test_numero_da_instancia(evo, monkeypatch):
    class Sessao(SessaoFalsa):
        def get(self, url, **kw):
            if "fetchInstances" in url:
                return RespostaFalsa(200, [
                    {"instance": {"instanceName": "chatbot", "owner": "554400000001@s.whatsapp.net"}},
                    {"instance": {"instanceName": "relatorio", "owner": "554400000002@s.whatsapp.net"}},
                ])
            return super().get(url, **kw)
    monkeypatch.setattr(evo, "_session", Sessao())
    assert evo.numero_da_instancia("relatorio") == "554400000002"


def test_criar_instancia_sem_qrcode_e_sem_webhook(evo):
    assert evo.criar_instancia("relatorio") is True
    metodo, url, corpo = evo.sessao_falsa.chamadas[-1]
    assert url.endswith("/instance/create")
    assert corpo["instanceName"] == "relatorio" and corpo["qrcode"] is False
    assert "webhook" not in corpo


def test_esta_conectada_e_qrcode(evo):
    evo.sessao_falsa.estados["relatorio"] = "open"
    assert evo.esta_conectada("relatorio") is True
    assert evo.esta_conectada("chatbot") is False
    assert evo.obter_qrcode("relatorio")["qrcode"]["base64"] == "x"
    assert evo.sessao_falsa.chamadas[-1][1].endswith("/instance/connect/relatorio")


def test_job_de_saude_nao_reconecta_a_relatorio(evo):
    evo.sessao_falsa.estados.update({"chatbot": "open", "relatorio": "close"})
    evo.verificar_e_reconectar_whatsapp()
    urls = [c[1] for c in evo.sessao_falsa.chamadas]
    assert not any("/instance/connect/" in u for u in urls)


def test_webhook_descarta_instancia_diferente():
    assert evento_da_instancia({"instance": "chatbot"}, "chatbot") is True
    assert evento_da_instancia({"instance": "relatorio"}, "chatbot") is False
    assert evento_da_instancia({"instance": {"instanceName": "relatorio"}}, "chatbot") is False
    assert evento_da_instancia({"event": "messages.upsert"}, "chatbot") is True   # sem campo: mantém
