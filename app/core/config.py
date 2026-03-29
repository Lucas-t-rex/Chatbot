import os
from dotenv import load_dotenv
import pytz

load_dotenv()

class Config:
    FUSO_HORARIO = pytz.timezone('America/Sao_Paulo')
    CLIENT_NAME = "Brooklyn Academia"
    RESPONSIBLE_NUMBER = "554491216103"
    ADMIN_USER = "brooklyn"
    ADMIN_PASS = "brooklyn2025"
    
    EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL")
    EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    MODEL_NAME = "gemini-3-flash-preview"
    
    # Database
    MONGO_DB_URI = os.environ.get("MONGO_DB_URI")
    MONGO_AGENDA_URI = os.environ.get("MONGO_AGENDA_URI")
    MONGO_AGENDA_COLLECTION = os.environ.get("MONGO_AGENDA_COLLECTION", "agendamentos")
    DB_NAME = "brooklyn_academia"
    
    # Application settings
    INTERVALO_SLOTS_MINUTOS = 15
    NUM_ATENDENTES = 50
    BUFFER_TIME_SECONDS = 15
    
    # Follow-up times
    TEMPO_FOLLOWUP_1 = 90
    TEMPO_FOLLOWUP_2 = 360
    TEMPO_FOLLOWUP_3 = 22 * 60
    TEMPO_FOLLOWUP_SUCESSO = 22 * 60
    TEMPO_FOLLOWUP_FRACASSO = 22 * 60

    # Business Logic Constants
    BLOCOS_DE_TRABALHO = {
        0: [{"inicio": "05:00", "fim": "22:00"}],
        1: [{"inicio": "05:00", "fim": "22:00"}],
        2: [{"inicio": "05:00", "fim": "22:00"}],
        3: [{"inicio": "05:00", "fim": "22:00"}],
        4: [{"inicio": "05:00", "fim": "21:00"}],
        5: [],
        6: []
    }
    
    FOLGAS_DIAS_SEMANA = []
    MAPA_DIAS_SEMANA_PT = { 5: "sábado", 6: "domingo" }
    
    MAPA_SERVICOS_DURACAO = {
        "musculação": 60,
        "muay thai": 60,
        "jiu-jitsu": 60,
        "jiu-jitsu kids": 60,
        "capoeira": 60,
        "dança": 60
    }
    
    GRADE_HORARIOS_SERVICOS = {
        "muay thai": {
            0: ["19:30"], 2: ["19:30"], 4: ["19:00"]
        },
        "jiu-jitsu": {
            1: ["20:00"], 3: ["20:00"]
        },
        "jiu-jitsu kids": {
            1: ["18:15"], 3: ["18:15"]
        },
        "capoeira": {
            0: ["20:40"], 2: ["20:40"], 4: ["20:00"]
        },
        "dança": {
            0: ["08:00"], 2: ["08:00"],
            1: ["19:00"], 3: ["19:00"]
        }
    }
    
    LISTA_SERVICOS_PROMPT = ", ".join(MAPA_SERVICOS_DURACAO.keys())
    SERVICOS_PERMITIDOS_ENUM = list(MAPA_SERVICOS_DURACAO.keys())
    
    CLEAN_CLIENT_NAME_GLOBAL = CLIENT_NAME.lower().replace(" ", "_").replace("-", "_")

config = Config()
