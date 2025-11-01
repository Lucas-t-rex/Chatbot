import google.generativeai as genai
import requests
import os
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

# --- 1. IDENTIDADE DO CLIENTE ATUALIZADA ---
CLIENT_NAME = "Marmitaria Sabor do Dia" 
# (RESPONSIBLE_NUMBER removido, pois este bot não tem intervenção)

load_dotenv()
EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_DB_URI = os.environ.get("MONGO_DB_URI")

COZINHA_WPP_NUMBER = "554898389781"
MOTOBOY_WPP_NUMBER = "554499242532"

BIFURCACAO_ENABLED = bool(COZINHA_WPP_NUMBER and MOTOBOY_WPP_NUMBER)
if BIFURCACAO_ENABLED:
    print(f"✅ Plano de Bifurcação ATIVO. Cozinha: {COZINHA_WPP_NUMBER}, Motoboy: {MOTOBOY_WPP_NUMBER}")
else:
    print("⚠️ Plano de Bifurcação INATIVO. (Configure COZINHA_WPP_NUMBER e MOTOBOY_WPP_NUMBER no .env)")
# --- FIM DA NOVA SEÇÃO ---

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

conversations_cache = {}
message_buffer = {}
message_timers = {}

modelo_ia = None
try:
    modelo_ia = genai.GenerativeModel('gemini-2.5-flash') # Recomendo usar o 1.5-flash
    print("✅ Modelo do Gemini inicializado com sucesso.")
except Exception as e:
    print(f"❌ ERRO: Não foi possível inicializar o modelo do Gemini. Verifique sua API Key. Erro: {e}")

def save_conversation_to_db(contact_id, sender_name, customer_name, chat_session, tokens_used):
    """Salva o histórico, nomes e atualiza a contagem de tokens no MongoDB."""
    try:
        history_list = [
            {'role': msg.role, 'parts': [part.text for part in msg.parts]}
            for msg in chat_session.history
        ]
        
        update_payload = {
            'sender_name': sender_name, # Nome do contato no WhatsApp (Ex: Gauchão)
            'history': history_list,
            'last_interaction': datetime.now()
        }
        # Adiciona o nome real do cliente ao payload se ele for conhecido
        if customer_name:
            update_payload['customer_name'] = customer_name

        conversation_collection.update_one(
            {'_id': contact_id},
            {
                '$set': update_payload,
                '$inc': {
                    'total_tokens_consumed': tokens_used
                }
            },
            upsert=True
        )
    except Exception as e:
        print(f"❌ Erro ao salvar conversa no MongoDB para {contact_id}: {e}")

def load_conversation_from_db(contact_id):
    """Carrega o histórico de uma conversa do MongoDB, se existir."""
    try:
        result = conversation_collection.find_one({'_id': contact_id})
        if result:
            print(f"🧠 Histórico anterior encontrado e carregado para {contact_id}.")
            return result
    except Exception as e:
        print(f"❌ Erro ao carregar conversa do MongoDB para {contact_id}: {e}")
    return None

