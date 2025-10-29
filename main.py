
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

CLIENT_NAME = "Neuro Soluções em Tecnologia"
RESPONSIBLE_NUMBER = "554898389781"

load_dotenv()
EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_DB_URI = os.environ.get("MONGO_DB_URI")


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

# <<< NOVO >>> Função para pegar as últimas mensagens e formatar para a notificação
def get_last_messages_summary(history, max_messages=4):
    """Formata as últimas mensagens de um histórico para um resumo legível, ignorando prompts do sistema."""
    summary = []
    # Pega as últimas mensagens do histórico
    relevant_history = history[-max_messages:]
    
    for message in relevant_history:
        # Ignora as mensagens iniciais do sistema e do bot que não são parte da conversa real
        if message['role'] == 'user' and message['parts'][0].strip().startswith("A data e hora atuais são:"):
            continue # Pula o prompt inicial
        if message['role'] == 'model' and message['parts'][0].strip().startswith("Entendido. A Regra de Ouro"):
            continue # Pula a confirmação inicial do bot

        role = "Cliente" if message['role'] == 'user' else "Bot"
        text = message['parts'][0].strip()
        summary.append(f"*{role}:* {text}")
        
    if not summary:
        return "Nenhum histórico de conversa encontrado."
        
    return "\n".join(summary)

