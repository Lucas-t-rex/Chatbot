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

CLIENT_NAME = "Mengatto Estratégia Digital" # <--- EDITAR NOME DO CLIENTE
RESPONSIBLE_NUMBER = "554898389781" # <--- EDITAR: Número do responsável com 55+DDD

load_dotenv()
EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL") # <--- EDITAR NO .ENV
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234") # <--- EDITAR NO .ENV
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") # <--- EDITAR NO .ENV
MONGO_DB_URI = os.environ.get("MONGO_DB_URI") # <--- EDITAR NO .ENV


message_buffer = {}
message_timers = {}
BUFFER_TIME_SECONDS = 8 


try:
    client = MongoClient(MONGO_DB_URI)
    db_name = CLIENT_NAME.lower().replace(" ", "_").replace("-", "_")
    db = client[db_name] 
    conversation_collection = db.conversations
    
    print(f"✅ Conectado ao MongoDB para o cliente: '{CLIENT_NAME}' no banco de dados '{db_name}'")
except Exception as e:
    print(f"❌ ERRO: Não foi possível conectar ao MongoDB. Erro: {e}")

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"AVISO: A chave de API do Google não foi configurada corretamente. Erro: {e}")
else:
    print("AVISO: A variável de ambiente GEMINI_API_KEY não foi definida.")


modelo_ia = None
try:
    modelo_ia = genai.GenerativeModel('gemini-2.5-flash')
    print("✅ Modelo do Gemini (gemini-2.5-flash) inicializado com sucesso.")
except Exception as e:
    print(f"❌ ERRO: Não foi possível inicializar o modelo do Gemini. Verifique sua API Key. Erro: {e}")

# <--- MELHORIA: Nova função para salvar mensagens individuais ---
def append_message_to_db(contact_id, role, text, message_id=None):
    """Salva uma única mensagem no histórico do DB."""
    try:
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
# --- Fim da Melhoria ---

# <--- MELHORIA: Função de salvar foi simplificada para salvar apenas METADADOS ---
def save_conversation_to_db(contact_id, sender_name, customer_name, tokens_used):
    """Salva metadados (nomes, tokens) no MongoDB."""
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
# --- Fim da MelhorIA ---

# <--- MELHORIA: Função de carregar agora ordena o histórico por data/hora ---
def load_conversation_from_db(contact_id):
    """Carrega o histórico de uma conversa do MongoDB, ordenando por timestamp."""
    try:
        result = conversation_collection.find_one({'_id': contact_id})
        if result:
            # Garante que 'history' exista e ordena
            history = result.get('history', [])
            history_sorted = sorted(history, key=lambda m: m.get('ts', ''))
            result['history'] = history_sorted
            print(f"🧠 Histórico anterior encontrado e carregado para {contact_id} ({len(history_sorted)} entradas).")
            return result
    except Exception as e:
        print(f"❌ Erro ao carregar conversa do MongoDB para {contact_id}: {e}")
    return None
# --- Fim da Melhoria ---

# (Função 'get_last_messages_summary' mantida - é essencial para a intervenção)
def get_last_messages_summary(history, max_messages=4):
    """Formata as últimas mensagens de um histórico para um resumo legível, ignorando prompts do sistema."""
    summary = []
    
    # <--- MELHORIA: Pequena correção no 'get_last_messages_summary' ---
    # O histórico agora vem no formato {'role': ..., 'text': ...}
    relevant_history = history[-max_messages:]
    
    for message in relevant_history:
        role = "Cliente" if message.get('role') == 'user' else "Bot"
        text = message.get('text', '').strip()

        # Ignora prompts do sistema (esta parte é do seu código de intervenção, mas adaptada)
        if role == "Cliente" and text.startswith("A data e hora atuais são:"):
            continue 
        if role == "Bot" and text.startswith("Entendido. A Regra de Ouro"):
            continue 
            
        summary.append(f"*{role}:* {text}")
        
    if not summary:
        # Pega a última mensagem de texto do cliente se o histórico estiver "poluído"
        # Esta é uma salvaguarda
        user_messages = [msg.get('text') for msg in history if msg.get('role') == 'user' and not msg.get('text', '').startswith("A data e hora atuais são:")]
        if user_messages:
            return f"*Cliente:* {user_messages[-1]}"
        else:
            return "Nenhum histórico de conversa encontrado."
            
    return "\n".join(summary)
# --- Fim da Melhoria ---

