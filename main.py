
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

CLIENT_NAME = "Neuro'up Soluções em Tecnologia"
RESPONSIBLE_NUMBER = "554898389781" 

load_dotenv()

EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_DB_URI = os.environ.get("MONGO_DB_URI") # DB de Conversas

MONGO_AGENDA_URI = os.environ.get("MONGO_AGENDA_URI")
MONGO_AGENDA_COLLECTION = os.environ.get("MONGO_AGENDA_COLLECTION", "agendamentos")

clean_client_name_global = CLIENT_NAME.lower().replace(" ", "_").replace("-", "_")
DB_NAME = "neuroup_solucoes_db"

INTERVALO_SLOTS_MINUTOS = 30 
NUM_ATENDENTES = 1

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
BUFFER_TIME_SECONDS = 8 

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

    def _checar_dia_de_folga(self, dt: datetime) -> Optional[str]:
        dia_semana_num = dt.weekday()
        if dia_semana_num in FOLGAS_DIAS_SEMANA:
            return MAPA_DIAS_SEMANA_PT.get(dia_semana_num, "dia de folga")
        return None

    def _get_duracao_servico(self, servico_str: str) -> Optional[int]:
        servico_key = servico_str.strip().lower()
        # Lógica flexível: se a chave exata não existir, tenta encontrar por palavra-chave
        if servico_key in MAPA_SERVICOS_DURACAO:
             return MAPA_SERVICOS_DURACAO.get(servico_key)
        
        if "reunião" in servico_key or "lucas" in servico_key:
             return MAPA_SERVICOS_DURACAO.get("reunião") # Retorna o padrão
        
        if "consultoria" in servico_key:
             return MAPA_SERVICOS_DURACAO.get("consultoria inicial")

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
        cpf = limpar_cpf(cpf_raw)
        if not cpf:
            return {"erro": "CPF inválido (deve ter 11 dígitos)."}
        
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

    def salvar(self, nome: str, cpf_raw: str, telefone: str, servico: str, data_str: str, hora_str: str) -> Dict[str, Any]:
        cpf = limpar_cpf(cpf_raw)
        if not cpf:
            return {"erro": "CPF inválido."}
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

            conflitos_atuais = self._contar_conflitos_no_banco(inicio_dt, fim_dt)

            if conflitos_atuais >= NUM_ATENDENTES:
                return {"erro": f"Horário {hora} indisponível. O proprietário já está ocupado neste horário."}
            
            novo_documento = {
                "nome": nome.strip(),
                "cpf": cpf,
                "telefone": telefone.strip(),
                "servico": servico.strip(),
                "duracao_minutos": duracao_minutos,
                "inicio": inicio_dt, 
                "fim": fim_dt,
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
            # A query busca todos os agendamentos futuros do CPF
            query = {"cpf": cpf, "inicio": {"$gte": agora}}
            
            # Usa delete_many para apagar todos que derem match
            resultado = self.collection.delete_many(query)
            
            count = resultado.deleted_count
            if count == 0:
                return {"erro": "Nenhum agendamento futuro encontrado para este CPF."}
            
            # Retorna a mensagem de sucesso com a contagem
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
                # --- Ferramentas da AGENDA ---
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
                }
            ]
        }
    ]


modelo_ia = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        if tools: 
            modelo_ia = genai.GenerativeModel('gemini-2.5-flash', tools=tools)
            print("✅ Modelo do Gemini (gemini-2.5-flash) inicializado com FERRAMENTAS.")
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

def save_conversation_to_db(contact_id, sender_name, customer_name, tokens_used):
    if conversation_collection is None: return
    try:
        update_payload = {
            'sender_name': sender_name,
            'last_interaction': datetime.now()
        }
        if customer_name:
            update_payload['customer_name'] = customer_name

        conversation_collection.update_one(
            {'_id': contact_id},
            {
                '$set': update_payload,
                '$inc': {'total_tokens_consumed': tokens_used}
            },
            upsert=True
        )
    except Exception as e:
        print(f"❌ Erro ao salvar metadados da conversa no MongoDB para {contact_id}: {e}")

def load_conversation_from_db(contact_id):
    if conversation_collection is None: return None
    try:
        result = conversation_collection.find_one({'_id': contact_id})
        if result:
            history = result.get('history', [])
            # Filtra o prompt do sistema antigo (boa prática)
            history_filtered = [msg for msg in history if not msg.get('text', '').strip().startswith("A data e hora atuais são:")]
            history_sorted = sorted(history_filtered, key=lambda m: m.get('ts', ''))
            result['history'] = history_sorted
            print(f"🧠 Histórico anterior encontrado e carregado para {contact_id} ({len(history_sorted)} entradas).")
            return result
    except Exception as e:
        print(f"❌ Erro ao carregar conversa do MongoDB para {contact_id}: {e}")
    return None

def get_last_messages_summary(history, max_messages=4):
    clean_history = []
    
    # 1. Processa o histórico COMPLETO para limpar o "lixo"
    for message in history: 
        role = "Cliente" if message.get('role') == 'user' else "Bot"
        text = message.get('text', '').strip()

        # --- FILTROS (Mantém os antigos e adiciona os novos) ---
        if role == "Cliente" and text.startswith("A data e hora atuais são:"):
            continue 
        if role == "Bot" and text.startswith("Entendido. A Regra de Ouro"):
            continue 
        
        # --- NOVOS FILTROS (Para limpar a notificação) ---
        if role == "Bot" and text.startswith("Chamando função:"):
            continue
        if role == "Bot" and text.startswith("[HUMAN_INTERVENTION]"):
            continue
        # --- FIM DOS NOVOS FILTROS ---
            
        clean_history.append(f"*{role}:* {text}")
    
    # 2. Pega os últimos 'max_messages' da lista JÁ LIMPA
    relevant_summary = clean_history[-max_messages:]
    
    if not relevant_summary:
        # Fallback: Se tudo for filtrado, pega a última mensagem real do cliente
        user_messages = [msg.get('text') for msg in history if msg.get('role') == 'user' and not msg.get('text', '').startswith("A data e hora atuais são:")]
        if user_messages:
            return f"*Cliente:* {user_messages[-1]}"
        else:
            return "Nenhum histórico de conversa encontrado."
            
    return "\n".join(relevant_summary)

