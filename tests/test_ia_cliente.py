import pytest
import requests

from app.core.config import config
from app.services import ia_cliente
from app.services.ia_cliente import IAErro, IAIndisponivel, extrair_json, gerar_json


class RespostaFalsa:
    def __init__(self, status=200, corpo=None, texto=""):
        self.status_code = status
        self._corpo = corpo
        self.text = texto or str(corpo)

    def json(self):
        if self._corpo is None:
            raise ValueError("sem json")
        return self._corpo


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    monkeypatch.setattr(config, "ANALISTA_IA_API_KEY", "chave-secreta-teste")
    monkeypatch.setattr(config, "ANALISTA_IA_MODELO", "modelo-x")
    monkeypatch.setattr(ia_cliente, "_dormir", lambda s: None)


def _simular(monkeypatch, respostas):
    """Devolve as respostas em ordem e guarda cada chamada feita."""
    chamadas = []
    fila = list(respostas)

    def falso_post(url, json=None, headers=None, timeout=None):
        chamadas.append({"url": url, "json": json, "headers": headers})
        item = fila.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ia_cliente.requests, "post", falso_post)
    return chamadas


# ---------------------------------------------------------------------------
def test_extrair_json_aceita_cerca_e_texto_em_volta():
    assert extrair_json('{"a": 1}') == {"a": 1}
    assert extrair_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extrair_json('Claro! Segue:\n{"a": {"b": 2}}\nEspero ter ajudado') == {"a": {"b": 2}}
    for ruim in ["", "   ", "sem json aqui", "[1, 2]", "{quebrado"]:
        with pytest.raises(IAErro):
            extrair_json(ruim)


def test_sem_chave_a_ia_fica_desligada(monkeypatch):
    monkeypatch.setattr(config, "ANALISTA_IA_API_KEY", None)
    assert not ia_cliente.ia_ligada()
    with pytest.raises(IAIndisponivel):
        gerar_json("sistema", "usuario")


def test_gemini_monta_o_pedido_e_le_a_resposta(monkeypatch):
    monkeypatch.setattr(config, "ANALISTA_IA_PROVEDOR", "gemini")
    chamadas = _simular(monkeypatch, [RespostaFalsa(200, {
        "candidates": [{"content": {"parts": [{"text": '{"nota": 8}'}]}}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 5},
    })])
    dado, uso = gerar_json("SIS", "USU")
    assert dado == {"nota": 8}
    assert uso == {"entrada": 100, "saida": 25}
    c = chamadas[0]
    assert "modelo-x:generateContent" in c["url"]
    assert c["headers"]["x-goog-api-key"] == "chave-secreta-teste"
    assert c["json"]["systemInstruction"]["parts"][0]["text"] == "SIS"
    assert c["json"]["generationConfig"]["responseMimeType"] == "application/json"


def test_vertex_monta_o_pedido_e_le_a_resposta(monkeypatch):
    monkeypatch.setattr(config, "ANALISTA_IA_PROVEDOR", "vertex")
    chamadas = _simular(monkeypatch, [RespostaFalsa(200, {
        "candidates": [{"content": {"parts": [
            {"text": "pensando...", "thought": True}, {"text": '{"nota": 6}'},
        ]}}],
        "usageMetadata": {"promptTokenCount": 80, "candidatesTokenCount": 15, "thoughtsTokenCount": 3},
    })])
    dado, uso = gerar_json("SIS", "USU", max_tokens=1234, temperatura=0.3)
    assert dado == {"nota": 6}
    assert uso == {"entrada": 80, "saida": 18}
    c = chamadas[0]
    assert c["url"] == ("https://aiplatform.googleapis.com/v1/publishers/google/models/"
                        "modelo-x:generateContent")
    assert c["headers"]["x-goog-api-key"] == "chave-secreta-teste"
    assert c["json"]["systemInstruction"]["parts"][0]["text"] == "SIS"
    assert c["json"]["contents"] == [{"role": "user", "parts": [{"text": "USU"}]}]
    assert c["json"]["generationConfig"] == {
        "temperature": 0.3, "maxOutputTokens": 1234, "responseMimeType": "application/json",
    }