def gerar_resposta_ia(contact_id, sender_name, user_message, known_customer_name):
    """
    (VERSÃO MELHORADA)
    Gera uma resposta usando a IA, agora usando o carregamento do DB (stateless).
    """
    global modelo_ia

    if not modelo_ia:
        return "Desculpe, estou com um problema interno (modelo IA não carregado)."

    # <--- MELHORIA: Lógica de cache removida. Carrega do DB a cada chamada ---
    print(f"🧠 Lendo o estado do DB para {contact_id}...")
    convo_data = load_conversation_from_db(contact_id)
    old_history = []
    
    if convo_data:
        # 'known_customer_name' passado como parâmetro é atualizado se já existir no DB
        known_customer_name = convo_data.get('customer_name', known_customer_name) 
        if 'history' in convo_data:
            # Converte o histórico do DB para o formato do Gemini
            history_from_db = [msg for msg in convo_data['history'] if not msg.get('text', '').strip().startswith("A data e hora atuais são:")]
            for msg in history_from_db:
                role = msg.get('role', 'user')
                if role == 'assistant':
                    role = 'model'
                
                if 'text' in msg:
                    old_history.append({
                        'role': role,
                        'parts': [msg['text']]
                    })
    if known_customer_name:
        print(f"👤 Cliente já conhecido pelo DB: {known_customer_name}")
    # --- Fim da Melhoria ---

    try: # <--- MELHORIA: Adicionado try/except para fuso horário ---
        fuso_horario_local = pytz.timezone('America/Sao_Paulo')
        agora_local = datetime.now(fuso_horario_local)
        horario_atual = agora_local.strftime("%Y-%m-%d %H:%M:%S")
        print(f"⏰ Hora local (America/Sao_Paulo) definida para: {horario_atual}")
    except Exception as e:
        print(f"⚠️ Erro ao definir fuso horário, usando hora do servidor. Erro: {e}")
        horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    prompt_name_instruction = ""
    final_user_name_for_prompt = ""

    # (Lógica de nome do cliente mantida, mas agora usa 'known_customer_name' vindo do DB)
    if known_customer_name:
        final_user_name_for_prompt = known_customer_name
        prompt_name_instruction = f"O nome do usuário com quem você está falando é: {final_user_name_for_prompt}. Trate-o por este nome."
    else:
        final_user_name_for_prompt = sender_name
        # (Este prompt de captura de nome é do seu código de intervenção)
        prompt_name_instruction = f"""
            REGRA CRÍTICA - CAPTURA DE NOME INTELIGENTE (PRIORIDADE MÁXIMA):
              Seu nome é {{Lyra}} e você é atendente da {{Mengatto Estratégia Digital}}.
              Seu primeiro objetivo é sempre descobrir o nome real do cliente, pois o nome de contato ('{sender_name}') pode ser um apelido. No entanto, você deve fazer isso de forma natural.
              1. Se a primeira mensagem do cliente for um simples cumprimento (ex: "oi", "boa noite"), peça o nome dele de forma direta e educada.
              2. Se a primeira mensagem do cliente já contiver uma pergunta (ex: "oi, qual o preço?", "quero saber como funciona"), você deve:
                 - Primeiro, acalmar o cliente dizendo que já vai responder.
                 - Em seguida, peça o nome para personalizar o atendimento.
                 - *IMPORTANTE*: Você deve guardar a pergunta original do cliente na memória.
              3. Quando o cliente responder com o nome dele (ex: "Meu nome é Marcos"), sua próxima resposta DEVE OBRIGATORIAMENTE:
                 - Começar com a tag: [NOME_CLIENTE]O nome do cliente é: [Nome Extraído].
                 - Agradecer ao cliente pelo nome.
                 - *RESPONDER IMEDIATAMENTE à pergunta original que ele fez no início da conversa.* Não o faça perguntar de novo.
              - *IMPORTANTE*: A simples apresentação do nome do cliente (ex: "meu nome é marcos") NÃO é um motivo para intervenção. Continue a conversa normalmente nesses casos.

              EXEMPLO DE FLUXO IDEAL:
              Cliente: "boa noite, queria saber o preço da assessoria"
              Você: "Boa noite! Claro, já te passo os detalhes da assessoria. Para que nosso atendimento fique mais próximo, como posso te chamar?"
              Cliente: "pode me chamar de Marcos"
              Sua Resposta: "[NOME_CLIENTE]O nome do cliente é: Marcos. Prazer em conhecê-lo, Marcos! A assessoria é voltada para negócios que buscam posicionamento estratégico e crescimento real no digital. Posso te explicar como ela funciona?"
            """
        
        prompt_inicial = f"""
              A data e hora atuais são: {horario_atual}.
              {prompt_name_instruction}
              Dever : vender nossos serviços ou, se o cliente quiser falar com o Raffael (proprietário), acionar intervenção.
              =====================================================
              🆘 REGRA DE OURO: ANÁLISE DE INTENÇÃO E INTERVENÇÃO HUMANA
              =====================================================
              - Sua função é identificar a intenção do cliente.
              - Se o cliente pedir para falar com o Raffael, o proprietário, ou quiser negociar algo fora do script, acione a intervenção.
              [HUMAN_INTERVENTION] Motivo: [Resumo da intenção]
              =====================================================
              🏷️ IDENTIDADE DO ATENDENTE
              =====================================================
              nome: {{Lyra}}
              sexo: {{Feminina}}
              idade: {{40}}
              função: {{Atendente, especialista em marketing e automação}} 
              papel: {{Compreender o negócio do cliente, indicar o serviço ideal e conduzir o fechamento da proposta.}}
              =====================================================
              🏢 IDENTIDADE DA EMPRESA
              =====================================================
              nome da empresa: {{Mengatto Estratégia Digital}}
              setor: {{Marketing, Tecnologia e Automação}}
              missão: {{Conectar propósito, estratégia e tecnologia para gerar resultados reais.}}
              valores: {{Autenticidade, clareza, performance e consciência.}}
              horário de atendimento: {{Segunda a sexta, das 8h às 18h}}
              endereço: {{Treze Tílias - SC, Brasil}}
              =====================================================
              🏛️ HISTÓRIA DA EMPRESA
              =====================================================
              {{Criada por Raffael Mengatto, estrategista digital e mentor de performance, a Mengatto Estratégia Digital nasceu para transformar negócios em marcas conscientes. 
              Unindo o humano e o tecnológico, a empresa entrega estratégias de posicionamento, automação e presença digital real — com inteligência aplicada à alma do negócio.}}
              =====================================================
              ℹ️ INFORMAÇÕES GERAIS
              =====================================================
              público-alvo: {{Empreendedores, terapeutas, prestadores de serviço e empresas que desejam crescer com posicionamento e previsibilidade.}}
              diferencial: {{Atendimento humano, estratégia personalizada e integração com tecnologia de ponta.}}
              slogan: {{Consciência que converte. Estratégia que sustenta.}}
              =====================================================
              💼 SERVIÇOS / SOLUÇÕES
              =====================================================
              - *Assessoria Estratégica 360°*: {{Acompanhamento completo de posicionamento, identidade, funil e campanhas. Foco em crescimento, estrutura e clareza.}}
              - *Acompanhamento 1:1*: {{Imersão personalizada de 30 dias com foco em comunicação, posicionamento, vendas e visão estratégica.}}
              - *Gestão de Tráfego Pago*: {{Planejamento e execução de campanhas no Meta Ads e Google Ads com análise de métricas e otimização constante.}}
              - *Social Media Estratégico*: {{Criação de conteúdo que une estética, propósito e copy magnética para redes sociais.}}
              - *Criação de Sites e Landing Pages*: {{Desenvolvimento profissional de páginas de conversão, institucionais e e-commerce, otimizadas para resultados.}}
              - *Assistente IA – Funcionário Inteligente*: {{Assistente virtual exclusiva, treinada para responder dúvidas sobre o comércio, captar leads e automatizar processos de atendimento. Um “funcionário digital” ativo 24h, que aprende com o negócio e melhora a experiência do cliente.}}
              =====================================================
              💰 PLANOS E INVESTIMENTO
              =====================================================
              - Valores sob consulta conforme personalização e escopo do projeto.
              - Setup inicial: inclui diagnóstico estratégico e estrutura base de integração. 
              =====================================================
              🧭 COMPORTAMENTO DE ATENDIMENTO
              =====================================================
              - Seja profissional, acolhedora e segura.
              - Use frases curtas e claras, mostre interesse genuíno no negócio do cliente.
              - Apresente os serviços como soluções personalizadas.
              - Se o cliente hesitar, ofereça um diagnóstico gratuito de posicionamento.
              =====================================================
              ⚙️ PERSONALIDADE DO ATENDENTE
              =====================================================
              - Tom de voz: {{estratégico, empático e humano}} 
              - Estilo: firme, claro e inspirador.
              - Emojis: usar de forma leve, apenas quando combinar com o tom da conversa.
              =====================================================
              PRONTO PARA ATENDER
              =====================================================
              Quando o cliente enviar mensagem, cumprimente de forma natural, descubra o nome e a necessidade, e conduza o fechamento com empatia e autoridade.
        """

    convo_start = [
        {'role': 'user', 'parts': [prompt_inicial]},
        {'role': 'model', 'parts': [f"Entendido. A Regra de Ouro e a captura de nome são prioridades. Estou pronto. Olá, {final_user_name_for_prompt}! Como posso te ajudar?"]}
    ]

    # <--- MELHORIA: Lógica de cache removida. Cria a sessão de chat com o 'old_history' vindo do DB ---
    chat_session = modelo_ia.start_chat(history=convo_start + old_history)
    customer_name_to_save = known_customer_name # Inicia com o nome que já sabemos
    # --- Fim da Melhoria ---

    try:
        print(f"Enviando para a IA: '{user_message}' (De: {sender_name})")
        
        # <--- MELHORIA: Contagem de tokens agora dentro de try/except ---
        try:
            input_tokens = modelo_ia.count_tokens(chat_session.history + [{'role':'user', 'parts': [user_message]}]).total_tokens
        except Exception:
            input_tokens = 0

        resposta = chat_session.send_message(user_message)
        
        try:
            output_tokens = modelo_ia.count_tokens(resposta.text).total_tokens
        except Exception:
            output_tokens = 0
        # --- Fim da Melhoria ---
            
        total_tokens_na_interacao = input_tokens + output_tokens
        
        if total_tokens_na_interacao > 0: # <--- MELHORIA: Log de tokens ---
            print(f"📊 Consumo de Tokens: Total={total_tokens_na_interacao}")
        
        ai_reply = resposta.text

        # (Lógica de extração de nome mantida - é do seu código de intervenção)
        if ai_reply.strip().startswith("[NOME_CLIENTE]"):
            print("📝 Tag [NOME_CLIENTE] detectada. Extraindo e salvando nome...")
            try:
                full_response_part = ai_reply.split("O nome do cliente é:")[1].strip()
                extracted_name = full_response_part.split('.')[0].strip()
                
                # <--- MELHORIA: Correção para extrair só o primeiro nome se houver lixo ---
                extracted_name = extracted_name.split(' ')[0].strip() 
                
                start_of_message_index = full_response_part.find(extracted_name) + len(extracted_name)
                ai_reply = full_response_part[start_of_message_index:].lstrip('.!?, ').strip()

                # Salva o nome limpo no banco de dados
                conversation_collection.update_one(
                    {'_id': contact_id},
                    {'$set': {'customer_name': extracted_name}},
                    upsert=True
                )
                customer_name_to_save = extracted_name # Atualiza o nome para salvar
                print(f"✅ Nome '{extracted_name}' salvo para o cliente {contact_id}.")

            except Exception as e:
                print(f"❌ Erro ao extrair o nome da tag: {e}")
                ai_reply = ai_reply.replace("[NOME_CLIENTE]", "").strip()

        # <--- MELHORIA: Salva os METADADOS (tokens, nome) ---
        # A função 'append_message_to_db' salvará o histórico
        if not ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
             save_conversation_to_db(contact_id, sender_name, customer_name_to_save, total_tokens_na_interacao)
        
        return ai_reply
    
    except Exception as e:
        print(f"❌ Erro ao comunicar com a API do Gemini: {e}")
        # <--- MELHORIA: Mensagem de erro mais amigável ---
        return "Desculpe, estou com um problema técnico no momento (IA_GEN_FAIL). Por favor, tente novamente em um instante."
    
