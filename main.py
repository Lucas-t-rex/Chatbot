import google.generativeai as genai
import requests
import os
import pytz 
from flask import Flask, request, jsonify
from datetime import datetime
from dotenv import load_dotenv
import base64
import threading
from pymongo import MongoClient
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from apscheduler.schedulers.background import BackgroundScheduler
import json 

load_dotenv()

# --- CONFIGURAÇÕES GLOBAIS (COMPARTILHADAS) ---
# Todas as chaves de API e a URL do Mongo vêm do .env (ou Secrets do Fly)
EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL") 
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") 
MONGO_DB_URI = os.environ.get("MONGO_DB_URI") 
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY')
EMAIL_RELATORIOS = os.environ.get('EMAIL_RELATORIOS')

message_buffer = {}
message_timers = {}
BUFFER_TIME_SECONDS = 8 

# --- CONEXÃO GLOBAL COM O MONGODB ---
try:
    client = MongoClient(MONGO_DB_URI)
    # Testa a conexão
    client.server_info()
    print("✅ Conectado ao Cluster MongoDB (Cérebro Mestre).")
except Exception as e:
    print(f"❌ ERRO GRAVE: Não foi possível conectar ao Cluster MongoDB. Erro: {e}")
    client = None

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"AVISO: A chave de API do Google não foi configurada corretamente. Erro: {e}")
else:
    print("AVISO: A variável de ambiente GEMINI_API_KEY não foi definida.")

modelo_ia = None
try:
    modelo_ia = genai.GenerativeModel('gemini-1.5-flash') 
    print("✅ Modelo do Gemini (gemini-1.5-flash) inicializado com sucesso.")
except Exception as e:
    print(f"❌ ERRO: Não foi possível inicializar o modelo do Gemini. Verifique sua API Key. Erro: {e}")

# --- FUNÇÕES DE BANCO DE DADOS (REATORADAS) ---
# Todas as funções de DB agora recebem 'client_db' para saber em qual banco de dados operar.

def append_message_to_db(client_db, contact_id, role, text, message_id=None):
    """(Reatorada) Salva uma única mensagem no histórico do DB do cliente correto."""
    try:
        conversation_collection = client_db.conversations
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
        print(f"❌ Erro ao append_message_to_db (DB: {client_db.name}): {e}")
        return False

def save_conversation_to_db(client_db, contact_id, sender_name, customer_name, tokens_used):
    """(Reatorada) Salva metadados (nomes, tokens) no MongoDB do cliente correto."""
    try:
        conversation_collection = client_db.conversations
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
        print(f"❌ Erro ao salvar metadados (DB: {client_db.name}, Contato: {contact_id}): {e}")

def load_conversation_from_db(client_db, contact_id):
    """(Reatorada) Carrega o histórico de uma conversa do MongoDB do cliente correto."""
    try:
        conversation_collection = client_db.conversations
        result = conversation_collection.find_one({'_id': contact_id})
        if result:
            history = result.get('history', [])
            history_sorted = sorted(history, key=lambda m: m.get('ts', ''))
            result['history'] = history_sorted
            print(f"🧠 Histórico anterior encontrado e carregado para {contact_id} (DB: {client_db.name}, {len(history_sorted)} entradas).")
            return result
    except Exception as e:
        print(f"❌ Erro ao carregar conversa (DB: {client_db.name}, Contato: {contact_id}): {e}")
    return None

# --- FUNÇÕES AUXILIARES ---

def get_last_messages_summary(history, max_messages=4):
    """(Sem Mudança) Formata as últimas mensagens de um histórico para um resumo legível."""
    summary = []
    relevant_history = history[-max_messages:]
    
    for message in relevant_history:
        role = "Cliente" if message.get('role') == 'user' else "Bot"
        text = message.get('text', '').strip()

        if role == "Cliente" and text.startswith("A data e hora atuais são:"):
            continue 
        if role == "Bot" and text.startswith("Entendido. A Regra de Ouro"):
            continue 
            
        summary.append(f"*{role}:* {text}")
        
    if not summary:
        user_messages = [msg.get('text') for msg in history if msg.get('role') == 'user' and not msg.get('text', '').startswith("A data e hora atuais são:")]
        if user_messages:
            return f"*Cliente:* {user_messages[-1]}"
        else:
            return "Nenhum histórico de conversa encontrado."
            
    return "\n".join(summary)

