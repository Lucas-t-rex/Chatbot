"""
Contatos (um por número de WhatsApp) + números travados + mapeamento de LID.
Substitui os acessos à coleção "conversations" do Mongo que não são histórico.
"""
from datetime import datetime, timezone

from app.core.db import db, json_param

# Colunas que podem ser alteradas por atualizar(). Protege o SQL montado
# dinamicamente: só nomes desta lista viram coluna.
_CAMPOS_EDITAVEIS = {
    "sender_name", "customer_name", "last_interaction", "conversation_status",
    "followup_stage", "name_transition_stage", "intervention_active",
    "scheduled_datetime", "client_profile", "profiler_last_ts",
    "processing", "processing_started_at",
}


def _agora():
    return datetime.now(timezone.utc)


def garantir(telefone):
    """Cria o contato se ainda não existir. Não mexe em nada se já existir."""
    db.executar(
        "insert into contatos (telefone) values (%s) on conflict (telefone) do nothing",
        (telefone,),
    )


def obter(telefone):
    return db.um("select * from contatos where telefone = %s", (telefone,))


def atualizar(telefone, **campos):
    """
    Upsert: cria o contato se não existir e grava os campos informados.
    Equivale ao update_one({'_id': tel}, {'$set': campos}, upsert=True) do Mongo.
    """
    if not campos:
        garantir(telefone)
        return
    invalidos = set(campos) - _CAMPOS_EDITAVEIS
    if invalidos:
        raise ValueError(f"Campos não editáveis em contatos: {invalidos}")

    nomes = list(campos)
    valores = [json_param(campos[n]) if n == "client_profile" else campos[n] for n in nomes]
    colunas = ", ".join(nomes)
    marcadores = ", ".join(["%s"] * len(nomes))
    sets = ", ".join(f"{n} = excluded.{n}" for n in nomes)
    db.executar(
        f"insert into contatos (telefone, {colunas}) values (%s, {marcadores}) "
        f"on conflict (telefone) do update set {sets}",
        [telefone, *valores],
    )


def incrementar_tokens(telefone, tokens_in, tokens_out):
    if not tokens_in and not tokens_out:
        return
    db.executar(
        """
        insert into contatos (telefone, tokens_input, tokens_output, total_tokens_consumed)
        values (%s, %s, %s, %s)
        on conflict (telefone) do update set
            tokens_input = contatos.tokens_input + excluded.tokens_input,
            tokens_output = contatos.tokens_output + excluded.tokens_output,
            total_tokens_consumed = contatos.total_tokens_consumed + excluded.total_tokens_consumed
        """,
        (telefone, tokens_in, tokens_out, tokens_in + tokens_out),
    )


def listar(status=None, desde=None, ate=None):
    """Contatos ordenados pela última interação (mais recente primeiro)."""
    filtros, params = [], []
    if status:
        filtros.append("conversation_status = %s")
        params.append(status)
    if desde is not None and ate is not None:
        filtros.append("last_interaction >= %s and last_interaction < %s")
        params += [desde, ate]
    where = ("where " + " and ".join(filtros)) if filtros else ""
    return db.todos(
        f"select * from contatos {where} order by last_interaction desc nulls last",
        params,
    )


def encerrar_intervencao(telefone):
    """Desliga o atendimento humano. Devolve True só se estava ligado (igual ao modified_count do Mongo)."""
    return db.executar(
        "update contatos set intervention_active = false where telefone = %s and intervention_active is true",
        (telefone,),
    ) > 0


def total_tokens():
    """(numero_de_contatos, soma_de_tokens) — usado no relatório de tokens."""
    linha = db.um("select count(*) as n, coalesce(sum(total_tokens_consumed), 0) as soma from contatos")
    return linha["n"], linha["soma"]


# ---------------------------------------------------------------------------
# Lock de processamento (impede duas respostas da IA ao mesmo tempo pro mesmo
# cliente). Mesmo comportamento do Mongo: só pega o lock quem achar processing
# diferente de true.
# ---------------------------------------------------------------------------
def liberar_lock_travado(telefone, iniciado_antes_de):
    db.executar(
        """
        update contatos set processing = false, processing_started_at = null
        where telefone = %s and processing is true and processing_started_at < %s
        """,
        (telefone, iniciado_antes_de),
    )


def adquirir_lock(telefone):
    """True se conseguiu o lock."""
    garantir(telefone)
    linha = db.um(
        """
        update contatos set processing = true, processing_started_at = %s
        where telefone = %s and processing is not true
        returning telefone
        """,
        (_agora(), telefone),
    )
    return linha is not None


def liberar_lock(telefone):
    db.executar(
        "update contatos set processing = false, processing_started_at = null where telefone = %s",
        (telefone,),
    )


# ---------------------------------------------------------------------------
# Follow-up automático (modo chat)
# ---------------------------------------------------------------------------
_CAMPOS_DATA_FOLLOWUP = {"last_interaction", "scheduled_datetime"}


def _filtro_estagio(estagio_atual):
    if estagio_atual == 0:
        return "(followup_stage = 0 or followup_stage is null)", []
    return "followup_stage = %s", [estagio_atual]


def expirar_followups(status, campo_data, limite, estagio_atual, prox_estagio):
    """Avança o estágio de quem passou do prazo sem receber nada. Devolve quantos."""
    if campo_data not in _CAMPOS_DATA_FOLLOWUP:
        raise ValueError(campo_data)
    filtro_est, params_est = _filtro_estagio(estagio_atual)
    return db.executar(
        f"""
        update contatos set followup_stage = %s
        where conversation_status = %s
          and {campo_data} is not null and {campo_data} < %s
          and {filtro_est}
          and processing is not true
          and intervention_active is not true
        """,
        [prox_estagio, status, limite, *params_est],
    )


def candidatos_followup(status, campo_data, ate, desde, estagio_atual, limite=50):
    """Contatos cujo follow-up está na janela certa de envio."""
    if campo_data not in _CAMPOS_DATA_FOLLOWUP:
        raise ValueError(campo_data)
    filtro_est, params_est = _filtro_estagio(estagio_atual)
    return db.todos(
        f"""
        select * from contatos
        where conversation_status = %s
          and {campo_data} < %s and {campo_data} >= %s
          and {filtro_est}
          and processing is not true
          and intervention_active is not true
        limit %s
        """,
        [status, ate, desde, *params_est, limite],
    )


# ---------------------------------------------------------------------------
# Números travados (atendimento humano: o bot ignora estes números)
# ---------------------------------------------------------------------------
def numero_travado(numero):
    return db.um("select 1 from numeros_travados where numero = %s", (numero,)) is not None


def travar(numero):
    db.executar(
        "insert into numeros_travados (numero) values (%s) on conflict (numero) do nothing",
        (numero,),
    )


def destravar(numero):
    db.executar("delete from numeros_travados where numero = %s", (numero,))


def listar_travados():
    return [l["numero"] for l in db.todos("select numero from numeros_travados order by criado_em")]


# ---------------------------------------------------------------------------
# LID: id interno do WhatsApp (iOS/Web) -> número real
# ---------------------------------------------------------------------------
def salvar_lid(lid, numero_real):
    db.executar(
        """
        insert into lid_mappings (lid, real_number, last_seen) values (%s, %s, %s)
        on conflict (lid) do update set real_number = excluded.real_number, last_seen = excluded.last_seen
        """,
        (lid, numero_real, _agora()),
    )


def resolver_lid(lid):
    linha = db.um("select real_number from lid_mappings where lid = %s", (lid,))
    return linha["real_number"] if linha else None
