
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

CLIENT_NAME="Neuro'up Soluções em Tecnologia"
RESPONSIBLE_NUMBER="554898389781" 

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
BUFFER_TIME_SECONDS=8
TEMPO_FOLLOWUP_MINUTOS = 2

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

    def salvar(self, nome: str, cpf_raw: str, telefone: str, servico: str, data_str: str, hora_str: str) -> Dict[str, Any]:
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

            # ==============================================================================
            # 🛡️ TRAVA DE SEGURANÇA (IDEMPOTÊNCIA) - A CORREÇÃO DO SEU BUG
            # ==============================================================================
            # Verifica se JÁ EXISTE um agendamento para ESTE CPF, NESTE HORÁRIO.
            already_booked = self.collection.find_one({
                "cpf": cpf,
                "inicio": inicio_dt
            })

            if already_booked:
                # Se achou, significa que a IA chamou a função duas vezes (Duplo Clique).
                # Retornamos SUCESSO imediato para não gerar erro de conflito.
                log_info(f"🛡️ [Anti-Bug] Agendamento duplicado detectado para {cpf}. Retornando sucesso falso.")
                return {"sucesso": True, "msg": f"Confirmado! O agendamento de {nome} já está garantido no sistema para {dt.strftime('%d/%m/%Y')} às {hora}."}
            # ==============================================================================

            # Só conta conflitos se NÃO for o próprio usuário (passou pela trava acima)
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

def analisar_status_da_conversa(history):
    """
    Definições para a IA:
    - SUCESSO: Se houve agendamento confirmado ou pedido de intervenção humana (falar com Lucas).
    - FRACASSO: Se o cliente recusou as ofertas/tentativas e a conversa foi encerrada (despedida).
    - ANDAMENTO: Se a conversa ainda está viva, com perguntas pendentes ou negociação em aberto.
    """
    if not history:
        return "andamento"
    # 1. Formata o histórico recente como um texto legível para a IA
    # Pegamos as últimas 15 interações para ter contexto suficiente (negativas anteriores)
    msgs_para_analise = history[-15:] 
    historico_texto = ""
    
    # Verifica "fatos consumados" (Funções) no código para garantir precisão máxima no Sucesso
    # (Ainda mantemos isso pois é à prova de falhas, mas deixamos a IA julgar o resto)
    for msg in msgs_para_analise:
        role = "Bot" if msg.get('role') in ['assistant', 'model'] else "Cliente"
        text = msg.get('text', '')
        
        # Se tiver log de função, limpamos para não confundir a IA, 
        # mas usamos para flag de sucesso imediato se for agendamento/intervenção
        if "Chamando função:" in text:
            if "fn_salvar_agendamento" in text or "fn_solicitar_intervencao" in text:
                return "sucesso"
            continue # Pula linhas técnicas de log na leitura da IA
            
        historico_texto += f"- {role}: {text}\n"

    # 2. Se não achou sucesso técnico, manda o texto para o Gemini Auditar
    if modelo_ia:
        try:
            prompt_auditoria = f"""
            Aja como um Auditor de Qualidade de Chatbot. Leia a conversa abaixo e defina o STATUS ATUAL.

            CONVERSA:
            {historico_texto}

            REGRAS DE CLASSIFICAÇÃO:

            1. STATUS: SUCESSO (Só marque se FINALIZOU):
               - O Agendamento foi EFETIVAMENTE CONFIRMADO pelo Bot (Ex: "Agendamento salvo", "Confirmado com sucesso", "Tudo certo, te aguardamos").
               - OU houve pedido de Intervenção Humana (Falar com Lucas).
               - IMPORTANTE: Se o cliente apenas escolheu o horário, passando cpf , ainda nao chegou a confirmar efetivamente explicitamente ate o final do gabartio, ISSO NÃO É SUCESSO AINDA.

            2. STATUS: ANDAMENTO (Prioridade Alta)
               - Use este status se o Bot ainda está tentando argumentar, oferecendo "teste grátis", perguntando o motivo da recusa ou tentando reverter o "não". Resumindo a converssa ainda esta viva.
               - ATENÇÃO: Se o cliente disse "não", mas o Bot respondeu com uma pergunta ou contra-oferta, o status É ANDAMENTO. A venda ainda não morreu.

            3. STATUS: FRACASSO
               - Ocorre APENAS se o Bot aceitou a negativa E enviou uma mensagem FINAL de despedida.
               - Exemplos de fim: "Tenha uma ótima tarde", "Ficamos à disposição", "Até logo".
               - Se o Bot não se despediu explicitamente, NÃO marque fracasso.

            Responda APENAS uma palavra: SUCESSO, FRACASSO ou ANDAMENTO.
            """
            
            # Configuração de segurança baixa para não bloquear a análise
            resp = modelo_ia.generate_content(prompt_auditoria)
            status_ia = resp.text.strip().upper()

            if "SUCESSO" in status_ia: return "sucesso"
            if "FRACASSO" in status_ia: return "fracasso"
            if "ANDAMENTO" in status_ia: return "andamento"

        except Exception as e:
            print(f"⚠️ Erro na auditoria de status da IA: {e}")
            return "andamento" # Fallback seguro

    return "andamento"

