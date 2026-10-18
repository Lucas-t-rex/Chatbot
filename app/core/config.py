import os
from dotenv import load_dotenv
import pytz

load_dotenv()

class Config:
    # ==========================================================================
    # MODO DE OPERAÇÃO — a chave geral do sistema
    #   "analista" = o bot NÃO responde ninguém. Só escuta, salva as conversas
    #                (cliente + atendente humano), mede o atendimento e manda
    #                os resumos pro dono.
    #   "chat"     = comportamento original: a IA responde, agenda e faz follow-up.
    #   "ambos"    = a IA responde E o analista mede tudo.
    # O modo de verdade fica salvo no banco (tabela "configuracoes") e é trocado
    # pelo app, sem reiniciar. Este valor só é usado na primeira vez, quando o
    # banco ainda não tem nenhum modo salvo.
    # ==========================================================================
    MODO_OPERACAO = os.environ.get("MODO_OPERACAO", "analista")

    # No modo analista, transcrever áudios via Gemini.
    # Custa tokens, mas sem isso a análise fica cega em quem manda áudio.
    AUDITOR_TRANSCREVER_AUDIO = os.environ.get("AUDITOR_TRANSCREVER_AUDIO", "true").lower() == "true"

    FUSO_HORARIO = pytz.timezone('America/Sao_Paulo')
    CLIENT_NAME = "Brooklyn Academia"
    # Quem recebe os resumos e avisos. Em teste: fly secrets set RESPONSIBLE_NUMBER=...
    # Para voltar ao dono: fly secrets unset RESPONSIBLE_NUMBER (cai neste padrão).
    RESPONSIBLE_NUMBER = os.environ.get("RESPONSIBLE_NUMBER", "554491216103")
    ADMIN_USER = "brooklyn"
    ADMIN_PASS = "brooklyn2025"

    # Assina os tokens de login do app. Em produção: fly secrets set API_SECRET=...
    API_SECRET = os.environ.get("API_SECRET", "troque-este-segredo-em-producao")
    API_TOKEN_VALIDADE_DIAS = 30

    EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL")
    EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    MODEL_NAME = "gemini-3-flash-preview"

    # Database (Supabase / Postgres). Use a URL do "Session pooler" do Supabase:
    # Project Settings > Database > Connection string > Session pooler
    SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL")

    # ==========================================================================
    # ANALISTA — regras das métricas
    # ==========================================================================
    # Pausa que separa uma conversa da outra do mesmo contato.
    GAP_NOVA_CONVERSA_HORAS = 12
    # A partir de quantos minutos esperando o cliente conta como "sem resposta".
    LIMITE_SEM_RESPOSTA_MIN = 15
    # Horários (hora cheia) em que o dono recebe o resumo no WhatsApp.
    # 12h = só a manhã; os demais = o dia todo acumulado.
    HORARIOS_RESUMO = [12, 18, 22]
    # Quantos clientes sem resposta listar na mensagem do dono.
    MAX_SEM_RESPOSTA_NO_RESUMO = 10

    # ==========================================================================
    # ANALISTA COM IA — lê cada conversa e dá notas. Fica DESLIGADO enquanto não
    # houver ANALISTA_IA_API_KEY (de propósito não reaproveita a GEMINI_API_KEY
    # do bot). Para ligar: fly secrets set ANALISTA_IA_API_KEY=...
    # Provedores: "gemini" | "anthropic" | "openai".
    # ==========================================================================
    ANALISTA_IA_PROVEDOR = os.environ.get("ANALISTA_IA_PROVEDOR", "gemini").lower()
    ANALISTA_IA_MODELO = os.environ.get("ANALISTA_IA_MODELO", "gemini-3-flash-preview")
    ANALISTA_IA_API_KEY = os.environ.get("ANALISTA_IA_API_KEY")
    ANALISTA_IA_TIMEOUT_SEG = 90
    # Limites de uma rodada de análise (12h/18h/22h).
    ANALISTA_IA_MAX_CONVERSAS_RODADA = 80
    ANALISTA_IA_MAX_MINUTOS_RODADA = 20
    ANALISTA_IA_DIAS_PARA_TRAS = 2          # só analisa conversas com atividade nos últimos N dias
    ANALISTA_IA_MAX_CHARS_CONVERSA = 15000  # conversa maior: fica o começo e o fim
    # De onde vêm as técnicas de venda: "geral" (boas práticas, provisório) ou
    # "curso" (extraídas do PDF do curso da academia). Veja app/prompts/metodo_vendas.md
    METODO_FONTE = os.environ.get("METODO_FONTE", "geral")
    # Dia e hora do resumo semanal (0 = segunda ... 5 = sábado).
    RESUMO_SEMANAL_DIA = 5
    RESUMO_SEMANAL_HORA = 18

    # Application settings
    INTERVALO_SLOTS_MINUTOS = 15
    NUM_ATENDENTES = 50
    BUFFER_TIME_SECONDS = 25
    
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
    
    # Horário em que a EQUIPE atende o WhatsApp (0 = segunda). O analista só conta
    # o tempo de resposta dentro destes blocos (mensagem das 3h respondida às 7h
    # conta só a partir da abertura). Fim de semana fechado: não conta espera.
    HORARIO_ATENDIMENTO = {
        0: [{"inicio": "06:00", "fim": "21:00"}],
        1: [{"inicio": "06:00", "fim": "21:00"}],
        2: [{"inicio": "06:00", "fim": "21:00"}],
        3: [{"inicio": "06:00", "fim": "21:00"}],
        4: [{"inicio": "06:00", "fim": "20:00"}],
        5: [],
        6: [],
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
            0: ["18:30"], 2: ["18:30"],  # Seg/Qua 18:30
            1: ["20:00"], 3: ["20:00"]   # Ter/Qui 20:00 | Sex (sparring) sem agendamento
        },
        "jiu-jitsu": {
            0: ["19:30"], 2: ["19:30"]  # Seg/Qua 19:30
        },
        "jiu-jitsu kids": {
            1: ["18:00"], 3: ["18:00"]
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