def transcrever_audio_gemini(caminho_do_audio):
    """(Sem Mudança) Envia um arquivo de áudio para a API do Gemini e retorna a transcrição."""
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

# --- FUNÇÃO DE ENVIO (REATORADA) ---

def send_whatsapp_message(instance_name, number, text_message):
    """
    (Reatorada) Envia uma mensagem de texto via Evolution API.
    Agora usa a 'instance_name' correta para montar a URL.
    """
    
    if not instance_name:
        print(f"❌ ERRO FATAL: Tentativa de enviar mensagem sem 'instance_name' para {number}.")
        return

    clean_number = number.split('@')[0]
    payload = {"number": clean_number, "textMessage": {"text": text_message}}
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}

    base_url = EVOLUTION_API_URL
    api_path = f"/message/sendText/{instance_name}" # <-- MUDANÇA CRÍTICA
    
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
        print(f"✅ Enviando resposta para a URL: {final_url} (Instância: {instance_name}, Destino: {clean_number})")
        response = requests.post(final_url, json=payload, headers=headers)
        
        if response.status_code < 400:
            print(f"✅ Resposta da IA enviada com sucesso para {clean_number}\n")
        else:
            print(f"❌ ERRO DA API EVOLUTION (Instância: {instance_name}) ao enviar para {clean_number}: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Erro de CONEXÃO ao enviar mensagem para {clean_number}: {e}")

# --- FUNÇÃO DE RELATÓRIO (REATORADA) ---

def gerar_e_enviar_relatorio_semanal():
    """
    (Reatorada) Gera um relatório para CADA CLIENTE (cada DB) no Mongo.
    """
    global client 
    if not client:
        print("🗓️ Relatório Semanal: Pulando, cliente Mongo não conectado.")
        return

    if not all([SENDGRID_API_KEY, EMAIL_RELATORIOS]):
        print("🗓️ Relatório Semanal: Variáveis SENDGRID_API_KEY e EMAIL_RELATORIOS não configuradas. Relatório não pode ser enviado.")
        return

    print(f"🗓️ Iniciando geração de relatórios semanais para TODOS os clientes...")
    
    try:
        db_names = client.list_database_names()
        excluded_dbs = ['admin', 'local', 'config']
        client_dbs = [db for db in db_names if db not in excluded_dbs]
        
        if not client_dbs:
            print("🗓️ Relatório Semanal: Nenhum banco de dados de cliente encontrado.")
            return

        print(f"🗓️ Bancos de dados encontrados: {client_dbs}")

        for db_name in client_dbs:
            try:
                client_db = client[db_name]
                
                config_data = client_db.config.find_one({"_id": "configuracao"})
                if not config_data:
                    print(f"⚠️ Relatório (DB: {db_name}): Pulando, não foi possível encontrar o documento 'configuracao'.")
                    continue
                
                CLIENT_NAME_FROM_DB = config_data.get('client_name', db_name)
                print(f"🗓️ Gerando relatório para o cliente: {CLIENT_NAME_FROM_DB} (DB: {db_name})...")
                
                conversation_collection = client_db.conversations
                hoje = datetime.now()
                
                usuarios_do_bot = list(conversation_collection.find({}))
                numero_de_contatos = len(usuarios_do_bot)
                total_geral_tokens = 0
                media_por_contato = 0

                if numero_de_contatos > 0:
                    for usuario in usuarios_do_bot:
                        total_geral_tokens += usuario.get('total_tokens_consumed', 0)
                    media_por_contato = total_geral_tokens / numero_de_contatos
                
                corpo_email_texto = f"""
                Relatório de Consumo Acumulado do Cliente: '{CLIENT_NAME_FROM_DB}'
                Data do Relatório: {hoje.strftime('%d/%m/%Y')}

                --- RESUMO GERAL DE USO ---

                👤 Número de Contatos Únicos: {numero_de_contatos}
                🔥 Consumo Total de Tokens (Acumulado): {total_geral_tokens}
                📊 Média de Tokens por Contato: {media_por_contato:.0f}

                ---------------------------
                Atenciosamente,
                Seu Sistema de Monitoramento.
                """

                message = Mail(
                    from_email=EMAIL_RELATORIOS,
                    to_emails=EMAIL_RELATORIOS,
                    subject=f"Relatório Semanal de Tokens - {CLIENT_NAME_FROM_DB} - {hoje.strftime('%d/%m')}",
                    plain_text_content=corpo_email_texto
                )
                
                sendgrid_client = SendGridAPIClient(SENDGRID_API_KEY)
                response = sendgrid_client.send(message)
                
                if response.status_code == 202:
                    print(f"✅ Relatório semanal para '{CLIENT_NAME_FROM_DB}' enviado com sucesso via SendGrid!")
                else:
                    print(f"❌ Erro ao enviar e-mail para '{CLIENT_NAME_FROM_DB}'. Status: {response.status_code}. Body: {response.body}")

            except Exception as e:
                print(f"❌ Erro ao gerar relatório para o DB '{db_name}': {e}")
                
    except Exception as e:
        print(f"❌ Erro fatal ao listar bancos de dados para relatório: {e}")


