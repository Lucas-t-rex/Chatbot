"""
LIMPAR CONVERSAS — apaga o histórico do banco para começar do zero (ex.: troca
do celular conectado na Evolution). Antes de apagar, salva TUDO num .json.

Apaga: contatos (e, em cascata, mensagens, sessoes_atendimento e
analises_conversa) e relatorios_dono.
Mantém: configuracoes (modo, bot on/off), agendamentos, numeros_travados e
lid_mappings.

Uso (dentro de chatbot-python/, com SUPABASE_DB_URL no .env):
    python scripts/limpar_conversas.py --backup PASTA            # só mostra quanto tem
    python scripts/limpar_conversas.py --backup PASTA --apagar   # salva o backup e apaga
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app.core.config import config  # noqa: E402,F401  (carrega o .env)
from app.core.db import db  # noqa: E402

TABELAS = ["contatos", "mensagens", "sessoes_atendimento", "analises_conversa", "relatorios_dono"]


def contar():
    return {t: db.um(f"select count(*) as n from {t}")["n"] for t in TABELAS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", required=True, help="pasta onde salvar o backup .json")
    ap.add_argument("--apagar", action="store_true", help="sem isto, só mostra as contagens")
    args = ap.parse_args()

    print("Antes:", contar())
    if not args.apagar:
        print("Nada apagado (use --apagar).")
        return

    pasta = Path(args.backup)
    pasta.mkdir(parents=True, exist_ok=True)
    arquivo = pasta / f"backup_conversas_{datetime.now():%Y-%m-%d_%H%M%S}.json"
    backup = {t: db.todos(f"select * from {t}") for t in TABELAS}
    arquivo.write_text(json.dumps(backup, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Backup salvo em {arquivo} ({arquivo.stat().st_size // 1024} KB)")

    with db.conexao() as conn:
        with conn.transaction():
            conn.execute("delete from relatorios_dono")
            conn.execute("delete from contatos")   # leva mensagens, sessões e análises junto
    print("Depois:", contar())


if __name__ == "__main__":
    main()
