"""
Relatórios enviados ao dono (tabela relatorios_dono): registro do que foi mandado
e base para comparar ("ontem 7,2", "vs. semana passada").
"""
from app.core.db import db, json_param


def salvar(tipo, referencia, hora, dados, texto, enviado):
    db.executar(
        """
        insert into relatorios_dono (tipo, referencia, hora, dados, texto, enviado)
        values (%s, %s, %s, %s, %s, %s)
        """,
        (tipo, referencia, hora, json_param(dados), texto, enviado),
    )


def diario_anterior(referencia):
    """
    O último diário enviado ANTES do dia 'referencia' (normalmente o de ontem;
    depois de um fim de semana sem resumo, o da sexta). None se não houver.
    """
    return db.um(
        """
        select * from relatorios_dono
        where tipo = 'diario' and referencia < %s and enviado
        order by referencia desc, hora desc, criado_em desc limit 1
        """,
        (referencia,),
    )


def semanal_da_semana(segunda):
    """O semanal cuja semana começou na 'segunda' informada (ou None)."""
    return db.um(
        """
        select * from relatorios_dono
        where tipo = 'semanal' and referencia = %s and enviado
        order by criado_em desc limit 1
        """,
        (segunda,),
    )