def transcrever_audio_gemini(caminho_do_audio):
    """
    Envia um arquivo de áudio para a API do Gemini e retorna a transcrição em texto.
    (Função mantida)
    """
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

# <--- MELHORIA: Função de envio de mensagem robusta (do 'codigo atual') ---
def send_whatsapp_message(number, text_message):
    """Envia uma mensagem de texto via Evolution API, corrigindo a URL dinamicamente."""
    
    INSTANCE_NAME = "chatbot" # <--- EDITAR se o nome da sua instância for outro
    
    clean_number = number.split('@')[0]
    payload = {"number": clean_number, "textMessage": {"text": text_message}}
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}

    base_url = EVOLUTION_API_URL
    api_path = f"/message/sendText/{INSTANCE_NAME}"
    
    final_url = ""
    
    # Lógica para corrigir a URL
    if not base_url:
        print("❌ ERRO: EVOLUTION_API_URL não está definida no .env")
        return

    if base_url.endswith(api_path):
        final_url = base_url
    elif base_url.endswith('/'):
        final_url = base_url[:-1] + api_path
    else:
        final_url = base_url + api_path
    # --- Fim da Lógica ---

    try:
        print(f"✅ Enviando resposta para a URL: {final_url} (Destino: {clean_number})")
        response = requests.post(final_url, json=payload, headers=headers)
        
        if response.status_code < 400:
            print(f"✅ Resposta da IA enviada com sucesso para {clean_number}\n")
        else:
            print(f"❌ ERRO DA API EVOLUTION ao enviar para {clean_number}: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Erro de CONEXÃO ao enviar mensagem para {clean_number}: {e}")