def gerar_resposta_ia(contact_id, sender_name, user_message, known_customer_name):
    """
    Gera uma resposta usando a IA, com lógica robusta de cache e fallback para o banco de dados.
    """
    global modelo_ia, conversations_cache

    if not modelo_ia:
        return "Desculpe, estou com um problema interno (modelo IA não carregado)."

    # --- LÓGICA DE CACHE E RESTAURAÇÃO ---
    # Primeiro, tenta pegar a sessão de chat da memória rápida (cache)
    cached_session_data = conversations_cache.get(contact_id)

    if cached_session_data:
        # Se encontrou no cache, usa a sessão que já existe. É o caminho mais rápido.
        chat_session = cached_session_data['ai_chat_session']
        customer_name_in_cache = cached_session_data.get('customer_name')
        print(f"🧠 Sessão para {contact_id} encontrada no cache.")
    else:
        # Se NÃO encontrou no cache, precisamos construir (ou reconstruir) a sessão.
        print(f"⚠️ Sessão para {contact_id} não encontrada no cache. Reconstruindo...")
        
        horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Decide qual instrução de nome usar (se já sabemos o nome ou não)
        if known_customer_name:
            final_user_name_for_prompt = known_customer_name
            prompt_name_instruction = f"O nome do usuário com quem você está falando é: {final_user_name_for_prompt}. Trate-o por este nome."
        else:
            final_user_name_for_prompt = sender_name
            prompt_name_instruction = f"""
            REGRA CRÍTICA - CAPTURA DE NOME INTELIGENTE (PRIORIDADE MÁXIMA):
              Seu nome é {{Lyra}} e você é atendente da {{Neuro Soluções em Tecnologia}}.
              Seu primeiro objetivo é sempre descobrir o nome real do cliente, pois o nome de contato ('{sender_name}') pode ser um apelido. No entanto, você deve fazer isso de forma natural.
              1. Se a primeira mensagem do cliente for um simples cumprimento (ex: "oi", "boa noite"), peça o nome dele de forma direta e educada.
              2. Se a primeira mensagem do cliente já contiver uma pergunta (ex: "oi, qual o preço?", "quero saber como funciona"), você deve:
                 - Primeiro, acalmar o cliente dizendo que já vai responder.
                 - Em seguida, peça o nome para personalizar o atendimento.
                 - **IMPORTANTE**: Você deve guardar a pergunta original do cliente na memória.
              3. Quando o cliente responder com o nome dele (ex: "Meu nome é Marcos"), sua próxima resposta DEVE OBRIGATORIAMENTE:
                 - Começar com a tag: `[NOME_CLIENTE]O nome do cliente é: [Nome Extraído].`
                 - Agradecer ao cliente pelo nome.
                 - **RESPONDER IMEDIATAMENTE à pergunta original que ele fez no início da conversa.** Não o faça perguntar de novo.
              - **IMPORTANTE**: A simples apresentação do nome do cliente (ex: "meu nome é marcos") NÃO é um motivo para intervenção. Continue a conversa normalmente nesses casos.

              EXEMPLO DE FLUXO IDEAL:
              Cliente: "boa noite, queria saber o preço do plano secretário"
              Você: "Boa noite! Claro, já te passo os detalhes do Plano Secretário. Para que nosso atendimento fique mais próximo, como posso te chamar?"
              Cliente: "pode me chamar de Marcos"
              Sua Resposta: "[NOME_CLIENTE]O nome do cliente é: Marcos. Prazer em conhecê-lo, Marcos! O Plano Secretário custa R$500,00 por mês, mais a taxa de instalação. Ele é perfeito para quem precisa de agendamentos inteligentes e integrados. Quer saber mais sobre as funcionalidades dele?"
            """
        
        # Monta o prompt do sistema (a base de conhecimento do bot)
        prompt_inicial = f"""
              A data e hora atuais são: {horario_atual}.
              {prompt_name_instruction}
              Dever : vender nosso produto e se quer saber sobre a empresa ou falar com o Lucas(Proprietario)
              =====================================================
              🆘 REGRA DE OURO: ANÁLISE DE INTENÇÃO E INTERVENÇÃO HUMANA (PRIORIDADE MÁXIMA)
              =====================================================
              - SUA TAREFA MAIS IMPORTANTE É ANALISAR A INTENÇÃO DO CLIENTE. Se a intenção for falar com um humano, sua única ação é acionar a intervenção. ESTA REGRA SOBREPÕE TODAS AS OUTRAS REGRAS DE COMPORTAMENTO.
              - CASOS PARA INTERVENÇÃO OBRIGATÓRIA:
              - Pedidos explícitos: "falar com o dono", "falar com o responsável", "quero falar com um humano", "falar com o proprietário", "quero fazer um investimento".
              - Perguntas complexas sem resposta: Pedidos de produtos/planos que não existem, reclamações graves, negociações de preços especiais.
              - IMPORTANTE: A simples apresentação do nome do cliente (ex: "meu nome é marcos") NÃO é um motivo para intervenção. Continue a conversa normalmente nesses casos.
              - COMO ACIONAR: Sua ÚNICA resposta DEVE ser a tag abaixo, sem saudações, sem explicações.
              [HUMAN_INTERVENTION] Motivo: [Resumo do motivo do cliente]
              - O QUE NÃO FAZER (ERRO CRÍTICO):
              - ERRADO: Cliente diz "Quero falar com o dono" e você responde "Compreendo, para isso, ligue para o número X...".
              - CORRETO: Cliente diz "Quero falar com o dono" e sua resposta é APENAS: [HUMAN_INTERVENTION] Motivo: Cliente solicitou falar com o dono.
              - Se a intenção do cliente NÃO se encaixar nos casos acima, você deve seguir as regras de atendimento normais abaixo.
              =====================================================
              🏷️ IDENTIDADE DO ATENDENTE
              =====================================================
              nome: {{Lyra}}
              sexo: {{Feminina}}
              idade: {{40}}
              função: {{Atendente, vendedora, especialista em Ti e machine learning}} 
              papel: {{Você deve atender a pessoa, entender a necessidade da pessoa, vender o plano de acordo com a necessidade, tirar duvidas, ajudar.}}  (ex: tirar dúvidas, passar preços, enviar catálogos, agendar horários)
              =====================================================
              🏢 IDENTIDADE DA EMPRESA
              =====================================================
              nome da empresa: {{Neuro Soluções em Tecnologia}}
              setor: {{Tecnologia e Automação}} 
              missão: {{Facilitar e organizar as empresas de clientes.}}
              valores: {{Organização, trasparencia,persistencia e ascenção.}}
              horário de atendimento: {{De segunda-feira a sexta-feira das 8:00 as 18:00}}
              endereço: {{R. Pioneiro Alfredo José da Costa, 157 - Jardim Alvorada, Maringá - PR, 87035-270}}
              =====================================================
              🏛️ HISTÓRIA DA EMPRESA
              =====================================================
              {{Fundada em Maringá - PR, em 2025, a Neuro Soluções em Tecnologia nasceu com o propósito de unir inovação e praticidade. Criada por profissionais apaixonados por tecnologia e automação, a empresa cresceu ajudando empreendedores a otimizar processos, economizar tempo e aumentar vendas por meio de chatbots e sistemas inteligentes.}}
              =====================================================
              ℹ️ INFORMAÇÕES GERAIS
              =====================================================
              público-alvo: {{Empresas, empreendedores e prestadores de serviço que desejam automatizar atendimentos e integrar inteligência artificial ao seu negócio.}}
              diferencial: {{Atendimento personalizado, chatbots sob medida e integração total com o WhatsApp e ferramentas de IA.}}
              tempo de mercado: {{Desde de 2025}}
              slogan: {{O futuro é agora!}}
              =====================================================
              💼 SERVIÇOS / CARDÁPIO
              =====================================================
              - Plano Atendente: {{Atendente personalizada, configurada conforme a necessidade do cliente. Neste plano, o atendimento pode funcionar de três formas: Atendimento Autônomo, Intervenção Humana, Bifurcação de Mensagens.}}
              - Plano Secretário: {{Agendamento Inteligente, Avisos Automáticos, Agenda Integrada.}}
              - Plano Premium: {{Em construção}}
              =====================================================
              💰 PLANOS E VALORES
              =====================================================
              Instalação: {{R$200,00 taxa única}} para setup inicial do projeto e requisitos da IA. 
              plano Atendente: {{R$300,00 mensal}}
              Plano Secretário: {{R$500,00 mensal}}
              plano avançado: {{Em analise}}
              observações: {{valores podem variar conforme personalização ou integrações extras.}}
              =====================================================
              🧭 COMPORTAMENTO E REGRAS DE ATENDIMENTO
              =====================================================
              - Ações: Seja profissional, empática, natural, use frases curtas, mantenha um tom positivo, ajude o cliente, entenda a necessidade para vender o plano ideal, use quebras de objeções e, como último recurso, ofereça uma semana grátis antes de se despedir.
              - Não fazer: Não invente informações, não discuta, não compartilhe dados pessoais, não responda fora de contexto e nunca deixe o cliente sair sem ter vendido, se possível.
              =====================================================
              ⚙️ PERSONALIDADE DO ATENDENTE
              =====================================================
              - Tom de voz: {{alegre, acolhedor, profissional, descontraído}} 
              - Estilo: humano, prestativo e simpático.
              - Emojis: usar com moderação (máximo 2 por mensagem).
              =====================================================
              PRONTO PARA ATENDER O CLIENTE
              =====================================================
              Quando o cliente enviar uma mensagem, cumprimente e inicie o atendimento de forma natural, usando o nome do cliente se disponível, tente entender o que ele precisa e sempre coloque o cliente em primeiro lugar.
        """

        convo_start = [
            {'role': 'user', 'parts': [prompt_inicial]},
            {'role': 'model', 'parts': [f"Entendido. A Regra de Ouro e a captura de nome são prioridades. Estou pronto."]}
        ]

        # Carrega o histórico da memória de longo prazo (MongoDB)
        loaded_conversation = load_conversation_from_db(contact_id)
        old_history = []
        if loaded_conversation and 'history' in loaded_conversation:
            # Filtra o prompt antigo para não o enviar duas vezes
            old_history = [msg for msg in loaded_conversation['history'] if not msg['parts'][0].strip().startswith("A data e hora atuais são:")]
        
        # Inicia o chat combinando o novo prompt com o histórico antigo
        chat_session = modelo_ia.start_chat(history=convo_start + old_history)
        
        # Salva a sessão reconstruída na memória de curto prazo (cache)
        conversations_cache[contact_id] = {
            'ai_chat_session': chat_session, 
            'name': sender_name, 
            'customer_name': known_customer_name
        }
        customer_name_in_cache = known_customer_name

    # --- FIM DA LÓGICA DE CACHE ---

    try:
        print(f"Enviando para a IA: '{user_message}' (De: {sender_name})")
        
        # O resto do código continua como antes...
        input_tokens = modelo_ia.count_tokens(chat_session.history + [{'role':'user', 'parts': [user_message]}]).total_tokens
        resposta = chat_session.send_message(user_message)
        output_tokens = modelo_ia.count_tokens(resposta.text).total_tokens
        total_tokens_na_interacao = input_tokens + output_tokens
        
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
                # ATUALIZA O NOME NO CACHE TAMBÉM!
                conversations_cache[contact_id]['customer_name'] = extracted_name
                customer_name_in_cache = extracted_name
                print(f"✅ Nome '{extracted_name}' salvo para o cliente {contact_id}.")

            except Exception as e:
                print(f"❌ Erro ao extrair o nome da tag: {e}")
                ai_reply = ai_reply.replace("[NOME_CLIENTE]", "").strip()

        if not ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
            save_conversation_to_db(contact_id, sender_name, customer_name_in_cache, chat_session, total_tokens_na_interacao)
        
        return ai_reply
    
    except Exception as e:
        print(f"❌ Erro ao comunicar com a API do Gemini: {e}")
        # Se der um erro grave, limpa o cache daquele usuário para forçar uma reconstrução limpa na próxima vez.
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