# --- FUNÇÃO PRINCIPAL DA IA (REATORADA PARA ARRAY) ---

def gerar_resposta_ia(client_db, contact_id, sender_name, user_message, known_customer_name):
    """
    (Reatorada - LÓGICA DE ARRAY) Gera uma resposta usando a IA.
    Agora lê os PROMPTS em formato ARRAY do 'client_db.config' e os junta.
    """
    global modelo_ia 

    if not modelo_ia:
        return "Desculpe, estou com um problema interno (modelo IA não carregado)."

    # (NOVA LÓGICA - PLANO D) Carregar a configuração (em formato Array)
    try:
        config_data = client_db.config.find_one({"_id": "configuracao"})
        if not config_data:
            print(f"❌ ERRO: Não foi possível encontrar 'configuracao' no DB {client_db.name}")
            return "Desculpe, estou com um problema interno (config não encontrada)."
        
        # Carrega os templates (que são Arrays)
        main_template_array = config_data.get("prompt_main_template")
        capture_rule_array = config_data.get("prompt_name_capture_rule")
        known_rule_string = config_data.get("prompt_name_known_rule") # Este é uma string simples

        if not all([main_template_array, capture_rule_array, known_rule_string]):
             print(f"❌ ERRO: Um dos templates (main, capture, known) está faltando no DB {client_db.name}")
             return "Desculpe, estou com um problema interno (prompt template missing)."

        # (MUDANÇA CRÍTICA) Junta os arrays de volta em strings
        SYSTEM_PROMPT_TEMPLATE = "\n".join(main_template_array)
        NAME_CAPTURE_RULE_TEMPLATE = "\n".join(capture_rule_array)
        
    except Exception as e:
        print(f"❌ ERRO ao ler config do DB {client_db.name}: {e}")
        return "Desculpe, estou com um problema interno (DB config read error)."

    print(f"🧠 Lendo o estado do DB {client_db.name} para {contact_id}...")
    convo_data = load_conversation_from_db(client_db, contact_id)
    old_history = []
    
    if convo_data:
        known_customer_name = convo_data.get('customer_name', known_customer_name) 
        if 'history' in convo_data:
            history_from_db = [msg for msg in convo_data['history'] if not msg.get('text', '').strip().startswith("A data e hora atuais são:")]
            
            for msg in history_from_db:
                role = msg.get('role', 'user')
                if role == 'assistant':
                    role = 'model'
                
                if 'text' in msg:
                    old_history.append({'role': role, 'parts': [msg['text']]})
    if known_customer_name:
        print(f"👤 Cliente já conhecido pelo DB: {known_customer_name}")

    try:
        fuso_horario_local = pytz.timezone('America/Sao_Paulo')
        agora_local = datetime.now(fuso_horario_local)
        horario_atual = agora_local.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    prompt_name_instruction = ""
    
    # (LÓGICA DE MONTAGEM) Decide qual regra de nome usar
    if known_customer_name:
        # Usa a regra simples (String)
        prompt_name_instruction = known_rule_string.format(customer_name=known_customer_name)
    else:
        # Usa a regra complexa (que veio do Array)
        prompt_name_instruction = NAME_CAPTURE_RULE_TEMPLATE.format(sender_name=sender_name)
    
    # --- MONTAGEM DO PROMPT FINAL ---
    try:
        prompt_inicial_de_sistema = SYSTEM_PROMPT_TEMPLATE.format(
            horario_atual=horario_atual,
            prompt_name_instruction=prompt_name_instruction
        )
    except KeyError as e:
        print(f"❌ ERRO DE FORMATAÇÃO DE PROMPT (DB: {client_db.name}): Chave {e} não encontrada no template.")
        return "Desculpe, estou com um problema interno (prompt format error)."

    customer_name_to_save = known_customer_name

    try:
        # 1. Inicializa o modelo COM a instrução de sistema
        modelo_com_sistema = genai.GenerativeModel(
            modelo_ia.model_name,
            system_instruction=prompt_inicial_de_sistema 
        )
        
        # 2. Inicia o chat SÓ com o histórico
        chat_session = modelo_com_sistema.start_chat(history=old_history) 
        
        print(f"Enviando para a IA (DB: {client_db.name}): '{user_message}' (De: {sender_name})")
        
        try:
            input_tokens = modelo_com_sistema.count_tokens(chat_session.history + [{'role':'user', 'parts': [user_message]}]).total_tokens
        except Exception:
            input_tokens = 0

        resposta = chat_session.send_message(user_message)
        
        try:
            output_tokens = modelo_com_sistema.count_tokens(resposta.text).total_tokens
        except Exception:
            output_tokens = 0
            
        total_tokens_na_interacao = input_tokens + output_tokens
        
        if total_tokens_na_interacao > 0:
            print(f"📊 Consumo de Tokens (DB: {client_db.name}): Total={total_tokens_na_interacao}")
        
        ai_reply = resposta.text

        # Lógica de extração de nome (sem mudança)
        if ai_reply.strip().startswith("[NOME_CLIENTE]"):
            print("📝 Tag [NOME_CLIENTE] detectada. Extraindo e salvando nome...")
            try:
                name_part = ai_reply.split("[HUMAN_INTERVENTION]")[0]
                full_response_part = name_part.split("O nome do cliente é:")[1].strip()
                extracted_name = full_response_part.split('.')[0].strip()
                extracted_name = extracted_name.split(' ')[0].strip() 
                
                client_db.conversations.update_one(
                    {'_id': contact_id},
                    {'$set': {'customer_name': extracted_name}},
                    upsert=True
                )
                customer_name_to_save = extracted_name
                print(f"✅ Nome '{extracted_name}' salvo para {contact_id} (DB: {client_db.name}).")

                if "[HUMAN_INTERVENTION]" in ai_reply:
                    ai_reply = "[HUMAN_INTERVENTION]" + ai_reply.split("[HUMAN_INTERVENTION]")[1]
                else:
                    start_of_message_index = full_response_part.find(extracted_name) + len(extracted_name)
                    ai_reply = full_response_part[start_of_message_index:].lstrip('.!?, ').strip()

            except Exception as e:
                print(f"❌ Erro ao extrair o nome da tag: {e}")
                ai_reply = ai_reply.replace("[NOME_CLIENTE]", "").strip()

        if not ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
             save_conversation_to_db(client_db, contact_id, sender_name, customer_name_to_save, total_tokens_na_interacao)
        
        return ai_reply
    
    except Exception as e:
        print(f"❌ Erro ao comunicar com a API do Gemini (DB: {client_db.name}): {e}")
        return "Desculpe, estou com um problema técnico no momento (IA_GEN_FAIL). Por favor, tente novamente em um instante."