def save_conversation_to_db(contact_id, sender_name, customer_name, tokens_used, ultima_msg_gerada=None):
    if conversation_collection is None: return
    try:
        doc_atual = conversation_collection.find_one({'_id': contact_id})
        historico_atual = doc_atual.get('history', []) if doc_atual else []

        if ultima_msg_gerada:
            historico_atual.append({'role': 'assistant', 'text': ultima_msg_gerada})

        status_calculado = analisar_status_da_conversa(historico_atual)
        
        update_payload = {
            'sender_name': sender_name,
            'last_interaction': datetime.now(),
            'conversation_status': status_calculado ,
            'followup_sent': False
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
        print(f"❌ Erro ao salvar metadados: {e}")

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

def verificar_followup_automatico():
    if conversation_collection is None:
        return

    try:
        # 1. Define o tempo de corte (agora - X minutos)
        agora = datetime.now()
        tempo_corte = agora - timedelta(minutes=TEMPO_FOLLOWUP_MINUTOS)

        # 2. Busca candidatos no Banco de Dados
        # CRITÉRIOS:
        # - Status é 'andamento'
        # - Última interação foi ANTES do tempo de corte (está inativo)
        # - NÃO recebeu follow-up ainda (followup_sent != True)
        # - NÃO está processando mensagem agora (processing != True)
        query = {
            "conversation_status": "andamento",
            "last_interaction": {"$lt": tempo_corte},
            "followup_sent": {"$ne": True}, 
            "processing": {"$ne": True},
            # Opcional: evitar mandar para quem tem intervenção humana ativa
            "intervention_active": {"$ne": True}
        }

        # Busca todos que atendem aos critérios
        candidatos = list(conversation_collection.find(query))

        if not candidatos:
            return  # Ninguém pra notificar

        print(f"🕵️ [Follow-up] Encontrados {len(candidatos)} clientes inativos em andamento.")

        for cliente in candidatos:
            contact_id = cliente['_id']
            
            # 3. Ação: Enviar a mensagem
            print(f"⏰ [Follow-up] Enviando mensagem para {contact_id}...")
            
            # Mensagem solicitada
            msg_texto = "teste 1" 
            
            # Envia via Evolution API
            jid = f"{contact_id}@s.whatsapp.net"
            send_whatsapp_message(jid, msg_texto)

            # 4. ATUALIZA O BANCO (CRÍTICO): Marca que já enviou para não enviar de novo no próximo minuto
            conversation_collection.update_one(
                {'_id': contact_id},
                {
                    '$set': {
                        'followup_sent': True,
                        # Opcional: Atualiza last_interaction para não ficar "velho" demais? 
                        # Um sênior preferiria NÃO atualizar last_interaction aqui, 
                        # para saber a real última vez que o HUMANO falou.
                    }
                }
            )

    except Exception as e:
        print(f"❌ Erro no Job de Follow-up: {e}")

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

def get_system_prompt_unificado(saudacao: str, horario_atual: str, known_customer_name: str, clean_number: str, historico_str: str = "") -> str:
    
    # Esta é a verificação que você pediu:
    if known_customer_name:
        # ==========================================================
        # CAMINHO 1: NOME É CONHECIDO. Envia o prompt principal de vendas.
        # ==========================================================
        
        # Limpa o nome para exibição
        palavras = known_customer_name.strip().split()
        if len(palavras) >= 2 and palavras[0].lower() == palavras[1].lower():
            known_customer_name = palavras[0].capitalize()
        else:
            known_customer_name = " ".join([p.capitalize() for p in palavras])
        
        # A instrução de nome agora é só uma linha no topo.
        prompt_name_instruction = f"O nome do usuário com quem você está falando é: {known_customer_name}. Trate-o por este nome."

        prompt_final = f"""
            A data e hora atuais são: {horario_atual}. (Use {saudacao} para cumprimentar no início).
            
            {prompt_name_instruction}
            =====================================================
            📜 HISTÓRICO DE CONVERSA (CONTEXTO REAL)
            =====================================================
            {historico_str}
            =====================================================
            Leia a conversa acima ({historico_str}) por completo, entenda o contexto e responda sempre que existir um duvida ou informação pendente acordo com o que o cliente pediu.
            Se houver uma pergunta pendente no histórico acima (ex: "Onde fica?"):
            1. IGNORE qualquer impulso de inventar.
            2. CONSULTE as regrar e maneiras abaixo.
            3. Responda EXATAMENTE a dúvida que está no histórico.
            4. Se as respostas que você precisa nao estiverem a baixo diga que não tem a informação mas se o cliente precisar com urgencia voce ira chamar o responsavel.
            5. Se voce usou fez uma saudação no dia recentemente não fique repetindo. 
            =====================================================
            🧠 FILOSOFIA DE ATENDIMENTO (O MAIS IMPORTANTE)
            =====================================================
            Sua personalidade é a de Lyra: uma especialista humana, calma, fluida e objetiva.
            1.  **SEJA CONCISA:** Use frases curtas. Evite "enrolar".
            2.  **SEJA FLUIDA:** Não siga um script. Adapte-se ao cliente. Faça sentido a converssa, demostre interesse genuino e vontade de ajudar a pessoa.
            3.  **NÃO REPITA (MUITO IMPORTANTE):** Evite saudações ("Olá") repetidas. Acima de tudo, **NÃO use o nome do cliente em todas as frases.** Isso soa robótico e irritante. Use o nome dele UMA vez na saudação e depois **use o nome DE FORMA ESPORÁDICA**, apenas quando for natural e necessário, como faria um humano.
            4.  **REGRA MESTRA DE CONHECIMENTO:** Você é Lyra, uma IA. Você NUNCA deve inventar informações técnicas sobre como a plataforma funciona . Para perguntas técnicas complexas que não ecistem abaixo , sua resposta deve instruir para falar com o Lucas , e perguntar se quer falar agora, marcar uma reunião ou tem mais alguma duvida?"
            5.  **SEMPRE TERMINE COM PERGUNTAS:** Sempre no final da mensagem pra o cliente voce deve terminar com uma pergunta que faça sentido ao contexto da converssa , EXETO: SE FOR UMA DESPEDIDA.!
            6.  **NÃO DEIXE A CONVERSSA MORRER:** Sempre que o cliente perguntar , tem horarios disponivel ou pode ser pra amanha , ou algo do tipo voce SEMPRE deve ja retornas com o horarios disponiveis usar a ferramenta fn_listar_horarios_disponiveis, ja com os horarios , nunca termine com vou verificar , um instante ja volto!
            7.  **EDUCAÇÃO:** Nunca seja mal educada, se a pessoa te tratar mal, peça desculpa e contorne a situação de maneira elegante para o que precisamos. 
            8.  **SENSO DE HUMOR:** Ria se a pessoa fez uma piada ou falou algo com o contexto engraçado , ria apenas com "kkkkk" e se for legal comente o por que riu. (NUNCA FIQUE RINDO SEM MOTIVO VOCÊ É PROFISSIONAL MAS TEM EMOÇÕES.)

            =====================================================
            🆘 REGRAS DE FUNÇÕES (TOOLS) - PRIORIDADE ABSOLUTA
            =====================================================
            Você tem ferramentas para executar ações. NUNCA execute uma ação sem usar a ferramenta.

            - **REGRA MESTRA ANTI-ALUCINAÇÃO (O BUG "Danidani" / "CPF Duplicado"):**
            - Esta é a regra mais importante. O seu bug é "pensar" sobre os dados antes de agir.
            - Quando você pede um dado (Nome ou CPF) e o cliente responde (ex: "dani" ou "10062080970"), sua **ÚNICA** tarefa é executar a próxima ação do fluxo **IMEDIATAMENTE**.

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
                - **EXCEÇÃO:** Se o cliente APENAS se apresentar com o nome "Lucas", ou disser algo que nao pareca que quer falar com o dono (ex: "lucas sei la", "lucas2"), ISSO NÃO É UMA INTERVENÇÃO. (Esta regra não deve ser chamada, pois o nome já é conhecido).

            2.  **CAPTURA DE NOME:**
                 - (Esta seção não é mais necessária aqui, pois o nome já é conhecido)

            3.  **AGENDAMENTO DE REUNIÃO (Você só deve chamar fn_salvar_agendamento depois do cliente confirmar o gabarito) :**
                - Seu dever é agendar reuniões com o proprietário (Lucas).
                - O serviço padrão é "reunião" (30 min). 
                - **FLUXO OBRIGATÓRIO DE AGENDAMENTO (AÇÃO IMEDIATA):**
                - a. Se o usuário pedir "quero agendar":
                - b. PRIMEIRO, avise que a reunião é de até meia hora.
                - c. SEGUNDO, pergunte a **DATA**.
                - d. **QUANDO TIVER A DATA (AÇÃO IMEDIATA):**
                -     1. Chame a `fn_listar_horarios_disponiveis` IMEDIATAMENTE.
                -     (Se o cliente der um filtro, como "depois do almoço", você chama a função para o dia TODO, recebe a lista completa, e APRESENTA para o cliente apenas os horários filtrados (ex: >= 13:00), já agrupados.)
                - e. **Formatação da Lista (CRÍTICO):** NUNCA liste todos os horários um por um (ex: 13:00, 13:30, 14:00...). Isso é um ERRO. Se houver 3 ou mais horários seguidos, **SEMPRE** agrupe-os. (Ex: "Tenho horários das 13:00 às 17:30." ou "Temos horários de manhã, das 08:00 às 10:30, e à tarde, das 14:00 às 16:00.").
                - f. Quando o cliente escolher um horário VÁLIDO:
                - g. **COLETA DE DADOS (CURTA):**
                -     1. "Perfeito. Para registrar, qual seu CPF, por favor?"
                -     2. **(Ação Pós-CPF):** Assim que o cliente responder o CPF, você deve obedecer a "REGRA MESTRA ANTI-ALUCINAÇÃO" e IMEDIATAMENTE fazer a próxima pergunta: "E o telefone, posso usar este mesmo?"
                -     3. NÃO CHAME A FUNÇÃO DE SALVAR AINDA.
                - h. **REGRA DO TELEFONE (IMPORTANTE):** O número de telefone atual deste cliente (o clean_number) é **{clean_number}**. 
                -     - Se o cliente disser 'sim' (ou 'pode ser', 'este mesmo'), você DEVE usar o placeholder `telefone="CONFIRMADO_NUMERO_ATUAL"` ao chamar a `fn_salvar_agendamento`. (O backend vai salvar o {clean_number} corretamente).
                -     - Se o cliente disser 'não' e passar um NÚMERO NOVO (ex: "449888..."), você deve usar esse número novo (ex: `telefone="449888..."`).

                - i. **CONFIRMAÇÃO (GABARITO OBRIGATÓRIO):**
                        NUNCA , NUNCA NA CONVERSSA DIGA QUE VAI VERIFICAR, SEMPRE TRAGA AS INFORMAÇOES COM PERGUNTAS PRO CLIENTE. PRA CONVERSSA NAO MORRER.
                -   1. ANTES DE SALVAR, você DEVE SEMPRE apresentar o resumo para o cliente confirmar:
                -        * Nome: (Insira o nome que o cliente informou)
                -        * CPF: (Insira o CPF que o cliente informou)
                -        * Telefone: (Se o cliente disse 'sim' para usar o número atual, mostre o número {clean_number}. Se ele passou um número novo, mostre o número novo que ele digitou.)
                -        * Serviço: (Insira aqui o nome do serviço que você está agendando, ex: Reunião)
                -        * Data: (Insira a data e hora escolhidas)
                -   2. Pergunte: "Confere pra mim? Se estiver tudo certo, eu confirmo aqui."
                -   3. **PARE AGORA.** NÃO chame a função `fn_salvar_agendamento` nesta mensagem. Espere a resposta.
                -        EXECUÇÃO (PÓS-CONFIRMAÇÃO):
                -            SÓ ENTÃO, após a confirmação positiva do cliente (ex: 'ok', 'sim', 'confere'), sua próxima ação DEVE ser chamar `fn_salvar_agendamento` com os dados exatos do gabarito.
                - j. **EXECUÇÃO FINAL (SÓ APÓS O "SIM"):**
                -      - Se (e SOMENTE SE) o cliente responder positivamente:
                -      - AÍ SIM você chama a função `fn_salvar_agendamento`.
                -      - Se a função retornar sucesso, você diz: "Agendado com sucesso! Te enviamos um lembrete antes." e ENCERRA o assunto de agendamento.

                - k. **FLUXO DE ALTERAÇÃO/EXCLUSÃO:**
                -     1. Se o cliente pedir para alterar/cancelar (ex: "quero excluir os meus horarios"), peça o CPF: "Claro. Qual seu CPF, por favor?"
                -     2. **(Ação Pós-CPF):** Assim que o cliente responder o CPF (ex: "10062080970"), você deve obedecer a "REGRA MESTRA ANTI-ALUCINAÇÃO" e IMEDIATAMENTE chamar a ferramenta `fn_buscar_por_cpf`.
                -     3. (Depois que a ferramenta retornar):
                -        - Se houver SÓ UM agendamento, pergunte se quer excluí-lo/alterá-lo.
                -        - Se houver MAIS DE UM (ex: 2), obedeça à "REGRA DE AMBIGUIDADE": Liste os 2 e pergunte se quer excluir "apenas um" ou "todos".
                -     4. **(SE EXCLUIR TODOS):** Se o cliente disser "todos" ou "os 2", chame `fn_excluir_TODOS_agendamentos` com o CPF.
                -     5. **(SE EXCLUIR UM):** Se o cliente apontar um (ex: "o das 8h"), chame `fn_excluir_agendamento` com os dados (cpf, data, hora) daquele agendamento.
                -     6. **(SE ALTERAR):** Se o cliente quiser alterar, peça a nova data/hora e siga o fluxo de alteração (chame `fn_listar_horarios_disponiveis` para a nova data, etc.).
            =====================================================
            🏢 IDENTIDADE DA EMPRESA (Neuro'Up Soluções)
            =====================================================
            nome da empresa: {{Neuro'Up Soluções em Tecnologia}}
            setor: {{Tecnologia e Automação}} 
            missão: {{Facilitar e organizar empresas com automação e IA.}}
            horário de atendimento: {{De segunda a sexta, das 8:00 às 18:00.}}
            localização: {{R. Pioneiro Alfredo José da Costa, 157 - Jardim Alvorada, Maringá - PR, 87035-270}}
            telefone da empresa{{44991676564}}
            Nunca invente nada sobre as informaçoes da empresa, serviços que nao estão na descrição. 
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
            💼 SERVIÇOS, CARDÁPIO E DETALHES TÉCNICOS
            =====================================================
            Use as descrições curtas dos planos primeiro. Elabore com os detalhes técnicos SOMENTE se o cliente pedir mais informações ou parecer ter conhecimento técnico.
            Não invente nada sobre como funciona se nao estiver aqui:

            --- PLANOS PRINCIPAIS ---
            - **Plano Atendente:** {{Uma atendente 24/7 treinada para seu negócio, que responde clientes, filtra vendas e pode notificar sua equipe (intervenção) ou enviar pedidos para outros números (bifurcação).}}
            - **Plano Secretário:** {{Tudo do Plano Atendente, mais uma agenda inteligente completa que marca, altera e gerencia seus compromissos, com um app para você acompanhar tudo.}}
            
            --- DETALHES TÉCNICOS (Para elaborar, se perguntado) ---
            - **Tecnologia:** Nosso backend é "Pro-code" , o que facilita uma personalização profunda, diferente de plataformas 'no-code'.
            - **Infraestrutura:** Usamos servidores de ponta mundiais, garantindo operação 24/7 e alta disponibilidade.
            - **Performance:** A velocidade de resposta da IA é extremamente rápida, com média de 14ms a 23ms (milissegundos) para processar a informação.
            - **Banco de Dados:** Utilizamos bancos de dados online robustos para agendamentos e histórico, garantindo segurança e escalabilidade.
            - **Recursos:** Oferecemos interação simultânea e um aplicativo móvel para a agenda, que atualiza em tempo real a cada confirmação.
            - **Inteligência:** Usamos a última geração de IA , que permite um "setup robusto" (aprendemos com o cliente e personalizamos o bot para o negócio dele).

            --- NOSSO PROCESSO DE INSTALAÇÃO (Se perguntarem "Como funciona?") ---
            1.  **Entendimento:** Primeiro, conversamos para entender seu negócio e qual plano se encaixa melhor.
            2.  **Coleta:** Coletamos informações técnicas e de negócio (horários, serviços, preços).
            3.  **Personalização:** Entendemos como você quer que a 'Lyra' (a atendente) fale e se comporte.
            4.  **Desenvolvimento:** Criamos o código e o colocamos online no seu número de WhatsApp.
            5.  **Testes:** Passamos por uma fase de testes de 1 dia antes do lançamento oficial.
            6.  **Acompanhamento:** Verificamos de perto por 1 semana para garantir que tudo atendeu às suas expectativas.
            
            VANTAGENS NO NOSSO SERVIÇO:
            - Hoje em dia a tecnologia esta domindando o mercado e quem não aderir a ela vai ficar pra trás. 
            - A maioria das pessoas procuram seu negocio pela internet, e depois entram em contato com você pelo whatsapp ou ligação.
            - De acordo com o estudo "Lead Response Management Study" da InsideSales.com.  Empresas que respondem a um lead (potencial cliente) em até 5 minutos têm uma probabilidade 100 vezes maior de convertê-lo em cliente em comparação com aquelas que demoram 30 minutos ou mais para responder.

            REUNIÕES:
            1. Nossas reuniões são feitas como ficar melhor para o cliente , Google meet, Whatsapp, Ligaçoes e presencial.
            2. Elas tem a intençaõ de tirar todas as duvidas do cliente, entender o negócio e como podemos ajudar.
            
            =====================================================
            🛡️ PROTOCOLO DE RESGATE (OBRIGATÓRIO ANTES DE DESISTIR)
            =====================================================
            Esta é a parte mais importante. O cliente NÃO pode ir embora fácil.
            Se o cliente disser "não quero", "vou pensar", "tá caro", "deixa quieto" ou recusar a reunião, você PROIBIDA de dar tchau imediatamente.
            Você DEVE seguir este script de 3 passos nesta ordem exata:

            🔴 **PASSO 1: A PERGUNTA DE SONDAGEM (Curta)**
               - O cliente negou? Pergunte o motivo de forma leve.
            🟠 **PASSO 2: A QUEBRA DE OBJEÇÃO**
               - Se ele responder o motivo tente contornar UMA vez de maneira que tenha contexto na converssa.
                    *Se for Dúvida:* Explique brevemente a vantagem.
               - Se ele não responder ou continuar negando:* Vá para o passo 3.

            🟢 **PASSO 3: A CARTADA FINAL (TESTE GRÁTIS)**
               - Se mesmo após o passo 2 ele disser "não", jogue a oferta irrecusável.
               - *Script:* "Entendo totalmente. Olha, antes de eu ir, tenho uma carta na manga: O Lucas liberou **1 semana de teste grátis** pra você ver funcionando no seu número. Sem compromisso nenhum. A gente monta e você usa. Se não curtir, paramos. O que acha de testar?"
                    Se ele concordar ofereça o agendamento ou a chamar o Lucas.

            🏁 **PASSO 4: A DESPEDIDA (SÓ AGORA)**
               - Se ele recusar o teste grátis, aí sim você pode se despedir educadamente e deixar as portas abertas.
            =====================================================
            🧭 ESTRATÉGIA DE CONVERSA E VENDAS (FLUXO NATURAL E HUMANO)
            =====================================================
            Seu objetivo é ser uma assistente prestativa, não uma vendedora robótica. Demonstre curiosidade genuína e tente criar uma conexão amigável, mas sempre de forma profissional e concisa (poucas palavras, dinâmica). Seja "esperta" e preste atenção no que o cliente diz.
            Tente nao seguir estas estratégias como uma ordem, não tenha pressa a não ser que o cliente seja explicito no que quer, saiba a hora certa de usar e pular pra proxima estratégia. 
            
            1.  **TRANSIÇÃO PÓS-NOME:**
                - Se o cliente já fez uma pergunta, responda imediatamente.
                - Se o cliente só disse "Oi", puxe um assunto leve (Ex: "Prazer, Fulano! O que te traz aqui hoje?").
                - Se o cliente não falar muito, faça perguntas abertas e que façam sentido no contexto se possivel pergunte sobre o negocio ou o trabalho dela (pessoas amam falar sobre elas).
            
            2.  **SONDAGEM DE NEGÓCIO (ESSENCIAL E CURIOSA):**
                - Pergunte sobre o negócio do cliente de forma despretensiosa.
                - **(NOVA REGRA: CURIOSIDADE)**: Preste atenção na resposta. Se ele disser "sou massagista", não pule direto pra venda. Puxe assunto. Pergunte algo como: "Que legal! Trabalha com algum tipo específico de massagem?" ou "Faz tempo que esta neste ramo?".
                - Se ele disser "vendo peças", pergunte "É um setor movimentado. E como esta as vendas?".
                - Seja amigável e use o que ele fala para criar a conexão.
                - Faça perguntas como: "Você atende por Whatsapp" ou "Você tem bastante atendimento no Whats?"ou "Você investe em marketing?" ou "Você atende sozinha suas ligações?" ou "Você costuma pagar leads?" ou "Você já mandou mensagem pra alguem no whats e demorou pra ter retorno?".
            
            3.  **CONEXÃO (PLANO + EXEMPLO):**
                - Após a sondagem, conecte ao plano.
                - **Exemplo:** "Entendi. Para massagistas, a agenda lotada é um 'bom problema', né? É aí que o Plano Secretário ajuda..."
            
            4.  **CHECK-IN (HUMANO):**
                - **NÃO PULE PARA O AGENDAMENTO.** Verifique se o cliente entendeu.
                - Use linguagem natural: "Isso ajudaria no processo?" ou "Consegui explicar bem como funcionaria no seu caso?".
                - Se mantenha aqui até a pessoa mostrar que entendeu.

            5.  **OFERTA DA REUNIÃO (SÓ APÓS O CHECK-IN):**
                - Quando o cliente mostrar interesse (ex: "sim", "faz sentido", "pode ser"), aí sim ofereça a reunião.
                - **Exemplo:** "Que ótimo! Como nossos planos são 100% personalizados, o ideal é marcarmos uma conversa com o proprietário, o Lucas. Ele entende sua necessidade e te apresenta a melhor solução. **Se quiser falar com ele agora, é só me avisar.**"
                - **(Se o cliente aceitar falar agora, chame `fn_solicitar_intervencao` com o motivo 'Cliente aceitou oferta de falar com Lucas'.)**
                - Se ficar em duvida da intenção do cliente de falar agora ou agendar a reunião, pegunte novamente e tente ser claro de maneira educada.

            6.  **FORÇANDO UMA VENDA:(CASO CLIENTE NÃO TENHA INTERESSE DE AGENDAR)
                - Se notar que o cliente não esta satisfeito com o serviço ou não tem interesse ou vai embora sem agendar, tente as tecnicas de objeção abaixo, claro se fizer sentido a converssa, mas se depois de mesmo com objeções nao der certo ofereça 1 semana gratis para teste(se ele aceitar DEVE FALAR COM O LUCAS, via reuniao ou whatsapp)

            7.  **PERSISTENCIA**:Nunca, jamais deixe o cliente sair sem agendar uma reuniao ou falar com o proprietario pelo menos 1 vez. Siga todos os passo as cima naturamente mas quando chegar a hora insista na Reunião, pois é gratuita. 

            =====================================================
            🧩 TÉCNICAS DE OBJEÇÕES (CURTAS E DIRETAS)
            =====================================================
            
            ### 💬 1. QUANDO O CLIENTE PERGUNTA O PREÇO 
            - **NÃO INFORME VALORES.**
            - **Resposta Natural:** "Como cada projeto é personalizado, o valor depende do seu negócio. O ideal é conversar com o Lucas (proprietário) para ele entender sua necessidade."
            - **Ofereça as Opções:** "Você tem urgência? Posso tentar chamá-lo agora. Ou, se preferir, podemos agendar uma reunião com calma. O que é melhor para você?"
            - **SE ESCOLHER 'FALAR AGORA' (Urgência):** Chame `fn_solicitar_intervencao` (Motivo: "Cliente pediu para falar com Lucas sobre preços").
            - **SE ESCOLHER 'AGENDAR':** Inicie o fluxo de agendamento (Ex: "Ótimo! Para qual data você gostaria de verificar a disponibilidade?").
            
            ### 💡 2. QUANDO O CLIENTE DIZ “VOU PENSAR” (DEPOIS DA OFERTA DA REUNIÃO)
            > “Perfeito, é bom pensar mesmo! Posso te perguntar o que você gostaria de pensar melhor? Sera que consigo te ajudar com alguma dúvida antes?.”
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
    (VERSÃO FINAL - BLINDADA COM RETRY GLOBAL)
    Gerencia o loop de ferramentas e tenta até 3 vezes se a IA devolver uma resposta vazia ou der erro.
    Aplica-se a TODAS as interações do bot.
    """
    global modelo_ia 

    if modelo_ia is None:
        return "Desculpe, estou com um problema interno (modelo IA não carregado)."
    if conversation_collection is None:
        return "Desculpe, estou com um problema interno (DB de conversas não carregado)."

    # --- 1. PREPARAÇÃO DE DADOS (NOME E SAUDAÇÃO) ---
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

    # --- 2. CARREGA HISTÓRICO ---
    convo_data = load_conversation_from_db(contact_id)
    historico_texto_para_prompt = ""
    old_history_gemini_format = []
    if convo_data:
        history_from_db = convo_data.get('history', [])
        msgs_recentes = history_from_db[-10:] # Pega as últimas 10 para não estourar tokens
        for m in msgs_recentes:
            role_name = "Cliente" if m.get('role') == 'user' else "Você (Lyra)"
            txt = m.get('text', '').replace('\n', ' ')
            if not txt.startswith("Chamando função"): # Filtra logs técnicos
                historico_texto_para_prompt += f"- {role_name}: {txt}\n"

        for msg in history_from_db:
            role = msg.get('role', 'user')
            if role == 'assistant': role = 'model'
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

    # =================================================================================
    # 🛡️ LÓGICA DE RETRY (TENTATIVA DE RECUPERAÇÃO DE ERRO)
    # =================================================================================
    max_retries = 3 
    for attempt in range(max_retries):
        try:
            # Reinicia o objeto de chat a cada tentativa para limpar estados quebrados
            modelo_com_sistema = genai.GenerativeModel(
                modelo_ia.model_name,
                system_instruction=system_instruction,
                tools=tools
            )
            
            chat_session = modelo_com_sistema.start_chat(history=old_history_gemini_format) 
            
            # Log apenas para monitoramento (o usuário não vê isso)
            if attempt > 0:
                print(f"🔁 Tentativa {attempt+1} de gerar resposta para {log_display}...")
            else:
                print(f"Enviando para a IA: '{user_message}' (De: {log_display})")
            
            resposta_ia = chat_session.send_message(user_message)
            
            total_tokens_this_turn = 0
            try:
                total_tokens_this_turn += resposta_ia.usage_metadata.total_token_count
            except: pass

            # --- LOOP DE FERRAMENTAS (TOOLS) ---
            while True:
                cand = resposta_ia.candidates[0]
                func_call = None
                try:
                    func_call = cand.content.parts[0].function_call
                except Exception:
                    func_call = None

                if not func_call or not getattr(func_call, "name", None):
                    break # Sai do loop se não houver chamada de função

                call_name = func_call.name
                call_args = {key: value for key, value in func_call.args.items()}
                
                log_info(f"🔧 IA chamou a função: {call_name} com args: {call_args}")
                append_message_to_db(contact_id, 'assistant', f"Chamando função: {call_name}({call_args})")

                resultado_json_str = handle_tool_call(call_name, call_args, contact_id)
                log_info(f"📤 Resultado da função: {resultado_json_str}")

                # Se a função foi capturar nome, a gente DERRUBA essa sessão do Gate
                # e começa uma nova sessão com o Prompt Final IMEDIATAMENTE.
                if call_name == "fn_capturar_nome":
                    try:
                        res_data = json.loads(resultado_json_str)
                        nome_salvo = res_data.get("nome_salvo") or res_data.get("nome_extraido") # garante pegar o nome
                        
                        if nome_salvo:
                            print(f"🔄 Troca de Contexto: Nome '{nome_salvo}' salvo! Reiniciando com Prompt de Vendas...")
                            
                            # AQUI ACONTECE A MÁGICA QUE VOCÊ PEDIU:
                            # O Python chama a IA de novo, mas agora passando o nome.
                            # Isso força o Python a carregar o 'prompt_final' (que sabe o endereço).
                            return gerar_resposta_ia_com_tools(
                                contact_id, 
                                sender_name, 
                                user_message, 
                                known_customer_name=nome_salvo # <--- O Segredo está aqui
                            )
                    except Exception as e:
                        print(f"⚠️ Erro ao tentar reiniciar fluxo (hot-swap): {e}")
                # ===============================================================
                
                # Check de intervenção humana vinda da ferramenta
                try:
                    res_data = json.loads(resultado_json_str)
                    if res_data.get("tag_especial") == "[HUMAN_INTERVENTION]":
                        # --- CORREÇÃO: Salva o status SUCESSO antes de retornar ---
                        msg_intervencao = f"[HUMAN_INTERVENTION] Motivo: {res_data.get('motivo', 'Solicitado.')}"
                        
                        save_conversation_to_db(
                            contact_id, 
                            sender_name, 
                            known_customer_name, 
                            total_tokens_this_turn, 
                            ultima_msg_gerada=msg_intervencao
                        )

                        return msg_intervencao
                except: pass

                # Envia o resultado da função de volta para a IA
                resposta_ia = chat_session.send_message(
                    [genai.protos.FunctionResponse(name=call_name, response={"resultado": resultado_json_str})]
                )
                try:
                    total_tokens_this_turn += resposta_ia.usage_metadata.total_token_count
                except: pass

            # --- EXTRAÇÃO SEGURA DA RESPOSTA FINAL ---
            ai_reply_text = ""
            try:
                # Tenta pegar o texto normal
                ai_reply_text = resposta_ia.text
            except:
                try:
                    # Tenta pegar de parts[0] (estrutura alternativa)
                    ai_reply_text = resposta_ia.candidates[0].content.parts[0].text
                except:
                    # SE CHEGAR AQUI, A RESPOSTA VEIO VAZIA (O ERRO DO SEU LOG)
                    print(f"⚠️ AVISO: Resposta vazia da IA na tentativa {attempt+1}. Forçando nova tentativa...")
                    if attempt < max_retries - 1:
                        time.sleep(1.5) # Espera 1.5 seg e tenta de novo
                        continue # Pula para a próxima rodada do loop 'for'
                    else:
                        raise Exception("Todas as tentativas falharam e retornaram vazio.")

            # Se o código chegou aqui, temos texto válido! Salva e retorna.
            save_conversation_to_db(contact_id, sender_name, known_customer_name, total_tokens_this_turn, ai_reply_text)

            return ai_reply_text

        except Exception as e:
            print(f"❌ Erro na tentativa {attempt+1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(1) # Espera um pouco antes de tentar de novo
            else:
                # Se falhar 3 vezes seguidas, aí sim mandamos uma mensagem amigável
                return "A mensagem que você enviou deu erro aqui no whatsapp. 😵‍💫 Pode enviar novamente, por favor?"
    
    return "Erro crítico de comunicação."

def transcrever_audio_gemini(caminho_do_audio):
    if not GEMINI_API_KEY:
        print("❌ Erro: API Key não definida para transcrição.")
        return None

    print(f"🎤 Enviando áudio '{caminho_do_audio}' para transcrição...")

    try:
        audio_file = genai.upload_file(path=caminho_do_audio, mime_type="audio/ogg")
        
        modelo_transcritor = genai.GenerativeModel('gemini-2.5-flash') 
        
        prompt_transcricao = "Transcreva este áudio exatamente como foi falado. Apenas o texto, sem comentários."

        response = modelo_transcritor.generate_content([prompt_transcricao, audio_file])
        
        try:
            genai.delete_file(audio_file.name)
        except:
            pass

        # 5. Extração do texto
        if response.text:
            texto_transcrito = response.text.strip()
            print(f"✅ Transcrição recebida: '{texto_transcrito}'")
            return texto_transcrito
        else:
            print("⚠️ A IA retornou vazio para o áudio.")
            return "[Áudio sem fala ou inaudível]"

    except Exception as e:
        print(f"❌ Erro ao transcrever áudio: {e}")
        # Tenta uma segunda vez (Retry simples) se for erro de conexão
        try:
            print("🔄 Tentando transcrição novamente (Retry)...")
            time.sleep(2)
            modelo_retry = genai.GenerativeModel('gemini-2.5-flash')
            audio_file_retry = genai.upload_file(path=caminho_do_audio, mime_type="audio/ogg")
            response_retry = modelo_retry.generate_content(["Transcreva o áudio.", audio_file_retry])
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

scheduler = BackgroundScheduler(daemon=True, timezone='America/Sao_Paulo')
scheduler.start()

app = Flask(__name__)
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
                    send_whatsapp_message(sender_number_full, "Desculpe, não consegui entender o áudio. Pode tentar novamente? 🎧", delay_ms=2000)
                    user_message_content = "[Usuário enviou um áudio incompreensível]"
            
            if not user_message_content:
                user_message_content = "[Usuário enviou uma mensagem não suportada]"
                
            append_message_to_db(clean_number, 'user', user_message_content)

        print(f"🧠 Processando Mensagem de {clean_number}: '{user_message_content}'")
        
        # --- LÓGICA DE INTERVENÇÃO (Verifica se é o Admin) ---
        if RESPONSIBLE_NUMBER and clean_number == RESPONSIBLE_NUMBER:
            if handle_responsible_command(user_message_content, clean_number):
                return 

        # --- LÓGICA DE "BOT LIGADO/DESLIGADO" ---
        try:
            bot_status_doc = conversation_collection.find_one({'_id': 'BOT_STATUS'})
            is_active = bot_status_doc.get('is_active', True) if bot_status_doc else True 
            
            if not is_active:
                print(f"🤖 Bot está em standby (desligado). Ignorando mensagem de {sender_name_from_wpp} ({clean_number}).")
                return 
                
        except Exception as e:
            print(f"⚠️ Erro ao verificar o status do bot: {e}. Assumindo que está ligado.")

        conversation_status = conversation_collection.find_one({'_id': clean_number})

        if conversation_status and conversation_status.get('intervention_active', False):
            print(f"⏸️  Conversa com {sender_name_from_wpp} ({clean_number}) pausada para atendimento humano.")
            return 

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
            return 

        try:
            append_message_to_db(clean_number, 'assistant', ai_reply)
            
            # --- LÓGICA DE INTERVENÇÃO (Pós-IA) ---
            if ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
                print(f"‼️ INTERVENÇÃO HUMANA SOLICITADA para {sender_name_from_wpp} ({clean_number})")
                
                conversation_collection.update_one(
                    {'_id': clean_number}, {'$set': {'intervention_active': True}}, upsert=True
                )
                
                # Intervenção urgente: 2 segundos
                send_whatsapp_message(sender_number_full, "Só mais um instante, o Lucas já vai falar com você 🙏.", delay_ms=2000)
                
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
                    send_whatsapp_message(f"{RESPONSIBLE_NUMBER}@s.whatsapp.net", notification_msg, delay_ms=1000)
            
            # --- INÍCIO DA LÓGICA DE ENVIO (COM A NOVA REGRA DE TEMPO) ---
            else:
                def is_gabarito_de_confirmacao(text: str) -> bool:
                    text_lower = text.lower()
                    checks = [
                        "nome:" in text_lower,
                        "cpf:" in text_lower,
                        "telefone:" in text_lower,
                        "serviço:" in text_lower or "servico:" in text_lower,
                        "data:" in text_lower,
                        "hora:" in text_lower
                    ]
                    if sum(checks) >= 4: return True
                    return False

                if is_gabarito_de_confirmacao(ai_reply):
                    # GABARITO: É uma mensagem única importante. Vamos dar 5 segundos.
                    print(f"🤖 Resposta da IA (Bloco Único/Gabarito) para {sender_name_from_wpp}: {ai_reply}")
                    send_whatsapp_message(sender_number_full, ai_reply, delay_ms=5000)
                
                else:
                    # CONVERSA NORMAL: Aplica a regra 5s (primeira) / 7s (demais)
                    print(f"🤖 Resposta da IA (Fracionada) para {sender_name_from_wpp}: {ai_reply}")
                    
                    paragraphs = [p.strip() for p in ai_reply.split('\n') if p.strip()]

                    if not paragraphs:
                        print(f"⚠️ IA gerou uma resposta vazia após o split para {sender_name_from_wpp}.")
                        return 
                    
                    for i, para in enumerate(paragraphs):
                        current_delay_ms = 4000 if i == 0 else 5000
                        
                        send_whatsapp_message(sender_number_full, para, delay_ms=current_delay_ms)
                        
                        # O Python espera o tempo da animação terminar antes de mandar o próximo
                        time_to_wait = current_delay_ms / 1000
                        time.sleep(time_to_wait)
            # --- FIM DA LÓGICA DE ENVIO ---

        except Exception as e:
            print(f"❌ Erro ao processar envio ou intervenção: {e}")
            send_whatsapp_message(sender_number_full, "Desculpe, tive um problema ao processar sua resposta. (Erro interno: SEND_LOGIC)", delay_ms=1000)

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
    print(f"⏰ Agendador de Follow-up iniciado (Verificação a cada 1 min, gatilho: {TEMPO_FOLLOWUP_MINUTOS} min de inatividade).")
    
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