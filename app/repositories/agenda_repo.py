"""
Agenda de aulas (era a coleção "agendamentos" do Mongo).
inicio/fim são horário de parede de São Paulo, SEM fuso (datetime "naive"),
exatamente como o código já trabalhava.
"""
from datetime import datetime, timezone

from app.core.db import db

_CAMPOS_EDITAVEIS = {
    "inicio", "fim", "status", "reminder_sent", "post_attendance_followup_sent",
    "data_atualizacao_status",
}


def _como_doc(linha):
    """Devolve a linha com 'id' em string e também '_id' (compatível com o código antigo)."""
    if linha is None:
        return None
    doc = dict(linha)
    doc["id"] = str(doc["id"])
    doc["_id"] = doc["id"]
    return doc


def _como_docs(linhas):
    return [_como_doc(l) for l in linhas]


def obter(ag_id):
    try:
        return _como_doc(db.um("select * from agendamentos where id = %s", (str(ag_id),)))
    except Exception:
        return None  # id em formato inválido


def inserir(nome, telefone, servico, inicio, fim, duracao_minutos=None, observacao="",
            owner_whatsapp_id=None, status=None):
    linha = db.um(
        """
        insert into agendamentos
            (owner_whatsapp_id, nome, telefone, servico, observacao, duracao_minutos, inicio, fim, status, created_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (owner_whatsapp_id, nome, telefone or "", servico, observacao or "", duracao_minutos,
         inicio, fim, status, datetime.now(timezone.utc)),
    )
    return str(linha["id"]) if linha else None


def atualizar(ag_id, **campos):
    """Devolve quantas linhas foram alteradas (0 = não achou)."""
    invalidos = set(campos) - _CAMPOS_EDITAVEIS
    if invalidos:
        raise ValueError(f"Campos não editáveis em agendamentos: {invalidos}")
    sets = ", ".join(f"{n} = %s" for n in campos)
    return db.executar(
        f"update agendamentos set {sets} where id = %s",
        [*campos.values(), str(ag_id)],
    )


def excluir(ag_id):
    return db.executar("delete from agendamentos where id = %s", (str(ag_id),))


def listar_todos():
    return _como_docs(db.todos("select * from agendamentos order by inicio"))


def do_intervalo(inicio, fim):
    """Agendamentos com inicio em [inicio, fim)."""
    return _como_docs(db.todos(
        "select * from agendamentos where inicio >= %s and inicio < %s order by inicio",
        (inicio, fim),
    ))


def existe_bloqueio_no_dia(inicio_dia, fim_dia):
    """Folga administrativa marcada no dia (serviço 'Folga' ou status 'bloqueado')."""
    return db.um(
        """
        select 1 from agendamentos
        where inicio >= %s and inicio <= %s and (servico = 'Folga' or status = 'bloqueado')
        limit 1
        """,
        (inicio_dia, fim_dia),
    ) is not None


def contar_conflitos(novo_inicio, novo_fim, excluir_id=None):
    if excluir_id:
        linha = db.um(
            "select count(*) as n from agendamentos where inicio < %s and fim > %s and id <> %s",
            (novo_fim, novo_inicio, str(excluir_id)),
        )
    else:
        linha = db.um(
            "select count(*) as n from agendamentos where inicio < %s and fim > %s",
            (novo_fim, novo_inicio),
        )
    return linha["n"]


def futuros_do_telefone(telefone, a_partir_de):
    return _como_docs(db.todos(
        "select * from agendamentos where telefone = %s and inicio >= %s order by inicio",
        (telefone, a_partir_de),
    ))


def obter_por_telefone_inicio(telefone, inicio):
    return _como_doc(db.um(
        "select * from agendamentos where telefone = %s and inicio = %s limit 1",
        (telefone, inicio),
    ))


def existe_mesmo_nome_horario(telefone, inicio, nome):
    """Mesma pessoa (nome igual, sem diferenciar maiúsculas) já marcada no mesmo horário."""
    return db.um(
        """
        select 1 from agendamentos
        where telefone = %s and inicio = %s and lower(nome) = lower(%s)
        limit 1
        """,
        (telefone, inicio, nome),
    ) is not None


def excluir_por_telefone_inicio(telefone, inicio):
    """Apaga e devolve o agendamento removido (ou None)."""
    return _como_doc(db.um(
        """
        delete from agendamentos
        where id = (select id from agendamentos where telefone = %s and inicio = %s limit 1)
        returning *
        """,
        (telefone, inicio),
    ))


def excluir_futuros_do_telefone(telefone, a_partir_de):
    return db.executar(
        "delete from agendamentos where telefone = %s and inicio >= %s",
        (telefone, a_partir_de),
    )


def contar_atendimentos_ativos(inicio, fim):
    """Aulas de verdade no intervalo (ignora folgas, cancelados, ausências e bloqueios)."""
    return db.um(
        """
        select count(*) as n from agendamentos
        where inicio >= %s and inicio <= %s
          and servico <> 'Folga'
          and (status is null or status not in ('cancelado', 'ausencia', 'bloqueado'))
        """,
        (inicio, fim),
    )["n"]


def excluir_bloqueios_no_intervalo(inicio, fim):
    return db.executar(
        """
        delete from agendamentos
        where inicio >= %s and inicio <= %s and (servico = 'Folga' or status = 'bloqueado')
        """,
        (inicio, fim),
    )


# ---------------------------------------------------------------------------
# Jobs do modo chat
# ---------------------------------------------------------------------------
def pendentes_de_lembrete(agora, janela_limite, criado_antes_de):
    return _como_docs(db.todos(
        """
        select * from agendamentos
        where inicio > %s and inicio <= %s
          and reminder_sent is not true
          and created_at <= %s
        """,
        (agora, janela_limite, criado_antes_de),
    ))


def pendentes_pos_atendimento(atualizado_antes_de):
    return _como_docs(db.todos(
        """
        select * from agendamentos
        where status in ('concluido', 'ausencia')
          and post_attendance_followup_sent is not true
          and data_atualizacao_status is not null
          and data_atualizacao_status <= %s
        """,
        (atualizado_antes_de,),
    ))