def gerar_resposta_ia(contact_id, sender_name, user_message, known_customer_name, contact_phone):
    """
    Gera uma resposta usando a IA, com lógica robusta de cache e fallback para o banco de dados.
    """
    global modelo_ia, conversations_cache

    if not modelo_ia:
        return "Desculpe, estou com um problema interno (modelo IA não carregado)."

    cached_session_data = conversations_cache.get(contact_id)

    if cached_session_data:
        chat_session = cached_session_data['ai_chat_session']
        customer_name_in_cache = cached_session_data.get('customer_name')
        print(f"🧠 Sessão para {contact_id} encontrada no cache. Nome em cache: {customer_name_in_cache}")

        # --- CORREÇÃO DE AMNÉSIA (BUG GABRIEL/DANI) ---
        # Compara o nome do DB (known_customer_name) com o nome do cache (customer_name_in_cache)
        if known_customer_name and customer_name_in_cache != known_customer_name:
            print(f"🔄 Sincronizando nome no cache: de '{customer_name_in_cache}' para '{known_customer_name}'")
            conversations_cache[contact_id]['customer_name'] = known_customer_name
            customer_name_in_cache = known_customer_name
        # --- FIM DA CORREÇÃO ---
    else:
        print(f"⚠️ Sessão para {contact_id} não encontrada no cache. Reconstruindo...")
        
        horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # --- 1. LÓGICA DE NOME (Mantida como você pediu) ---
        prompt_name_instruction = ""
        final_user_name_for_prompt = ""
        
        if known_customer_name:
            final_user_name_for_prompt = known_customer_name
            prompt_name_instruction = f"O nome do usuário com quem você está falando é: {final_user_name_for_prompt}. Trate-o por este nome."
        else:
            final_user_name_for_prompt = sender_name
            # Esta é a sua regra de intervenção, que você queria manter
            prompt_name_instruction =  f"""
            REGRA CRÍTICA - CAPTURA DE NOME INTELIGENTE (PRIORIDADE MÁXIMA):
             Seu nome é {{Lyra}} e você é atendente da {{Marmitaria Sabor do Dia}}.
             Seu primeiro objetivo é sempre descobrir o nome real do cliente, pois o nome de contato ('{sender_name}') pode ser um apelido. No entanto, você deve fazer isso de forma natural.
             Se apresente e apresente a empresa de maneira curta e profissional.

             1. Se a primeira mensagem do cliente for um simples cumprimento (ex: "oi", "boa noite"), peça o nome dele de forma direta e educada.

             2. Se a primeira mensagem do cliente já contiver uma pergunta (ex: "oi, qual o preço?", "quero saber como funciona"), você deve:
                - Primeiro, acalmar o cliente dizendo que já vai responder.
                - Em seguida, peça o nome para personalizar o atendimento.
                - **IMPORTANTE**: Você deve guardar a pergunta original do cliente na memória.

             3. Quando o cliente responder com o nome dele (ex: "Meu nome é Marcos"), sua próxima resposta DEVE OBRIGATORIAMENTE:
                - Começar com a tag: `[NOME_CLIENTE]O nome do cliente é: [Nome Extraído].`
                - Agradecer ao cliente pelo nome.
                - **RESPONDER IMEDIATAMENTE à pergunta original que ele fez no início da conversa.** Não o faça perguntar de novo.

             EXEMPLO DE FLUXO IDEAL:
             Cliente: "boa noite, queria saber o preço ?"
             Você: "Boa noite! Claro, já te passo os detalhes. Para que nosso atendimento fique mais próximo, como posso te chamar?"
             Cliente: "pode me chamar de Marcos"
             Sua Resposta: "[NOME_CLIENTE]O nome do cliente é: Marcos. Prazer em conhecê-lo, Marcos! Os detalhes são ..."
            """
        # --- FIM DA LÓGICA DE NOME ---

        # --- Lógica do Prompt de Bifurcação ---
        prompt_bifurcacao = ""
        if BIFURCACAO_ENABLED:
            prompt_bifurcacao = f"""
            =====================================================
            ⚙️ MODO DE BIFURCAÇÃO DE PEDIDOS (PRIORIDADE ALTA)
            =====================================================
            Esta é a sua principal função. Você DEVE seguir este fluxo para CADA pedido.

            1.  **MISSÃO:** Você DEVE preencher TODOS os campos do "Gabarito de Pedido" abaixo.
            2.  **CARDÁPIO:** Use as informações do cardápio para informar o cliente e calcular os valores.
            3.  **COLETA:** Faça perguntas UMA de cada vez. Seja persistente. 
                **NOVO: Você DEVE perguntar se o pedido é para ENTREGA ou RETIRADA NO LOCAL.**
            4.  **TELEFONE:** O campo "telefone_contato" JÁ ESTÁ PREENCHIDO. É {contact_phone}. NÃO pergunte o telefone ao cliente.
            5.  **CÁLCULO:** Você DEVE calcular o `valor_total` somando os itens do pedido, bebidas e a `taxa_entrega` 
                **(APENAS se o tipo_pedido for 'Entrega'. Se for 'Retirada', a taxa é R$ 0,00).**
            6.  **CONFIRMAÇÃO (LOOP OBRIGATÓRIO):** Ao ter TODOS os campos, você DEVE apresentar um RESUMO COMPLETO ao cliente (incluindo o `valor_total` calculado) e perguntar "Confirma o pedido?".
            7.  **EDIÇÃO (LOOP OBRIGATÓRIO):** Se o cliente quiser alterar (ex: "quero tirar o feijao", "adicione uma coca"), você DEVE:
                a. Ajustar o gabarito (ex: adicionar em 'observacoes', alterar 'bebidas', alterar 'pedido_completo').
                b. RECALCULAR o `valor_total`.
                c. Apresentar o NOVO resumo completo e perguntar "Confirma o pedido?" novamente.
            8.  **ENVIO (AÇÃO CRÍTICA):** Quando o cliente responder "sim", "confirmo", "pode enviar", ou algo positivo, sua resposta DEVE, OBRIGATORIAMENTE E SEM EXCEÇÃO, começar com a tag [PEDIDO_CONFIRMADO] e ser seguida por um objeto JSON VÁLIDO contendo o gabarito.

            --- GABARITO DE PEDIDO (DEVE SER PREENCHIDO) ---
            {{
              "nome_cliente": "...", (Use o 'known_customer_name' ou o nome capturado)
              "tipo_pedido": "...", (Deve ser "Entrega" ou "Retirada")
              "endereco_completo": "...", (Se 'Retirada', preencha com 'Retirada no Local')
              "telefone_contato": "{contact_phone}", (JÁ PREENCHIDO)
              "pedido_completo": "...", (Lista de todos os itens, ex: "1 Marmita G, 2 Marmitas M (1 sem feijão), 1 Marmita P")
              "bebidas": "...", (ex: "1 Coca-Cola 2L", ou "Nenhuma")
              "forma_pagamento": "...", (ex: "Pix", "Cartão na entrega", "Dinheiro (troco para R$ 100)")
              "observacoes": "...", (ex: "1 das marmitas médias sem feijão", "Mandar sachês de ketchup", ou "Nenhuma")
              "valor_total": "..." (O valor total calculado por você, incluindo a taxa de entrega SE APLICÁVEL)
            }}
            --- FIM DO GABARITO ---
            """
        else:
            prompt_bifurcacao = "O plano de Bifurcação (envio para cozinha) não está ativo."
        
        prompt_inicial = f"""
            A data e hora atuais são: {horario_atual}.
            {prompt_name_instruction}
            
            =====================================================
            🏷️ IDENTIDADE DO ATENDENTE
            =====================================================
            nome: {{Lyra}}
            função: {{Atendente de restaurante (delivery)}} 
            papel: {{Você deve atender o cliente, apresentar o cardápio, anotar o pedido completo, calcular o valor total e confirmar a entrega.}}

            =====================================================
            🏢 IDENTIDADE DA EMPRESA
            =====================================================
            nome da empresa: {{Marmitaria Sabor do Dia}}
            horário de atendimento: {{Segunda a Sábado, das 11:00 às 14:00}}
            
            =====================================================
            🍲 CARDÁPIO E PREÇOS (BASE DO PEDIDO)
            =====================================================
            
            --- PRATO DO DIA (Exemplo) ---
            Hoje temos: {{Strogonoff de Frango}}
            Acompanhamentos: {{Arroz branco, Feijão, Batata palha e Salada de alface e tomate.}}

            --- TAMANHOS E VALORES (Marmitas) ---
            - Marmita Pequena (P): {{R$ 15,00}}
            - Marmita Média (M): {{R$ 18,00}}
            - Marmita Grande (G): {{R$ 22,00}}

            --- 🥤 BEBIDAS ---
            - Coca-Cola Lata (350ml): {{R$ 5,00}}
            - Guaraná Antartica Lata (350ml): {{R$ 5,00}}
            - Água Mineral (sem gás): {{R$ 3,00}}
            - Suco de Laranja (natural 500ml): {{R$ 8,00}}

            --- 🛵 TAXA DE ENTREGA ---
            - Taxa de Entrega Fixa: {{R$ 6,00}} (Use este valor para CÁLCULO do valor total APENAS PARA ENTREGAS)
            - Pedidos para Retirada no Local: {{R$ 0,00}} (não há taxa)

            {prompt_bifurcacao} 

            =====================================================
            🧭 COMPORTAMENTO E REGRAS DE ATENDIMENTO
            =====================================================
            - FOCO TOTAL: Seu primeiro objetivo é capturar o nome do cliente. Seu segundo objetivo é preencher o "Gabarito de Pedido" e confirmar.
            - não deve fazer: Não inventar pratos ou preços. Não falar sobre "IA" ou "Chatbot". Você é uma ATENDENTE HUMANA (Lyra).
            
            =====================================================
            PRONTO PARA ATENDER O CLIENTE
            =====================================================
            """

        # --- CORREÇÃO DE CONFLITO ---
        # A mensagem inicial do bot agora é neutra. Ela não tenta vender
        # e deixa a "REGRA CRÍTICA" funcionar primeiro.
        convo_start = [
            {'role': 'user', 'parts': [prompt_inicial]},
            {'role': 'model', 'parts': [f"Entendido. Eu sou Lyra, atendente da Marmitaria Sabor do Dia. Minha prioridade é capturar o nome do cliente ({final_user_name_for_prompt}) e depois anotar o pedido. Estou pronta."]}
        ]

        loaded_conversation = load_conversation_from_db(contact_id)
        old_history = []
        if loaded_conversation and 'history' in loaded_conversation:
            old_history = [msg for msg in loaded_conversation['history'] if not msg['parts'][0].strip().startswith("A data e hora atuais são:")]
        
        chat_session = modelo_ia.start_chat(history=convo_start + old_history)
        
        conversations_cache[contact_id] = {
            'ai_chat_session': chat_session, 
            'name': sender_name, 
            'customer_name': known_customer_name
        }
        customer_name_in_cache = known_customer_name

    # --- FIM DA LÓGICA DE CACHE/RECONSTRUÇÃO ---

    try:
        print(f"Enviando para a IA: '{user_message}' (De: {sender_name})")
        
        try:
            input_tokens = modelo_ia.count_tokens(chat_session.history + [{'role':'user', 'parts': [user_message]}]).total_tokens
        except Exception as e:
            print(f"Aviso: count_tokens falhou, continuando sem contar. Erro: {e}")
            input_tokens = 0

        resposta = chat_session.send_message(user_message)
        
        try:
            output_tokens = modelo_ia.count_tokens(resposta.text).total_tokens
        except Exception as e:
            print(f"Aviso: count_tokens (saída) falhou. Erro: {e}")
            output_tokens = 0
            
        total_tokens_na_interacao = input_tokens + output_tokens
        
        if total_tokens_na_interacao > 0:
             print(f"📊 Consumo de Tokens: Entrada={input_tokens}, Saída={output_tokens}, Total={total_tokens_na_interacao}")
        
        ai_reply = resposta.text

        if ai_reply.strip().startswith("[NOME_CLIENTE]"):
            print("📝 Tag [NOME_CLIENTE] detectada. Extraindo e salvando nome...")
            try:
                full_response_part = ai_reply.split("O nome do cliente é:")[1].strip()
                extracted_name = full_response_part.split('.')[0].strip()
                start_of_message_index = full_response_part.find(extracted_name) + len(extracted_name)
                ai_reply = full_response_part[start_of_message_index:].lstrip('.!?, ').strip()

                conversation_collection.update_one(
                    {'_id': contact_id},
                    {'$set': {'customer_name': extracted_name}},
                    upsert=True
                )
                # --- CORREÇÃO DE AMNÉSIA ---
                # Garante que o cache local seja atualizado IMEDIATAMENTE com o nome novo
                if contact_id in conversations_cache:
                    conversations_cache[contact_id]['customer_name'] = extracted_name
                customer_name_in_cache = extracted_name
                # --- FIM DA CORREÇÃO ---
                print(f"✅ Nome '{extracted_name}' salvo para o cliente {contact_id}.")

            except Exception as e:
                print(f"❌ Erro ao extrair o nome da tag: {e}")
                ai_reply = ai_reply.replace("[NOME_CLIENTE]", "").strip()

        save_conversation_to_db(contact_id, sender_name, customer_name_in_cache, chat_session, total_tokens_na_interacao)
        
        return ai_reply
    
    except Exception as e:
        print(f"❌ Erro ao comunicar com a API do Gemini: {e}")
        if contact_id in conversations_cache:
            del conversations_cache[contact_id]
        return "Tive um pequeno problema para processar sua mensagem e precisei reiniciar nossa conversa. Você poderia repetir, por favor?"
    