def get_system_prompt_unificado(saudacao: str, horario_atual: str, known_customer_name: str, sender_name: str, clean_number: str) -> str:
    
    # Lógica de Nome Dinâmico
    prompt_name_instruction = ""
    if known_customer_name:
        palavras = known_customer_name.strip().split()
        if len(palavras) >= 2 and palavras[0].lower() == palavras[1].lower():
            known_customer_name = palavras[0].capitalize()
        else:
            known_customer_name = " ".join([p.capitalize() for p in palavras])
        prompt_name_instruction = f"O nome do usuário com quem você está falando é: {known_customer_name}. Trate-o por este nome."
    else:
        # ==========================================================
        # PARTE 1: GATE DE CAPTURA DE NOME (O BOT SÓ FAZ ISSO)
        # ==========================================================
        prompt_name_instruction = f"""
        GATE DE CAPTURA DE NOME (PRIORIDADE MÁXIMA)
        
        Seu nome é {{Lyra}}. O nome do cliente AINDA NÃO É CONHECIDO.
        Sua **ÚNICA MISSÃO** neste momento é capturar o nome do cliente.
        O restante do seu prompt (sobre preços, serviços, etc.) só deve ser usado DEPOIS que o nome for capturado.

        A **ÚNICA EXCEÇÃO** é se o cliente pedir intervenção humana (falar com Lucas, dono, proprietário). Fora isso, NADA é mais importante que capturar o nome.
        
        **REGRA CRÍTICA:** NÃO FORNEÇA NENHUMA INFORMAÇÃO (preços, serviços, como funciona) ANTES de ter o nome. Sua resposta deve ser CURTA e HUMANA.
        
        Tente captar se a pessoa esta dizendo o nome(se apresentando) ou falar com o dono. Se a pessoa disser apenas "lucas" ou "meu nome é lucas" é uma apresentação.
        
        FLUXO DE EXECUÇÃO:
        CASO 1: A primeira mensagem do cliente é SÓ um cumprimento (ex: "Oi", "Bom dia", "Tudo bem?").
        1.  **Sua Resposta (Apresentação Natural):**
            - Cumprimente (use {saudacao} se for adequado).
            - Responda a perguntas como "Tudo bem?" de forma natural.
            - Apresente-se ("Eu sou Lyra, da Neuro'Up Soluções") e coloque-se à disposição.

        CASO 2: O cliente JÁ FAZ UMA PERGUNTA (ex: "quanto custa?", "como funciona?", "vi no instagram").
        1.  **Sua Resposta (Focada SÓ no Nome):**
            - Conecte-se BREVEMENTE com a pergunta (ex: "Que ótimo que nos viu no Instagram!", "Claro, já te falo sobre...").
            - **REGRA CRÍTICA DESTE CASO:** NÃO pergunte sobre o "negócio" do cliente. NÃO pergunte "como posso ajudar?". Sua única pergunta DEVE ser pelo nome.
            - **Exemplo Correto:** "Que legal que nos viu no Instagram! Pra eu poder te ajudar melhor, como é seu nome mesmo?"
            - **Exemplo Errado:** "Que ótimo!... poderia me contar... sobre o seu negócio?"
            - **NÃO FAÇA MAIS NADA.** Pare e espere o nome.

        DEPOIS QUE VOCÊ PEDIR O NOME (Fluxo do CASO 2):
        - O cliente vai responder com o nome (ex: "Meu nome é Marcos", "lucas", "dani").
        - **REGRA DE PALAVRA ÚNICA (CRÍTICO):** Se o cliente responder com uma única palavra e for um nome ou algo que pareça uma apresentação (ex: "sabrina", "daniel"), você DEVE assumir que essa é a resposta para sua pergunta ("como posso te chamar?").
        - **Sua Próxima Ação (Tool Call OBRIGATÓRIA):**
            1. Sua **ÚNICA** ação neste momento deve ser chamar a ferramenta `fn_capturar_nome`.
            2. Você **NÃO DEVE** gerar nenhum texto de saudação (como "Prazer, Marcos!"). Apenas chame a ferramenta.
            3. **REGRA ANTI-DUPLICAÇÃO:** Ao extrair o nome, NUNCA o combine com o `{sender_name}`. Se o cliente digitou "dani", a ferramenta deve ser chamada com `nome_extraido="dani"`.

        QUANDO A FERRAMENTA `fn_capturar_nome` RETORNAR SUCESSO (ex: `{{"sucesso": true, "nome_salvo": "Dani"}}`):
        - **Agora sim, sua próxima resposta DEVE:**
            1. Saudar o cliente pelo nome que a ferramenta salvou (ex: "Prazer, Dani!").
            2. **RESPONDER IMEDIATAMENTE** à pergunta original que o cliente tinha feito (a pergunta que você guardou na memória antes de pedir o nome).
        
        **RESUMO:** Se o nome não é conhecido, `prompt_name_instruction` é a única regra. Se o nome é conhecido, o `prompt_final` (o resto do prompt) é ativado.
        """

    # ==========================================================
    # PARTE 2: PROMPT PRINCIPAL (QUANDO O NOME JÁ É CONHECIDO)
    # ==========================================================
    prompt_final = f"""
        A data e hora atuais são: {horario_atual}. (Use {saudacao} para cumprimentar no início).
        
        =====================================================
        🧠 FILOSOFIA DE ATENDIMENTO (O MAIS IMPORTANTE)
        =====================================================
        Sua personalidade é a de Lyra: uma especialista humana, calma, fluida e objetiva.
        1.  **SEJA CONCISA:** Use frases curtas. Evite "enrolar".
        2.  **SEJA FLUIDA:** Não siga um script. Adapte-se ao cliente.
        3.  **NÃO REPITA (MUITO IMPORTANTE):** Evite saudações ("Olá") repetidas. Acima de tudo, **NÃO use o nome do cliente em todas as frases.** Isso soa robótico e irritante. Use o nome dele UMA vez na saudação e depois **use o nome DE FORMA ESPORÁDICA**, apenas quando for natural e necessário, como faria um humano.
        4.  **REGRA MESTRA DE CONHECIMENTO:** Você é Lyra, uma IA. Você NUNCA deve inventar informações técnicas sobre como a plataforma funciona . Para perguntas técnicas complexas , sua resposta deve instruir para falar com o Lucas , e perguntar se quer falar agora, marcar uma reunião ou tem mais alguma duvida?"

        =====================================================
        🆘 REGRAS DE FUNÇÕES (TOOLS) - PRIORIDADE ABSOLUTA
        =====================================================
        Você tem ferramentas para executar ações. NUNCA execute uma ação sem usar a ferramenta.

        - **REGRA MESTRA ANTI-ALUCINAÇÃO (O BUG "Danidani" / "CPF Duplicado"):**
        - Esta é a regra mais importante. O seu bug é "pensar" sobre os dados antes de agir.
        - Quando você pede um dado (Nome ou CPF) e o cliente responde (ex: "dani" ou "10062080970"), sua **ÚNICA** tarefa é executar a próxima ação do fluxo **IMEDIATAMENTE**.
        - **NUNCA, JAMAIS, SOB NENHUMA HIPÓTESE,** valide, comente, analise ou repita o dado que o cliente enviou.
        - **FLUXO CORRETO (Sem Pensar):**
        -   Você: "...qual seu CPF, por favor?"
        -   Cliente: "10062080970"
        -   Você (Próxima Ação IMEDIATA): "Certo. E o telefone, posso usar este mesmo?" (Se for agendamento)
        -   *OU*
        -   Você (Próxima Ação IMEDIATA): [Chama a ferramenta `fn_buscar_por_cpf`] (Se for exclusão)
        - **FLUXO ERRADO (O BUG):**
        -   Você: "...qual seu CPF, por favor?"
        -   Cliente: "10062080970"
        -   Você: "Danidani, o CPF que você me passou..." <-- (ERRADO! VOCÊ PENSOU!)

        - **REGRA DE AÇÃO IMEDIATA (CRÍTICO):**
        - NUNCA termine sua resposta dizendo que "vai verificar" (ex: "Vou verificar a disponibilidade..."). Isso é um ERRO GRAVE. A conversa morre.
        - Se você tem os dados suficientes para usar uma ferramenta (ex: o cliente disse "amanhã depois das 3"), você DEVE:
            1. Chamar a ferramenta `fn_listar_horarios_disponiveis` IMEDIATAMENTE.
            2. **Formular sua resposta para o cliente JÁ COM OS HORÁRIOS VAGOS.**

        - **REGRA DE CONFIRMAÇÃO (CRÍTICO - ANTI-BUG):**
        - Você NUNCA deve confirmar uma ação (salvar, alterar, excluir) sem ANTES ter chamado a ferramenta e recebido uma resposta de 'sucesso'.
        - Sua resposta DEVE ser baseada no JSON de resultado da ferramenta.
        - Se a ferramenta retornar `{{"sucesso": true, "msg": "Excluído."}}`, sua resposta é "Perfeito! Excluído com sucesso."
        - Se a ferramenta retornar `{{"erro": "Não encontrado."}}`, sua resposta é "Estranho, não encontrei esse agendamento, pode confirmar?."

        - **REGRA DE AMBIGUIDADE (CRÍTICO - ANTI-BUG):**
        - Se o cliente (descoberto via `fn_buscar_por_cpf`) tem MAIS DE UM agendamento e pede para "cancelar" ou "alterar", você DEVE perguntar QUAL agendamento.
        - NÃO assuma qual é. (Exemplo correto: "Claro. Você tem dois agendamentos: [lista]. Qual deles você quer cancelar?")

        REGRA DE INTERVENÇÃO (APÓS A OFERTA DE LUCAS): Esta regra SÓ é aplicada após a oferta de "falar com Lucas agora OU agendar reunião". Em qualquer outro contexto, se o cliente pedir por Lucas, use sempre fn_solicitar_intervencao.
            INTENÇÃO DE AGENDAMENTO: Se o cliente usar palavras como "reunião", "marcar", "agendar", "amanhã" ou horários, sua intenção é AGENDAR. Você DEVE usar a ferramenta fn_listar_horarios_disponiveis.
            INTENÇÃO DE INTERVENÇÃO IMEDIATA: Você SÓ DEVE usar a ferramenta fn_solicitar_intervencao se o cliente pedir expressamente para falar com o Lucas AGORA ("chama ele agora", "me passa pra ele", "urgente").
            AMBIGUIDADE: Se o cliente disser apenas "sim" ou "pode ser" após a oferta, pergunte: "Perfeito. Você prefere que eu chame o Lucas agora, ou que eu agende a reunião para amanhã?" para confirmar a intenção.

        1.  **INTERVENÇÃO HUMANA (Falar com Lucas, ou o dono.):**
            - SE a mensagem do cliente contiver PEDIDO para falar com "Lucas" (ex: "quero falar com o Lucas", "falar com o dono", "chama o Lucas agora").
            - Você DEVE chamar a função `fn_solicitar_intervencao` com o motivo.
            - **EXCEÇÃO:** Se o cliente APENAS se apresentar com o nome "Lucas", ou disser algo que nao pareca que quer falar com o dono (ex: "lucas sei la", "lucas2"), ISSO NÃO É UMA INTERVENÇÃO. Você deve chamar `fn_capturar_nome`.

        2.  **CAPTURA DE NOME:**
            - {prompt_name_instruction}

        3.  **AGENDAMENTO DE REUNIÃO:**
            - Seu dever é agendar reuniões com o proprietário (Lucas).
            - O serviço padrão é "reunião" (30 min). 
            - **FLUXO OBRIGATÓRIO DE AGENDAMENTO (AÇÃO IMEDIATA):**
            - a. Se o usuário pedir "quero agendar":
            - b. PRIMEIRO, avise que a reunião é de até meia hora.
            - c. SEGUNDO, pergunte a **DATA**.
            - d. **QUANDO TIVER A DATA (AÇÃO IMEDIATA):**
            -    1. Chame a `fn_listar_horarios_disponiveis` IMEDIATAMENTE.
            -    (Se o cliente der um filtro, como "depois do almoço", você chama a função para o dia TODO, recebe a lista completa, e APRESENTA para o cliente apenas os horários filtrados (ex: >= 13:00), já agrupados.)
            - e. **Formatação da Lista (CRÍTICO):** NUNCA liste todos os horários um por um (ex: 13:00, 13:30, 14:00...). Isso é um ERRO. Se houver 3 ou mais horários seguidos, **SEMPRE** agrupe-os. (Ex: "Tenho horários das 13:00 às 17:30." ou "Temos horários de manhã, das 08:00 às 10:30, e à tarde, das 14:00 às 16:00.").
            - f. Quando o cliente escolher um horário VÁLIDO:
            - g. **COLETA DE DADOS (CURTA):**
            -    1. "Perfeito. Para registrar, qual seu CPF, por favor?"
            -    2. **(Ação Pós-CPF):** Assim que o cliente responder o CPF, você deve obedecer a "REGRA MESTRA ANTI-ALUCINAÇÃO" e IMEDIATAMENTE fazer a próxima pergunta: "E o telefone, posso usar este mesmo?"
            - h. **REGRA DO TELEFONE (IMPORTANTE):** O número de telefone atual deste cliente (o clean_number) é **{clean_number}**. 
            -    - Se o cliente disser 'sim' (ou 'pode ser', 'este mesmo'), você DEVE usar o placeholder `telefone="CONFIRMADO_NUMERO_ATUAL"` ao chamar a `fn_salvar_agendamento`. (O backend vai salvar o {clean_number} corretamente).
            -    - Se o cliente disser 'não' e passar um NÚMERO NOVO (ex: "449888..."), você deve usar esse número novo (ex: `telefone="449888..."`).

            - i. **CONFIRMAÇÃO (GABARITO CURTO):**
            -    1. Apresente o resumo COMPLETO. (Lembre-se, o serviço padrão que você está agendando é 'reunião', a menos que outro tenha sido especificado pelo cliente).
            -       * Nome: (Insira o nome que o cliente informou)
            -       * CPF: (Insira o CPF que o cliente informou)
            -       * Telefone: (Se o cliente disse 'sim' para usar o número atual, mostre o número {clean_number}. Se ele passou um número novo, mostre o número novo que ele digitou.)
            -       * Serviço: (Insira aqui o nome do serviço que você está agendando, ex: Reunião)
            -       * Data: (Insira a data e hora escolhidas)
            -    2. Pergunte: "Confere pra mim? Se estiver tudo certo, eu confirmo aqui."
            - j. SÓ ENTÃO, após a confirmação, chame `fn_salvar_agendamento`.
            
            - k. **FLUXO DE ALTERAÇÃO/EXCLUSÃO:**
            -    1. Se o cliente pedir para alterar/cancelar (ex: "quero excluir os meus horarios"), peça o CPF: "Claro. Qual seu CPF, por favor?"
            -    2. **(Ação Pós-CPF):** Assim que o cliente responder o CPF (ex: "10062080970"), você deve obedecer a "REGRA MESTRA ANTI-ALUCINAÇÃO" e IMEDIATAMENTE chamar a ferramenta `fn_buscar_por_cpf`.
            -    3. (Depois que a ferramenta retornar):
            -       - Se houver SÓ UM agendamento, pergunte se quer excluí-lo/alterá-lo.
            -       - Se houver MAIS DE UM (ex: 2), obedeça à "REGRA DE AMBIGUIDADE": Liste os 2 e pergunte se quer excluir "apenas um" ou "todos".
            -    4. **(SE EXCLUIR TODOS):** Se o cliente disser "todos" ou "os 2", chame `fn_excluir_TODOS_agendamentos` com o CPF.
            -    5. **(SE EXCLUIR UM):** Se o cliente apontar um (ex: "o das 8h"), chame `fn_excluir_agendamento` com os dados (cpf, data, hora) daquele agendamento.
            -    6. **(SE ALTERAR):** Se o cliente quiser alterar, peça a nova data/hora e siga o fluxo de alteração (chame `fn_listar_horarios_disponiveis` para a nova data, etc.).
        =====================================================
        🏢 IDENTIDADE DA EMPRESA (Neuro'Up Soluções)
        =====================================================
        nome da empresa: {{Neuro'Up Soluções em Tecnologia}}
        setor: {{Tecnologia e Automação}} 
        missão: {{Facilitar e organizar empresas com automação e IA.}}
        horário de atendimento: {{De segunda a sexta, das 8:00 às 18:00.}}
        
        =====================================================
        🏷️ IDENTIDADE DO ATENDENTE (Lyra)
        =====================================================
        nome: {{Lyra}}
        função: {{Atendente e secretária especialista em automação.}} 
        personalidade: {{Profissional, alegre e muito humana. Falo de forma calma e fluida. Sou objetiva, mas empática. Uso frases curtas e diretas. Uso emojis com moderação (máx 1 ou 2).}}
        USO DO NOME (CRÍTICO): Use o nome do cliente de forma ESPORÁDICA, a cada 3 ou 4 turnos. REGRAS RÍGIDAS DE EVASÃO:
            NUNCA use o nome em frases de confirmação simples (ex: "Perfeito, Sabrina!", "Maravilha, Sabrina!").
            NUNCA use o nome se ele já foi usado na mensagem anterior.
        **ESTILO DE CONFIRMAÇÃO:** Mantenha as confirmações curtas, profissionais e amigáveis. Prefira confirmar o recebimento do dado (Ex: "Certo. Qual a data?"), ou use interjeições concisas e amigáveis (Ex: "Maravilha!", "Perfeito!", "Combinado.").
        =====================================================
        💼 SERVIÇOS / CARDÁPIO (Vendas)
        =====================================================
        Use estas descrições curtas primeiro. Elabore *apenas* se o cliente pedir mais detalhes.
        
        - **Plano Atendente:** {{Uma atendente 24/7 treinada para seu negócio, que responde clientes, filtra vendas e pode notificar sua equipe (intervenção) ou enviar pedidos para outros números (bifurcação).}}
        - **Plano Secretário:** {{Tudo do Plano Atendente, mais uma agenda inteligente completa que marca, altera e gerencia seus compromissos, com um app para você acompanhar tudo.}}
        
        =====================================================
        🧭 ESTRATÉGIA DE CONVERSA E VENDAS (FLUXO NATURAL)
        =====================================================
        Seu objetivo é ser uma assistente prestativa, não uma vendedora robótica.
        
        1.  **TRANSIÇÃO PÓS-NOME:** (Se o cliente já fez uma pergunta).
            - Use uma transição natural. Responda imediatamente.
        
        2.  **SONDAGEM DE NEGÓCIO (ESSENCIAL):**
            - Pergunte de forma despretensiosa sobre o negócio do cliente, pra poder usar na converssa.
            
        3.  **CONEXÃO (PLANO + EXEMPLO):**
            - Explique o plano (Atendente ou Secretário) e conecte-o ao negocio dele.
            - **Exemplo de como usar (Curto):** Se ele disser "Sou da cozinha", responda "Legal! Para quem é da cozinha, o Plano Atendente com bifurcação é ótimo. Imagina ele recebendo o pedido e já enviando para o WhatsApp da cozinha, tudo automático."
            
        4.  **CHECK-IN (NÃO PULE ESSA ETAPA):**
            - **NÃO PULE PARA O AGENDAMENTO AINDA.** Antes, verifique se o cliente entendeu e se interessou de maneira com suporte para o cliente ver que voce quer ajudar ele.
            - Se mantenha nesta etapa ate a pessoa mostrar que realmente entendeu.

        5.  **OFERTA DA REUNIÃO (SÓ APÓS O CHECK-IN):**
            - Quando o cliente mostrar interesse (ex: "sim", "faz sentido", "pode ser"), aí sim ofereça a reunião.
            - **Exemplo:** "Que ótimo! Como nossos planos são 100% personalizados, o ideal é marcarmos uma conversa com o proprietário, o Lucas. Ele entende sua necessidade e te apresenta a melhor solução. **Se quiser falar com ele agora, é só me avisar.**"
            - **(Se o cliente aceitar falar agora, chame `fn_solicitar_intervencao` com o motivo 'Cliente aceitou oferta de falar com Lucas'.)**

        =====================================================
        🧩 TÉCNICAS DE OBJEÇÕES (CURTAS E DIRETAS)
        =====================================================
        
        ### 💬 1. QUANDO O CLIENTE PERGUNTA O PREÇO 
        - **NÃO INFORME VALORES.**
        - **Resposta Natural:** "Entendo. Como cada projeto é personalizado, o valor depende do seu negócio. O ideal é conversar com o Lucas (proprietário) para ele entender sua necessidade."
        - **Ofereça as Opções:** "Você tem urgência? Posso tentar chamá-lo agora. Ou, se preferir, podemos agendar uma reunião com calma. O que é melhor para você?"
        
        - **SE ESCOLHER 'FALAR AGORA' (Urgência):** Chame `fn_solicitar_intervencao` (Motivo: "Cliente pediu para falar com Lucas sobre preços").
        - **SE ESCOLHER 'AGENDAR':** Inicie o fluxo de agendamento (Ex: "Ótimo! Para qual data você gostaria de verificar a disponibilidade?").
        
        ### 💡 2. QUANDO O CLIENTE DIZ “VOU PENSAR” (DEPOIS DA OFERTA DA REUNIÃO)
        > “Perfeito, é bom pensar mesmo! Posso te perguntar o que você gostaria de analisar melhor? Assim vejo se consigo te ajudar com alguma dúvida antes de marcarmos.”
    """
    return prompt_final