# --- Fim da Melhoria ---


def gerar_e_enviar_relatorio_semanal():
    """Calcula um RESUMO do uso de tokens e envia por e-mail usando SendGrid."""
    print(f"🗓️ Gerando relatório semanal para o cliente: {CLIENT_NAME}...")
    
    SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY')
    EMAIL_RELATORIOS = os.environ.get('EMAIL_RELATORIOS')

    if not all([SENDGRID_API_KEY, EMAIL_RELATORIOS]):
        print("⚠️ Variáveis SENDGRID_API_KEY e EMAIL_RELATORIOS não configuradas. Relatório não pode ser enviado.")
        return

    hoje = datetime.now()
    
    try:
        usuarios_do_bot = list(conversation_collection.find({}))
        numero_de_contatos = len(usuarios_do_bot)
        total_geral_tokens = 0
        media_por_contato = 0

        if numero_de_contatos > 0:
            for usuario in usuarios_do_bot:
                total_geral_tokens += usuario.get('total_tokens_consumed', 0)
            media_por_contato = total_geral_tokens / numero_de_contatos
        
        corpo_email_texto = f"""
        Relatório de Consumo Acumulado do Cliente: '{CLIENT_NAME}'
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
            subject=f"Relatório Semanal de Tokens - {CLIENT_NAME} - {hoje.strftime('%d/%m')}",
            plain_text_content=corpo_email_texto
        )
        
        sendgrid_client = SendGridAPIClient(SENDGRID_API_KEY)
        response = sendgrid_client.send(message)
        
        if response.status_code == 202:
             print(f"✅ Relatório semanal para '{CLIENT_NAME}' enviado com sucesso via SendGrid!")
        else:
             print(f"❌ Erro ao enviar e-mail via SendGrid. Status: {response.status_code}. Body: {response.body}")

    except Exception as e:
        print(f"❌ Erro ao gerar ou enviar relatório para '{CLIENT_NAME}': {e}")

# <--- MELHORIA: Scheduler e App inicializados globalmente ---
scheduler = BackgroundScheduler(daemon=True, timezone='America/Sao_Paulo')
scheduler.start()

app = Flask(__name__)
processed_messages = set() # <--- MELHORIA: Adicionado set de mensagens processadas

@app.route('/webhook', methods=['POST'])
def receive_webhook():
    """
    (VERSÃO MELHORADA)
    Recebe mensagens do WhatsApp e as coloca no buffer.
    """
    data = request.json
    print(f"📦 DADO BRUTO RECEBIDO NO WEBHOOK: {data}")

    event_type = data.get('event')
    
    # <--- MELHORIA: Adicionado filtro de 'event' (do 'codigo atual') ---
    if event_type and event_type != 'messages.upsert':
        print(f"➡️  Ignorando evento: {event_type} (não é uma nova mensagem)")
        return jsonify({"status": "ignored_event_type"}), 200

    try:
        # <--- MELHORIA: Lógica de extração de 'data' e 'key' ---
        message_data = data.get('data', {}) 
        if not message_data:
             # Fallback para o formato do 'codigo intervenção'
             message_data = data
             
        key_info = message_data.get('key', {})
        if not key_info:
            print("➡️ Evento sem 'key'. Ignorando.")
            return jsonify({"status": "ignored_no_key"}), 200
        # --- Fim da Melhoria ---

        # (Lógica 'fromMe' mantida, mas adaptada)
        if key_info.get('fromMe'):
            sender_number_full = key_info.get('remoteJid')
            if not sender_number_full:
                return jsonify({"status": "ignored_from_me_no_sender"}), 200
            
            clean_number = sender_number_full.split('@')[0]
            
            if clean_number != RESPONSIBLE_NUMBER:
                print(f"➡️  Mensagem do próprio bot ignorada (remetente: {clean_number}).")
                return jsonify({"status": "ignored_from_me"}), 200
            
            print(f"⚙️  Mensagem do próprio bot PERMITIDA (é um comando do responsável: {clean_number}).")
            # Deixa o comando do responsável passar para a lógica de buffer/processamento

        message_id = key_info.get('id')
        if not message_id:
            return jsonify({"status": "ignored_no_id"}), 200

        # <--- MELHORIA: Verificação de duplicatas ---
        if message_id in processed_messages:
            print(f"⚠️ Mensagem {message_id} já processada, ignorando.")
            return jsonify({"status": "ignored_duplicate"}), 200
        processed_messages.add(message_id)
        if len(processed_messages) > 1000:
            processed_messages.clear()
        # --- Fim da Melhoria ---

        # <--- MELHORIA: Chama o BUFFER em vez de processar direto ---
        handle_message_buffering(message_data)
        # --- Fim da Melhoria ---
        
        return jsonify({"status": "received"}), 200

    except Exception as e:
        print(f"❌ Erro inesperado no webhook: {e}")
        print("DADO QUE CAUSOU ERRO:", data)
        return jsonify({"status": "error"}), 500

@app.route('/', methods=['GET'])
def health_check():
    return f"Estou vivo! ({CLIENT_NAME} Bot - Intervenção)", 200

# <--- MELHORIA: Nova função de buffering (do 'codigo atual') ---
def handle_message_buffering(message_data):
    """
    Agrupa mensagens de um mesmo usuário que chegam rápido
    e dispara o processamento após um 'delay'.
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
        
        # --- Processa ÁUDIO imediatamente ---
        if message.get('audioMessage'):
            print("🎤 Áudio recebido, processando imediatamente (sem buffer)...")
            threading.Thread(target=process_message_logic, args=(message_data, None)).start()
            return
        
        # --- Processa TEXTO no buffer ---
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
        print(f"⏰ Buffer de {clean_number} resetado. Aguardando {BUFFER_TIME_SECONDS}s...")

    except Exception as e:
        print(f"❌ Erro no 'handle_message_buffering': {e}")
