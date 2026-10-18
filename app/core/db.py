import logging
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.core.config import config

log = logging.getLogger(__name__)


class Database:
    """
    Pool único de conexões com o Postgres do Supabase.
    Todo acesso ao banco passa pelos repositórios em app/repositories/,
    que usam os atalhos um() / todos() / executar() daqui.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.pool = None
            cls._instance._connect()
        return cls._instance

    def _connect(self):
        if not config.SUPABASE_DB_URL:
            log.error("❌ [DB] SUPABASE_DB_URL não definida. Banco indisponível.")
            print("❌ [DB] SUPABASE_DB_URL não definida. Banco indisponível.")
            return
        try:
            self.pool = ConnectionPool(
                conninfo=config.SUPABASE_DB_URL,
                min_size=1,
                max_size=10,
                timeout=15,
                # autocommit: cada comando é sua própria transação (igual era no Mongo).
                # prepare_threshold=None: compatível com o pooler do Supabase.
                kwargs={"row_factory": dict_row, "autocommit": True, "prepare_threshold": None},
                open=True,
            )
            print("✅ [DB] Pool de conexões com o Supabase criado.")
        except Exception as e:
            log.error(f"❌ [DB] Não foi possível criar o pool do Supabase: {e}")
            print(f"❌ [DB] Não foi possível criar o pool do Supabase: {e}")
            self.pool = None

    @property
    def disponivel(self) -> bool:
        return self.pool is not None

    @contextmanager
    def conexao(self):
        with self.pool.connection() as conn:
            yield conn

    def um(self, sql, params=None):
        """Primeira linha (dict) ou None."""
        with self.conexao() as conn:
            return conn.execute(sql, params).fetchone()

    def todos(self, sql, params=None):
        """Lista de linhas (dicts)."""
        with self.conexao() as conn:
            return conn.execute(sql, params).fetchall()

    def executar(self, sql, params=None) -> int:
        """Executa um comando e devolve quantas linhas foram afetadas."""
        with self.conexao() as conn:
            return conn.execute(sql, params).rowcount


def json_param(valor):
    """Envolve um dict/list para gravar em coluna jsonb."""
    return Jsonb(valor)


db = Database()