def handle_tool_call(call_name: str, args: Dict[str, Any], contact_id: str) -> str:
    """
    Processa a chamada de ferramenta vinda da IA.
    NOTA: 'agenda_instance' e 'conversation_collection' são globais.
    """
    global agenda_instance, conversation_collection
    
    try:
        # --- Ferramentas da AGENDA ---
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
            
            # Se a IA usou o placeholder, troque pelo contact_id (clean_number)
            if telefone_arg == "CONFIRMADO_NUMERO_ATUAL":
                telefone_arg = contact_id 
                print(f"ℹ️ Placeholder 'CONFIRMADO_NUMERO_ATUAL' detectado. Usando o contact_id: {contact_id}")
            # --- FIM DA MODIFICAÇÃO ---

            resp = agenda_instance.salvar(
                nome=args.get("nome", ""),
                cpf_raw=args.get("cpf", ""),
                telefone=telefone_arg, # Use a variável modificada
                servico=args.get("servico", ""),
                data_str=args.get("data", ""),
                hora_str=args.get("hora", "")
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
                    # 1. Tenta dividir por espaço (Ex: "Daniel Daniel")
                    palavras = nome_bruto.split()
                    if len(palavras) >= 2 and palavras[0].lower() == palavras[1].lower():
                        nome_limpo = palavras[0].capitalize() # Pega só o primeiro
                        print(f"--- [DEBUG ANTI-BUG] Corrigido (Espaço): '{nome_bruto}' -> '{nome_limpo}'")

                    # 2. SE NÃO FOR O CASO 1, checa se é uma palavra só duplicada (Ex: "Danieldaniel")
                    # Esta é a lógica nova e crucial
                    else:
                        l = len(nome_bruto)
                        if l > 2 and l % 2 == 0: # Se for par e maior que 2
                            metade1 = nome_bruto[:l//2]
                            metade2 = nome_bruto[l//2:]
                            
                            # Se as metades forem IDÊNTICAS
                            if metade1.lower() == metade2.lower():
                                nome_limpo = metade1.capitalize() # Pega só a primeira metade
                                print(f"--- [DEBUG ANTI-BUG] Corrigido (Sem Espaço): '{nome_bruto}' -> '{nome_limpo}'")
                            else:
                                # Se não for duplicado, só capitaliza o que veio
                                nome_limpo = " ".join([p.capitalize() for p in palavras])
                        else:
                            # Se for ímpar ou não duplicado, só capitaliza
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

        else:
            return json.dumps({"erro": f"Ferramenta desconhecida: {call_name}"}, ensure_ascii=False)
            
    except Exception as e:
        log_info(f"Erro fatal em handle_tool_call ({call_name}): {e}")
        return json.dumps({"erro": f"Exceção ao processar ferramenta: {e}"}, ensure_ascii=False)


def gerar_resposta_ia_com_tools(contact_id, sender_name, user_message, known_customer_name): 
    """
    (VERSÃO FINAL - COM TOOLS E CONTAGEM DE TOKENS)
    Esta função agora gerencia o loop de ferramentas.
    """
    global modelo_ia 

    if modelo_ia is None:
        return "Desculpe, estou com um problema interno (modelo IA não carregado)."
    if conversation_collection is None:
        return "Desculpe, estou com um problema interno (DB de conversas não carregado)."

    total_tokens_this_turn = 0

    convo_data = load_conversation_from_db(contact_id)
    old_history_gemini_format = []
    
    if convo_data:
        # read saved name from DB (se houver)
        known_customer_name = convo_data.get('customer_name', known_customer_name) 
        history_from_db = convo_data.get('history', [])
        
        for msg in history_from_db:
            role = msg.get('role', 'user')
            if role == 'assistant':
                role = 'model'
            
            if 'text' in msg:
                if msg['text'].startswith("Chamando função:") or msg['text'].startswith("Resultado da função:"):
                    continue
                
                old_history_gemini_format.append({
                    'role': role,
                    'parts': [msg['text']]
                })

    def _normalize_name(n: Optional[str]) -> Optional[str]:
        if not n:
            return None
        s = str(n).strip()
        if not s:
            return None
        parts = [p for p in re.split(r'\s+', s) if p]
        if len(parts) >= 2 and parts[0].lower() == parts[1].lower():
            return parts[0]
        return s

    sender_name = _normalize_name(sender_name) or ""
    known_customer_name = _normalize_name(known_customer_name) 
    
    if known_customer_name:
        print(f"👤 Cliente já conhecido (nome real): {known_customer_name}")
    else:
        print(f"👤 Cliente novo. Sender_name (ignorar na saudação): {sender_name}")

    try:
        fuso_horario_local = pytz.timezone('America/Sao_Paulo')
        agora_local = datetime.now(fuso_horario_local)
        horario_atual = agora_local.strftime("%Y-%m-%d %H:%M:%S")
        
        hora_do_dia = agora_local.hour
        if 5 <= hora_do_dia < 12:
            saudacao = "Bom dia"
        elif 12 <= hora_do_dia < 18:
            saudacao = "Boa tarde"
        else:
            saudacao = "Boa noite"
        
    except Exception as e:
        horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        saudacao = "Olá" 

    system_instruction = get_system_prompt_unificado(
        saudacao, 
        horario_atual,
        known_customer_name, 
        sender_name,  
        contact_id
    )

    try:
        modelo_com_sistema = genai.GenerativeModel(
            modelo_ia.model_name,
            system_instruction=system_instruction,
            tools=tools # Passa as tools globais
        )
        
        chat_session = modelo_com_sistema.start_chat(history=old_history_gemini_format) 
        
        # Log mais claro (agora usa 'known_customer_name' ou 'sender_name' corretamente)
        log_display = known_customer_name or sender_name or contact_id
        print(f"Enviando para a IA: '{user_message}' (De: {log_display})")
        
        resposta_ia = chat_session.send_message(user_message)

        try:
            total_tokens_this_turn += resposta_ia.usage_metadata.total_token_count
        except Exception as e:
            print(f"Aviso: Não foi possível somar tokens (chamada inicial): {e}")

        while True:
            cand = resposta_ia.candidates[0]
            func_call = None
            try:
                func_call = cand.content.parts[0].function_call
            except Exception:
                func_call = None

            if not func_call or not getattr(func_call, "name", None):
                break # Sai do loop

            call_name = func_call.name
            call_args = {key: value for key, value in func_call.args.items()}
            
            log_info(f"🔧 IA chamou a função: {call_name} com args: {call_args}")
            append_message_to_db(contact_id, 'assistant', f"Chamando função: {call_name}({call_args})")

            resultado_json_str = handle_tool_call(call_name, call_args, contact_id)
            log_info(f"📤 Resultado da função: {resultado_json_str}")
            
            try:
                resultado_data = json.loads(resultado_json_str)
                if resultado_data.get("tag_especial") == "[HUMAN_INTERVENTION]":
                    print("‼️ Intervenção detectada pela Tool. Encerrando o loop.")
                    return f"[HUMAN_INTERVENTION] Motivo: {resultado_data.get('motivo', 'Solicitado pelo cliente.')}"
            except Exception:
                pass 

            resposta_ia = chat_session.send_message(
                [genai.protos.FunctionResponse(name=call_name, response={"resultado": resultado_json_str})]
            )
            
            try:
                total_tokens_this_turn += resposta_ia.usage_metadata.total_token_count
            except Exception as e:
                print(f"Aviso: Não foi possível somar tokens (loop de ferramenta): {e}")

        ai_reply_text = ""
        try:
            # Tentativa 1: Acessar .text (o mais comum)
            ai_reply_text = resposta_ia.text
        except Exception as e1:
            #
            # ▼▼▼ DEBUG ADICIONADO ▼▼▼
            print(f"--- [DEBUG EXCEÇÃO 1] Falha ao ler .text. Erro: {e1}")
            # ▲▲▲ FIM DO DEBUG ▲▲▲
            #
            try:
                # Tentativa 2: Acessar a estrutura interna (parts)
                ai_reply_text = resposta_ia.candidates[0].content.parts[0].text
            except Exception as e2:
                #
                # ▼▼▼ DEBUG ADICIONADO ▼▼▼
                print(f"--- [DEBUG EXCEÇÃO 2] Falha ao ler .parts[0].text. Erro: {e2}")
                print(f"--- [DEBUG EXCEÇÃO 2] Objeto 'resposta_ia' completo: {resposta_ia}")
                # ▲▲▲ FIM DO DEBUG ▲▲▲
                #
                ai_reply_text = "Pode ser mais claro?" # Fallback final

        save_conversation_to_db(contact_id, sender_name, known_customer_name, total_tokens_this_turn)
        print(f"🔥 Tokens consumidos nesta rodada para {contact_id}: {total_tokens_this_turn}")
        
        return ai_reply_text
    
    except Exception as e:
        print(f"❌ Erro ao comunicar com a API do Gemini (loop de tools): {e}")
        return "Desculpe, estou com um problema técnico no momento (IA_TOOL_FAIL). Por favor, tente novamente em um instante."

def transcrever_audio_gemini(caminho_do_audio):
    global modelo_ia 
    
    if not modelo_ia:
        print("❌ Modelo de IA não inicializado. Impossível transcrever.")
        return None
    
    print(f"🎤 Enviando áudio '{caminho_do_audio}' para transcrição no Gemini...")
    try:
        audio_file = genai.upload_file(
            path=caminho_do_audio,
            mime_type="audio/ogg" 
        )
        
        # CORRIGIDO: Usando 'modelo_ia' (o global)
        response = modelo_ia.generate_content(["Por favor, transcreva o áudio a seguir.", audio_file])
        genai.delete_file(audio_file.name)
        
        if response.text:
            print(f"✅ Transcrição recebida: '{response.text}'")
            return response.text
        else:
            print("⚠️ A IA não retornou texto para o áudio. Pode ser um áudio sem falas.")
            return None
    except Exception as e:
        print(f"❌ Erro ao transcrever áudio com Gemini: {e}")
        return None

def send_whatsapp_message(number, text_message):
    INSTANCE_NAME = "chatbot" 
    clean_number = number.split('@')[0]
    payload = {"number": clean_number, "textMessage": {"text": text_message}}
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
        print(f"✅ Enviando resposta para a URL: {final_url} (Destino: {clean_number})")
        response = requests.post(final_url, json=payload, headers=headers)
        
        if response.status_code < 400:
            print(f"✅ Resposta da IA enviada com sucesso para {clean_number}\n")
        else:
            print(f"❌ ERRO DA API EVOLUTION ao enviar para {clean_number}: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Erro de CONEXÃO ao enviar mensagem para {clean_number}: {e}")

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

scheduler = BackgroundScheduler(daemon=True, timezone='America/Sao_Paulo')
scheduler.start()

app = Flask(__name__)
processed_messages = set() 

@app.route('/webhook', methods=['POST'])
def receive_webhook():
    data = request.json

    event_type = data.get('event')
    if event_type and event_type != 'messages.upsert':
        # print(f"➡️  Ignorando evento: {event_type}")
        return jsonify({"status": "ignored_event_type"}), 200

    try:
        message_data = data.get('data', {}) 
        if not message_data:
            message_data = data
            
        key_info = message_data.get('key', {})
        if not key_info:
            return jsonify({"status": "ignored_no_key"}), 200

        if key_info.get('fromMe'):
            sender_number_full = key_info.get('remoteJid')
            if not sender_number_full:
                return jsonify({"status": "ignored_from_me_no_sender"}), 200
            
            clean_number = sender_number_full.split('@')[0]
            
            if clean_number != RESPONSIBLE_NUMBER:
                return jsonify({"status": "ignored_from_me"}), 200
            
            print(f"⚙️  Mensagem do próprio bot PERMITIDA (é um comando do responsável: {clean_number}).")

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
        print("DADO QUE CAUSOU ERRO:", data)
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
            send_whatsapp_message(responsible_number, "✅ *Bot REATIVADO.* O bot está respondendo aos clientes normally.")
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
    # ...
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

        # --- Lógica de LOCK ---
        now = datetime.now()
        res = conversation_collection.update_one(
            {'_id': clean_number, 'processing': {'$ne': True}},
            {'$set': {'processing': True, 'processing_started_at': now}},
            upsert=True 
        )

        if res.matched_count == 0 and res.upserted_id is None:
            print(f"⏳ {clean_number} já está sendo processado (lock). Reagendando...")
            if buffered_message_text:
                if clean_number not in message_buffer: message_buffer[clean_number] = []
                message_buffer[clean_number].insert(0, buffered_message_text)
            
            timer = threading.Timer(10.0, _trigger_ai_processing, args=[clean_number, message_data])
            message_timers[clean_number] = timer
            timer.start()
            return 
        
        lock_acquired = True
        # --- Fim do Lock ---
        
        user_message_content = None
        
        if buffered_message_text:
            user_message_content = buffered_message_text
            messages_to_save = user_message_content.split(". ")
            for msg_text in messages_to_save:
                if msg_text and msg_text.strip():
                    append_message_to_db(clean_number, 'user', msg_text)
        else:
            # --- INÍCIO DA CORREÇÃO DE INDENTAÇÃO ---
            message = message_data.get('message', {})
            if message.get('audioMessage') and message.get('base64'):
                message_id = key_info.get('id')
                print(f"🎤 Mensagem de áudio recebida de {clean_number}. Transcrevendo...")
                audio_base64 = message['base64']
                audio_data = base64.b64decode(audio_base64)
                os.makedirs("/tmp", exist_ok=True) 
                temp_audio_path = f"/tmp/audio_{clean_number}_{message_id}.ogg"
                with open(temp_audio_path, 'wb') as f: f.write(audio_data)
                
                user_message_content = transcrever_audio_gemini(temp_audio_path)
                
                try:
                    os.remove(temp_audio_path)
                except Exception as e:
                    print(f"Aviso: não foi possível remover áudio temporário. {e}")

                if not user_message_content:
                    send_whatsapp_message(sender_number_full, "Desculpe, não consegui entender o áudio. Pode tentar novamente? 🎧")
                    user_message_content = "[Usuário enviou um áudio incompreensível]"
            
            # Estas duas linhas foram movidas PARA DENTRO do 'else'
            if not user_message_content:
                user_message_content = "[Usuário enviou uma mensagem não suportada]"
                
            append_message_to_db(clean_number, 'user', user_message_content)
            # --- FIM DA CORREÇÃO DE INDENTAÇÃO ---

        print(f"🧠 Processando Mensagem de {clean_number}: '{user_message_content}'")
        
        # --- LÓGICA DE INTERVENÇÃO (Verifica se é o Admin) ---
        if RESPONSIBLE_NUMBER and clean_number == RESPONSIBLE_NUMBER:
            if handle_responsible_command(user_message_content, clean_number):
                return # 'finally' vai liberar o lock

        # --- LÓGICA DE "BOT LIGADO/DESLIGADO" ---
        try:
            bot_status_doc = conversation_collection.find_one({'_id': 'BOT_STATUS'})
            is_active = bot_status_doc.get('is_active', True) if bot_status_doc else True 
            
            if not is_active:
                print(f"🤖 Bot está em standby (desligado). Ignorando mensagem de {sender_name_from_wpp} ({clean_number}).")
                return # 'finally' vai liberar o lock
                
        except Exception as e:
            print(f"⚠️ Erro ao verificar o status do bot: {e}. Assumindo que está ligado.")

        conversation_status = conversation_collection.find_one({'_id': clean_number})

        if conversation_status and conversation_status.get('intervention_active', False):
            print(f"⏸️  Conversa com {sender_name_from_wpp} ({clean_number}) pausada para atendimento humano.")
            return # 'finally' vai liberar o lock

        known_customer_name = conversation_status.get('customer_name') if conversation_status else None
        
        log_info(f"[DEBUG RASTREIO | PONTO 2] Conteúdo final para IA (Cliente {clean_number}): '{user_message_content}'")

        ai_reply = gerar_resposta_ia_com_tools(
            clean_number,
            sender_name_from_wpp,
            user_message_content,
            known_customer_name
        )
        
        if not ai_reply:
            print("⚠️ A IA não gerou resposta.")
            return # 'finally' vai liberar o lock

        try:
            append_message_to_db(clean_number, 'assistant', ai_reply)
            
            # --- LÓGICA DE INTERVENÇÃO (Pós-IA) ---
            if ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
                print(f"‼️ INTERVENÇÃO HUMANA SOLICITADA para {sender_name_from_wpp} ({clean_number})")
                
                conversation_collection.update_one(
                    {'_id': clean_number}, {'$set': {'intervention_active': True}}, upsert=True
                )
                
                send_whatsapp_message(sender_number_full, "Só mais um instante, o Lucas já vai falar com você 🙏. ")
                
                if RESPONSIBLE_NUMBER:
                    reason = ai_reply.replace("[HUMAN_INTERVENTION] Motivo:", "").strip()
                    display_name = known_customer_name or sender_name_from_wpp
                    
                    history_summary = "Nenhum histórico de conversa encontrado."
                    if conversation_status:
                        history_com_ultima_msg = load_conversation_from_db(clean_number).get('history', [])
                        history_summary = get_last_messages_summary(history_com_ultima_msg)

                    notification_msg = (
                        f"🔔 *NOVA SOLICITAÇÃO DE ATENDIMENTO HUMANO* 🔔\n\n"
                        f"👤 *Cliente:* {display_name}\n"
                        f"📞 *Número:* `{clean_number}`\n\n"
                        f"💬 *Motivo da Chamada:*\n_{reason}_\n\n"
                        f"-----------------------------------\n"
                        f"*AÇÃO NECESSÁRIA:*\nApós resolver, envie para *ESTE NÚMERO* o comando:\n`ok {clean_number}`\n"
                        f"-----------------------------------\n"
                        f"📜 *Resumo da Conversa:*\n{history_summary}\n\n"
                        
                    )
                    send_whatsapp_message(f"{RESPONSIBLE_NUMBER}@s.whatsapp.net", notification_msg)
            
            else:
                # (Envio de resposta normal - AGORA FRACIONADO)
                print(f"🤖  Resposta da IA (Fracionada) para {sender_name_from_wpp}: {ai_reply}")
                
                # Quebra a resposta da IA por quebras de linha (parágrafos)
                paragraphs = [p.strip() for p in ai_reply.split('\n') if p.strip()]

                if not paragraphs:
                    print(f"⚠️ IA gerou uma resposta vazia após o split para {sender_name_from_wpp}.")
                    return # 'finally' vai liberar o lock
                
                for i, para in enumerate(paragraphs):
                    # Envia o parágrafo atual
                    send_whatsapp_message(sender_number_full, para)
                    

                    if i < len(paragraphs) - 1:
                        time.sleep(2.0) # A pausa de 2 segundos que você pediu

        except Exception as e:
            print(f"❌ Erro ao processar envio ou intervenção: {e}")
            send_whatsapp_message(sender_number_full, "Desculpe, tive um problema ao processar sua resposta. (Erro interno: SEND_LOGIC)")

    except Exception as e:
        print(f"❌ Erro fatal ao processar mensagem: {e}")
    finally:
        # --- Libera o Lock ---
        if clean_number and lock_acquired and conversation_collection is not None:
            conversation_collection.update_one(
                {'_id': clean_number},
                {'$unset': {'processing': "", 'processing_started_at': ""}}
            )
            # print(f"🔓 Lock liberado para {clean_number}.")

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
    # --- FIM DA ALTERAÇÃO ---
    
    import atexit
    atexit.register(lambda: scheduler.shutdown())
    
else:
    print("\nEncerrando o programa devido a erros na inicialização (Verifique APIs e DBs).")
    # (O programa não deve continuar se os componentes principais falharem)
    exit() # Encerra se o modelo ou DBs falharem

if __name__ == '__main__':
    print("Iniciando em MODO DE DESENVOLVIMENTO LOCAL (app.run)...")
    port = int(os.environ.get("PORT", 8000))
    # Desative o 'debug=True' em produção. Use 'debug=False'.
    app.run(host='0.0.0.0', port=port, debug=False)