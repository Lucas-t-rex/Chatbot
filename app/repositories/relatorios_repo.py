"""
Relatórios enviados ao dono (tabela relatorios_dono): registro do que foi mandado
e base para comparar ("+4 desde as 12h", "vs. semana passada").
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


def diario_anterior(referencia, antes_da_hora):
    """O último diário enviado nesse dia com hora anterior à informada (ou None)."""
    return db.um(
        """
        select * from relatorios_dono
        where tipo = 'diario' and referencia = %s and enviado and hora < %s
        order by hora desc, criado_em desc limit 1
        """,
        (referencia, antes_da_hora),
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