def send_whatsapp_message(number, text_message):
    """Envia uma mensagem de texto para um número via Evolution API."""
    clean_number = number.split('@')[0]
    payload = {"number": clean_number, "textMessage": {"text": text_message}}
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    
    try:
        response = requests.post(EVOLUTION_API_URL, json=payload, headers=headers)
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

    try:
        message_data = data.get('data', {}) or data
        key_info = message_data.get('key', {})

        if key_info.get('fromMe'):
            sender_number_full = key_info.get('remoteJid')
            
            if not sender_number_full:
                return jsonify({"status": "ignored_from_me_no_sender"}), 200

            clean_number = sender_number_full.split('@')[0]
            
            if clean_number != RESPONSIBLE_NUMBER:
                print(f"➡️  Mensagem do próprio bot ignorada (remetente: {clean_number}).")
                return jsonify({"status": "ignored_from_me"}), 200
            
            print(f"⚙️  Mensagem do próprio bot PERMITIDA (é um comando do responsável: {clean_number}).")

        message_id = key_info.get('id')
        if not message_id:
            return jsonify({"status": "ignored_no_id"}), 200

        if message_id in processed_messages:
            print(f"⚠️ Mensagem {message_id} já processada, ignorando.")
            return jsonify({"status": "ignored_duplicate"}), 200
        processed_messages.add(message_id)
        if len(processed_messages) > 1000:
            processed_messages.clear()

        threading.Thread(target=handle_message_buffering, args=(message_data,)).start()
        return jsonify({"status": "received"}), 200

    except Exception as e:
        print(f"❌ Erro inesperado no webhook: {e}")
        print("DADO QUE CAUSOU ERRO:", data)
        return jsonify({"status": "error"}), 500


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

            if customer_number_to_reactivate in conversations_cache:
                del conversations_cache[customer_number_to_reactivate]
                print(f"🗑️  Cache da conversa do cliente {customer_number_to_reactivate} limpo com sucesso.")

            if result.modified_count > 0:
                send_whatsapp_message(responsible_number, f"✅ Atendimento automático reativado para o cliente `{customer_number_to_reactivate}`.")
                send_whatsapp_message(customer_number_to_reactivate, "Oi sou eu a Lyra novamente, voltei pro seu atendimento. se precisar de algo me diga! 😊")
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
    