# --- INICIALIZAÇÃO E WEBHOOKS ---
scheduler = BackgroundScheduler(daemon=True, timezone='America/Sao_Paulo')
scheduler.start()

app = Flask(__name__)
processed_messages = set() 

@app.route('/webhook', methods=['POST'])
def receive_webhook():
    """
    (Reatorada) Recebe o webhook, identifica a INSTÂNCIA e passa para o buffer.
    """
    data = request.json
    print(f"📦 DADO BRUTO RECEBIDO NO WEBHOOK: {data}")

    # --- (NOVA LÓGICA) IDENTIFICAÇÃO DA INSTÂNCIA ---
    instance_name = data.get('instance')
    if not instance_name:
        print("➡️ Ignorando evento: JSON sem 'instance'.")
        return jsonify({"status": "ignored_no_instance"}), 200
    
    print(f"➡️ Evento para Instância: {instance_name}")
    # --- FIM DA NOVA LÓGICA ---

    event_type = data.get('event')
    
    if event_type and event_type != 'messages.upsert':
        print(f"➡️ Ignorando evento: {event_type} (não é uma nova mensagem)")
        return jsonify({"status": "ignored_event_type"}), 200

    try:
        message_data = data.get('data', {}) 
        if not message_data:
             message_data = data
            
        key_info = message_data.get('key', {})
        if not key_info:
            print("➡️ Evento sem 'key'. Ignorando.")
            return jsonify({"status": "ignored_no_key"}), 200
            
        message_id = key_info.get('id')
        if not message_id:
            return jsonify({"status": "ignored_no_id"}), 200

        if message_id in processed_messages:
            print(f"⚠️ Mensagem {message_id} já processada, ignorando.")
            return jsonify({"status": "ignored_duplicate"}), 200
        processed_messages.add(message_id)
        if len(processed_messages) > 1000:
            processed_messages.clear()

        handle_message_buffering(instance_name, message_data) 
        
        return jsonify({"status": "received"}), 200

    except Exception as e:
        print(f"❌ Erro inesperado no webhook: {e}")
        print("DADO QUE CAUSOU ERRO:", data)
        return jsonify({"status": "error"}), 500

