import logging
from pymongo import MongoClient
from app.core.config import config
import time

log = logging.getLogger(__name__)

class DatabaseConnections:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DatabaseConnections, cls).__new__(cls)
            cls._instance.client_conversas = None
            cls._instance.client_agenda = None
            cls._instance._connect()
        return cls._instance

    def _connect(self):
        try:
            # Added maxPoolSize for better network concurrency
            self.client_conversas = MongoClient(config.MONGO_DB_URI, maxPoolSize=50, socketTimeoutMS=10000)
            db_conversas = self.client_conversas[config.DB_NAME]
            self.conversation_collection = db_conversas.conversations
            
            # Create indexes
            self.conversation_collection.create_index([
                ("conversation_status", 1), 
                ("last_interaction", 1), 
                ("followup_stage", 1)
            ])
            log.info("🚀 [Performance] Índices de busca rápida garantidos no DB Conversas.")
            
        except Exception as e:
            log.error(f"❌ ERRO: [DB Conversas] Não foi possível conectar ao MongoDB. Erro: {e}")
            self.conversation_collection = None
            
        try:
            self.client_agenda = MongoClient(config.MONGO_AGENDA_URI, maxPoolSize=50, socketTimeoutMS=10000)
            log.info("✅ [DB Agenda] Conectado com sucesso.")
        except Exception as e:
            log.error(f"❌ ERRO: [DB Agenda] DB: {e}")
            self.client_agenda = None

db = DatabaseConnections()
conversation_collection = db.conversation_collection
client_agenda = db.client_agenda
