"""
RODADA DO ANALISTA — o que acontece às 22h (resumo do dia) e no sábado às 18h (semanal):

  1. a IA lê as conversas pendentes (uma de cada vez) e grava as análises;
  2. monta o resumo (do dia às 22h, ou da semana no sábado às 18h);
  3. manda para o dono (no sábado, junto com a planilha) e registra o que foi enviado.

Os envios chegam como funções (enviar_texto / enviar_documento), assim a trava de
segurança do main.py (_envio_permitido) continua valendo e dá para testar sem WhatsApp.

Regra de ouro: o dono nunca fica sem mensagem. Se a IA ou o relatório novo falharem,
sai o resumo só com números.
"""
import threading
from datetime import datetime, timezone

from app.core.config import config
from app.repositories import relatorios_repo
from app.services import analista, analista_excel, analista_ia, analista_relatorios

TZ = config.FUSO_HORARIO
_trava = threading.Lock()      # duas rodadas nunca andam juntas (a IA é lenta)


def eh_hora_do_semanal(hora, agora):
    """Sábado às 18h (config) o semanal sai NO LUGAR do diário."""
    return agora.astimezone(TZ).weekday() == config.RESUMO_SEMANAL_DIA and hora == config.RESUMO_SEMANAL_HORA


def dia_fechado(agora):
    """A equipe não atende hoje (ex.: sábado e domingo)?"""
    return not config.HORARIO_ATENDIMENTO.get(agora.astimezone(TZ).weekday())


def _registrar(tipo, referencia, hora, dados, texto, enviado):
    try:
        relatorios_repo.salvar(tipo, referencia, hora, dados, texto, enviado)
    except Exception as e:
        print(f"⚠️ [Analista] Não consegui registrar o relatório no banco: {e}")


def _resumo_so_com_numeros(hora, agora):
    """Plano B: o resumo antigo, que só depende do banco."""
    inicio, fim = analista.janela_do_dia(agora)
    resumo = analista.resumo_periodo(inicio, fim, agora)
    return analista.texto_resumo(resumo, analista.rotulo_do_horario(hora, agora)), resumo["conversas"]


def executar_diario(hora, destino, enviar_texto, agora=None, ia=None):
    agora = agora or datetime.now(timezone.utc)
    analise = analista_ia.analisar_pendentes(agora=agora, ia=ia)
    try:
        out = analista_relatorios.montar_diario(hora, agora=agora, ia=ia)
        texto, conversas, dados, referencia = out["texto"], out["conversas"], out["dados"], out["referencia"]
    except Exception as e:
        print(f"⚠️ [Analista] Relatório com IA falhou ({e}); mandando o resumo só com números.")
        texto, conversas = _resumo_so_com_numeros(hora, agora)
        dados, referencia = {}, agora.astimezone(TZ).date()

    if dia_fechado(agora) and not conversas:
        print(f"🔕 [Analista] Resumo das {hora}h não enviado: dia sem atendimento e sem conversas.")
        return {"enviado": False, "motivo": "dia fechado sem conversas", "analise": analise}

    enviado = bool(enviar_texto(destino, texto))
    _registrar("diario", referencia, hora, dados, texto, enviado)
    print(f"📊 [Analista] Resumo das {hora}h {'enviado' if enviado else 'NÃO enviado'}: {conversas} conversas; "
          f"IA analisou {analise['analisadas']} (erros: {analise['erros']}).")
    return {"enviado": enviado, "conversas": conversas, "analise": analise, "texto": texto}


def executar_semanal(destino, enviar_texto, enviar_documento, agora=None, ia=None):
    agora = agora or datetime.now(timezone.utc)
    analise = analista_ia.analisar_pendentes(agora=agora, ia=ia, dias=7)
    out = analista_relatorios.montar_semanal(agora=agora, ia=ia)
    texto, dados = out["texto"], out["dados"]
    enviado = bool(enviar_texto(destino, texto))
    planilha_ok = False
    try:
        conteudo = analista_excel.gerar(out["inicio"], out["fim"], agora)
        ini, fim = out["inicio"].astimezone(TZ), out["fim"].astimezone(TZ)
        nome = f"atendimento_semana_{ini:%d-%m}_a_{fim:%d-%m-%Y}.xlsx"
        planilha_ok = bool(enviar_documento(destino, conteudo, nome, "Planilha da semana"))
    except Exception as e:
        print(f"❌ [Analista] Não consegui gerar/enviar a planilha da semana: {e}")
    _registrar("semanal", out["referencia"], config.RESUMO_SEMANAL_HORA, dados, texto, enviado)
    print(f"📈 [Analista] Resumo semanal {'enviado' if enviado else 'NÃO enviado'}, "
          f"planilha {'enviada' if planilha_ok else 'NÃO enviada'}: {out['conversas']} conversas.")
    return {"enviado": enviado, "planilha": planilha_ok, "analise": analise, "texto": texto}


def executar(hora, destino, enviar_texto, enviar_documento, agora=None, ia=None):
    """Ponto de entrada do job do resumo (22h e sábado 18h). Nunca levanta exceção."""
    agora = agora or datetime.now(timezone.utc)
    if not _trava.acquire(blocking=False):
        print(f"⏳ [Analista] Já há uma rodada em andamento; a das {hora}h foi pulada.")
        return {"enviado": False, "motivo": "rodada em andamento"}
    try:
        if eh_hora_do_semanal(hora, agora):
            try:
                return executar_semanal(destino, enviar_texto, enviar_documento, agora=agora, ia=ia)
            except Exception as e:
                print(f"⚠️ [Analista] Resumo semanal falhou ({e}); mandando o resumo do dia no lugar.")
        return executar_diario(hora, destino, enviar_texto, agora=agora, ia=ia)
    except Exception as e:
        print(f"❌ [Analista] Erro na rodada das {hora}h: {e}")
        return {"enviado": False, "motivo": f"erro: {e}"}
    finally:
        _trava.release()