# --- Fim da Melhoria ---
            
# <--- MELHORIA: Nova função de trigger (do 'codigo atual') ---
def _trigger_ai_processing(clean_number, last_message_data):
    """
    Função chamada pelo Timer. Junta as mensagens e chama a IA.
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
    
    print(f"⚡️ DISPARANDO IA para {clean_number} com mensagem agrupada: '{full_user_message}'")

    threading.Thread(target=process_message_logic, args=(last_message_data, full_user_message)).start()
# --- Fim da Melhoria ---


# (Função 'handle_responsible_command' mantida - é essencial para a intervenção)
def handle_responsible_command(message_content, responsible_number):
    """
    Processa comandos enviados pelo número do responsável.
    """
    print(f"⚙️  Processando comando do responsável: '{message_content}'")
    
    command_parts = message_content.lower().strip().split()

    if len(command_parts) == 2 and command_parts[0] == "ok":
        customer_number_to_reactivate = command_parts[1].replace('@s.whatsapp.net', '').strip()
        
        try:
            customer = conversation_collection.find_one({'_id': customer_number_to_reactivate})

            if not customer:
                send_whatsapp_message(responsible_number, f"⚠️ *Atenção:* O cliente com o número `{customer_number_to_reactivate}` não foi encontrado no banco de dados.")
                return 

            result = conversation_collection.update_one(
                {'_id': customer_number_to_reactivate},
                {'$set': {'intervention_active': False}}
            )

            # <--- MELHORIA: 'conversations_cache' foi removido ---
            # O cache não é mais usado, então não precisamos limpá-lo

            if result.modified_count > 0:
                send_whatsapp_message(responsible_number, f"✅ Atendimento automático reativado para o cliente `{customer_number_to_reactivate}`.")
                send_whatsapp_message(customer_number_to_reactivate, "Personalizar o retorno do atendimento do bot de acordo com o cliente! 😊") # <--- EDITAR MENSAGEM
            else:
                send_whatsapp_message(responsible_number, f"ℹ️ O atendimento para `{customer_number_to_reactivate}` já estava ativo. Nenhuma alteração foi necessária.")

        except Exception as e:
            print(f"❌ Erro ao tentar reativar cliente: {e}")
            send_whatsapp_message(responsible_number, f"❌ Ocorreu um erro técnico ao tentar reativar o cliente. Verifique o log do sistema.")
            
    else:
        print("⚠️ Comando não reconhecido do responsável.")
        help_message = (
            "Comando não reconhecido. 🤖\n\n"
            "Para reativar o atendimento de um cliente, envie a mensagem no formato exato:\n"
            "`ok <numero_do_cliente>`\n\n"
            "*(Exemplo):*\n`ok 5544912345678`"
        )
        send_whatsapp_message(responsible_number, help_message)
    return True 

# <--- MELHORIA: Esta é a fusão das duas lógicas de processamento ---
def process_message_logic(message_data, buffered_message_text=None):
    """
    (VERSÃO MELHORADA)
    Esta é a função "worker" principal. Ela pega o lock e chama a IA.
    Combina o LOCK do 'codigo atual' com a LÓGICA DE INTERVENÇÃO do 'codigo intervenção'.
    """
    lock_acquired = False
    clean_number = None
    
    try:
        key_info = message_data.get('key', {})
        sender_number_full = key_info.get('senderPn') or key_info.get('participant') or key_info.get('remoteJid')
        if not sender_number_full or sender_number_full.endswith('@g.us'): return
        
        clean_number = sender_number_full.split('@')[0]
        sender_name_from_wpp = message_data.get('pushName') or 'Cliente'

        # --- MELHORIA: Lógica de LOCK robusta (do 'codigo atual') ---
        now = datetime.now()
        
        res = conversation_collection.update_one(
            {'_id': clean_number, 'processing': {'$ne': True}},
            {'$set': {'processing': True, 'processing_started_at': now}},
            upsert=True  # Cria o documento se for um novo usuário
        )

        if res.matched_count == 0 and res.upserted_id is None:
            # Não conseguiu o lock
            print(f"⏳ {clean_number} já está sendo processado (lock). Reagendando...")
            
            if buffered_message_text:
                if clean_number not in message_buffer: message_buffer[clean_number] = []
                message_buffer[clean_number].insert(0, buffered_message_text)
            
            timer = threading.Timer(10.0, _trigger_ai_processing, args=[clean_number, message_data])
            message_timers[clean_number] = timer
            timer.start()
            return 
        
        lock_acquired = True
        if res.upserted_id:
             print(f"✅ Novo usuário {clean_number}. Documento criado e lock adquirido.")
        # --- Fim da Lógica de LOCK ---
        
        user_message_content = None
        
        # --- MELHORIA: Lógica de processamento de buffer/áudio (do 'codigo atual') ---
        if buffered_message_text:
            user_message_content = buffered_message_text
            messages_to_save = user_message_content.split(". ")
            for msg_text in messages_to_save:
                if msg_text and msg_text.strip():
                    append_message_to_db(clean_number, 'user', msg_text)
        else:
            # Lógica de Áudio (processamento imediato)
            message = message_data.get('message', {})
            if message.get('audioMessage') and message.get('base64'):
                message_id = key_info.get('id')
                print(f"🎤 Mensagem de áudio recebida de {clean_number}. Transcrevendo...")
                audio_base64 = message['base64']
                audio_data = base64.b64decode(audio_base64)
                os.makedirs("/tmp", exist_ok=True) # Garante que /tmp existe
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
            
            if not user_message_content:
                 user_message_content = "[Usuário enviou uma mensagem não suportada]"
                 
            append_message_to_db(clean_number, 'user', user_message_content)
        # --- Fim da Melhoria ---

        print(f"🧠 Processando Mensagem de {clean_number}: '{user_message_content}'")
        
        # --- LÓGICA DE INTERVENÇÃO (do 'codigo intervenção') ---
        if RESPONSIBLE_NUMBER and clean_number == RESPONSIBLE_NUMBER:
            handle_responsible_command(user_message_content, clean_number)
            return # Sai da função, mas o 'finally' vai liberar o lock

        conversation_status = conversation_collection.find_one({'_id': clean_number})

        if conversation_status and conversation_status.get('intervention_active', False):
            print(f"⏸️  Conversa com {sender_name_from_wpp} ({clean_number}) pausada para atendimento humano.")
            return # Sai da função, mas o 'finally' vai liberar o lock

        known_customer_name = conversation_status.get('customer_name') if conversation_status else None
        if known_customer_name:
            print(f"👤 Cliente já conhecido: {known_customer_name} ({clean_number})")
        # --- FIM DA LÓGICA DE INTERVENÇÃO (Pré-IA) ---

        
        ai_reply = gerar_resposta_ia(
            clean_number,
            sender_name_from_wpp,
            user_message_content,
            known_customer_name
        )
        
        if not ai_reply:
             print("⚠️ A IA não gerou resposta.")
             return # 'finally' vai liberar o lock

        try:
            # <--- MELHORIA: Salva a resposta da IA no histórico ---
            append_message_to_db(clean_number, 'assistant', ai_reply)
            
            # --- LÓGICA DE INTERVENÇÃO (Pós-IA) ---
            if ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
                print(f"‼️ INTERVENÇÃO HUMANA SOLICITADA para {sender_name_from_wpp} ({clean_number})")
                
                conversation_collection.update_one(
                    {'_id': clean_number}, {'$set': {'intervention_active': True}}, upsert=True
                )
                
                send_whatsapp_message(sender_number_full, "Entendido. Já notifiquei um de nossos especialistas para te ajudar pessoalmente. Por favor, aguarde um momento. 👨‍💼") # <--- EDITAR MENSAGEM
                
                if RESPONSIBLE_NUMBER:
                    reason = ai_reply.replace("[HUMAN_INTERVENTION] Motivo:", "").strip()
                    display_name = known_customer_name or sender_name_from_wpp
                    
                    # <--- MELHORIA: Usa o 'conversation_status' que já carregamos ---
                    history_summary = "Nenhum histórico de conversa encontrado."
                    if conversation_status and 'history' in conversation_status:
                        # Adiciona a última mensagem do usuário (que não está em 'conversation_status' ainda)
                        history_com_ultima_msg = conversation_status.get('history', []) + [{'role': 'user', 'text': user_message_content}]
                        history_summary = get_last_messages_summary(history_com_ultima_msg)

                    notification_msg = (
                        f"🔔 *NOVA SOLICITAÇÃO DE ATENDIMENTO HUMANO* 🔔\n\n"
                        f"👤 *Cliente:* {display_name}\n"
                        f"📞 *Número:* `{clean_number}`\n\n"
                        f"💬 *Motivo da Chamada:*\n_{reason}_\n\n"
                        f"📜 *Resumo da Conversa:*\n{history_summary}\n\n"
                        f"-----------------------------------\n"
                        f"*AÇÃO NECESSÁRIA:*\nApós resolver, envie para *ESTE NÚMERO* o comando:\n`ok {clean_number}`"
                    )
                    send_whatsapp_message(f"{RESPONSIBLE_NUMBER}@s.whatsapp.net", notification_msg)
            
            else:
                # (Envio de resposta normal)
                print(f"🤖  Resposta da IA para {sender_name_from_wpp}: {ai_reply}")
                send_whatsapp_message(sender_number_full, ai_reply)

        except Exception as e:
            print(f"❌ Erro ao processar envio ou intervenção: {e}")
            send_whatsapp_message(sender_number_full, "Desculpe, tive um problema ao processar sua resposta. (Erro interno: SEND_LOGIC)")

    except Exception as e:
        print(f"❌ Erro fatal ao processar mensagem: {e}")
    finally:
        # --- MELHORIA: Libera o Lock ---
        if clean_number and lock_acquired: 
            conversation_collection.update_one(
                {'_id': clean_number},
                {'$unset': {'processing': "", 'processing_started_at': ""}}
            )
            print(f"🔓 Lock liberado para {clean_number}.")
# --- Fim da Função Aprimorada ---


# <--- MELHORIA: Estrutura de inicialização para Gunicorn ---
if modelo_ia:
    print("\n=============================================")
    print("   CHATBOT WHATSAPP COM IA INICIADO")
    print(f"   CLIENTE: {CLIENT_NAME}")
    if not RESPONSIBLE_NUMBER:
        print("   AVISO: 'RESPONSIBLE_NUMBER' não configurado. O recurso de intervenção humana não notificará ninguém.")
    else:
        print(f"   Intervenção Humana notificará: {RESPONSIBLE_NUMBER}")
    print("=============================================")
    print("Servidor aguardando mensagens no webhook...")

    scheduler.add_job(gerar_e_enviar_relatorio_semanal, 'cron', day_of_week='sun', hour=8, minute=0)
    print("⏰ Agendador de relatórios iniciado. O relatório será enviado todo Domingo às 08:00.")
    
    import atexit
    atexit.register(lambda: scheduler.shutdown())
    
else:
    print("\nEncerrando o programa devido a erros na inicialização.")

if __name__ == '__main__':
    # Esta parte só roda se você executar 'python main.py'
    print("Iniciando em MODO DE DESENVOLVIMENTO LOCAL (app.run)...")
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port, debug=False)
# --- Fim da Melhoria ---