@app.route('/', methods=['GET'])
def health_check():
    return "Servidor Cérebro-Mestre de Chatbots - Estou vivo!", 200

# --- LÓGICA DE BUFFER (REATORADA) ---

def handle_message_buffering(instance_name, message_data):
    """
    (Reatorada) Agrupa mensagens e dispara o processamento, passando a 'instance_name'.
    """
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
            print(f"🎤 Áudio recebido (Instância: {instance_name}), processando imediatamente...")
            threading.Thread(target=process_message_logic, args=(instance_name, message_data, None)).start()
            return
        
        if message.get('conversation'):
            user_message_content = message['conversation']
        elif message.get('extendedTextMessage'):
            user_message_content = message['extendedTextMessage'].get('text')
        
        if not user_message_content:
            print("➡️ Mensagem sem conteúdo de texto ignorada pelo buffer.")
            return

        if clean_number not in message_buffer:
            message_buffer[clean_number] = []
        message_buffer[clean_number].append(user_message_content)
        
        print(f"📥 Mensagem adicionada ao buffer de {clean_number} (Instância: {instance_name}): '{user_message_content}'")

        if clean_number in message_timers:
            message_timers[clean_number].cancel()

        timer = threading.Timer(
            BUFFER_TIME_SECONDS, 
            _trigger_ai_processing, 
            args=[instance_name, clean_number, message_data] 
        )
        message_timers[clean_number] = timer
        timer.start()
        print(f"⏰ Buffer de {clean_number} resetado. Aguardando {BUFFER_TIME_SECONDS}s...")

    except Exception as e:
        print(f"❌ Erro no 'handle_message_buffering': {e}")
            
def _trigger_ai_processing(instance_name, clean_number, last_message_data):
    """
    (Reatorada) Função chamada pelo Timer. Passa 'instance_name' para a IA.
    """
    global message_buffer, message_timers
    
    if clean_number not in message_buffer:
        return 

    messages_to_process = message_buffer.pop(clean_number, [])
    if clean_number in message_timers:
        del message_timers[clean_number]
        
    if not messages_to_process:
        return

    full_user_message = ". ".join(messages_to_process)
    
    print(f"⚡️ DISPARANDO IA para {clean_number} (Instância: {instance_name}) com msg: '{full_user_message}'")

    threading.Thread(target=process_message_logic, args=(instance_name, last_message_data, full_user_message)).start()

# --- LÓGICA DE COMANDO E PROCESSAMENTO (REATORADAS) ---

