from datetime import datetime, timedelta

import pytest
import pytz

from app.core.config import config
from app.services import analista_ia as aia
from app.services.analista_ia import (
    RespostaInvalida, cortar_transcricao, media_das_notas, montar_entrada,
    nota_de_tempo, notas_de_tempo, validar_resposta,
)
from app.services.ia_cliente import IAErro

TZ = pytz.timezone("America/Sao_Paulo")


def ts(dia, hora, minuto):
    return TZ.localize(datetime(2026, 10, dia, hora, minuto))


def msg(dia, hora, minuto, origem, text, tipo="texto"):
    return {"ts": ts(dia, hora, minuto), "origem": origem, "tipo": tipo, "text": text}


METODO = {"fonte": "geral", "titulo": "Boas práticas de venda",
          "tecnicas": {"T03": {"nome": "x", "como": "y"}, "T05": {"nome": "x", "como": "y"}}}

NOTAS_BOAS = {k: 8 for k in aia.CRITERIOS_IA}


def bruto(**extra):
    base = {
        "eh_lead": True, "tipo_contato": "lead", "genero": "feminino", "interesse": "muay thai",
        "desfecho": "em_andamento", "temperatura": "morno", "precisa_atencao": True,
        "motivo_atencao": "Sem data marcada", "notas": dict(NOTAS_BOAS), "propos_ligacao": False,
        "resumo": "Maria quer muay thai", "ponto_forte": "Foi simpática", "pode_melhorar": "Convidar com dia e hora",
        "proxima_acao": "Mandar mensagem", "mensagem_sugerida": "Oi Maria!",
        "objecoes": [{"tipo": "preco", "trecho": "ta caro"}],
        "tecnicas_aplicadas": ["t03", "T99"], "tecnicas_a_reforcar": ["T05"],
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Notas calculadas pelo código
# ---------------------------------------------------------------------------
def test_regua_do_tempo():
    assert nota_de_tempo(None) is None
    assert [nota_de_tempo(m * 60) for m in (1, 5, 6, 15, 16, 30, 45, 60, 120, 180, 181)] == \
           [10, 10, 8, 8, 6, 6, 4, 4, 2, 2, 0]


def test_notas_de_tempo_com_resposta_sem_resposta_e_aguardando():
    assert notas_de_tempo({"primeira_resposta_util_seg": 240, "tempo_medio_resposta_util_seg": 1200}) == \
           {"primeira_resposta": 10.0, "tempo_medio": 6.0}
    # ninguém esperava resposta: critério não se aplica
    assert notas_de_tempo({"primeira_resposta_util_seg": None, "aguardando_desde": None}) == \
           {"primeira_resposta": None, "tempo_medio": None}
    # cliente esperando há horas no horário útil (seg 10h -> seg 15h): estourou o limite
    sessao = {"primeira_resposta_util_seg": None, "aguardando_desde": ts(5, 10, 0)}
    assert notas_de_tempo(sessao, agora=ts(5, 15, 0))["primeira_resposta"] == 0.0
    # esperando há 3 min: ainda dentro do limite, não pune
    assert notas_de_tempo(sessao, agora=ts(5, 10, 3))["primeira_resposta"] is None


def test_media_ignora_o_que_nao_se_aplica():
    assert media_das_notas({"a": 10, "b": None, "c": 5}) == 7.5
    assert media_das_notas({"a": None}) is None


# ---------------------------------------------------------------------------
# Validação da resposta
# ---------------------------------------------------------------------------
def test_validacao_caminho_feliz_e_nota_final_do_codigo():
    tempo = {"primeira_resposta": 10.0, "tempo_medio": 6.0}
    campos, dados = validar_resposta(bruto(), METODO, tempo)
    assert campos["genero"] == "Feminino" and campos["desfecho"] == "em_andamento"
    # 15 notas 8 + 10 + 6 -> (120 + 16) / 17 = 8.0
    assert campos["nota_final"] == 8.0
    assert dados["notas"]["primeira_resposta"] == 10.0
    assert dados["tecnicas_aplicadas"] == ["T03"]          # T99 não existe: descartada; "t03" normalizada
    assert dados["tecnicas_a_reforcar"] == ["T05"]
    assert campos["precisa_atencao"] is True and campos["nota_intuito_venda"] == 8.0


def test_validacao_corrige_o_que_vem_torto():
    b = bruto(
        genero="???", desfecho="talvez", temperatura="fervendo", tipo_contato="alienigena",
        notas={**NOTAS_BOAS, "empatia": 15, "valor": -3, "clareza": "7,5", "ortografia": "n/a", "objecoes": None},
        resumo="x" * 1000,
    )
    campos, dados = validar_resposta(b, METODO, {})
    assert campos["genero"] == "Indefinido"
    assert campos["desfecho"] == "em_andamento"
    assert campos["temperatura"] is None
    assert campos["tipo_contato"] == "lead"
    assert dados["notas"]["empatia"] == 10.0 and dados["notas"]["valor"] == 0.0
    assert dados["notas"]["clareza"] == 7.5 and dados["notas"]["ortografia"] is None
    assert len(campos["resumo"]) <= aia.LIMITES_TEXTO["resumo"]


def test_quem_nao_e_lead_fica_fora_das_medias():
    campos, dados = validar_resposta(
        bruto(eh_lead=False, tipo_contato="aluno", desfecho="sucesso", precisa_atencao=True),
        METODO, {"primeira_resposta": 10.0, "tempo_medio": 10.0})
    assert campos["desfecho"] == "nao_lead"
    assert campos["nota_final"] is None and campos["nota_ligacao"] is None
    assert campos["precisa_atencao"] is False and campos["temperatura"] is None
    assert dados["tecnicas_aplicadas"] == []


def test_perdido_sempre_tem_motivo_e_sucesso_nao_precisa_atencao():
    campos, _ = validar_resposta(bruto(desfecho="perdido", motivo_perda="sei lá"), METODO, {})
    assert campos["motivo_perda"] == "outro"
    campos, _ = validar_resposta(bruto(desfecho="sucesso", motivo_perda="preco"), METODO, {})
    assert campos["motivo_perda"] is None and campos["precisa_atencao"] is False


def test_resposta_sem_o_essencial_e_rejeitada():
    for ruim in [{}, {"resumo": "só isso"}, [], None]:
        with pytest.raises(RespostaInvalida):
            validar_resposta(ruim, METODO, {})


# ---------------------------------------------------------------------------
# Entrada para a IA
# ---------------------------------------------------------------------------
def _sessao(**extra):
    base = {"telefone": "5544991234567", "inicio": ts(5, 10, 0), "fim": ts(5, 10, 30), "nome": "Maria Souza",
            "nova": True, "iniciada_por": "cliente", "primeira_resposta_util_seg": 420,
            "tempo_medio_resposta_util_seg": 450, "tempo_max_resposta_util_seg": 600,
            "respondida_por": "atendente", "aguardando_desde": None}
    base.update(extra)
    return base


def test_entrada_tem_contexto_numeros_e_transcricao():
    mensagens = [
        msg(5, 10, 0, "cliente", "Oi, quanto custa?"),
        msg(5, 10, 1, "bot", "Chamando função: x()", tipo="ferramenta"),
        msg(5, 10, 7, "atendente", "Bom dia!\nR$ 120"),
        msg(6, 9, 0, "cliente", "Obrigado"),
    ]
    texto = montar_entrada(_sessao(), mensagens)
    assert "Contato: Maria Souza" in texto and "NOVO" in texto
    assert "1ª resposta da equipe: 7 min" in texto
    assert "ninguém aguardando resposta" in texto
    assert "--- seg 05/10 ---" in texto and "--- ter 06/10 ---" in texto
    assert "10:00 Cliente: Oi, quanto custa?" in texto
    assert "10:07 Atendente: Bom dia! R$ 120" in texto       # quebra de linha vira espaço
    assert "Chamando função" not in texto                     # log interno não vai para a IA


def test_entrada_inclui_analises_anteriores_e_espera_atual():
    anterior = {"sessao_inicio": ts(3, 10, 0), "desfecho": "em_andamento", "resumo": "pediu preço",
                "nota_final": 6.5}
    sessao = _sessao(nova=False, aguardando_desde=ts(5, 10, 20))
    texto = montar_entrada(sessao, [msg(5, 10, 20, "cliente", "Alguém aí?")],
                           mesma_conversa=anterior, conversa_anterior=anterior, agora=ts(5, 11, 20))
    assert "QUE VOLTOU" in texto
    assert "espera resposta há 1 h" in texto or "espera resposta há 60 min" in texto
    assert "SUA ANÁLISE ANTERIOR DESTA MESMA CONVERSA" in texto
    assert "CONVERSA ANTERIOR DESTE CONTATO (dia 03/10)" in texto
    assert "pediu preço" in texto


def test_conversa_gigante_fica_com_comeco_e_fim():
    linhas = [f"10:{i % 60:02d} Cliente: mensagem numero {i} " + "x" * 50 for i in range(300)]
    cortadas = cortar_transcricao(linhas, 3000)
    assert sum(len(l) + 1 for l in cortadas) < 3300
    assert cortadas[0] == linhas[0] and cortadas[-1] == linhas[-1]
    assert any("omitidas" in l for l in cortadas)
    assert cortar_transcricao(linhas[:3], 3000) == linhas[:3]


def test_instrucoes_trazem_as_tecnicas_e_o_metodo_carrega():
    metodo = aia.carregar_metodo()
    assert metodo["fonte"] == "geral" and "T03" in metodo["tecnicas"] and len(metodo["tecnicas"]) >= 9
    instrucoes = aia.montar_instrucoes(metodo)
    assert "{{TECNICAS}}" not in instrucoes and "T05 — Convite com dia e hora" in instrucoes


def test_fonte_curso_sem_arquivo_cai_nas_boas_praticas(monkeypatch):
    monkeypatch.setattr(config, "METODO_FONTE", "curso")
    assert aia.carregar_metodo()["fonte"] == "geral"


# ---------------------------------------------------------------------------
# A rodada (banco e IA simulados)
# ---------------------------------------------------------------------------
class IAFalsa:
    def __init__(self, respostas, ligada=True):
        self.respostas = list(respostas)
        self.chamadas = 0
        self._ligada = ligada

    def ia_ligada(self):
        return self._ligada

    def gerar_json(self, sistema, usuario, max_tokens=0):
        self.chamadas += 1
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, {"entrada": 100, "saida": 50}

    IAErro = IAErro


@pytest.fixture
def banco(monkeypatch):
    """Substitui o banco: 3 conversas pendentes, grava o que seria salvo."""
    estado = {"salvas": [], "erros": []}
    fila = [_sessao(telefone=f"55449912345{i}0", inicio=ts(5, 10, i)) for i in range(3)]

    monkeypatch.setattr(aia.analises_repo, "pendentes", lambda desde, excluir=None, limite=80: fila)
    monkeypatch.setattr(aia.analises_repo, "da_sessao", lambda t, i: None)
    monkeypatch.setattr(aia.analises_repo, "anterior_do_contato", lambda t, i: None)
    monkeypatch.setattr(aia.analises_repo, "salvar",
                        lambda *a, **k: estado["salvas"].append(a))
    monkeypatch.setattr(aia.analises_repo, "registrar_erro",
                        lambda t, i, e: estado["erros"].append((t, e)))
    monkeypatch.setattr(aia.mensagens_repo, "listar_brutas", lambda t, desde=None: [
        msg(5, 10, 0, "cliente", "Oi"), msg(5, 10, 5, "atendente", "Olá!"),
        msg(6, 22, 0, "cliente", "outra conversa, depois de uma pausa longa")])
    monkeypatch.setattr(aia, "_ignorados", lambda: [])
    return estado


def test_rodada_analisa_todas_e_grava_ate_a_ultima_mensagem_da_conversa(banco):
    ia = IAFalsa([bruto(), bruto(), bruto()])
    r = aia.analisar_pendentes(agora=ts(5, 12, 0), ia=ia)
    assert r["analisadas"] == 3 and r["erros"] == 0 and r["parou_por"] is None
    assert len(banco["salvas"]) == 3
    telefone, sessao_inicio, analisado_ate = banco["salvas"][0][:3]
    assert analisado_ate == ts(5, 10, 5)      # a mensagem de 06/10 é de OUTRA conversa: não conta


def test_uma_falha_nao_derruba_as_outras(banco):
    ia = IAFalsa([IAErro("HTTP 500"), {"lixo": True}, bruto()])
    r = aia.analisar_pendentes(agora=ts(5, 12, 0), ia=ia)
    assert r["analisadas"] == 1 and r["erros"] == 2
    assert len(banco["erros"]) == 2 and len(banco["salvas"]) == 1


def test_tres_falhas_seguidas_param_a_rodada(banco, monkeypatch):
    fila = [_sessao(telefone=f"5544991234{i}00", inicio=ts(5, 10, i)) for i in range(6)]
    monkeypatch.setattr(aia.analises_repo, "pendentes", lambda desde, excluir=None, limite=80: fila)
    ia = IAFalsa([IAErro("fora")] * 6)
    r = aia.analisar_pendentes(agora=ts(5, 12, 0), ia=ia)
    assert ia.chamadas == 3 and r["parou_por"] == "erros" and r["restantes"] == 3


def test_ia_desligada_nao_faz_nada(banco):
    r = aia.analisar_pendentes(ia=IAFalsa([], ligada=False))
    assert r["ligada"] is False and r["analisadas"] == 0 and banco["salvas"] == []


def test_limite_de_tempo_interrompe_a_rodada(banco, monkeypatch):
    monkeypatch.setattr(config, "ANALISTA_IA_MAX_MINUTOS_RODADA", 0)
    ia = IAFalsa([bruto()] * 3)
    r = aia.analisar_pendentes(agora=ts(5, 12, 0), ia=ia)
    assert r["parou_por"] == "tempo" and r["restantes"] == 3 and ia.chamadas == 0