def handle_message_buffering(message_data):
    """
    Esta função recebe a mensagem, a coloca em um buffer e gerencia um timer.
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

        # Inicia um novo timer de 15 segundos
        # Quando o timer acabar, ele chamará a função _trigger_ai_processing
        timer = threading.Timer(10.0, _trigger_ai_processing, args=[message_data])
        message_timers[clean_number] = timer
        timer.start()
        print(f"⏳ Timer de 10s iniciado/reiniciado para {clean_number}.")

    except Exception as e:
        print(f"❌ Erro ao gerenciar buffer da mensagem: {e}")

def _trigger_ai_processing(message_data):
    """
    Esta função é chamada pelo timer. Ela pega todas as mensagens do buffer,
    junta-as e envia para a IA.
    """
    key_info = message_data.get('key', {})
    sender_number_full = key_info.get('senderPn') or key_info.get('participant') or key_info.get('remoteJid')
    clean_number = sender_number_full.split('@')[0]
    sender_name_from_wpp = message_data.get('pushName') or 'Cliente'
    
    # Verifica se ainda há mensagens no buffer (poderiam ter sido processadas)
    if clean_number not in message_buffer:
        return
        
    # Junta todas as mensagens do buffer com uma quebra de linha
    full_user_message = "\n".join(message_buffer[clean_number])
    
    # Limpa o buffer para este usuário
    del message_buffer[clean_number]
    del message_timers[clean_number]
    
    print(f"⏰ Timer finalizado! Processando mensagem completa de {clean_number}: '{full_user_message}'")

    # A partir daqui, a lógica é a mesma que a sua antiga process_message
    if RESPONSIBLE_NUMBER and clean_number == RESPONSIBLE_NUMBER:
        handle_responsible_command(full_user_message, clean_number)
        return

    conversation_status = conversation_collection.find_one({'_id': clean_number})

    if conversation_status and conversation_status.get('intervention_active', False):
        print(f"⏸️ Conversa com {sender_name_from_wpp} ({clean_number}) pausada para atendimento humano.")
        return

    known_customer_name = conversation_status.get('customer_name') if conversation_status else None
    
    ai_reply = gerar_resposta_ia(clean_number, sender_name_from_wpp, full_user_message, known_customer_name)

    if ai_reply and ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
        # Lógica de intervenção humana (mantida igual)
        print(f"‼️ INTERVENÇÃO HUMANA SOLICITADA para {sender_name_from_wpp} ({clean_number})")
        conversation_collection.update_one(
            {'_id': clean_number}, {'$set': {'intervention_active': True}}, upsert=True
        )
        send_whatsapp_message(sender_number_full, "Entendido. Já notifiquei um de nossos especialistas para te ajudar pessoalmente. Por favor, aguarde um momento. 👨‍💼")
        if RESPONSIBLE_NUMBER:
            reason = ai_reply.replace("[HUMAN_INTERVENTION] Motivo:", "").strip()
            display_name = known_customer_name or sender_name_from_wpp
            conversa_db = load_conversation_from_db(clean_number)
            history_summary = "Nenhum histórico de conversa encontrado."
            if conversa_db and 'history' in conversa_db:
                history_summary = get_last_messages_summary(conversa_db['history'])
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
    elif ai_reply:
        print(f"🤖 Resposta da IA para {sender_name_from_wpp}: {ai_reply}")
        send_whatsapp_message(sender_number_full, ai_reply)

if __name__ == '__main__':
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

        scheduler = BackgroundScheduler(daemon=True, timezone='America/Sao_Paulo') 
        scheduler.add_job(gerar_e_enviar_relatorio_semanal, 'cron', day_of_week='sun', hour=8, minute=0)
        scheduler.start()
        print("⏰ Agendador de relatórios iniciado. O relatório será enviado todo Domingo às 08:00.")
        
        import atexit
        atexit.register(lambda: scheduler.shutdown())
        
        app.run(host='0.0.0.0', port=8000)
    else:
        print("\nEncerrando o programa devido a erros na inicialização.")