def handle_responsible_command(client_db, instance_name, message_content, responsible_number):
    """
    (Reatorada) Processa comandos do responsável, usando o DB e Instância corretos.
    """
    print(f"⚙️ Processando comando do responsável (DB: {client_db.name}): '{message_content}'")
    
    conversation_collection = client_db.conversations
    command_lower = message_content.lower().strip()
    command_parts = command_lower.split()

    if command_lower == "bot off":
        try:
            conversation_collection.update_one(
                {'_id': 'BOT_STATUS'},
                {'$set': {'is_active': False}},
                upsert=True
            )
            send_whatsapp_message(instance_name, responsible_number, f"✅ *Bot PAUSADO* (Apenas para o cliente: {client_db.name}).")
            return True
        except Exception as e:
            send_whatsapp_message(instance_name, responsible_number, f"❌ Erro ao pausar o bot (DB: {client_db.name}): {e}")
            return True

    elif command_lower == "bot on":
        try:
            conversation_collection.update_one(
                {'_id': 'BOT_STATUS'},
                {'$set': {'is_active': True}},
                upsert=True
            )
            send_whatsapp_message(instance_name, responsible_number, f"✅ *Bot REATIVADO* (Para o cliente: {client_db.name}).")
            return True
        except Exception as e:
            send_whatsapp_message(instance_name, responsible_number, f"❌ Erro ao reativar o bot (DB: {client_db.name}): {e}")
            return True

    if len(command_parts) == 2 and command_parts[0] == "ok":
        customer_number_to_reactivate = command_parts[1].replace('@s.whatsapp.net', '').strip()
        
        try:
            customer = conversation_collection.find_one({'_id': customer_number_to_reactivate})

            if not customer:
                send_whatsapp_message(instance_name, responsible_number, f"⚠️ *Atenção (DB: {client_db.name}):* O cliente `{customer_number_to_reactivate}` não foi encontrado.")
                return True 

            result = conversation_collection.update_one(
                {'_id': customer_number_to_reactivate},
                {'$set': {'intervention_active': False}}
            )

            if result.modified_count > 0:
                send_whatsapp_message(instance_name, responsible_number, f"✅ Atendimento automático reativado para `{customer_number_to_reactivate}` (Cliente: {client_db.name}).")
                send_whatsapp_message(instance_name, customer_number_to_reactivate, "Oi sou eu a Lyra novamente, espero que tenha resolvido o que precisava.\nSe quiser tirar mais alguma duvida só me avisar!😊")
            else:
                send_whatsapp_message(instance_name, responsible_number, f"ℹ️ O atendimento para `{customer_number_to_reactivate}` já estava ativo.")
            
            return True 
        except Exception as e:
            print(f"❌ Erro ao tentar reativar cliente: {e}")
            send_whatsapp_message(instance_name, responsible_number, f"❌ Erro técnico ao reativar cliente (DB: {client_db.name}).")
            return True
            
    help_message = (
        f"Comando não reconhecido para o cliente '{client_db.name}'. 🤖\n\n"
        "*COMANDOS DISPONÍVEIS:*\n"
        "1️⃣ `bot on` (Liga o bot para este cliente)\n"
        "2️⃣ `bot off` (Desliga o bot para este cliente)\n"
        "3️⃣ `ok <numero_do_cliente>` (Reativa um cliente em intervenção)"
    )
    send_whatsapp_message(instance_name, responsible_number, help_message)
    return True