def transcrever_audio_gemini(caminho_do_audio):
    """
    Envia um arquivo de áudio para a API do Gemini e retorna a transcrição em texto.
    """
    global modelo_ia 

    if not modelo_ia:
        print("❌ Modelo de IA não inicializado. Impossível transcrever.")
        return None

    print(f"🎤 Enviando áudio '{caminho_do_audio}' para transcrição no Gemini...")
    try:
        audio_file = genai.upload_file(
            path=caminho_do_audio, 
            mime_type="audio/ogg" # O formato padrão da Evolution
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

def send_whatsapp_message(number, text_message):
    """Envia uma mensagem de texto para um número via Evolution API."""
    # O nome da sua instância
    INSTANCE_NAME = "chatbot" 
    
    full_url = f"{EVOLUTION_API_URL}/message/sendText/{INSTANCE_NAME}"

    # A sua função base já espera o JID completo (ex: 55...@s.whatsapp.net),
    # mas a API da evolution quer o número limpo. A sua função já trata isso.
    clean_number = number.split('@')[0]
    payload = {"number": clean_number, "textMessage": {"text": text_message}}
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    try:
        print(f"✅ Enviando resposta para a URL: {full_url} (Destino: {clean_number})")
        response = requests.post(full_url, json=payload, headers=headers)
        response.raise_for_status()
        print(f"✅ Resposta da IA enviada com sucesso para {clean_number}\n")
    except requests.exceptions.RequestException as e:
        print(f"❌ Erro ao enviar mensagem para {clean_number}: {e}")

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

app = Flask(__name__)

processed_messages = set() 

@app.route('/webhook', methods=['POST'])
def receive_webhook():
    """Recebe mensagens do WhatsApp enviadas pela Evolution API."""
    data = request.json
    print(f"📦 DADO BRUTO RECEBIDO NO WEBHOOK: {data}")

    # --- Lógica de filtro de evento (Mantida da sua base) ---
    event_type = data.get('event')
    
    if event_type != 'messages.upsert':
        print(f"➡️  Ignorando evento: {event_type} (não é uma nova mensagem)")
        return jsonify({"status": "ignored_event_type"}), 200
    # --- FIM DA CORREÇÃO ---

    try:
        message_data = data.get('data', {}) 
        if not message_data:
            print("➡️  Evento 'messages.upsert' sem 'data'. Ignorando.")
            return jsonify({"status": "ignored_no_data"}), 200
            
        key_info = message_data.get('key', {})

        # --- 6. LÓGICA DE 'fromMe' MODIFICADA ---
        # (Removemos a checagem do RESPONSIBLE_NUMBER)
        if key_info.get('fromMe'):
            print(f"➡️  Mensagem do próprio bot ignorada.")
            return jsonify({"status": "ignored_from_me"}), 200

        message_id = key_info.get('id')
        if not message_id:
            return jsonify({"status": "ignored_no_id"}), 200

        if message_id in processed_messages:
            print(f"⚠️ Mensagem {message_id} já processada, ignorando.")
            return jsonify({"status": "ignored_duplicate"}), 200
        processed_messages.add(message_id)
        if len(processed_messages) > 1000:
            processed_messages.clear()

        # --- Inicia o Buffer (Mantido da sua base) ---
        threading.Thread(target=handle_message_buffering, args=(message_data,)).start()
        return jsonify({"status": "received"}), 200

    except Exception as e:
        print(f"❌ Erro inesperado no webhook: {e}")
        print("DADO QUE CAUSOU ERRO:", data)
        return jsonify({"status": "error"}), 500

@app.route('/', methods=['GET'])
def health_check():
    return "Estou vivo! (Marmitaria Bot)", 200 # Mensagem de saúde atualizada

# <<< FUNÇÃO handle_responsible_command REMOVIDA >>>
# (Não é necessária, pois não há intervenção humana)

def handle_message_buffering(message_data):
    """
    Esta função recebe a mensagem, a coloca em um buffer e gerencia um timer.
    (Mantida 100% da sua base)
    """
    try:
        key_info = message_data.get('key', {})
        sender_number_full = key_info.get('senderPn') or key_info.get('participant') or key_info.get('remoteJid')

        if not sender_number_full or sender_number_full.endswith('@g.us'):
            return

        clean_number = sender_number_full.split('@')[0]
        
        user_message_content = None
        message = message_data.get('message', {})
        
        if message.get('conversation'):
            user_message_content = message['conversation']
        elif message.get('extendedTextMessage'):
            user_message_content = message['extendedTextMessage'].get('text')
        elif message.get('audioMessage') and message.get('base64'):
            message_id = key_info.get('id')
            print(f"🎤 Mensagem de áudio recebida de {clean_number}. Aguardando timer para transcrever.")
            audio_base64 = message['base64']
            audio_data = base64.b64decode(audio_base64)
            temp_audio_path = f"/tmp/audio_{clean_number}_{message_id}.ogg"
            with open(temp_audio_path, 'wb') as f:
                f.write(audio_data)
            user_message_content = transcrever_audio_gemini(temp_audio_path)
            os.remove(temp_audio_path)
            if not user_message_content:
                send_whatsapp_message(sender_number_full, "Desculpe, não consegui entender o áudio. Pode tentar novamente? 🎧")
                return
        
        if not user_message_content:
            return

        # Adiciona a nova mensagem ao buffer do usuário
        if clean_number not in message_buffer:
            message_buffer[clean_number] = []
        message_buffer[clean_number].append(user_message_content)
        print(f"📥 Mensagem de {clean_number} adicionada ao buffer. Buffer atual: {message_buffer[clean_number]}")

        # Se já existe um timer para este usuário, cancele-o
        if clean_number in message_timers:
            message_timers[clean_number].cancel()

        # Inicia um novo timer de 10 segundos
        timer = threading.Timer(10.0, _trigger_ai_processing, args=[message_data])
        message_timers[clean_number] = timer
        timer.start()
        print(f"⏳ Timer de 10s iniciado/reiniciado para {clean_number}.")

    except Exception as e:
        print(f"❌ Erro ao gerenciar buffer da mensagem: {e}")

# --- 7. FUNÇÃO _trigger_ai_processing MODIFICADA ---
def _trigger_ai_processing(message_data):
    """
    Esta função é chamada pelo timer. Ela pega todas as mensagens do buffer,
    junta-as e envia para a IA.
    Agora usa a lógica de BIFURCAÇÃO ao invés de intervenção.
    """
    key_info = message_data.get('key', {})
    sender_number_full = key_info.get('senderPn') or key_info.get('participant') or key_info.get('remoteJid')
    clean_number = sender_number_full.split('@')[0]
    sender_name_from_wpp = message_data.get('pushName') or 'Cliente'
    
    if clean_number not in message_buffer:
        return
        
    full_user_message = "\n".join(message_buffer[clean_number])
    
    del message_buffer[clean_number]
    del message_timers[clean_number]
    
    print(f"⏰ Timer finalizado! Processando mensagem completa de {clean_number}: '{full_user_message}'")

    # <<< REMOVIDO: Bloco de 'handle_responsible_command' >>>
    # <<< REMOVIDO: Bloco de checagem 'intervention_active' >>>

    conversation_status = conversation_collection.find_one({'_id': clean_number})
    known_customer_name = conversation_status.get('customer_name') if conversation_status else None
    
    # --- ATUALIZADO: Passa o 'clean_number' como 'contact_phone' ---
    ai_reply = gerar_resposta_ia(clean_number, sender_name_from_wpp, full_user_message, known_customer_name, clean_number)

    # --- 8. LÓGICA DE BIFURCAÇÃO (DA MARMITARIA) ---
    if BIFURCACAO_ENABLED and ai_reply and ai_reply.strip().startswith("[PEDIDO_CONFIRMADO]"):
        print(f"📦 Tag [PEDIDO_CONFIRMADO] detectada. Processando e bifurcando pedido para {clean_number}...")
        try:
            # 1. Isolar o JSON do resto da mensagem
            json_start = ai_reply.find('{')
            json_end = ai_reply.rfind('}') + 1

            if json_start == -1 or json_end == 0:
                raise ValueError("JSON de pedido não encontrado após a tag.")

            json_string = ai_reply[json_start:json_end]

            # 2. Isolar a mensagem de resposta para o cliente
            remaining_reply = ai_reply[json_end:].strip()
            if not remaining_reply:
                remaining_reply = "Seu pedido foi confirmado e enviado para a cozinha! 😋" # Fallback

            # 3. Parsear o JSON
            order_data = json.loads(json_string)

            # 4. Formatar as mensagens de bifurcação
            # Mensagem para a COZINHA (Completa)
            msg_cozinha = f"""
            --- 🍳 NOVO PEDIDO (COZINHA) 🍳 ---
            
            Cliente: {order_data.get('nome_cliente', 'N/A')}
            Telefone: {order_data.get('telefone_contato', 'N/A')}
            Endereço: {order_data.get('endereco_completo', 'N/A')}
            
            --- PEDIDO ---
            {order_data.get('pedido_completo', 'N/A')}
            
            --- BEBIDAS ---
            {order_data.get('bebidas', 'N/A')}
            
            --- OBSERVAÇÕES ---
            {order_data.get('observacoes', 'N/A')}
            
            Forma de Pagto: {order_data.get('forma_pagamento', 'N/A')}
            Valor Total: {order_data.get('valor_total', 'N/A')}
            """

            # Mensagem para o MOTOBOY (Parcial)
            msg_motoboy = f"""
            --- 🛵 NOVA ENTREGA (MOTOBOY) 🛵 ---
            
            Cliente: {order_data.get('nome_cliente', 'N/A')}
            Telefone: {order_data.get('telefone_contato', 'N/A')}
            Endereço: {order_data.get('endereco_completo', 'N/A')}
            
            Forma de Pagto: {order_data.get('forma_pagamento', 'N/A')}
            Valor Total: {order_data.get('valor_total', 'N/A')}
            """

            # 5. Enviar as mensagens (em threads para não bloquear a resposta)
            # A sua função send_whatsapp_message espera o JID completo (com @s.whatsapp.net)
            threading.Thread(target=send_whatsapp_message, args=(f"{COZINHA_WPP_NUMBER}@s.whatsapp.net", msg_cozinha.strip())).start()
            threading.Thread(target=send_whatsapp_message, args=(f"{MOTOBOY_WPP_NUMBER}@s.whatsapp.net", msg_motoboy.strip())).start()

            print(f"✅ Pedido bifurcado com sucesso para {COZINHA_WPP_NUMBER} e {MOTOBOY_WPP_NUMBER}.")

            # 6. Atualiza a resposta para o cliente
            ai_reply = remaining_reply

        except Exception as e:
            print(f"❌ Erro ao processar bifurcação [PEDIDO_CONFIRMADO]: {e}")
            ai_reply = ai_reply.replace("[PEDIDO_CONFIRMADO]", "").strip()
            if '{' in ai_reply and '}' in ai_reply:
                ai_reply = "Tive um problema ao enviar seu pedido para a cozinha. Pode confirmar os dados novamente, por favor? (Erro interno: JSON_PARSE)"
        
        # Envia a resposta final (seja de sucesso ou erro) para o cliente
        print(f"🤖 Resposta da IA para {sender_name_from_wpp}: {ai_reply}")
        send_whatsapp_message(sender_number_full, ai_reply)

    elif ai_reply:
        # Se não for um pedido, é uma conversa normal
        print(f"🤖 Resposta da IA para {sender_name_from_wpp}: {ai_reply}")
        send_whatsapp_message(sender_number_full, ai_reply)
    # --- FIM DO BLOCO DE BIFURCAÇÃO ---


if __name__ == '__main__':
    if modelo_ia:
        print("\n=============================================")
        print("   CHATBOT WHATSAPP COM IA INICIADO")
        # --- 9. MENSAGEM DE START ATUALIZADA ---
        print(f"   CLIENTE: {CLIENT_NAME}")
        if not BIFURCACAO_ENABLED:
            print("   AVISO: 'COZINHA_WPP_NUMBER' ou 'MOTOBOY_WPP_NUMBER' não configurados. O recurso de bifurcação está DESATIVADO.")
        else:
            print(f"   Bifurcação ATIVA. Cozinha: {COZINHA_WPP_NUMBER} | Motoboy: {MOTOBOY_WPP_NUMBER}")
        print("=============================================")
        print("Servidor aguardando mensagens no webhook...")

        scheduler = BackgroundScheduler(daemon=True, timezone='America/Sao_Paulo') 
        scheduler.add_job(gerar_e_enviar_relatorio_semanal, 'cron', day_of_week='sun', hour=8, minute=0)
        scheduler.start()
        print("⏰ Agendador de relatórios iniciado. O relatório será enviado todo Domingo às 08:00.")
        
        import atexit
        atexit.register(lambda: scheduler.shutdown())
        
        # O Fly.io vai injetar a variável PORT, mas 8000 é um bom padrão
        port = int(os.environ.get("PORT", 8000))
        app.run(host='0.0.0.0', port=port)
    else:
        print("\nEncerrando o programa devido a erros na inicialização.")