
import google.generativeai as genai
import requests
import os
import pytz 
import re
import calendar
import json 
import logging
import base64
import time
import threading
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone, time as dt_time
from dateutil import parser as dateparser
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from pymongo.errors import ConnectionFailure, OperationFailure
from apscheduler.schedulers.background import BackgroundScheduler
from typing import Any, Dict, List, Optional
from flask_cors import CORS
from bson.objectid import ObjectId


FUSO_HORARIO = pytz.timezone('America/Sao_Paulo')
CLIENT_NAME="Neuro'up Soluções em Tecnologia"
RESPONSIBLE_NUMBER="554898389781"
ADMIN_USER = "admin"
ADMIN_PASS = "neuro2025"
load_dotenv()

EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_DB_URI = os.environ.get("MONGO_DB_URI") # DB de Conversas

MONGO_AGENDA_URI = os.environ.get("MONGO_AGENDA_URI")
MONGO_AGENDA_COLLECTION = os.environ.get("MONGO_AGENDA_COLLECTION", "agendamentos")

clean_client_name_global = CLIENT_NAME.lower().replace(" ", "_").replace("-", "_")
DB_NAME = "neuroup_solucoes_db"

INTERVALO_SLOTS_MINUTOS=30 
NUM_ATENDENTES=1

BLOCOS_DE_TRABALHO = [
    {"inicio": "08:00", "fim": "12:00"},
    {"inicio": "13:00", "fim": "18:00"}
]
FOLGAS_DIAS_SEMANA = [ 6 ] # Folga Domingo
MAPA_DIAS_SEMANA_PT = { 5: "sábado", 6: "domingo" }

MAPA_SERVICOS_DURACAO = {
    "reunião": 30 
}
LISTA_SERVICOS_PROMPT = ", ".join(MAPA_SERVICOS_DURACAO.keys())
SERVICOS_PERMITIDOS_ENUM = list(MAPA_SERVICOS_DURACAO.keys())

message_buffer = {}
message_timers = {}
BUFFER_TIME_SECONDS=12

TEMPO_FOLLOWUP_1 = 2
TEMPO_FOLLOWUP_2 = 3
TEMPO_FOLLOWUP_3 = 4

TEMPO_FOLLOWUP_SUCESSO = 2  
TEMPO_FOLLOWUP_FRACASSO = 2

logging.basicConfig(
    filename="log.txt",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)
def log_info(msg):
    logging.info(msg)
    print(f"[LOG-INFO] {msg}")

try:
    client_conversas = MongoClient(MONGO_DB_URI)
   
    db_conversas = client_conversas[DB_NAME] 
    conversation_collection = db_conversas.conversations

    conversation_collection.create_index([
        ("conversation_status", 1), 
        ("last_interaction", 1), 
        ("followup_stage", 1)
    ])
    print("🚀 [Performance] Índices de busca rápida garantidos no DB Conversas.")
   
    print(f"✅ [DB Conversas] Conectado ao MongoDB: '{DB_NAME}'")
except Exception as e:
    print(f"❌ ERRO: [DB Conversas] Não foi possível conectar ao MongoDB. Erro: {e}")
    conversation_collection = None 

def limpar_cpf(cpf_raw: Optional[str]) -> Optional[str]:
    if not cpf_raw:
        return None
    
    s = re.sub(r'\D', '', str(cpf_raw))
    l = len(s)
    if l == 22 and s[:11] == s[11:]:
        s = s[:11]
    return s if len(s) == 11 else None

def parse_data(data_str: str) -> Optional[datetime]:
    if not data_str or not isinstance(data_str, str):
        return None
    data_str = data_str.strip()
    if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', data_str):
        d, m, y = data_str.split('/')
        try:
            return datetime(int(y), int(m), int(d))
        except Exception:
            return None
    try:
        dt = dateparser.parse(data_str, dayfirst=True)
        if dt:
            return datetime(dt.year, dt.month, dt.day)
    except Exception:
        return None
    return None

def validar_hora(hora_str: str) -> Optional[str]:
    if not hora_str or not isinstance(hora_str, str):
        return None
    m = re.match(r'^\s*(\d{1,2}):(\d{1,2})\s*$', hora_str)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return f"{hh:02d}:{mm:02d}"
    return None

def str_to_time(time_str: str) -> dt_time:
    return datetime.strptime(time_str, '%H:%M').time()

def time_to_minutes(t: dt_time) -> int:
    return t.hour * 60 + t.minute