def process_message_logic(instance_name, message_data, buffered_message_text=None):
    """
    (Reatorada) Esta é a função "worker" principal.
    Ela se conecta ao DB do cliente baseado na 'instance_name'.
    """
    global client
    lock_acquired = False
    clean_number = None
    client_db = None
    
    if not client:
        print(f"❌ Processamento (Instância: {instance_name}) falhou: Cliente Mongo não está conectado.")
        return

    try:
        # --- (NOVA LÓGICA) Conexão e Configuração Dinâmica ---
        try:
            # AQUI ESTÁ A MÁGICA: Conecta ao DB com o nome da instância
            client_db = client[instance_name] 
            config_data = client_db.config.find_one({"_id": "configuracao"})
            
            if not config_data:
                print(f"❌ ERRO GRAVE: Instância '{instance_name}' não possui documento 'configuracao' no MongoDB. Mensagem ignorada.")
                return
            
            # Carrega as variáveis específicas do cliente
            RESPONSIBLE_NUMBER_FROM_DB = config_data.get("responsible_number")
            INSTANCE_NAME_FROM_DB = config_data.get("evolution_instance_name") 
            
            if not all([RESPONSIBLE_NUMBER_FROM_DB, INSTANCE_NAME_FROM_DB]):
                 print(f"❌ ERRO GRAVE (DB: {instance_name}): 'responsible_number' ou 'evolution_instance_name' não estão no 'config'.")
                 return
                 
        except Exception as e:
            print(f"❌ ERRO GRAVE ao carregar config do DB para instância '{instance_name}': {e}")
            return
        # --- Fim da Lógica de Configuração ---

        conversation_collection = client_db.conversations

        key_info = message_data.get('key', {})
        sender_number_full = key_info.get('senderPn') or key_info.get('participant') or key_info.get('remoteJid')
        if not sender_number_full or sender_number_full.endswith('@g.us'): return
        
        clean_number = sender_number_full.split('@')[0]
        sender_name_from_wpp = message_data.get('pushName') or 'Cliente'
        
        if key_info.get('fromMe'):
            if not sender_number_full:
                return 
            
            if clean_number != RESPONSIBLE_NUMBER_FROM_DB:
                print(f"➡️ Mensagem do próprio bot ignorada (remetente: {clean_number}, Instância: {instance_name}).")
                return 
            
            print(f"⚙️ Mensagem do próprio bot PERMITIDA (Comando do responsável: {clean_number}, Instância: {instance_name}).")

        # --- Lógica de LOCK (Reatorada) ---
        now = datetime.now()
        res = conversation_collection.update_one(
            {'_id': clean_number, 'processing': {'$ne': True}},
            {'$set': {'processing': True, 'processing_started_at': now}},
            upsert=True
        )

        if res.matched_count == 0 and res.upserted_id is None:
            print(f"⏳ {clean_number} já está sendo processado (lock). Reagendando (Instância: {instance_name})...")
            if buffered_message_text:
                if clean_number not in message_buffer: message_buffer[clean_number] = []
                message_buffer[clean_number].insert(0, buffered_message_text)
            
            timer = threading.Timer(10.0, _trigger_ai_processing, args=[instance_name, clean_number, message_data])
            message_timers[clean_number] = timer
            timer.start()
            return 
        
        lock_acquired = True
        if res.upserted_id:
            print(f"✅ Novo usuário {clean_number} (DB: {instance_name}). Documento criado e lock adquirido.")
        # --- Fim do Lock ---
        
        user_message_content = None
        
        if buffered_message_text:
            user_message_content = buffered_message_text
            messages_to_save = user_message_content.split(". ")
            for msg_text in messages_to_save:
                if msg_text and msg_text.strip():
                    append_message_to_db(client_db, clean_number, 'user', msg_text)
        else:
            message = message_data.get('message', {})
            if message.get('audioMessage') and message.get('base64'):
                message_id = key_info.get('id')
                print(f"🎤 Mensagem de áudio recebida de {clean_number} (DB: {instance_name}). Transcrevendo...")
                audio_base64 = message['base64']
                audio_data = base64.b64decode(audio_base64)
                os.makedirs("/tmp", exist_ok=True)
                temp_audio_path = f"/tmp/audio_{instance_name}_{clean_number}_{message_id}.ogg"
                with open(temp_audio_path, 'wb') as f: f.write(audio_data)
                user_message_content = transcrever_audio_gemini(temp_audio_path)
                try:
                    os.remove(temp_audio_path)
                except Exception as e:
                    print(f"Aviso: não foi possível remover áudio temporário. {e}")
                if not user_message_content:
                    send_whatsapp_message(INSTANCE_NAME_FROM_DB, sender_number_full, "Desculpe, não consegui entender o áudio. Pode tentar novamente? 🎧")
                    user_message_content = "[Usuário enviou um áudio incompreensível]"
            
            if not user_message_content:
                user_message_content = "[Usuário enviou uma mensagem não suportada]"
                
            append_message_to_db(client_db, clean_number, 'user', user_message_content)

        print(f"🧠 Processando Mensagem de {clean_number} (DB: {instance_name}): '{user_message_content}'")
        
        if clean_number == RESPONSIBLE_NUMBER_FROM_DB:
            if handle_responsible_command(client_db, INSTANCE_NAME_FROM_DB, user_message_content, RESPONSIBLE_NUMBER_FROM_DB):
                return 
        
        try:
            bot_status_doc = conversation_collection.find_one({'_id': 'BOT_STATUS'})
            is_active = bot_status_doc.get('is_active', True) if bot_status_doc else True 
            
            if not is_active:
                print(f"🤖 Bot está em standby (desligado) para {instance_name}. Ignorando {clean_number}.")
                return
                
        except Exception as e:
            print(f"⚠️ Erro ao verificar o status do bot (DB: {instance_name}): {e}. Assumindo que está ligado.")

        conversation_status = conversation_collection.find_one({'_id': clean_number})

        if conversation_status and conversation_status.get('intervention_active', False):
            print(f"⏸️ Conversa com {clean_number} (DB: {instance_name}) pausada para atendimento humano.")
            return 

        known_customer_name = conversation_status.get('customer_name') if conversation_status else None
        
        ai_reply = gerar_resposta_ia(
            client_db,
            clean_number,
            sender_name_from_wpp,
            user_message_content,
            known_customer_name
        )
        
        if not ai_reply:
            print(f"⚠️ A IA não gerou resposta (DB: {instance_name}).")
            return

        try:
            append_message_to_db(client_db, clean_number, 'assistant', ai_reply)
            
            if ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
                print(f"‼️ INTERVENÇÃO HUMANA SOLICITADA para {clean_number} (DB: {instance_name})")
                
                conversation_collection.update_one(
                    {'_id': clean_number}, {'$set': {'intervention_active': True}}, upsert=True
                )
                
                send_whatsapp_message(INSTANCE_NAME_FROM_DB, sender_number_full, "Entendido. Já avisei um especialista. Por favor, aguarde um momento. 👨‍💼")
                
                if RESPONSIBLE_NUMBER_FROM_DB:
                    reason = ai_reply.replace("[HUMAN_INTERVENTION] Motivo:", "").strip()
                    display_name = known_customer_name or sender_name_from_wpp
                    
                    history_com_ultima_msg = load_conversation_from_db(client_db, clean_number).get('history', [])
                    history_summary = get_last_messages_summary(history_com_ultima_msg)

                    notification_msg = (
                        f"🔔 *NOVA SOLICITAÇÃO DE ATENDIMENTO HUMANO* 🔔\n\n"
                        f"🤖 *Cliente:* {config_data.get('client_name', instance_name)}\n"
                        f"👤 *Usuário:* {display_name}\n"
                        f"📞 *Número:* `{clean_number}`\n\n"
                        f"💬 *Motivo da Chamada:*\n_{reason}_\n\n"
                        f"📜 *Resumo da Conversa:*\n{history_summary}\n\n"
                        f"-----------------------------------\n"
                        f"*AÇÃO NECESSÁRIA:*\nApós resolver, envie para *ESTE NÚMERO* o comando:\n`ok {clean_number}`"
                    )
                    send_whatsapp_message(INSTANCE_NAME_FROM_DB, f"{RESPONSIBLE_NUMBER_FROM_DB}@s.whatsapp.net", notification_msg)
            
            else:
                print(f"🤖 Resposta da IA para {clean_number} (DB: {instance_name}): {ai_reply}")
                send_whatsapp_message(INSTANCE_NAME_FROM_DB, sender_number_full, ai_reply)

        except Exception as e:
            print(f"❌ Erro ao processar envio ou intervenção (DB: {instance_name}): {e}")
            send_whatsapp_message(INSTANCE_NAME_FROM_DB, sender_number_full, "Desculpe, tive um problema ao processar sua resposta. (Erro interno: SEND_LOGIC)")

    except Exception as e:
        print(f"❌ Erro fatal ao processar mensagem (DB: {instance_name}): {e}")
    finally:
        if clean_number and lock_acquired and client_db: 
            client_db.conversations.update_one(
                {'_id': clean_number},
                {'$unset': {'processing': "", 'processing_started_at': ""}}
            )
            print(f"🔓 Lock liberado para {clean_number} (DB: {instance_name}).")


# --- INICIALIZAÇÃO DO SERVIÇO ---
if modelo_ia and client:
    print("\n=============================================")
    print("      CHATBOT CÉREBRO-MESTRE INICIADO")
    print(f"      Conectado ao Evolution: {EVOLUTION_API_URL}")
    print(f"      Conectado ao Mongo: {MONGO_DB_URI.split('@')[-1].split('/')[0]}")
    print("=============================================")
    print("Servidor aguardando webhooks de TODAS as instâncias...")

    scheduler.add_job(gerar_e_enviar_relatorio_semanal, 'cron', day_of_week='sun', hour=8, minute=0)
    print("⏰ Agendador de relatórios (Multi-Cliente) iniciado. O relatório será enviado todo Domingo às 08:00.")
    
    import atexit
    atexit.register(lambda: scheduler.shutdown())
    
else:
    print("\nEncerrando o programa devido a erros na inicialização (Verifique Mongo, Gemini ou Cliente).")

if __name__ == '__main__':
    print("Iniciando em MODO DE DESENVOLVIMENTO LOCAL (app.run)...")
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port, debug=False)