def test_vertex_erro_definitivo_nao_vaza_a_chave(monkeypatch):
    monkeypatch.setattr(config, "ANALISTA_IA_PROVEDOR", "vertex")
    chamadas = _simular(monkeypatch, [RespostaFalsa(403, texto="Agent Platform API has not been used")])
    with pytest.raises(IAErro) as e:
        gerar_json("a", "b")
    assert len(chamadas) == 1
    assert "403" in str(e.value)
    assert "chave-secreta-teste" not in str(e.value)


def test_anthropic_monta_o_pedido_com_cache_e_le_a_resposta(monkeypatch):
    monkeypatch.setattr(config, "ANALISTA_IA_PROVEDOR", "anthropic")
    chamadas = _simular(monkeypatch, [RespostaFalsa(200, {
        "content": [{"type": "text", "text": '```json\n{"nota": 7}\n```'}],
        "usage": {"input_tokens": 50, "output_tokens": 30, "cache_read_input_tokens": 200},
    })])
    dado, uso = gerar_json("SIS", "USU")
    assert dado == {"nota": 7}
    assert uso == {"entrada": 250, "saida": 30}
    c = chamadas[0]
    assert c["url"].endswith("/v1/messages")
    assert c["headers"]["x-api-key"] == "chave-secreta-teste"
    assert c["json"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert c["json"]["messages"][0]["content"] == "USU"


def test_openai_monta_o_pedido_e_le_a_resposta(monkeypatch):
    monkeypatch.setattr(config, "ANALISTA_IA_PROVEDOR", "openai")
    chamadas = _simular(monkeypatch, [RespostaFalsa(200, {
        "choices": [{"message": {"content": '{"nota": 9}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    })])
    dado, uso = gerar_json("SIS", "USU")
    assert dado == {"nota": 9} and uso == {"entrada": 10, "saida": 4}
    c = chamadas[0]
    assert c["headers"]["Authorization"] == "Bearer chave-secreta-teste"
    assert c["json"]["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in c["json"]["messages"]] == ["system", "user"]


def test_erro_temporario_tenta_de_novo_e_funciona(monkeypatch):
    monkeypatch.setattr(config, "ANALISTA_IA_PROVEDOR", "openai")
    ok = RespostaFalsa(200, {"choices": [{"message": {"content": "{}"}}], "usage": {}})
    chamadas = _simular(monkeypatch, [
        RespostaFalsa(429, texto="limite"), requests.Timeout("lento"), ok,
    ])
    dado, _ = gerar_json("a", "b")
    assert dado == {} and len(chamadas) == 3


def test_erro_definitivo_nao_repete_e_nao_vaza_a_chave(monkeypatch):
    monkeypatch.setattr(config, "ANALISTA_IA_PROVEDOR", "openai")
    chamadas = _simular(monkeypatch, [RespostaFalsa(401, texto="chave inválida")])
    with pytest.raises(IAErro) as e:
        gerar_json("a", "b")
    assert len(chamadas) == 1
    assert "chave-secreta-teste" not in str(e.value)


def test_desiste_depois_de_tres_falhas(monkeypatch):
    monkeypatch.setattr(config, "ANALISTA_IA_PROVEDOR", "openai")
    chamadas = _simular(monkeypatch, [RespostaFalsa(503, texto="fora")] * 3)
    with pytest.raises(IAErro):
        gerar_json("a", "b")
    assert len(chamadas) == 3


def test_json_quebrado_vira_erro(monkeypatch):
    monkeypatch.setattr(config, "ANALISTA_IA_PROVEDOR", "openai")
    _simular(monkeypatch, [RespostaFalsa(200, {"choices": [{"message": {"content": "isso não é json"}}]})])
    with pytest.raises(IAErro):
        gerar_json("a", "b")