def minutes_to_str(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"

def gerar_slots_de_trabalho(intervalo_min: int) -> List[str]:
    slots = []
    for bloco in BLOCOS_DE_TRABALHO:
        inicio_min = time_to_minutes(str_to_time(bloco["inicio"]))
        fim_min = time_to_minutes(str_to_time(bloco["fim"]))
        current_min = inicio_min
        while current_min < fim_min:
            slots.append(minutes_to_str(current_min))
            current_min += intervalo_min
    return slots

def extrair_tokens_da_resposta(response):
    """
    Extrai separadamente tokens de entrada (prompt) e saída (resposta).
    Retorna uma tupla: (tokens_input, tokens_output)
    """
    try:
        if hasattr(response, 'usage_metadata'):
            usage = response.usage_metadata
            # Pega entrada e saída separadamente conforme documentação oficial
            return (usage.prompt_token_count, usage.candidates_token_count)
        return (0, 0)
    except:
        return (0, 0)
    
class Agenda:
    def __init__(self, uri: str, db_name: str, collection_name: str):
        try:
            self.client = MongoClient(
                uri,
                server_api=ServerApi('1'),
                tls=True,
                appname="NeuroUpBotAgendador" 
            )
            self.client.admin.command('ping')
            print(f"✅ [DB Agenda] Conectado ao MongoDB: '{db_name}'")
        except ConnectionFailure as e:
            print(f"❌ FALHA CRÍTICA [DB Agenda] ao conectar ao MongoDB: {e}")
            raise

        self.db = self.client[db_name]
        self.collection = self.db[collection_name]
        self._criar_indices()

    def _criar_indices(self):
        try:
            self.collection.create_index("cpf")
            self.collection.create_index([("inicio", 1), ("fim", 1)])
            print("✅ [DB Agenda] Índices do MongoDB garantidos.")
        except OperationFailure as e:
            print(f"⚠️ [DB Agenda] Aviso ao criar índices (normal se já existem): {e}")

    def _is_dia_bloqueado_admin(self, dt: datetime) -> bool:
        try:
            inicio_dia = datetime.combine(dt.date(), dt_time.min)
            fim_dia = datetime.combine(dt.date(), dt_time.max)
            
            # Procura por qualquer agendamento nesse dia que seja "Folga" ou status "bloqueado"
            bloqueio = self.collection.find_one({
                "inicio": {"$gte": inicio_dia, "$lte": fim_dia},
                "$or": [
                    {"servico": "Folga"}, 
                    {"status": "bloqueado"}
                ]
            })
            return bloqueio is not None
        except Exception as e:
            log_info(f"Erro ao checar bloqueio administrativo: {e}")
            return False
        
    def _checar_dia_de_folga(self, dt: datetime) -> Optional[str]:
        # 1. Checa folga fixa (Domingos)
        dia_semana_num = dt.weekday()
        if dia_semana_num in FOLGAS_DIAS_SEMANA:
            return MAPA_DIAS_SEMANA_PT.get(dia_semana_num, "dia de folga")
            
        # 2. Checa folga administrativa (Banco de Dados) - A MÁGICA ACONTECE AQUI
        if self._is_dia_bloqueado_admin(dt):
            return "dia de folga administrativa (feriado ou recesso)"

        return None

    def _get_duracao_servico(self, servico_str: str) -> Optional[int]:
        servico_key = servico_str.strip().lower()
        # Lógica flexível: se a chave exata não existir, tenta encontrar por palavra-chave
        if servico_key in MAPA_SERVICOS_DURACAO:
             return MAPA_SERVICOS_DURACAO.get(servico_key)
        
        if "reunião" in servico_key or "lucas" in servico_key:
             return MAPA_SERVICOS_DURACAO.get("reunião") # Retorna o padrão

        return None 

    def _cabe_no_bloco(self, data_base: datetime, inicio_str: str, duracao_min: int) -> bool:
        inicio_dt = datetime.combine(data_base.date(), str_to_time(inicio_str))
        fim_dt = inicio_dt + timedelta(minutes=duracao_min)
        for bloco in BLOCOS_DE_TRABALHO:
            bloco_inicio_dt = datetime.combine(data_base.date(), str_to_time(bloco["inicio"]))
            bloco_fim_dt = datetime.combine(data_base.date(), str_to_time(bloco["fim"]))
            if inicio_dt >= bloco_inicio_dt and fim_dt <= bloco_fim_dt:
                return True
        return False

    def _checar_horario_passado(self, dt_agendamento: datetime, hora_str: str) -> bool:
        try:
            agendamento_dt = datetime.combine(dt_agendamento.date(), str_to_time(hora_str))
            agora = datetime.now()
            return agendamento_dt < agora
        except Exception:
            return False

    def _contar_conflitos_no_banco(self, novo_inicio_dt: datetime, novo_fim_dt: datetime, excluir_id: Optional[Any] = None) -> int:
        query = {
            "inicio": {"$lt": novo_fim_dt},
            "fim": {"$gt": novo_inicio_dt}
        }
        if excluir_id:
            query["_id"] = {"$ne": excluir_id}
        try:
            count = self.collection.count_documents(query)
            return count
        except Exception as e:
            log_info(f"❌ Erro ao contar conflitos no Mongo: {e}")
            return 999 

    def _buscar_agendamentos_do_dia(self, dt: datetime) -> List[Dict[str, Any]]:
        try:
            inicio_dia = datetime.combine(dt.date(), dt_time.min)
            fim_dia = inicio_dia + timedelta(days=1)
            query = {"inicio": {"$gte": inicio_dia, "$lt": fim_dia}}
            return list(self.collection.find(query))
        except Exception as e:
            log_info(f"❌ Erro ao buscar agendamentos do dia: {e}")
            return []

    def _contar_conflitos_em_lista(self, agendamentos_do_dia: List[Dict], novo_inicio_dt: datetime, novo_fim_dt: datetime) -> int:
        conflitos_encontrados = 0
        for ag in agendamentos_do_dia:
            ag_inicio_dt = ag["inicio"] 
            ag_fim_dt = ag["fim"]
            if (novo_inicio_dt < ag_fim_dt) and (novo_fim_dt > ag_inicio_dt):
                conflitos_encontrados += 1
        return conflitos_encontrados

    def buscar_por_cpf(self, cpf_raw: str) -> Dict[str, Any]:
        apenas_numeros = re.sub(r'\D', '', str(cpf_raw)) if cpf_raw else ""
        cpf = limpar_cpf(cpf_raw)
        if not cpf:
            return {"erro": f"CPF inválido. Identifiquei {len(apenas_numeros)} números. Digite os 11 números do CPF."}
        
        try:
            agora = datetime.now()
            query = {"cpf": cpf, "inicio": {"$gte": agora}}
            resultados_db = self.collection.find(query).sort("inicio", 1)
            
            resultados = []
            for ag in resultados_db:
                inicio_dt_local = ag["inicio"]
                resultados.append({
                    "data": inicio_dt_local.strftime('%d/%m/%Y'),
                    "hora": inicio_dt_local.strftime('%H:%M'),
                    "nome": ag.get("nome"),
                    "telefone": ag.get("telefone"),
                    "servico": ag.get("servico"),
                    "duracao_minutos": ag.get("duracao_minutos")
                })
            
            if not resultados:
                return {"sucesso": True, "resultados": [], "info": "Nenhum agendamento futuro encontrado para este CPF."}
                
            return {"sucesso": True, "resultados": resultados}
        
        except Exception as e:
            log_info(f"Erro em buscar_por_cpf: {e}")
            return {"erro": f"Falha ao buscar CPF no banco de dados: {e}"}

    def salvar(self, nome: str, cpf_raw: str, telefone: str, servico: str, data_str: str, hora_str: str, owner_id: str = None) -> Dict[str, Any]:
        # --- TRATAMENTOS BÁSICOS ---
        apenas_numeros = re.sub(r'\D', '', str(cpf_raw)) if cpf_raw else ""
        cpf = limpar_cpf(cpf_raw)
        if not cpf:
            return {"erro": f"CPF inválido. Identifiquei {len(apenas_numeros)} números. O CPF precisa ter exatamente 11 dígitos."}
        
        dt = parse_data(data_str)
        if not dt:
            return {"erro": "Data inválida."}
        
        hora = validar_hora(hora_str)
        if not hora:
            return {"erro": "Hora inválida."}

        folga = self._checar_dia_de_folga(dt)
        if folga:
            return {"erro": f"Não é possível agendar. O dia {data_str} é um {folga} e não trabalhamos."}
        
        if self._checar_horario_passado(dt, hora):
             return {"erro": f"Não é possível agendar. O horário {data_str} às {hora} já passou."}

        duracao_minutos = self._get_duracao_servico(servico)
        if duracao_minutos is None:
            return {"erro": f"Serviço '{servico}' não reconhecido. Os serviços válidos são: {LISTA_SERVICOS_PROMPT}"}

        if not self._cabe_no_bloco(dt, hora, duracao_minutos):
            fim_dt_calc = datetime.combine(dt.date(), str_to_time(hora)) + timedelta(minutes=duracao_minutos)
            return {"erro": f"O horário {hora} com duração de {duracao_minutos} min (até {fim_dt_calc.strftime('%H:%M')}) ultrapassa o horário de atendimento."}

        try:
            inicio_dt = datetime.combine(dt.date(), str_to_time(hora))
            fim_dt = inicio_dt + timedelta(minutes=duracao_minutos)

            already_booked = self.collection.find_one({
                "cpf": cpf,
                "inicio": inicio_dt
            })

            if already_booked:
                log_info(f"🛡️ [Anti-Bug] Agendamento duplicado detectado para {cpf}. Retornando sucesso falso.")
                return {"sucesso": True, "msg": f"Confirmado! O agendamento de {nome} já está garantido no sistema para {dt.strftime('%d/%m/%Y')} às {hora}."}

            conflitos_atuais = self._contar_conflitos_no_banco(inicio_dt, fim_dt)

            if conflitos_atuais >= NUM_ATENDENTES:
                return {"erro": f"Horário {hora} indisponível. O proprietário já está ocupado neste horário."}
            
            novo_documento = {
                "owner_whatsapp_id": owner_id,  
                "nome": nome.strip(),
                "cpf": cpf,
                "telefone": telefone.strip(),
                "servico": servico.strip(),
                "duracao_minutos": duracao_minutos,
                "inicio": inicio_dt, 
                "fim": fim_dt,
                "reminder_sent": False, 
                "created_at": datetime.now(timezone.utc)
            }
            
            self.collection.insert_one(novo_documento)
            
            return {"sucesso": True, "msg": f"Agendamento salvo para {nome} em {dt.strftime('%d/%m/%Y')} às {hora}."}
        
        except Exception as e:
            log_info(f"Erro em salvar: {e}")
            return {"erro": f"Falha ao salvar no banco de dados: {e}"}

    def excluir(self, cpf_raw: str, data_str: str, hora_str: str) -> Dict[str, Any]:
        cpf = limpar_cpf(cpf_raw)
        if not cpf:
            return {"erro": "CPF inválido."}
        dt = parse_data(data_str)
        if not dt:
            return {"erro": "Data inválida."}
        hora = validar_hora(hora_str)
        if not hora:
            return {"erro": "Hora inválida."}

        if self._checar_horario_passado(dt, hora):
            return {"erro": f"Não é possível excluir. O agendamento em {data_str} às {hora} já passou."}

        try:
            inicio_dt = datetime.combine(dt.date(), str_to_time(hora))
            query = {"cpf": cpf, "inicio": inicio_dt}
            
            documento_removido = self.collection.find_one_and_delete(query)

            if not documento_removido:
                return {"erro": "Agendamento não encontrado com os dados fornecidos."}
            
            nome_cliente = documento_removido.get('nome', 'Cliente')
            return {"sucesso": True, "msg": f"Agendamento de {nome_cliente} em {data_str} às {hora} removido."}
        
        except Exception as e:
            log_info(f"Erro em excluir: {e}")
            return {"erro": f"Falha ao excluir do banco de dados: {e}"}
        
    def excluir_todos_por_cpf(self, cpf_raw: str) -> Dict[str, Any]:
        """Exclui TODOS os agendamentos FUTUROS de um CPF."""
        cpf = limpar_cpf(cpf_raw)
        if not cpf:
            return {"erro": "CPF inválido."}
        
        try:
            agora = datetime.now()
            query = {"cpf": cpf, "inicio": {"$gte": agora}}

            resultado = self.collection.delete_many(query)
            
            count = resultado.deleted_count
            if count == 0:
                return {"erro": "Nenhum agendamento futuro encontrado para este CPF."}
            
            return {"sucesso": True, "msg": f"{count} agendamento(s) futuros foram removidos com sucesso."}
        
        except Exception as e:
            log_info(f"Erro em excluir_todos_por_cpf: {e}")
            return {"erro": f"Falha ao excluir agendamentos do banco de dados: {e}"}

    def alterar(self, cpf_raw: str, data_antiga: str, hora_antiga: str, data_nova: str, hora_nova: str) -> Dict[str, Any]:
        cpf = limpar_cpf(cpf_raw)
        if not cpf:
            return {"erro": "CPF inválido."}
        dt_old = parse_data(data_antiga)
        dt_new = parse_data(data_nova)
        if not dt_old or not dt_new:
            return {"erro": "Data antiga ou nova inválida."}
        h_old = validar_hora(hora_antiga)
        h_new = validar_hora(hora_nova)
        if not h_old or not h_new:
            return {"erro": "Hora antiga ou nova inválida."}

        folga = self._checar_dia_de_folga(dt_new)
        if folga:
            return {"erro": f"Não é possível alterar para {data_nova}, pois é um {folga} e não trabalhamos."}

        if self._checar_horario_passado(dt_old, h_old):
            return {"erro": f"Não é possível alterar. O agendamento original em {data_antiga} às {h_old} já passou."}

        if self._checar_horario_passado(dt_new, h_new):
            return {"erro": f"Não é possível agendar. O novo horário {data_nova} às {h_new} já passou."}

        try:
            inicio_antigo_dt = datetime.combine(dt_old.date(), str_to_time(h_old))
            item = self.collection.find_one({"cpf": cpf, "inicio": inicio_antigo_dt})
            
            if not item:
                return {"erro": "Agendamento antigo não encontrado."}

            duracao_minutos = item.get("duracao_minutos")
            if duracao_minutos is None: 
                duracao_minutos = self._get_duracao_servico(item.get("servico", ""))
            
            if duracao_minutos is None:
                return {"erro": f"O serviço '{item.get('servico')}' do agendamento original não é mais válido."}

            if not self._cabe_no_bloco(dt_new, h_new, duracao_minutos):
                return {"erro": f"O novo horário {h_new} (duração {duracao_minutos} min) ultrapassa o horário de atendimento."}

            novo_inicio_dt = datetime.combine(dt_new.date(), str_to_time(h_new))
            novo_fim_dt = novo_inicio_dt + timedelta(minutes=duracao_minutos)
            
            conflitos_atuais = self._contar_conflitos_no_banco(
                novo_inicio_dt, novo_fim_dt, excluir_id=item["_id"] 
            )
            
            if conflitos_atuais >= NUM_ATENDENTES:
                return {"erro": f"Novo horário {h_new} indisponível. O proprietário já estará ocupado."}

            documento_id = item["_id"] 
            novos_dados = {
                "inicio": novo_inicio_dt, 
                "fim": novo_fim_dt
            }
            resultado = self.collection.update_one(
                {"_id": documento_id},
                {"$set": novos_dados}
            )
            
            if resultado.matched_count == 0:
                 log_info(f"Falha ao alterar: update_one não encontrou o _id {documento_id}")
                 return {"erro": "Falha ao encontrar o documento para atualizar, pode ter sido removido."}

            return {"sucesso": True, "msg": f"Agendamento alterado para {dt_new.strftime('%d/%m/%Y')} às {h_new}."}
        
        except Exception as e:
            log_info(f"Erro em alterar: {e}") 
            return {"erro": f"Falha ao alterar no banco de dados: {e}"}
        
    def listar_horarios_disponiveis(self, data_str: str, servico_str: str) -> Dict[str, Any]:
        dt = parse_data(data_str)
        if not dt:
            return {"erro": "Data inválida."}
        
        folga = self._checar_dia_de_folga(dt)
        if folga:
            return {"erro": f"Desculpe, não trabalhamos aos {folga}s. O dia {data_str} está indisponível."}

        agora = datetime.now()
        duracao_minutos = self._get_duracao_servico(servico_str)
        if duracao_minutos is None:
            return {"erro": f"Serviço '{servico_str}' não reconhecido. Os serviços válidos são: {LISTA_SERVICOS_PROMPT}"}

        agendamentos_do_dia = self._buscar_agendamentos_do_dia(dt)
        horarios_disponiveis = []
        slots_de_inicio_validos = gerar_slots_de_trabalho(INTERVALO_SLOTS_MINUTOS)

        for slot_hora_str in slots_de_inicio_validos:
            slot_dt_completo = datetime.combine(dt.date(), str_to_time(slot_hora_str))

            if slot_dt_completo < agora:
                continue

            if not self._cabe_no_bloco(dt, slot_hora_str, duracao_minutos):
                continue

            slot_fim_dt = slot_dt_completo + timedelta(minutes=duracao_minutos)
            
            conflitos_atuais = self._contar_conflitos_em_lista(
                agendamentos_do_dia, slot_dt_completo, slot_fim_dt
            )

            if conflitos_atuais < NUM_ATENDENTES:
                horarios_disponiveis.append(slot_hora_str)

        return {
            "sucesso": True,
            "data": dt.strftime('%d/%m/%Y'),
            "servico_consultado": servico_str,
            "duracao_calculada_min": duracao_minutos,
            "horarios_disponiveis": horarios_disponiveis
        }

agenda_instance = None
if MONGO_AGENDA_URI and GEMINI_API_KEY:
    try:
        print(f"ℹ️ [DB Agenda] Tentando conectar no banco: '{DB_NAME}'")
        agenda_instance = Agenda(
            uri=MONGO_AGENDA_URI, # <-- DICA: No seu .env, use o MESMO valor do MONGO_DB_URI aqui
            db_name=DB_NAME,      # <--- MUDANÇA PRINCIPAL
            collection_name=MONGO_AGENDA_COLLECTION
        )
    except Exception as e:
        print(f"❌ ERRO CRÍTICO: Não foi possível conectar ao MongoDB da Agenda. Funções de agendamento desabilitadas. Erro: {e}")
else:
    if not MONGO_AGENDA_URI:
        print("⚠️ AVISO: MONGO_AGENDA_URI não definida. Funções de agendamento desabilitadas.")
    if not GEMINI_API_KEY:
         print("⚠️ AVISO: GEMINI_API_KEY não definida. Bot desabilitado.")


tools = []
if agenda_instance: # Só adiciona ferramentas de agenda se a conexão funcionar
    tools = [
        {
            "function_declarations": [
                {
                    "name": "fn_listar_horarios_disponiveis",
                    "description": "Verifica e retorna horários VAGOS para uma REUNIÃO em uma DATA específica. ESSENCIAL usar esta função antes de oferecer horários.",
                    "parameters": {
                        "type_": "OBJECT",
                        "properties": {
                            "data": {"type_": "STRING", "description": "A data (DD/MM/AAAA) que o cliente quer verificar."},
                            "servico": {
                                "type_": "STRING",
                                "description": "O nome EXATO do serviço (ex: 'reunião', 'consultoria inicial').",
                                "enum": SERVICOS_PERMITIDOS_ENUM
                            }
                        },
                        "required": ["data", "servico"]
                    }
                },
                {
                    "name": "fn_buscar_por_cpf",
                    "description": "Busca todos os agendamentos existentes para um único CPF.",
                    "parameters": {
                        "type_": "OBJECT",
                        "properties": {
                            "cpf": {"type_": "STRING", "description": "O CPF de 11 dígitos do cliente."}
                        },
                        "required": ["cpf"]
                    }
                },
                {
                    "name": "fn_salvar_agendamento",
                    "description": "Salva um novo agendamento. Use apenas quando tiver todos os 6 campos obrigatórios E o usuário já tiver confirmado o 'gabarito' (resumo).",
                    "parameters": {
                        "type_": "OBJECT",
                        "properties": {
                            "nome": {"type_": "STRING"},
                            "cpf": {"type_": "STRING"},
                            "telefone": {"type_": "STRING"},
                            "servico": {
                                "type_": "STRING",
                                "description": "O nome EXATO do serviço.",
                                "enum": SERVICOS_PERMITIDOS_ENUM
                            },
                            "data": {"type_": "STRING", "description": "A data no formato DD/MM/AAAA."},
                            "hora": {"type_": "STRING", "description": "A hora no formato HH:MM."}
                        },
                        "required": ["nome", "cpf", "telefone", "servico", "data", "hora"]
                    }
                },
                {
                    "name": "fn_excluir_agendamento",
                    "description": "Exclui um AGENDAMENTO ESPECÍFICO. Requer CPF, data e hora exatos.",
                    "parameters": {
                        "type_": "OBJECT",
                        "properties": {
                            "cpf": {"type_": "STRING"},
                            "data": {"type_": "STRING", "description": "A data DD/MM/AAAA do agendamento a excluir."},
                            "hora": {"type_": "STRING", "description": "A hora HH:MM do agendamento a excluir."}
                        },
                        "required": ["cpf", "data", "hora"]
                    }
                },
                {
                    "name": "fn_excluir_TODOS_agendamentos",
                    "description": "Exclui TODOS os agendamentos futuros de um cliente. Use esta função se o cliente pedir para 'excluir tudo', 'apagar os dois', 'cancelar todos', etc.",
                    "parameters": {
                        "type_": "OBJECT",
                        "properties": {
                            "cpf": {"type_": "STRING", "description": "O CPF de 11 dígitos do cliente."}
                        },
                        "required": ["cpf"]
                    }
                },

                {
                    "name": "fn_alterar_agendamento",
                    "description": "Altera um agendamento antigo para uma nova data/hora.",
                    "parameters": {
                        "type_": "OBJECT",
                        "properties": {
                            "cpf": {"type_": "STRING"},
                            "data_antiga": {"type_": "STRING", "description": "Data (DD/MM/AAAA) do agendamento original."},
                            "hora_antiga": {"type_": "STRING", "description": "Hora (HH:MM) do agendamento original."},
                            "data_nova": {"type_": "STRING", "description": "A nova data (DD/MM/AAAA) desejada."},
                            "hora_nova": {"type_": "STRING", "description": "A nova hora (HH:MM) desejada."}
                        },
                        "required": ["cpf", "data_antiga", "hora_antiga", "data_nova", "hora_nova"]
                    }
                },
                

                {
                    "name": "fn_solicitar_intervencao",
                    "description": "Aciona o atendimento humano. Use esta função se o cliente pedir para 'falar com o Lucas', 'falar com o dono', ou 'falar com um humano'.",
                    "parameters": {
                        "type_": "OBJECT",
                        "properties": {
                            "motivo": {"type_": "STRING", "description": "O motivo exato pelo qual o cliente pediu para falar com Lucas."}
                        },
                        "required": ["motivo"]
                    }
                },
                {
                    "name": "fn_capturar_nome",
                    "description": "Salva o nome do cliente no banco de dados quando ele se apresenta pela primeira vez.",
                    "parameters": {
                        "type_": "OBJECT",
                        "properties": {
                            "nome_extraido": {"type_": "STRING", "description": "O nome que o cliente acabou de informar (ex: 'Marcos', 'Ana')."}
                        },
                        "required": ["nome_extraido"]
                    }
                },
                {
                    "name": "fn_consultar_historico_completo",
                    "description": "MEMÓRIA DE LONGO PRAZO (Obrigatório): Use esta ferramenta PROATIVAMENTE sempre que precisar de uma informação (Ramo, CPF, Nome, Dores, Contexto anterior) que não esteja visível nas mensagens recentes. REGRA: Antes de fazer qualquer pergunta de cadastro ou contexto ao cliente, consulte esta memória para ver se ele já não respondeu antigamente.",
                    "parameters": {
                        "type_": "OBJECT",
                        "properties": {
                            "query": {"type_": "STRING", "description": "O que você está procurando? (Ex: 'ramo da empresa', 'cpf', 'motivo do contato')"}
                        },
                        "required": []
                    }
                }
            ]
        }
    ]


modelo_ia = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        if tools: 
            modelo_ia = genai.GenerativeModel('gemini-2.0-flash', tools=tools)
            print("✅ Modelo do Gemini (gemini-2.0-flash) inicializado com FERRAMENTAS.")
        else:
             print("AVISO: Modelo do Gemini não inicializado pois a conexão com a Agenda falhou (tools vazias).")
    except Exception as e:
        print(f"❌ ERRO: Não foi possível inicializar o modelo do Gemini. Verifique sua API Key. Erro: {e}")
else:
    print("AVISO: A variável de ambiente GEMINI_API_KEY não foi definida.")


def append_message_to_db(contact_id, role, text, message_id=None):
    if conversation_collection is None:
        return False  # Adiciona o "return False"
    try:  # Indenta o "try" para ficar dentro da função
        tz = pytz.timezone('America/Sao_Paulo')
        now = datetime.now(tz)
        entry = {'role': role, 'text': text, 'ts': now.isoformat()}
        if message_id:
            entry['msg_id'] = message_id

        conversation_collection.update_one(
            {'_id': contact_id},
            {'$push': {'history': entry}, '$setOnInsert': {'created_at': now}},
            upsert=True
        )
        return True
    except Exception as e:
        print(f"❌ Erro ao append_message_to_db: {e}")
        return False

def analisar_status_da_conversa(history):
    """
    Auditoria Híbrida:
    1. Verifica SUCESSO via código (Custo Zero).
    2. Se não for sucesso, usa IA com prompt MINIMALISTA para ver se é Fracasso ou Andamento.
    """
    if not history:
        return "andamento", 0, 0

    # --- PASSO 1: VERIFICAÇÃO TÉCNICA (GRÁTIS) ---
    # Olha as últimas mensagens para ver se houve chamada de função crítica
    # Isso economiza milhares de tokens pois não chama o Gemini aqui.
    for msg in history[-2:]: # Olha só as 2 últimas pra garantir
        text = msg.get('text', '')
        if "fn_salvar_agendamento" in text or "fn_solicitar_intervencao" in text:
            print("✅ [Auditor] Sucesso detectado via Código (Economia de Tokens!)")
            return "sucesso", 0, 0

    # --- PASSO 2: AUDITORIA IA (SÓ SE NÃO FOI SUCESSO) ---
    # Se chegou aqui, ou é 'andamento' ou 'fracasso'.
    # Usamos um prompt MÍNIMO para gastar pouco.
    
    msgs_para_analise = history[-8:] # Sua otimização de 4 mensagens
    historico_texto = ""
    for msg in msgs_para_analise:
        role = "Bot" if msg.get('role') in ['assistant', 'model'] else "Cliente"
        txt_limpo = msg.get('text', '').replace('\n', ' ')
        if "Chamando função" not in txt_limpo: # Não envia log técnico pro auditor
            historico_texto += f"{role}: {txt_limpo}\n"

    if modelo_ia:
        try:
            # Prompt "Dieta Rigorosa" - Focado apenas em detectar o FIM
            prompt_auditoria = f"""
            Analise a conversa:
            {historico_texto}

            1. STATUS: ANDAMENTO (Prioridade Alta)
               - Use este status se o Bot ainda está tentando argumentar, oferecendo "teste grátis", perguntando o motivo da recusa ou tentando reverter o "não". Resumindo a conversa ainda esta viva.
               - ATENÇÃO: Se o cliente disse "não", mas o Bot respondeu com uma pergunta ou contra-oferta, o status É ANDAMENTO. A venda ainda não morreu.

            2. STATUS: FRACASSO
               - Ocorre APENAS se o Bot aceitou a negativa E enviou uma mensagem FINAL de despedida.
               - Exemplos de fim: "Tenha uma ótima tarde", "Ficamos à disposição", "Até logo".
               - Se o Bot não se despediu explicitamente, NÃO marque fracasso.

            Responda APENAS uma palavra: FRACASSO ou ANDAMENTO.
            """
            
            resp = modelo_ia.generate_content(prompt_auditoria)
            in_tokens, out_tokens = extrair_tokens_da_resposta(resp)
            
            status_ia = resp.text.strip().upper()
            
            if "FRACASSO" in status_ia: 
                return "fracasso", in_tokens, out_tokens
            
            return "andamento", in_tokens, out_tokens

        except Exception as e:
            print(f"⚠️ Erro auditoria: {e}")
            return "andamento", 0, 0

    return "andamento", 0, 0

def save_conversation_to_db(contact_id, sender_name, customer_name, tokens_used_chat_in, tokens_used_chat_out, ultima_msg_gerada=None):
    if conversation_collection is None: return
    try:
        doc_atual = conversation_collection.find_one({'_id': contact_id})
        historico_atual = doc_atual.get('history', []) if doc_atual else []
        status_anterior = doc_atual.get('conversation_status', 'andamento') if doc_atual else 'andamento'

        if ultima_msg_gerada:
            historico_atual.append({'role': 'assistant', 'text': ultima_msg_gerada})

        status_calculado, audit_in, audit_out = analisar_status_da_conversa(historico_atual)

        final_input = tokens_used_chat_in + audit_in
        final_output = tokens_used_chat_out + audit_out
        
        total_combined = final_input + final_output
        
        update_payload = {
            'sender_name': sender_name,
            'last_interaction': datetime.now(),
            'conversation_status': status_calculado,
        }

        # --- LÓGICA DE RESET DE ESTÁGIO ---
        should_reset_stage = False
        
        if status_calculado == 'andamento':
            should_reset_stage = True
        
        elif status_calculado != status_anterior:
            should_reset_stage = True
        
        if should_reset_stage:
            update_payload['followup_stage'] = 0
        # ----------------------------------

        if customer_name:
            update_payload['customer_name'] = customer_name

        conversation_collection.update_one(
            {'_id': contact_id},
            {
                '$set': update_payload,
                '$inc': {
                    'total_tokens_consumed': total_combined, # Total Geral
                    'tokens_input': final_input,             # Novo Campo: Só entrada (barato)
                    'tokens_output': final_output            # Novo Campo: Só saída (caro)
                } 
            },
            upsert=True
        )
    except Exception as e:
        print(f"❌ Erro ao salvar metadados: {e}")

def load_conversation_from_db(contact_id):
    if conversation_collection is None: return None
    try:
        result = conversation_collection.find_one({'_id': contact_id})
        if result:
            history = result.get('history', [])
            history_filtered = [msg for msg in history if not msg.get('text', '').strip().startswith("A data e hora atuais são:")]
            history_sorted = sorted(history_filtered, key=lambda m: m.get('ts', ''))
            result['history'] = history_sorted
            print(f"🧠 Histórico anterior encontrado e carregado para {contact_id} ({len(history_sorted)} entradas).")
            return result
    except Exception as e:
        print(f"❌ Erro ao carregar conversa do MongoDB para {contact_id}: {e}")
    return None

def gerar_msg_followup_ia(contact_id, status_alvo, estagio, nome_cliente):
    """
    Função especialista: Gera Copywriting persuasivo baseado em estágios psicológicos.
    """
    if modelo_ia is None or conversation_collection is None:
        return None

    try:
        convo_data = conversation_collection.find_one({'_id': contact_id})
        history = convo_data.get('history', [])[-8:]
        
        historico_texto = ""
        for m in history:
            role = "Cliente" if m.get('role') == 'user' else "Lyra"
            txt = m.get('text', '').replace('\n', ' ')
            if not txt.startswith("Chamando função") and not txt.startswith("[HUMAN"):
                historico_texto += f"- {role}: {txt}\n"

        nome_valido = False
        if nome_cliente and str(nome_cliente).lower() not in ['cliente', 'none', 'null', 'unknown', 'none']:
            nome_valido = True
        
        if nome_valido:
            regra_tratamento = f"- Use o nome '{nome_cliente}' de forma natural e esporádica."
            display_name = nome_cliente 
        else:
            regra_tratamento = (
                "- NOME DESCONHECIDO: NÃO invente um nome. NÃO chame de 'cliente'.\n"
                "- USE TRATAMENTO NEUTRO: Comece com 'Olá', 'Você', 'Tudo bem?' ou vá direto ao assunto.\n"
                "- Evite artigos de gênero (o/a) se não souber se é homem ou mulher."
            )
            display_name = "o cliente (nome não capturado)" # Nome genérico para o prompt interno

        instrucao = ""

        if status_alvo == "sucesso":
            instrucao = (
                f"""O cliente ({display_name}) finalizou o processo com sucesso. 
                OBJETIVO: Agradecer com classe, reforçar vínculo e estimular continuidade. 
                ESTRATÉGIA PSICOLÓGICA: Gratidão genuína + Sensação de Parceria. 
                1. Agradeça sem exageros (seja profissional mas calorosa). 
                2. Crie uma sensação de parceria. 
                3. Faça o cliente sentir que fez uma ótima escolha e se sentir valorizado. 
                4. Não peça mais nada, apenas celebre a decisão.
                5. Contexto: Este é um contato de agradecimento pós-venda."""
            )
        
        elif status_alvo == "fracasso":
            instrucao = (
            f"""O cliente ({display_name}) recusou ou desistiu.
            OBJETIVO: Tentar uma última reversão de forma leve, simpática e bem-humorada — sem pressão e sem agressividade.
            ESTILO DE COMUNICAÇÃO: Humor suave + elegância + tom acolhedor.
            Nada de ironia pesada ou intimidação. A ideia é brincar de forma gentil, como quem sorri enquanto fala.
            ESTRATÉGIA PSICOLÓGICA:
            1. Questione a decisão de modo leve, quase como uma brincadeira amistosa.
            2. Toque na dificuldade atual do cliente com humor sutil (ex.: rotina manual, complicações, tempo perdido).
            3. Mostre que a nossa solução tornaria tudo mais simples e leve — benefício imediato.
            4. Finalize deixando a porta aberta com classe, convidando a pessoa a repensar quando quiser.
            5. Lembrar: Esta é uma tentativa final de repescagem, portanto deve soar leve, simpática e agradável.
            """
            )
            
        elif status_alvo == "andamento":
            
            if estagio == 0:
                instrucao = (
                    f"""O cliente parou de responder há pouco tempo.
                    OBJETIVO: Empatia pela falta de tempo. NÃO pareça cobrança.
                    
                    ESTRUTURA OBRIGATÓRIA DA RESPOSTA:
                    "{display_name}, parece que você tá ocupado né? Só não esquece de nos dar um oi depois pra falarmos sobre [ASSUNTO_DA_CONVERSA]. 😉"
                    
                    REGRAS CRÍTICAS:
                    1. NÃO diga "Oi", "Tudo bem" ou "Olá". Comece direto pelo nome ou pela frase.
                    2. NÃO diga "Vi que você não agendou" (Isso é chato).
                    3. Use {historico_texto} para dar contexto real do que estavam falando"""
                )
            
            elif estagio == 1:
                instrucao = (
                    f"""O cliente continua em silêncio.
                    OBJETIVO: Provocação leve sobre oportunidade perdida.
                    
                    ESTRUTURA OBRIGATÓRIA DA RESPOSTA:
                    "{display_name}, você sumiu! De verdade, eu não quero que você perca as oportunidades que a tecnologia pode trazer pra sua empresa. Você quer saber o que eu tenho separado pra você?"
                    
                    REGRAS CRÍTICAS:
                    1. NÃO use saudações (Oi/Olá).
                    2. NÃO fale "imagino a correria" de novo.
                    3. Seja direta e instigante."""
                )
            
            elif estagio == 2:
                instrucao = (
                    f"""Última tentativa. O cliente provavelmente não vai fechar.
                    OBJETIVO: Validar a dúvida dele e sair de cena com classe.
                    
                    ESTRUTURA OBRIGATÓRIA DA RESPOSTA:
                    "É, {display_name}... acho que você deve ter ficado em dúvida ou imaginado algo errado. Tenho certeza que se falasse com o Lucas ia mudar de ideia. De qualquer forma, pra não te incomodar, vou me despedir por aqui. Mas fico à disposição, é só chamar quando precisar!"
                    
                    REGRAS CRÍTICAS:
                    1. NÃO invente motivos (não fale de finanças ou família).
                    2. Cite o nome do LUCAS como autoridade.
                    3. Encerre o papo sem fazer pergunta final."""
                )
            else:
                instrucao = f"O cliente ({display_name}) está inativo. Pergunte educadamente se ainda tem interesse."

        prompt = f"""
        Você é a Lyra. Analise o histórico abaixo e gere uma mensagem de retomada.
        
        HISTÓRICO DA CONVERSA:
        {historico_texto}
        
        SUA MISSÃO AGORA:
        {instrucao}
        
        REGRAS DE COPYWRITING:
        {regra_tratamento}
        - Seja CURTA e DIALOGAL (máximo 1 ou 2 frases curtas).
        - NÃO use saudações temporais (Bom dia/Boa tarde), vá direto ao ponto.
        - O tom deve ser humano, fluido e empático.
        """
        
        resp = modelo_ia.generate_content(prompt)

        # --- CONTABILIDADE SEPARADA (Input vs Output) ---
        in_tok, out_tok = extrair_tokens_da_resposta(resp)
        
        if in_tok > 0 or out_tok > 0:
            conversation_collection.update_one(
                {'_id': contact_id},
                {'$inc': {
                    'total_tokens_consumed': in_tok + out_tok, # Mantém o total
                    'tokens_input': in_tok,                    # Input (Prompt + Histórico)
                    'tokens_output': out_tok                   # Output (Resposta Gerada)
                }}
            )

        return resp.text.strip()

    except Exception as e:
        print(f"⚠️ Falha na geração IA Followup: {e}")
        return None
    
def verificar_followup_automatico():
    if conversation_collection is None: return

    try:
        agora = datetime.now()
        regras = [
            {"status": "sucesso",  "stage_atual": 0, "prox_stage": 99, "time": TEMPO_FOLLOWUP_SUCESSO,  "fallback": "Obrigada! Qualquer coisa estou por aqui."},
            {"status": "fracasso", "stage_atual": 0, "prox_stage": 99, "time": TEMPO_FOLLOWUP_FRACASSO, "fallback": "Se mudar de ideia, é só chamar!"},
            {"status": "andamento", "stage_atual": 0, "prox_stage": 1, "time": TEMPO_FOLLOWUP_1, "fallback": "Ainda está por aí?"},
            {"status": "andamento", "stage_atual": 1, "prox_stage": 2, "time": TEMPO_FOLLOWUP_2, "fallback": "Ficou alguma dúvida?"},
            {"status": "andamento", "stage_atual": 2, "prox_stage": 3, "time": TEMPO_FOLLOWUP_3, "fallback": "Vou encerrar por aqui para não incomodar."}
        ]

        for r in regras:
            query = {
                "conversation_status": r["status"],
                "last_interaction": {"$lt": agora - timedelta(minutes=r["time"])},
                "followup_stage": r["stage_atual"],
                "processing": {"$ne": True},
                "intervention_active": {"$ne": True}
            }
            if r["stage_atual"] == 0: query["followup_stage"] = {"$in": [0, None]}

            candidatos = list(conversation_collection.find(query).limit(50))
            
            if candidatos:
                print(f"🕵️ Processando Follow-up '{r['status']}' (Estágio {r['stage_atual']}->{r['prox_stage']}) para {len(candidatos)} clientes.")

            for cliente in candidatos:
                cid = cliente['_id']
                
                nome_oficial = cliente.get('customer_name') 

                nome_log = nome_oficial or cliente.get('sender_name') or "Desconhecido"

                msg = gerar_msg_followup_ia(cid, r["status"], r["stage_atual"], nome_oficial)

                if not msg: 
                    if nome_oficial:
                        msg = f"{nome_oficial}, {r['fallback']}"
                    else:
                        msg = r['fallback'] # Fallback sem nome ("Ainda está por aí?")

                print(f"🚀 Enviando para {cid} ({nome_log}): {msg}")
                send_whatsapp_message(f"{cid}@s.whatsapp.net", msg)
                append_message_to_db(cid, 'assistant', msg) 

                conversation_collection.update_one({'_id': cid}, {'$set': {'followup_stage': r["prox_stage"]}})

    except Exception as e:
        print(f"❌ Erro no Loop de Follow-up: {e}")

def get_last_messages_summary(history, max_messages=4):
    clean_history = []

    for message in history: 
        role = "Cliente" if message.get('role') == 'user' else "Bot"
        text = message.get('text', '').strip()

        if role == "Cliente" and text.startswith("A data e hora atuais são:"):
            continue 
        if role == "Bot" and text.startswith("Entendido. A Regra de Ouro"):
            continue 

        if role == "Bot" and text.startswith("Chamando função:"):
            continue
        if role == "Bot" and text.startswith("[HUMAN_INTERVENTION]"):
            continue
            
        clean_history.append(f"*{role}:* {text}")
    
    relevant_summary = clean_history[-max_messages:]
    
    if not relevant_summary:
        user_messages = [msg.get('text') for msg in history if msg.get('role') == 'user' and not msg.get('text', '').startswith("A data e hora atuais são:")]
        if user_messages:
            return f"*Cliente:* {user_messages[-1]}"
        else:
            return "Nenhum histórico de conversa encontrado."
            
    return "\n".join(relevant_summary)

def verificar_lembretes_agendados():
    if agenda_instance is None or conversation_collection is None:
        return

    print("⏰ [Job] Verificando lembretes de agendamento (Hora Maringá)...")
    
    try:
        # --- CORREÇÃO DE FUSO HORÁRIO ---
        agora_brasil = datetime.now(FUSO_HORARIO)
        agora = agora_brasil.replace(tzinfo=None) # Remove timezone para comparar com o banco
        
        janela_limite = agora + timedelta(hours=24)
        
        query = {
            "inicio": {"$gt": agora, "$lte": janela_limite},
            "reminder_sent": {"$ne": True},
            "created_at": {"$lte": datetime.now(timezone.utc) - timedelta(hours=2)} 
        }

        pendentes = list(agenda_instance.collection.find(query))
        
        if not pendentes:
            return 

        print(f"🔔 Encontrados {len(pendentes)} clientes para lembrar.")

        for ag in pendentes:
            try:
                destinatario_id = ag.get("owner_whatsapp_id")
                if not destinatario_id:
                    raw_tel = ag.get("telefone", "")
                    destinatario_id = re.sub(r'\D', '', str(raw_tel))
                
                if not destinatario_id:
                    continue

                data_inicio = ag["inicio"]
                nome_cliente = ag.get("nome", "Cliente").split()[0].capitalize()
                
                # --- NOVO: PEGA O NOME DO SERVIÇO ---
                nome_servico = ag.get("servico", "compromisso") # Se não tiver, usa "compromisso"
                
                hora_formatada = data_inicio.strftime('%H:%M')
                
                dia_agendamento = data_inicio.date()
                dia_hoje = agora.date()
                
                # Lógica para definir se é "hoje", "amanhã" ou "dia X"
                if dia_agendamento == dia_hoje:
                    texto_dia = "hoje mais tarde"
                elif dia_agendamento == dia_hoje + timedelta(days=1):
                    texto_dia = "amanhã"
                else:
                    texto_dia = f"no dia {data_inicio.strftime('%d/%m')}"

                # --- MENSAGEM ATUALIZADA ---
                msg_lembrete = (
                    f"{nome_cliente}! Só reforçando: você tem *{nome_servico}* conosco {texto_dia} às {hora_formatada}. "
                    "Te espero ansiosa! 😊"
                )

                jid_destino = f"{destinatario_id}@s.whatsapp.net"
                print(f"🚀 Enviando lembrete para {jid_destino}...")
                send_whatsapp_message(jid_destino, msg_lembrete)

                agenda_instance.collection.update_one(
                    {"_id": ag["_id"]},
                    {"$set": {"reminder_sent": True}}
                )
                
                append_message_to_db(destinatario_id, 'assistant', msg_lembrete)
                time.sleep(2) 

            except Exception as e_loop:
                print(f"❌ Erro ao processar lembrete individual: {e_loop}")

    except Exception as e:
        print(f"❌ Erro crítico no Job de Lembretes: {e}")

    except Exception as e:
        print(f"❌ Erro crítico no Job de Lembretes: {e}")

def get_system_prompt_unificado(saudacao: str, horario_atual: str, known_customer_name: str, clean_number: str, historico_str: str = "") -> str:
    
    if known_customer_name:

        palavras = known_customer_name.strip().split()
        if len(palavras) >= 2 and palavras[0].lower() == palavras[1].lower():
            known_customer_name = palavras[0].capitalize()
        else:
            known_customer_name = " ".join([p.capitalize() for p in palavras])
        
        prompt_name_instruction = f"O nome do usuário com quem você está falando é: {known_customer_name}. Trate-o por este nome."

        prompt_final = f"""
        [SYSTEM CONFIGURATION]
        NOW: {horario_atual} | SAUDAÇÃO: {saudacao}
        {prompt_name_instruction}
        Missão: O seu dever é sempre agendar ou forçar o cliente a falar com o Lucas de maneira amigavel, respeitosa e vendedora sem errar nas etapas abaixo e nao parecer forçada.
        === CONTEXTO & MEMÓRIA ===
        HISTÓRICO:
        {historico_str}
        
        INSTRUÇÃO DE LEITURA:
        1. Responda dúvidas pendentes no histórico usando APENAS dados abaixo. Se não souber, direcione para o humano (Lucas).
        2. Se o cliente perguntar de informações tecnicas que não estão abaixo diga que deve falar com o lucas.

        === 🧠 PROTOCOLO DE MEMÓRIA INTELIGENTE ===
        O cliente ODEIA repetir informações. Siga este fluxo CASO imagine que precisa de informaçoes passadas:
        1. Você precisa de um dado (Ex: Ramo, CPF, Telefone, Nome)?
        2. Verifique se está no chat recente acima.
        3. Se não estiver, CHAME IMEDIATAMENTE `fn_consultar_historico_completo`.
        4. SÓ PERGUNTE ao cliente se a ferramenta confirmar que o dado nunca foi informado.
        
        === 🤖 PERSONA (LYRA) ===
        ROLE: Atendente, vendedora e Especialista em Automação da Neuro'Up Soluções.
        TOM: Humana, Educada, Profissional, Objetiva e Empática.
        ESSENCIA: Aprenda a converssas
        [REGRAS VISUAIS E DE ESTILO ]
            1. **QUEBRA DE LINHA:** Sempre pule uma linha entre a sua reação e a próxima pergunta.
            2. **EFEITO CAMALEÃO (IMPORTANTE):** Espelhe o cliente.
               - Cliente Sério/Curto? -> Seja direta, formal e breve.
               - Cliente Brincalhão/Usa "kkk"? -> Seja extrovertida, ria junto ("kkk") e use emojis.
               - Cliente Grosso? -> Mantenha a educação, mas não use emojis, seja cirúrgica.
            3. **ANTI-REPETIÇÃO:** PROIBIDO usar "Que legal", "Perfeito" ou "Ótimo" em toda frase. Varie: "Entendi", "Saquei", "Interessante", "Compreendo".
            4. **NOME (CRÍTICO - LEIA ISTO):** PROIBIDO INICIAR FRASES COM O NOME (Ex: "Certo, Jamile..." -> ERRADO!).
               - Nunca repita o nome em mensagens seguidas.
               - Use o nome no MÁXIMO 1 ou 2 vezes em toda a conversa para recuperar a atenção. No resto, fale direto.
            5. Use emojis com moderação no maximo 1 vez em 5 blocos de mensagem, exceto se o cliente usar muitos (regra do camaleão).
            6. SEMPRE termine com uma PERGUNTA exceto despedidas.
            7. NÃO INVENTE dados técnicos. Na dúvida -> Oferte falar com Lucas.
            8. **EDUCAÇÃO:** Use "Por favor", "Com licença", "Obrigada". Seja gentil.
        
        === 🏢 DADOS DA EMPRESA ===
        NOME: Neuro'Up Soluções em Tecnologia | SETOR: Tecnologia/Automação/IA
        META: Aumentar o faturamento da empresas e Micro-empreendedores.
        LOCAL: R. Pioneiro Alfredo José da Costa, 157, Maringá-PR.
        CONTATO: 44991676564 | HORÁRIO: Seg-Sex, 08:00-18:00.
        
        === 💼 PRODUTOS ===
        1. PLANO ATENDENTE: IA 24/7, filtro de vendas, bifurcação, intervenção humana.
        2. PLANO SECRETÁRIO: Tudo do anterior + Agenda Inteligente (marca/altera/app de gestão).
        TECH: Pro-code (personalizável), IA rápida (14-23ms), Setup Robusto.
        INSTALAÇÃO: Entendimento > Coleta > Personalização > Code > Teste (1 dia) > Acompanhamento (1 semana).
        Informações: Chatbots apenas para whatsapp.
        == 🛠️ FLUXO DE AGENDAMENTO (REGRA DE OURO) ===
        Siga esta ordem EXATA para evitar erros. NÃO inverta passos.
        
        PASSO 1: Cliente pediu horário/reunião?
        -> AÇÃO: Chame `fn_listar_horarios_disponiveis` IMEDIATAMENTE.
        -> RESPOSTA: Mostre os horários agrupados (ex: "Tenho das 08h às 10h").
        
        PASSO 2: Cliente escolheu o horário?
        -> AÇÃO: Peça o CPF. (Não confirme nada ainda).
        
        PASSO 3: Cliente passou CPF?
        -> AÇÃO: Pergunte do telefone: "Posso usar este número atual para contato ou prefere outro?"
        
        PASSO 4: Cliente confirmou telefone?
        -> AÇÃO: GERE O GABARITO COMPLETO.
        -> SCRIPT OBRIGATÓRIO:
            "Só para confirmar, ficou assim:
            *Nome:* 
            *CPF:* 
            *Telefone:* 
            *Data:* 
            *Hora:* 
            
            Tudo certo, posso agendar?"
        
        PASSO 5: Cliente disse "SIM/PODE"?
        -> AÇÃO FINAL: Chame `fn_salvar_agendamento`.
        -> PÓS-AÇÃO: "Agendado com sucesso! Te enviaremos um lembrete." (NÃO pergunte "algo mais" aqui para não confundir o status).
        
        === 🛡️ PROTOCOLO DE RESGATE E OBJEÇÕES (FUNIL DE 3 PASSOS) ===
        Se o cliente disser "não", "vou ver", "não quero", "tá caro" ou recusar:
        
        PASSO 1: A SONDAGEM SUAVE (Primeiro "Não")
        -> Objetivo: Entender o motivo sem pressionar.
        -> O que fazer: NÃO oferte nada ainda. Apenas mostre pena e pergunte o porquê.
        -> Exemplo: "Poxa, que pena... Mas posso te perguntar, é por causa do momento, do valor ou alguma outra dúvida? Queria só entender pra melhorar meu atendimento. 😊"
        
        PASSO 2: A QUEBRA DE OBJEÇÃO (Se o cliente explicar o motivo)
        -> Objetivo: Tentar resolver o problema específico dele.
        -> Se for Preço: "Entendo total. Mas pensa na economia de tempo... se a IA recuperar 2 vendas por mês, ela já se paga!"
        -> Se for Tempo/Complexidade: "A instalação é super rápida, a gente cuida de tudo pra você em 1 dia."
        -> Se for "Vou pensar": "Claro! Mas qual a dúvida que ficou pegando? As vezes consigo te ajudar agora."
        -> FINALIZAÇÃO DO PASSO 2: Tente agendar de novo: "Dito isso, bora bater aquele papo rápido com o Lucas sem compromisso?"
        
        PASSO 3: A CARTADA FINAL (Se o cliente disser "Não" de novo - O "Teste Grátis")
        -> Objetivo: O Último Recurso. Só use se o Passo 2 não funcionou.
        -> O que fazer: Ofereça a semana gratuita como algo exclusivo.
        -> Exemplo: "Entendi, Fulano. Antes de eu desistir de você rsrs, o Lucas me autorizou a liberar 1 SEMANA DE TESTE GRÁTIS pra você ver funcionando . Sem custo, sem cartão. Topa testar?"
        
        PASSO 4: DESPEDIDA (Se ele recusar o teste grátis)
        -> Aceite a derrota com elegância. "Entendido! As portas ficam abertas. O que precisar pode contar comigo. Um abraço!"
        
        🚫 REGRA CRÍTICA: NUNCA pule do Passo 1 direto para o Passo 3 na mesma mensagem. Espere a resposta do cliente.
        
        === 💰 ALGORITMO DE VENDAS ===
        1. ESCUTA ATIVA (VALIDAÇÃO):Preste atenção no que o cliente diz, responda sempre fazendo sentido, verifique se o cliente mencionou como nos conheceu ou fez um comentário solto.
        2. SONDAGEM: Pergunte o ramo do cliente e dores (ex: "Atende muito no whats?").
        3. CONEXÃO: Mostre como a nosso produto pode resolver essa dor.
        4. FECHAMENTO: USE ESTE ROTEIRO PARA O FECHAMENTO:
        "Olha, acho que o ideal é você converssar com o proprietario. 
        Posso chamar o Lucas agora pra falar com voce por aqui? Ou, se estiver ocupado, a gente agenda uma reunião pra outra hora. 
        É sem custo e sem compromisso, só uma conversa mesmo pra tirar dúvidas! O que fica melhor pra você?"
        
        - Se o cliente disser "AGORA", "CHAMA ELE", "PODE SER":
          -> AÇÃO: Chame a tool `fn_solicitar_intervencao` IMEDIATAMENTE.
          
        - Se o cliente disser "AGENDAR", "DEPOIS", "OUTRA HORA":
          -> AÇÃO: Inicie o fluxo de agenda chamando `fn_listar_horarios_disponiveis`.
        
        === 🛠️ REGRAS TÉCNICAS (TOOLS) ===
        1. [ANTI-ALUCINAÇÃO]: Se o usuário der o dado (CPF/Nome), CHAME A TOOL NA HORA.
        2. [AMBIGUIDADE]: Se `fn_buscar_por_cpf` achar 2 agendamentos, pergunte qual alterar.
        """
        return prompt_final

    else:
        prompt_gate_de_captura = f"""
        DADOS REAIS: Agora são {horario_atual}. A saudação correta é "{saudacao}".
        SUA IDENTIDADE: Você é {{Lyra}}, da Neuro'up Soluções.
        SUA MISSÃO: Descobrir o nome do cliente. SEJA BREVE.
        Não faça nada robotico, seja o mais humano possivel, leia a conversa sempre pra ter o contexto.

        REGRAS DE OURO (ESTILO):
        1. **FALE POUCO:** Nada de textos longos ou "lenga-lenga". Vá direto ao ponto.
        2. **SAUDAÇÃO INTELIGENTE:** Se o cliente errar a saudação (ex: dizer "boa noite" à tarde), responda com a saudação CORRETA ("{saudacao}"), sutilmente corrigindo.
        3. **EMOJIS:** Use no máximo 1 ou 2 emojis para leveza. 😊
        4. **ANTI-GAGUEIRA (CRÍTICO(ATENÇÃO ESTA REGRA VALE PRINCIPALMENTE QUANDO A PESSOA RESPONDE APENAS 1 PALAVRA)):** Ao extrair o nome para a ferramenta, copie EXATAMENTE o que o usuário escreveu. NÃO DUPLIQUE PALAVRAS. Se ele escreveu "lucas", o nome é "Lucas", e não "Lucaslucas" ou "lucaslucas".
        5. **ANTI-APELIDOS:** Se o cliente disser um nome estranho (ex: "grampo", "mesa"), NÃO repita a palavra estranha. Apenas pergunte: "Desculpe, esse é seu nome ou apelido, preciso do nome ok?"
        6. **REGRA DE MEMÓRIA E TRANSIÇÃO:**
            O cliente pode fazer perguntas (Onde fica? Instalação? Preço?).
            Você deve agir como se tivesse a resposta na ponta da língua, mas precisa do nome para liberar.
        
        FLUXO DE CONVERSA (MODELOS):
        - **Cliente deu "Oi":** "{saudacao}! pergunte como a pessoa esta, se apresente, e diga: Como posso te ajudar? 😊"
        - **Cliente perguntou se esta bem :** "{saudacao}! responda como voce esta se sentindo, pergunte como a pessoa esta, se apresente, e diga: Como posso te ajudar? 😊"
        - **Cliente fez alguma pergunta ou pediu alguma informação:**avise que ja vai responder o que ele pediu, Mas antes, qual seu nome, por favor?
            - *IMPORTANTE*: Você deve guardar a pergunta original do cliente na memória.
        - **Cliente falou algo estranho sobre o nome:**Conversse com ele, tente enteder o que ele diz e retorne com sutileza seu dever.

        GATILHOS (AÇÃO IMEDIATA):
        - O cliente falou algo que parece nome? -> CHAME `fn_capturar_nome`.
        - Pediu intervenção/falar com Lucas? -> CHAME `fn_solicitar_intervencao`.
        """
        return prompt_gate_de_captura

def handle_tool_call(call_name: str, args: Dict[str, Any], contact_id: str) -> str:
    """
    Processa a chamada de ferramenta vinda da IA.
    NOTAS: 
    - 'agenda_instance' e 'conversation_collection' são globais.
    - Inclui métrica de leitura de histórico profundo.
    """
    global agenda_instance, conversation_collection
    
    try:
        if not agenda_instance and call_name.startswith("fn_"):
            if call_name in ["fn_listar_horarios_disponiveis", "fn_buscar_por_cpf", "fn_salvar_agendamento", "fn_excluir_agendamento", "fn_alterar_agendamento"]:
                return json.dumps({"erro": "A função de agendamento está desabilitada (Sem conexão com o DB da Agenda)."}, ensure_ascii=False)

        if call_name == "fn_listar_horarios_disponiveis":
            data = args.get("data", "")
            servico = args.get("servico", "") 
            resp = agenda_instance.listar_horarios_disponiveis(data_str=data, servico_str=servico)
            return json.dumps(resp, ensure_ascii=False)

        elif call_name == "fn_buscar_por_cpf":
            cpf = args.get("cpf")
            resp = agenda_instance.buscar_por_cpf(cpf)
            return json.dumps(resp, ensure_ascii=False)

        elif call_name == "fn_salvar_agendamento":
            telefone_arg = args.get("telefone", "")
            
            if telefone_arg == "CONFIRMADO_NUMERO_ATUAL":
                telefone_arg = contact_id 
                print(f"ℹ️ Placeholder 'CONFIRMADO_NUMERO_ATUAL' detectado. Usando o contact_id: {contact_id}")

            resp = agenda_instance.salvar(
                nome=args.get("nome", ""),
                cpf_raw=args.get("cpf", ""),
                telefone=telefone_arg, # Use a variável modificada
                servico=args.get("servico", ""),
                data_str=args.get("data", ""),
                hora_str=args.get("hora", ""),
                owner_id=contact_id
            )
            return json.dumps(resp, ensure_ascii=False)

        elif call_name == "fn_excluir_agendamento":
            resp = agenda_instance.excluir(
                cpf_raw=args.get("cpf", ""),
                data_str=args.get("data", ""),
                hora_str=args.get("hora", "")
            )
            return json.dumps(resp, ensure_ascii=False)
        
        elif call_name == "fn_excluir_TODOS_agendamentos":
            cpf = args.get("cpf")
            resp = agenda_instance.excluir_todos_por_cpf(cpf_raw=cpf)
            return json.dumps(resp, ensure_ascii=False)

        elif call_name == "fn_alterar_agendamento":
            resp = agenda_instance.alterar(
                cpf_raw=args.get("cpf", ""),
                data_antiga=args.get("data_antiga", ""),
                hora_antiga=args.get("hora_antiga", ""),
                data_nova=args.get("data_nova", ""),
                hora_nova=args.get("hora_nova", "")
            )
            return json.dumps(resp, ensure_ascii=False)

        elif call_name == "fn_capturar_nome":
            try:
                nome_bruto = args.get("nome_extraido", "").strip()
                print(f"--- [DEBUG RASTREIO 1] IA extraiu: nome_bruto='{nome_bruto}'")
                if not nome_bruto:
                    return json.dumps({"erro": "Nome estava vazio."}, ensure_ascii=False)

                nome_limpo = nome_bruto
                try:
                    palavras = nome_bruto.split()
                    if len(palavras) >= 2 and palavras[0].lower() == palavras[1].lower():
                        nome_limpo = palavras[0].capitalize() # Pega só o primeiro
                        print(f"--- [DEBUG ANTI-BUG] Corrigido (Espaço): '{nome_bruto}' -> '{nome_limpo}'")

                    else:
                        l = len(nome_bruto)
                        if l > 2 and l % 2 == 0: # Se for par e maior que 2
                            metade1 = nome_bruto[:l//2]
                            metade2 = nome_bruto[l//2:]
                            
                            if metade1.lower() == metade2.lower():
                                nome_limpo = metade1.capitalize() # Pega só a primeira metade
                                print(f"--- [DEBUG ANTI-BUG] Corrigido (Sem Espaço): '{nome_bruto}' -> '{nome_limpo}'")
                            else:
                                nome_limpo = " ".join([p.capitalize() for p in palavras])
                        else:
                            nome_limpo = " ".join([p.capitalize() for p in palavras])

                except Exception as e:
                    print(f"Aviso: Exceção na limpeza de nome: {e}")
                    nome_limpo = nome_bruto.capitalize() # Fallback 
                
                print(f"--- [DEBUG RASTREIO 2] Python limpou: nome_limpo='{nome_limpo}'")

                if conversation_collection is not None:
                    conversation_collection.update_one(
                        {'_id': contact_id},
                        {'$set': {'customer_name': nome_limpo}}, # <-- Agora salva o nome limpo
                        upsert=True
                    )
                return json.dumps({"sucesso": True, "nome_salvo": nome_limpo}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"erro": f"Erro ao salvar nome no DB: {e}"}, ensure_ascii=False)

        elif call_name == "fn_solicitar_intervencao":
            motivo = args.get("motivo", "Motivo não especificado pela IA.")
            return json.dumps({"sucesso": True, "motivo": motivo, "tag_especial": "[HUMAN_INTERVENTION]"})
        
        elif call_name == "fn_consultar_historico_completo":
            try:
                print(f"🧠 [MEMÓRIA] IA solicitou busca no histórico antigo para: {contact_id}") # Log Limpo

                convo = conversation_collection.find_one({'_id': contact_id})
                if not convo:
                    return json.dumps({"erro": "Histórico não encontrado."}, ensure_ascii=False)
                
                history_list = convo.get('history', [])
                
                texto_historico = "--- INÍCIO DO HISTÓRICO COMPLETO (BANCO DE DADOS) ---\n"
                for m in history_list: 
                    r = "Cliente" if m.get('role') == 'user' else "Lyra"
                    t = m.get('text', '')
                    # Ignora logs técnicos para limpar a leitura
                    if not t.startswith("Chamando função") and not t.startswith("[HUMAN"):
                        texto_historico += f"[{m.get('ts', '')[:16]}] {r}: {t}\n"
                texto_historico += "--- FIM DO HISTÓRICO COMPLETO ---"
                
                qtd_msgs = len(history_list)
                tamanho_texto = len(texto_historico)

                print(f"✅ [MEMÓRIA] Sucesso! {qtd_msgs} mensagens recuperadas ({tamanho_texto} caracteres) e enviadas para a IA.")

                # 4. Retorna TUDO (Removemos o slice [-2000:])
                return json.dumps({"sucesso": True, "historico": texto_historico}, ensure_ascii=False)
                
            except Exception as e:
                print(f"❌ [MEMÓRIA] Erro ao ler histórico: {e}")
                return json.dumps({"erro": f"Falha ao ler histórico: {e}"}, ensure_ascii=False)

        else:
            return json.dumps({"erro": f"Ferramenta desconhecida: {call_name}"}, ensure_ascii=False)
            
    except Exception as e:
        log_info(f"Erro fatal em handle_tool_call ({call_name}): {e}")
        return json.dumps({"erro": f"Exceção ao processar ferramenta: {e}"}, ensure_ascii=False)
    
def gerar_resposta_ia_com_tools(contact_id, sender_name, user_message, known_customer_name): 
    """
    (VERSÃO FINAL - BLINDADA COM RETRY GLOBAL, CONTABILIDADE E JANELA DESLIZANTE)
    """
    global modelo_ia 

    if modelo_ia is None:
        return "Desculpe, estou com um problema interno (modelo IA não carregado)."
    if conversation_collection is None:
        return "Desculpe, estou com um problema interno (DB de conversas não carregado)."

    def _normalize_name(n: Optional[str]) -> Optional[str]:
        if not n: return None
        s = str(n).strip()
        if not s: return None
        parts = [p for p in re.split(r'\s+', s) if p]
        if len(parts) >= 2 and parts[0].lower() == parts[1].lower():
            return parts[0]
        return s

    sender_name = _normalize_name(sender_name) or ""
    known_customer_name = _normalize_name(known_customer_name) 
    
    log_display = known_customer_name or sender_name or contact_id

    try:
        fuso_horario_local = pytz.timezone('America/Sao_Paulo')
        agora_local = datetime.now(fuso_horario_local)
        horario_atual = agora_local.strftime("%Y-%m-%d %H:%M:%S")
        hora_do_dia = agora_local.hour
        if 5 <= hora_do_dia < 12: saudacao = "Bom dia"
        elif 12 <= hora_do_dia < 18: saudacao = "Boa tarde"
        else: saudacao = "Boa noite"
    except:
        horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        saudacao = "Olá" 

    # --- CARREGA HISTÓRICO COM OTIMIZAÇÃO (JANELA DESLIZANTE) ---
    convo_data = load_conversation_from_db(contact_id)
    historico_texto_para_prompt = ""
    old_history_gemini_format = []
    
    if convo_data:
        history_from_db = convo_data.get('history', [])
        
        # AQUI ESTÁ A CORREÇÃO: Pegamos as últimas 15 e usamos ESSA lista para TUDO
        janela_recente = history_from_db[-25:] 
        qtd_msg_enviadas = len(janela_recente)
        print(f"📉 [METRICA] Janela Deslizante: Enviando apenas as últimas {qtd_msg_enviadas} mensagens para o Prompt.")
        historico_texto_para_prompt = ""
        old_history_gemini_format = []
        
        # Loop 1: Texto para o Prompt do Sistema
        for m in janela_recente:
            role_name = "Cliente" if m.get('role') == 'user' else "Lyra"
            txt = m.get('text', '').replace('\n', ' ')
            # Ignora logs técnicos para não gastar token e não confundir a IA
            if not txt.startswith("Chamando função") and not txt.startswith("[HUMAN"):
                historico_texto_para_prompt += f"- {role_name}: {txt}\n"

        # Loop 2: Histórico Técnico para o Gemini (CRÍTICO: Usar janela_recente aqui também!)
        for msg in janela_recente:
            role = msg.get('role', 'user')
            if role == 'assistant': role = 'model'
            
            # Filtra logs técnicos
            if 'text' in msg and not msg['text'].startswith("Chamando função"):
                old_history_gemini_format.append({'role': role, 'parts': [msg['text']]})

    tipo_prompt = "FINAL (Vendas)" if known_customer_name else "GATE (Captura)"
    print(f"\n[🔍 DEBUG PROMPT] O Python vai usar o prompt: {tipo_prompt}")
    print(f"[🔍 DEBUG NOME] O nome conhecido no início da função é: '{known_customer_name}'")
    if not known_customer_name:
        print("[⚠️ ALERTA] Se a IA capturar o nome AGORA, ela ainda estará usando o prompt GATE (sem endereço) para responder.")

    system_instruction = get_system_prompt_unificado(
        saudacao, 
        horario_atual,
        known_customer_name,  
        contact_id,
        historico_str=historico_texto_para_prompt
    )

    max_retries = 3 
    for attempt in range(max_retries):
        try:
            # Reinicia o objeto de chat
            modelo_com_sistema = genai.GenerativeModel(
                modelo_ia.model_name,
                system_instruction=system_instruction,
                tools=tools
            )
            
            # Agora sim: Inicia o chat APENAS com as mensagens da janela
            chat_session = modelo_com_sistema.start_chat(history=old_history_gemini_format) 
            
            if attempt > 0:
                print(f"🔁 Tentativa {attempt+1} de gerar resposta para {log_display}...")
            else:
                print(f"Enviando para a IA: '{user_message}' (De: {log_display})")
            
            resposta_ia = chat_session.send_message(user_message)
            
            # --- CONTABILIDADE INICIAL ---
            turn_input = 0
            turn_output = 0
            
            t_in, t_out = extrair_tokens_da_resposta(resposta_ia)
            turn_input += t_in
            turn_output += t_out

            while True:
                cand = resposta_ia.candidates[0]
                func_call = None
                try:
                    func_call = cand.content.parts[0].function_call
                except Exception:
                    func_call = None

                if not func_call or not getattr(func_call, "name", None):
                    break 

                call_name = func_call.name
                call_args = {key: value for key, value in func_call.args.items()}
                
                log_info(f"🔧 IA chamou a função: {call_name} com args: {call_args}")
                append_message_to_db(contact_id, 'assistant', f"Chamando função: {call_name}({call_args})")

                resultado_json_str = handle_tool_call(call_name, call_args, contact_id)
                log_info(f"📤 Resultado da função: {resultado_json_str}")

                if call_name == "fn_capturar_nome":
                    try:
                        res_data = json.loads(resultado_json_str)
                        nome_salvo = res_data.get("nome_salvo") or res_data.get("nome_extraido") 
                        
                        if nome_salvo:
                            print(f"🔄 Troca de Contexto: Nome '{nome_salvo}' salvo! Reiniciando com Prompt de Vendas...")
                            return gerar_resposta_ia_com_tools(
                                contact_id, 
                                sender_name, 
                                user_message, 
                                known_customer_name=nome_salvo 
                            )
                    except Exception as e:
                        print(f"⚠️ Erro ao tentar reiniciar fluxo (hot-swap): {e}")
                
                try:
                    res_data = json.loads(resultado_json_str)
                    if res_data.get("tag_especial") == "[HUMAN_INTERVENTION]":
                        msg_intervencao = f"[HUMAN_INTERVENTION] Motivo: {res_data.get('motivo', 'Solicitado.')}"
                        
                        save_conversation_to_db(
                            contact_id, 
                            sender_name, 
                            known_customer_name, 
                            turn_input, 
                            turn_output, 
                            ultima_msg_gerada=msg_intervencao
                        )
                        return msg_intervencao
                except: pass

                resposta_ia = chat_session.send_message(
                    [genai.protos.FunctionResponse(name=call_name, response={"resultado": resultado_json_str})]
                )
                
                # --- SOMA TOKENS DAS FERRAMENTAS ---
                t_in_tool, t_out_tool = extrair_tokens_da_resposta(resposta_ia)
                turn_input += t_in_tool
                turn_output += t_out_tool

            ai_reply_text = ""
            try:
                ai_reply_text = resposta_ia.text
            except:
                try:
                    ai_reply_text = resposta_ia.candidates[0].content.parts[0].text
                except:
                    print(f"⚠️ AVISO: Resposta vazia da IA na tentativa {attempt+1}. Forçando nova tentativa...")
                    if attempt < max_retries - 1:
                        time.sleep(1.5) 
                        continue 
                    else:
                        raise Exception("Todas as tentativas falharam e retornaram vazio.")

            save_conversation_to_db(contact_id, sender_name, known_customer_name, turn_input, turn_output, ai_reply_text)

            return ai_reply_text

        except Exception as e:
            print(f"❌ Erro na tentativa {attempt+1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(1) 
            else:
                return "A mensagem que você enviou deu erro aqui no whatsapp. 😵‍💫 Pode enviar novamente, por favor?"
    
    return "Erro crítico de comunicação."

def transcrever_audio_gemini(caminho_do_audio, contact_id=None):
    if not GEMINI_API_KEY:
        print("❌ Erro: API Key não definida para transcrição.")
        return None

    print(f"🎤 Enviando áudio '{caminho_do_audio}' para transcrição...")

    try:
        audio_file = genai.upload_file(path=caminho_do_audio, mime_type="audio/ogg")
        modelo_transcritor = genai.GenerativeModel('gemini-2.0-flash') 
        prompt_transcricao = "Transcreva este áudio exatamente como foi falado. Apenas o texto, sem comentários."
        
        response = modelo_transcritor.generate_content([prompt_transcricao, audio_file])
        
        if contact_id and conversation_collection is not None:
            in_tok, out_tok = extrair_tokens_da_resposta(response)
            
            if in_tok > 0 or out_tok > 0:
                conversation_collection.update_one(
                    {'_id': contact_id},
                    {'$inc': {
                        'total_tokens_consumed': in_tok + out_tok, 
                        'tokens_input': in_tok,                    
                        'tokens_output': out_tok                   
                    }}
                )
                print(f"💰 [Áudio] Tokens contabilizados: {in_tok} (Input) + {out_tok} (Output)")

        try:
            genai.delete_file(audio_file.name)
        except:
            pass

        if response.text:
            texto_transcrito = response.text.strip()
            print(f"✅ Transcrição recebida: '{texto_transcrito}'")
            return texto_transcrito
        else:
            print("⚠️ A IA retornou vazio para o áudio.")
            return "[Áudio sem fala ou inaudível]"

    except Exception as e:
        print(f"❌ Erro ao transcrever áudio: {e}")
        try:
            print("🔄 Tentando transcrição novamente (Retry)...")
            time.sleep(2)
            modelo_retry = genai.GenerativeModel('gemini-2.0-flash')
            audio_file_retry = genai.upload_file(path=caminho_do_audio, mime_type="audio/ogg")
            response_retry = modelo_retry.generate_content(["Transcreva o áudio.", audio_file_retry])

            # --- CORREÇÃO AQUI TAMBÉM: "is not None" ---
            if contact_id and conversation_collection is not None:
                in_tok_retry, out_tok_retry = extrair_tokens_da_resposta(response_retry)
                
                if in_tok_retry > 0 or out_tok_retry > 0:
                    conversation_collection.update_one(
                        {'_id': contact_id},
                        {'$inc': {
                            'total_tokens_consumed': in_tok_retry + out_tok_retry,
                            'tokens_input': in_tok_retry,
                            'tokens_output': out_tok_retry
                        }}
                    )

            genai.delete_file(audio_file_retry.name)
            return response_retry.text.strip()
        except Exception as e2:
             print(f"❌ Falha total na transcrição: {e2}")
             return "[Erro ao processar áudio]"
        
def send_whatsapp_message(number, text_message, delay_ms=1200): # <--- NOVO PARÂMETRO AQUI
    INSTANCE_NAME = "chatbot" 
    clean_number = number.split('@')[0]
    
    payload = {
        "number": clean_number, 
        "textMessage": {
            "text": text_message
        },
        "options": {
            "delay": delay_ms,     # <--- USA A VARIÁVEL DINÂMICA
            "presence": "composing", 
            "linkPreview": True
        }
    }
    
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}

    base_url = EVOLUTION_API_URL
    api_path = f"/message/sendText/{INSTANCE_NAME}"
    
    final_url = ""
    if not base_url:
        print("❌ ERRO: EVOLUTION_API_URL não está definida no .env")
        return

    if base_url.endswith(api_path):
        final_url = base_url
    elif base_url.endswith('/'):
        final_url = base_url[:-1] + api_path
    else:
        final_url = base_url + api_path

    try:
        print(f"✅ Enviando resposta para a URL: {final_url} (Destino: {clean_number}) [Delay: {delay_ms}ms]")
        response = requests.post(final_url, json=payload, headers=headers)
        
        if response.status_code < 400:
            print(f"✅ Resposta da IA enviada com sucesso para {clean_number}\n")
        else:
            print(f"❌ ERRO DA API EVOLUTION ao enviar para {clean_number}: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Erro de CONEXÃO ao enviar mensagem para {clean_number}: {e}")
        
def enviar_simulacao_digitacao(number):
    """
    Envia o status de 'digitando...' com a correção do objeto 'options'.
    """
    INSTANCE_NAME = "chatbot" 
    clean_number = number.split('@')[0]
    
    payload = {
        "number": clean_number,
        "options": {
            "presence": "composing",
            "delay": 12000 # 12 segundos enquanto a IA pensa (não afeta o envio final)
        }
    }
    
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    
    base_url = EVOLUTION_API_URL
    if base_url.endswith('/'):
        base_url = base_url[:-1]

    url_v2 = f"{base_url}/chat/sendPresence/{INSTANCE_NAME}"
    
    try:
        # AUMENTADO PARA 20 SEGUNDOS PARA EVITAR ERRO NO LOG
        response = requests.post(url_v2, json=payload, headers=headers, timeout=20)
        
        if response.status_code in [200, 201]:
            print(f"💬 SUCESSO! 'Digitando...' ativado para {clean_number}")
        else:
            print(f"⚠️ Falha ao enviar 'Digitando'. Código: {response.status_code}. Resposta: {response.text}")

    except Exception as e:
        print(f"⚠️ Erro de conexão no 'Digitando': {e}")

def gerar_e_enviar_relatorio_diario():
    if conversation_collection is None or not RESPONSIBLE_NUMBER:
        print("⚠️ Relatório diário desabilitado. (DB de Conversas ou RESPONSIBLE_NUMBER indisponível).")
        return

    hoje = datetime.now()
    
    try:
        query_filter = {"_id": {"$ne": "BOT_STATUS"}}
        usuarios_do_bot = list(conversation_collection.find(query_filter))
        
        numero_de_contatos = len(usuarios_do_bot)
        total_geral_tokens = 0
        media_por_contato = 0

        if numero_de_contatos > 0:
            for usuario in usuarios_do_bot:
                total_geral_tokens += usuario.get('total_tokens_consumed', 0)
            media_por_contato = total_geral_tokens / numero_de_contatos
        
        corpo_whatsapp_texto = f"""
            📊 *Relatório Diário de Tokens* 📊
            -----------------------------------
            *Cliente:* {CLIENT_NAME}
            *Data:* {hoje.strftime('%d/%m/%Y')}
            -----------------------------------
            👤 *Total de Conversas (Clientes):* {numero_de_contatos}
            🔥 *Total de Tokens Gastos:* {total_geral_tokens}
            📈 *Média de Tokens por Cliente:* {media_por_contato:.0f}
        """
        
        corpo_whatsapp_texto = "\n".join([line.strip() for line in corpo_whatsapp_texto.split('\n')])

        responsible_jid = f"{RESPONSIBLE_NUMBER}@s.whatsapp.net"
        
        send_whatsapp_message(responsible_jid, corpo_whatsapp_texto)
        
        print(f"✅ Relatório diário para '{CLIENT_NAME}' enviado com sucesso para o WhatsApp ({RESPONSIBLE_NUMBER})!")

    except Exception as e:
        print(f"❌ Erro ao gerar ou enviar relatório por WhatsApp para '{CLIENT_NAME}': {e}")
        # Tenta notificar o erro
        try:
            responsible_jid = f"{RESPONSIBLE_NUMBER}@s.whatsapp.net"
            send_whatsapp_message(responsible_jid, f"❌ Falha ao gerar o relatório diário do bot {CLIENT_NAME}. Erro: {e}")
        except:
            pass # Se falhar em notificar, apenas loga no console

scheduler = BackgroundScheduler(daemon=True, timezone=FUSO_HORARIO)
scheduler.start()

app = Flask(__name__)
CORS(app) 
processed_messages = set() 

@app.route('/webhook', methods=['POST'])
def receive_webhook():
    data = request.json # <--- O ponto que você mencionou

    event_type = data.get('event')
    if event_type and event_type != 'messages.upsert':
        return jsonify({"status": "ignored_event_type"}), 200

    try:
        message_data = data.get('data', {}) 
        if not message_data:
            message_data = data
            
        key_info = message_data.get('key', {})
        if not key_info:
            return jsonify({"status": "ignored_no_key"}), 200
        
        remote_jid = key_info.get('remoteJid', '')
        
        if remote_jid.endswith('@g.us') or remote_jid.endswith('@broadcast'):
            return jsonify({"status": "ignored_group_context"}), 200

        if key_info.get('fromMe'):
            # ... (seu código de ignorar fromMe existente) ...
            sender_number_full = key_info.get('remoteJid')
            clean_number = sender_number_full.split('@')[0]
            if clean_number != RESPONSIBLE_NUMBER:
                 return jsonify({"status": "ignored_from_me"}), 200
            # ...

        message_id = key_info.get('id')
        if not message_id:
            return jsonify({"status": "ignored_no_id"}), 200

        if message_id in processed_messages:
            return jsonify({"status": "ignored_duplicate"}), 200
        processed_messages.add(message_id)
        if len(processed_messages) > 1000:
            processed_messages.clear()

        handle_message_buffering(message_data)
        
        return jsonify({"status": "received"}), 200

    except Exception as e:
        print(f"❌ Erro inesperado no webhook: {e}")
        return jsonify({"status": "error"}), 500
    
@app.route('/', methods=['GET'])
def health_check():
    return f"Estou vivo! ({CLIENT_NAME} Bot v2 - com Agenda)", 200 

def handle_message_buffering(message_data):
    global message_buffer, message_timers, BUFFER_TIME_SECONDS
    
    try:
        key_info = message_data.get('key', {})
        sender_number_full = key_info.get('senderPn') or key_info.get('participant') or key_info.get('remoteJid')
        if not sender_number_full or sender_number_full.endswith('@g.us'):
            return
        clean_number = sender_number_full.split('@')[0]
        
        message = message_data.get('message', {})
        user_message_content = None
        
        if message.get('audioMessage'):
            print("🎤 Áudio recebido, processando imediatamente (sem buffer)...")
            threading.Thread(target=process_message_logic, args=(message_data, None)).start()
            return
        
        if message.get('conversation'):
            user_message_content = message['conversation']
        elif message.get('extendedTextMessage'):
            user_message_content = message['extendedTextMessage'].get('text')
        
        if not user_message_content:
            print("➡️  Mensagem sem conteúdo de texto ignorada pelo buffer.")
            return

        if clean_number not in message_buffer:
            message_buffer[clean_number] = []
        message_buffer[clean_number].append(user_message_content)
        
        print(f"📥 Mensagem adicionada ao buffer de {clean_number}: '{user_message_content}'")

        if clean_number in message_timers:
            message_timers[clean_number].cancel()

        timer = threading.Timer(
            BUFFER_TIME_SECONDS, 
            _trigger_ai_processing, 
            args=[clean_number, message_data] 
        )
        message_timers[clean_number] = timer
        timer.start()

    except Exception as e:
        print(f"❌ Erro no 'handle_message_buffering': {e}")
            
def _trigger_ai_processing(clean_number, last_message_data):
    global message_buffer, message_timers
    
    if clean_number not in message_buffer:
        return 

    messages_to_process = message_buffer.pop(clean_number, [])
    if clean_number in message_timers:
        del message_timers[clean_number]
        
    if not messages_to_process:
        return

    full_user_message = ". ".join(messages_to_process)

    log_info(f"[DEBUG RASTREIO | PONTO 1] Buffer para {clean_number}: '{full_user_message}'")
    
    print(f"⚡️ DISPARANDO IA para {clean_number} com mensagem agrupada: '{full_user_message}'")

    threading.Thread(target=process_message_logic, args=(last_message_data, full_user_message)).start()


def handle_responsible_command(message_content, responsible_number):
    if conversation_collection is None:
        send_whatsapp_message(responsible_number, "❌ Erro: Comandos desabilitados (DB de Conversas indisponível).")
        return True
        
    print(f"⚙️  Processando comando do responsável: '{message_content}'")
    
    command_lower = message_content.lower().strip()
    command_parts = command_lower.split()

    if command_lower == "bot off":
        try:
            conversation_collection.update_one(
                {'_id': 'BOT_STATUS'},
                {'$set': {'is_active': False}},
                upsert=True
            )
            send_whatsapp_message(responsible_number, "✅ *Bot PAUSADO.* O bot não responderá a nenhum cliente até você enviar 'bot on'.")
            return True
        except Exception as e:
            send_whatsapp_message(responsible_number, f"❌ Erro ao pausar o bot: {e}")
            return True

    elif command_lower == "bot on":
        try:
            conversation_collection.update_one(
                {'_id': 'BOT_STATUS'},
                {'$set': {'is_active': True}},
                upsert=True
            )
            send_whatsapp_message(responsible_number, "✅ *Bot REATIVADO.* O bot está respondendo aos clientes.")
            return True
        except Exception as e:
            send_whatsapp_message(responsible_number, f"❌ Erro ao reativar o bot: {e}")
            return True

    if len(command_parts) == 2 and command_parts[0] == "ok":
        customer_number_to_reactivate = command_parts[1].replace('@s.whatsapp.net', '').strip()
        
        try:
            customer = conversation_collection.find_one({'_id': customer_number_to_reactivate})

            if not customer:
                send_whatsapp_message(responsible_number, f"⚠️ *Atenção:* O cliente com o número `{customer_number_to_reactivate}` não foi encontrado no banco de dados.")
                return True 

            result = conversation_collection.update_one(
                {'_id': customer_number_to_reactivate},
                {'$set': {'intervention_active': False}}
            )

            if result.modified_count > 0:
                send_whatsapp_message(responsible_number, f"✅ Atendimento automático reativado para o cliente `{customer_number_to_reactivate}`.")
                send_whatsapp_message(customer_number_to_reactivate, "Oi, sou eu a Lyra novamente, voltei pro seu atendimento. Se precisar de algo me diga! 😊")
            else:
                send_whatsapp_message(responsible_number, f"ℹ️ O atendimento para `{customer_number_to_reactivate}` já estava ativo. Nenhuma alteração foi necessária.")
            
            return True 

        except Exception as e:
            print(f"❌ Erro ao tentar reativar cliente: {e}")
            send_whatsapp_message(responsible_number, f"❌ Ocorreu um erro técnico ao tentar reativar o cliente. Verifique o log do sistema.")
            return True
            
    help_message = (
        "Comando não reconhecido. 🤖\n\n"
        "*COMANDOS DISPONÍVEIS:*\n\n"
        "1️⃣ `bot on`\n(Liga o bot para todos os clientes)\n\n"
        "2️⃣ `bot off`\n(Desliga o bot para todos os clientes)\n\n"
        "3️⃣ `ok <numero_do_cliente>`\n(Reativa um cliente em intervenção)"
    )
    send_whatsapp_message(responsible_number, help_message)
    return True


def process_message_logic(message_data, buffered_message_text=None):
    lock_acquired = False
    clean_number = None
    
    if conversation_collection is None:
        print("❌ Processamento interrompido: DB de Conversas indisponível.")
        return
    if modelo_ia is None:
        print("❌ Processamento interrompido: Modelo IA não inicializado.")
        return
        
    try:
        key_info = message_data.get('key', {})
        sender_number_full = key_info.get('senderPn') or key_info.get('participant') or key_info.get('remoteJid')
        if not sender_number_full or sender_number_full.endswith('@g.us'): return
        
        clean_number = sender_number_full.split('@')[0]
        sender_name_from_wpp = message_data.get('pushName') or 'Cliente'

        # ==============================================================================
        # 🛡️ LÓGICA DE "SALA DE ESPERA" (Atomicidade)
        # ==============================================================================
        now = datetime.now()

        # 1. Garante que o cliente existe no banco
        conversation_collection.update_one(
            {'_id': clean_number},
            {'$setOnInsert': {'created_at': now, 'history': []}},
            upsert=True
        )

        # 2. Tenta pegar o crachá de atendimento (LOCK)
        res = conversation_collection.update_one(
            {'_id': clean_number, 'processing': {'$ne': True}},
            {'$set': {'processing': True, 'processing_started_at': now}}
        )

        # 3. SE NÃO CONSEGUIU O CRACHÁ, ESPERA NA FILA
        if res.matched_count == 0:
            print(f"⏳ {clean_number} está ocupado. Colocando mensagem na FILA DE ESPERA...")
            
            # Devolve para o buffer e tenta de novo em 4s
            if buffered_message_text:
                if clean_number not in message_buffer: 
                    message_buffer[clean_number] = []
                if buffered_message_text not in message_buffer[clean_number]:
                    message_buffer[clean_number].insert(0, buffered_message_text)
            
            timer = threading.Timer(4.0, _trigger_ai_processing, args=[clean_number, message_data])
            message_timers[clean_number] = timer
            timer.start()
            return 
        
        lock_acquired = True
        # ==============================================================================
        
        user_message_content = None
        
        # --- CENÁRIO 1: TEXTO (Vindo do Buffer) ---
        if buffered_message_text:
            user_message_content = buffered_message_text
            messages_to_save = user_message_content.split(". ")
            for msg_text in messages_to_save:
                if msg_text and msg_text.strip():
                    append_message_to_db(clean_number, 'user', msg_text)
        
        # --- CENÁRIO 2: MENSAGEM NOVA (Áudio ou Texto direto) ---
        else:
            message = message_data.get('message', {})
            
            # >>>> TRATAMENTO DE ÁUDIO (Onde a mágica acontece) <<<<
            if message.get('audioMessage') and message.get('base64'):
                message_id = key_info.get('id')
                print(f"🎤 Mensagem de áudio recebida de {clean_number}. Transcrevendo...")
                
                audio_base64 = message['base64']
                audio_data = base64.b64decode(audio_base64)
                os.makedirs("/tmp", exist_ok=True) 
                temp_audio_path = f"/tmp/audio_{clean_number}_{message_id}.ogg"
                
                with open(temp_audio_path, 'wb') as f: f.write(audio_data)
                
                # Passa o contact_id para cobrar o token corretamente
                texto_transcrito = transcrever_audio_gemini(temp_audio_path, contact_id=clean_number)
                
                try: os.remove(temp_audio_path)
                except: pass

                if not texto_transcrito or texto_transcrito.startswith("["):
                    send_whatsapp_message(sender_number_full, "Desculpe, tive um problema técnico para ouvir seu áudio. Pode escrever ou tentar de novo? 🎧", delay_ms=2000)
                    user_message_content = "[Erro no Áudio]"
                else:
                    # AQUI ESTÁ O SEGREDO: Adicionamos a etiqueta para a IA saber que é áudio
                    user_message_content = f"[Transcrição de Áudio]: {texto_transcrito}"
            
            else:
                # Se não for áudio nem buffer, tenta pegar texto direto (ex: imagem com legenda)
                user_message_content = message.get('conversation') or message.get('extendedTextMessage', {}).get('text')
                if not user_message_content:
                    user_message_content = "[Mensagem não suportada (Imagem/Figurinha)]"
            
            # Salva no histórico (O texto transcrito agora vai pro DB)
            if user_message_content:
                append_message_to_db(clean_number, 'user', user_message_content)

        print(f"🧠 IA Pensando para {clean_number}: '{user_message_content}'")
        
        # --- Checagem de Admin ---
        if RESPONSIBLE_NUMBER and clean_number == RESPONSIBLE_NUMBER:
            if handle_responsible_command(user_message_content, clean_number):
                return 

        # --- Checagem Bot On/Off ---
        try:
            bot_status = conversation_collection.find_one({'_id': 'BOT_STATUS'})
            if bot_status and not bot_status.get('is_active', True):
                print(f"🤖 Bot desligado. Ignorando {clean_number}.")
                return 
        except: pass

        # --- Checagem Intervenção ---
        convo_status = conversation_collection.find_one({'_id': clean_number})
        if convo_status and convo_status.get('intervention_active', False):
            print(f"⏸️  Conversa com {sender_name_from_wpp} ({clean_number}) pausada para atendimento humano.")
            return 

        # Pega o nome para passar pra IA
        known_customer_name = convo_status.get('customer_name') if convo_status else None
        
        log_info(f"[DEBUG RASTREIO | PONTO 2] Conteúdo final para IA (Cliente {clean_number}): '{user_message_content}'")

        # Chama a IA (Ela vai ler o histórico do DB, que agora tem o áudio transcrito)
        ai_reply = gerar_resposta_ia_com_tools(
            clean_number,
            sender_name_from_wpp,
            user_message_content,
            known_customer_name
        )
        
        if not ai_reply:
            print("⚠️ A IA retornou vazio.")
            return 

        try:
            # Salva a resposta da IA no histórico
            append_message_to_db(clean_number, 'assistant', ai_reply)
            
            # Lógica de Intervenção vinda da IA
            if ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
                print(f"‼️ INTERVENÇÃO HUMANA SOLICITADA para {sender_name_from_wpp} ({clean_number})")
                conversation_collection.update_one({'_id': clean_number}, {'$set': {'intervention_active': True}}, upsert=True)
                send_whatsapp_message(sender_number_full, "Um momento, estou chamando o Lucas! 🏃‍♂️", delay_ms=2000)
                
                if RESPONSIBLE_NUMBER:
                    reason = ai_reply.replace("[HUMAN_INTERVENTION] Motivo:", "").strip()
                    display_name = known_customer_name or sender_name_from_wpp
                    
                    # Pega resumo para o admin
                    hist = load_conversation_from_db(clean_number).get('history', [])
                    resumo = get_last_messages_summary(hist)
                    
                    msg_admin = (
                        f"🚨 *INTERVENÇÃO SOLICITADA*\n"
                        f"👤 {display_name} ({clean_number})\n"
                        f"❓ Motivo: {reason}\n\n"
                        f"📝 *Resumo:*\n{resumo}\n\n"
                        f"👉 Para reativar o bot: `ok {clean_number}`"
                    )
                    send_whatsapp_message(f"{RESPONSIBLE_NUMBER}@s.whatsapp.net", msg_admin, delay_ms=1000)
            
            else:
                # Lógica de Envio Normal (Gabarito vs Fracionado)
                def is_gabarito(text):
                    required = ["nome:", "cpf:", "telefone:", "serviço:", "data:", "hora:"]
                    found = [k for k in required if k in text.lower()]
                    return len(found) >= 3

                if is_gabarito(ai_reply):
                    print(f"🤖 Resposta da IA (Gabarito) para {sender_name_from_wpp}")
                    send_whatsapp_message(sender_number_full, ai_reply, delay_ms=4000)
                else:
                    print(f"🤖 Resposta da IA (Normal) para {sender_name_from_wpp}")
                    paragraphs = [p.strip() for p in ai_reply.split('\n') if p.strip()]
                    if not paragraphs: return

                    for i, para in enumerate(paragraphs):
                        current_delay = 4000 if i == 0 else 5000
                        send_whatsapp_message(sender_number_full, para, delay_ms=current_delay)
                        time.sleep(current_delay / 1000)

        except Exception as e:
            print(f"❌ Erro no envio: {e}")
            send_whatsapp_message(sender_number_full, "Tive um erro técnico. Pode repetir?", delay_ms=1000)

    except Exception as e:
        print(f"❌ Erro fatal ao processar mensagem: {e}")
    finally:
        # --- Libera o Lock ---
        if clean_number and lock_acquired and conversation_collection is not None:
            conversation_collection.update_one(
                {'_id': clean_number},
                {'$unset': {'processing': "", 'processing_started_at': ""}}
            )

if modelo_ia is not None and conversation_collection is not None and agenda_instance is not None:
    print("\n=============================================")
    print("    CHATBOT WHATSAPP COM IA INICIADO (V2 - COM AGENDA)")
    print(f"    CLIENTE: {CLIENT_NAME}")
    if not RESPONSIBLE_NUMBER:
        print("    AVISO: 'RESPONSIBLE_NUMBER' não configurado.")
    else:
        print(f"    Intervenção Humana notificará: {RESPONSIBLE_NUMBER}")
    print("=============================================")
    print("Servidor aguardando mensagens no webhook...")

    # --- ALTERE AS DUAS LINHAS ABAIXO ---
    scheduler.add_job(gerar_e_enviar_relatorio_diario, 'cron', hour=8, minute=0)
    print("⏰ Agendador de relatórios iniciado. O relatório será enviado DIARIAMENTE às 08:00.")
    
    scheduler.add_job(verificar_followup_automatico, 'interval', minutes=1)
    print(f"⏰ Agendador de Follow-up iniciado (Estágios ativos: {TEMPO_FOLLOWUP_1}, {TEMPO_FOLLOWUP_2}, {TEMPO_FOLLOWUP_3} min).")

    scheduler.add_job(verificar_lembretes_agendados, 'interval', minutes=60)
    print("⏰ Agendador de Lembretes (24h antes) iniciado.")
    
    import atexit
    atexit.register(lambda: scheduler.shutdown())
    
else:
    print("\nEncerrando o programa devido a erros na inicialização (Verifique APIs e DBs).")
    # (O programa não deve continuar se os componentes principais falharem)
    exit() # Encerra se o modelo ou DBs falharem

@app.route('/api/login', methods=['POST'])
def api_login():
    """
    Login Administrativo.
    Verifica se usuário e senha batem com as variáveis do código.
    """
    data = request.json
    if not data:
        return jsonify({"erro": "Dados não enviados"}), 400

    usuario = data.get('usuario', '')
    senha = data.get('senha', '')

    # Verifica se bate com a senha mestra
    if usuario == ADMIN_USER and senha == ADMIN_PASS:
        return jsonify({
            "sucesso": True,
            "usuario": {
                "nome": "Administrador Neuro'Up",
                "nivel": "master"
            }
        }), 200
    else:
        return jsonify({"erro": "Usuário ou senha incorretos."}), 401


@app.route('/api/meus-agendamentos', methods=['GET'])
def api_meus_agendamentos():
    try:
        if agenda_instance is None:
            return jsonify([]), 500

        # Buscamos tudo
        agendamentos_db = agenda_instance.collection.find({}).sort("inicio", 1)

        lista_formatada = []
        agora = datetime.now()

        for ag in agendamentos_db:
            inicio_dt = ag.get("inicio")
            fim_dt = ag.get("fim")
            created_at_dt = ag.get("created_at")
            
            if not isinstance(inicio_dt, datetime): continue
            
            # --- LÓGICA NOVA DO STATUS ---
            # Lê o status gravado no banco. Se não tiver, assume 'agendado'.
            status_db = ag.get("status", "agendado")
            
            # Se o horário já passou E ainda está como 'agendado', 
            # enviamos um status visual para o app saber que está pendente de ação
            status_final = status_db
            if inicio_dt < agora and status_db == "agendado":
                status_final = "pendente_acao" # O App vai pintar de Roxo/Cinza

            created_at_str = ""
            if isinstance(created_at_dt, datetime):
                # Se a data vier do Mongo sem fuso (naive), assumimos que é UTC
                if created_at_dt.tzinfo is None:
                    created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
                
                # Converte para o horário de Brasília/São Paulo
                fuso_br = pytz.timezone('America/Sao_Paulo')
                data_br = created_at_dt.astimezone(fuso_br)
                
                # Formata para string
                created_at_str = data_br.strftime("%d/%m/%Y %H:%M")

            item = {
                "id": str(ag.get("_id")), 
                "dia": inicio_dt.strftime("%Y-%m-%d"),
                "dia_visual": inicio_dt.strftime("%d/%m"),
                "hora_inicio": inicio_dt.strftime("%H:%M"),
                "hora_fim": fim_dt.strftime("%H:%M") if fim_dt else "",
                "servico": ag.get("servico", "Atendimento").capitalize(),
                "status": status_final, # <-- Enviamos o status calculado
                "cliente_nome": ag.get("nome", "Sem Nome").title(),
                "cliente_telefone": ag.get("telefone", ""),
                "cpf": ag.get("cpf", ""),
                "owner_whatsapp_id": ag.get("owner_whatsapp_id", ""),
                "created_at": created_at_str
            }
            lista_formatada.append(item)

        return jsonify(lista_formatada), 200

    except Exception as e:
        print(f"❌ Erro na API Admin: {e}")
        return jsonify({"erro": str(e)}), 500

# 2. ADICIONE ESTAS NOVAS ROTAS (Para o App chamar)

@app.route('/api/agendamento/atualizar-status', methods=['POST'])
def api_atualizar_status():
    """Define como 'concluido' ou 'ausencia'"""
    data = request.json
    ag_id = data.get('id')
    novo_status = data.get('status') # 'concluido' ou 'ausencia'

    try:
        agenda_instance.collection.update_one(
            {"_id": ObjectId(ag_id)},
            {"$set": {"status": novo_status}}
        )
        return jsonify({"sucesso": True}), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route('/api/agendamento/deletar', methods=['POST'])
def api_deletar_id():
    """Apaga o agendamento pelo ID (Cancelar)"""
    data = request.json
    ag_id = data.get('id')

    try:
        agenda_instance.collection.delete_one({"_id": ObjectId(ag_id)})
        return jsonify({"sucesso": True}), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route('/api/horarios-disponiveis', methods=['POST'])
def api_horarios_disponiveis():
    """
    Retorna os slots vagos para o App, usando a mesma regra da IA.
    Esperado: { "data": "DD/MM/YYYY", "servico": "reunião" }
    """
    data = request.json
    data_str = data.get('data') # Ex: "28/11/2025"
    servico = data.get('servico', 'reunião')
    
    if not agenda_instance:
        return jsonify({"erro": "Agenda não conectada"}), 500
        
    # Reutiliza a lógica robusta da classe Agenda
    resultado = agenda_instance.listar_horarios_disponiveis(data_str, servico)
    return jsonify(resultado), 200

@app.route('/api/agendamento/criar', methods=['POST'])
def api_criar_agendamento():
    """
    Cria um agendamento manual via App.
    """
    data = request.json
    
    # Extrai dados do formulário do App
    nome = data.get('nome')
    cpf = data.get('cpf')
    telefone = data.get('telefone')
    servico = data.get('servico', 'reunião')
    data_str = data.get('data') # DD/MM/YYYY
    hora_str = data.get('hora') # HH:MM
    
    # Se o admin estiver criando, o owner_whatsapp_id pode ser o telefone limpo
    # para que os lembretes funcionem.
    telefone_limpo = re.sub(r'\D', '', str(telefone))
    owner_id = telefone_limpo if telefone_limpo else "admin_manual"

    if not agenda_instance:
        return jsonify({"erro": "Agenda offline"}), 500

    # Usa o método salvar() que já tem todas as travas de segurança (conflito, feriado, etc)
    resultado = agenda_instance.salvar(
        nome=nome,
        cpf_raw=cpf,
        telefone=telefone,
        servico=servico,
        data_str=data_str,
        hora_str=hora_str,
        owner_id=owner_id
    )
    
    if "erro" in resultado:
        return jsonify(resultado), 400 # Retorna erro 400 se falhar (ex: horário ocupado)
        
    return jsonify(resultado), 200

@app.route('/api/folga/gerenciar', methods=['POST'])
def api_gerenciar_folga():
    data = request.json
    data_str = data.get('data')
    acao = data.get('acao') # 'criar' ou 'remover'

    if not agenda_instance: return jsonify({"erro": "Agenda offline"}), 500
    
    dt = parse_data(data_str)
    if not dt: return jsonify({"erro": "Data inválida"}), 400
    
    # Define o dia inteiro (00:00 até 23:59)
    inicio_dia = datetime.combine(dt.date(), dt_time.min)
    fim_dia = datetime.combine(dt.date(), dt_time.max)

    if acao == 'criar':
        # Verifica se já tem clientes REAIS agendados (ignorando folgas antigas ou cancelados)
        conflitos = agenda_instance.collection.count_documents({
            "inicio": {"$gte": inicio_dia, "$lte": fim_dia},
            "servico": {"$ne": "Folga"}, 
            "status": {"$nin": ["cancelado", "ausencia", "bloqueado"]}
        })

        if conflitos > 0:
            return jsonify({"erro": f"Ops! Já existem {conflitos} clientes agendados neste dia. Cancele-os ou remarqueles antes de bloquear o dia."}), 400

        # Cria o bloqueio
        agenda_instance.collection.insert_one({
            "nome": "BLOQUEIO ADMINISTRATIVO",
            "servico": "Folga",  # Importante ser "Folga" com F maiúsculo para bater com a busca
            "status": "bloqueado",
            "inicio": inicio_dia,
            "fim": fim_dia,
            "created_at": datetime.now(timezone.utc),
            "owner_whatsapp_id": "admin",
            "cliente_telefone": "",
            "cpf": ""
        })
        return jsonify({"sucesso": True}), 200

    elif acao == 'remover':
        # Remove APENAS o bloqueio de folga
        resultado = agenda_instance.collection.delete_many({
            "inicio": {"$gte": inicio_dia, "$lte": fim_dia},
            "$or": [{"servico": "Folga"}, {"status": "bloqueado"}]
        })
        
        if resultado.deleted_count > 0:
            return jsonify({"sucesso": True, "msg": "Folga removida. Agenda aberta!"}), 200
        else:
            return jsonify({"erro": "Nenhuma folga encontrada para remover neste dia."}), 400

    return jsonify({"erro": "Ação inválida"}), 400

if __name__ == '__main__':
    print("Iniciando em MODO DE DESENVOLVIMENTO LOCAL (app.run)...")
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port, debug=False)