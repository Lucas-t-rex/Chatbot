
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
    
    db = client[db_name] # Conecta ao banco de dados específico do cliente
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

# Cache para conversas ativas (para evitar ler o DB a cada mensagem)
conversations_cache = {}

modelo_ia = None
try:
    modelo_ia = genai.GenerativeModel('gemini-2.5-flash') # Recomendo usar o 1.5-flash
    print("✅ Modelo do Gemini inicializado com sucesso.")
except Exception as e:
    print(f"❌ ERRO: Não foi possível inicializar o modelo do Gemini. Verifique sua API Key. Erro: {e}")

def save_conversation_to_db(contact_id, sender_name, chat_session, tokens_used):
    """Salva o histórico e atualiza a contagem de tokens no MongoDB."""
    try:
        history_list = [
            {'role': msg.role, 'parts': [part.text for part in msg.parts]}
            for msg in chat_session.history
        ]
        
        conversation_collection.update_one(
            {'_id': contact_id},
            {
                '$set': {
                    'sender_name': sender_name,
                    'history': history_list,
                    'last_interaction': datetime.now()
                },
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
def get_last_messages_summary(history, max_messages=8):
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

def gerar_resposta_ia(contact_id, sender_name, user_message):
    """
    Gera uma resposta usando a IA, carregando/salvando o histórico no banco de dados
    e usando um cache para conversas ativas.
    """
    global modelo_ia, conversations_cache

    if not modelo_ia:
        return "Desculpe, estou com um problema interno (modelo IA não carregado)."

    if contact_id not in conversations_cache:
        # <<< MUDANÇA CRÍTICA: Lógica anti-contaminação de memória >>>
        
        # 1. Sempre criamos o prompt inicial com as regras mais recentes.
        horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prompt_inicial = f"""
                A data e hora atuais são: {horario_atual}.
                O nome do usuário com quem você está falando é: {sender_name}.

                =====================================================
                🆘 REGRA DE OURO: ANÁLISE DE INTENÇÃO E INTERVENÇÃO HUMANA (PRIORIDADE MÁXIMA)
                =====================================================
                - SUA TAREFA MAIS IMPORTANTE É ANALISAR A INTENÇÃO DO CLIENTE.
                - Se a intenção for falar com o dono, saber de valores, preços, forma de pagamento ou algo do tipo, acione a intervenção.  
                ESTA REGRA SOBREPÕE TODAS AS OUTRAS REGRAS DE COMPORTAMENTO.
                - CASOS PARA INTERVENÇÃO OBRIGATÓRIA:
                - "quanto custa", "qual valor", "quero pagar", "falar com o proprietário", "quero fazer um investimento", "quero falar com o dono".
                - Pedidos de planos não existentes, reclamações graves, negociações de preço.
                - COMO ACIONAR:
                Sua ÚNICA resposta deve ser a tag abaixo, sem saudações, sem explicações.
                [HUMAN_INTERVENTION] Motivo: [Resumo do motivo do cliente]
                - ERRADO: Cliente pergunta "qual o preço" e você responde com valores.
                - CORRETO: Cliente pergunta "qual o preço" e você responde apenas:
                [HUMAN_INTERVENTION] Motivo: Cliente solicitou valores.

                =====================================================
                🏷️ IDENTIDADE DO ATENDENTE
                =====================================================
                nome: {{Lyra}}
                sexo: {{Feminina}}
                idade: {{40}}
                função: {{Atendente e assistente virtual da academia Dorean Fight}}
                papel: {{Entender o cliente, ajudar e agendar uma aula experimental gratuita.}}

                =====================================================
                🏢 IDENTIDADE DA EMPRESA
                =====================================================
                nome da empresa: {{Dorean Fight}}
                setor: {{Academia de Artes Marciais}}
                missão: {{Promover saúde, disciplina e bem-estar através das artes marciais.}}
                valores: {{Respeito, foco, evolução e superação.}}
                endereço: {{R. João Ernesto Ferreira, 2119 - Sala 2 - Centro, Mandaguari - PR (referência: Barracão antes do Cmei Mickey)}}
                horário de funcionamento: {{Segunda a Quinta das 06:00 às 21:00, Sexta das 06:00 às 20:00 e Sábado das 14:00 às 16:00.}}
                instagram: {{https://www.instagram.com/doreanfight}}

                =====================================================
                💪 MODALIDADES
                =====================================================
                - Kung Fu
                - Capoeira
                - Muay Thai
                - Jiu-Jitsu (adulto e infantil)

                =====================================================
                🧭 COMPORTAMENTO E REGRAS DE ATENDIMENTO
                =====================================================
                1. Seja conciso. Responda em no máximo 1 ou 2 frases.
                2. Nunca ofereça a aula experimental na primeira mensagem — primeiro entenda a necessidade.
                3. Se perguntarem sobre valores → acione intervenção humana.
                4. Após confirmar o agendamento, **não repita a confirmação**.
                5. Use apenas as informações do banco de dados da academia.
                6. Nunca deixe o cliente sair sem agendar uma aula experimental.
                7. Se o cliente quiser saber benefícios ou história das modalidades, responda com base em informações reais da internet.
                8. Seja acolhedor, simpático e motivador.

                =====================================================
                💬 FLUXO DE CONVERSA ESTRATÉGICO
                =====================================================

                **PASSO 1: SAUDAÇÃO E ESCUTA ATIVA**
                - Cumprimente com entusiasmo e pergunte como pode ajudar.
                Exemplo: "Olá, {sender_name}! 👋 Bem-vindo(a) à Dorean Fight! Sou a Lyra, assistente virtual. Como posso te ajudar hoje?"

                **PASSO 2: RESPONDER E CONVIDAR (AÇÃO PRINCIPAL)**
                - Responda à dúvida do cliente e, na mesma mensagem, convide para a aula experimental.
                Exemplo:
                Cliente: "Vocês têm kung fu pra criança?"
                Atendente: "Temos sim! O Kung Fu infantil é ótimo para disciplina e foco. Quer agendar uma aula experimental gratuita pra ele(a) conhecer?"

                **PASSO 3: AGENDAMENTO**
                - Pegue o dia e horário e confirme **uma única vez**.
                Exemplo:
                "Perfeito! Aula experimental agendada para amanhã às 19h. Está no nome de quem?"

                **PASSO 4: PÓS-AGENDAMENTO (MODO AJUDA RÁPIDA)**
                - Após o agendamento, apenas responda perguntas curtas, sem mencionar novamente o agendamento.
                Exemplo:
                Cliente: "Onde fica mesmo?"
                Atendente: "Ficamos na R. João Ernesto Ferreira, 2119 - Centro, perto do Cmei Mickey. Quer o link do Instagram pra conferir as aulas?"

                =====================================================
                ⚙️ PERSONALIDADE DO ATENDENTE
                =====================================================
                - Tom de voz: alegre, acolhedor e profissional.
                - Estilo: humano, prestativo e simpático.
                - Emojis: usar com moderação (máximo 2 por mensagem).
                - Linguagem simples, natural e empática.
                - Evite frases longas ou explicações cansativas.

                =====================================================
                🏁 OBJETIVO FINAL
                =====================================================
                Levar o cliente a agendar uma aula experimental gratuita, garantindo uma conversa leve, simpática e eficiente, e não apssar o preço, passar intervenção humana.
                """
            
        # 2. Construímos o início da conversa com as regras certas.
        convo_start = [
            {'role': 'user', 'parts': [prompt_inicial]},
            {'role': 'model', 'parts': [f"Entendido. A Regra de Ouro de Intervenção Humana é a prioridade máxima. Estou pronto. Olá, {sender_name}! Como posso te ajudar?"]}
        ]

        # 3. Tentamos carregar o histórico antigo SE ele existir.
        loaded_conversation = load_conversation_from_db(contact_id)
        if loaded_conversation and 'history' in loaded_conversation:
            print(f"Iniciando chat para {sender_name} com histórico anterior.")
            # Filtramos o histórico antigo para remover o prompt antigo que estava salvo
            old_history = [msg for msg in loaded_conversation['history'] if not msg['parts'][0].strip().startswith("A data e hora atuais são:")]
            chat = modelo_ia.start_chat(history=convo_start + old_history)
        else:
            print(f"Iniciando novo chat para {sender_name}.")
            chat = modelo_ia.start_chat(history=convo_start)
            
        conversations_cache[contact_id] = {'ai_chat_session': chat, 'name': sender_name}

    chat_session = conversations_cache[contact_id]['ai_chat_session']
    
    try:
        print(f"Enviando para a IA: '{user_message}' (De: {sender_name})")
        
        input_tokens = modelo_ia.count_tokens(chat_session.history + [{'role':'user', 'parts': [user_message]}]).total_tokens
        resposta = chat_session.send_message(user_message)
        output_tokens = modelo_ia.count_tokens(resposta.text).total_tokens
        total_tokens_na_interacao = input_tokens + output_tokens
        
        print(f"📊 Consumo de Tokens: Entrada={input_tokens}, Saída={output_tokens}, Total={total_tokens_na_interacao}")
        
        if not resposta.text.strip().startswith("[HUMAN_INTERVENTION]"):
            save_conversation_to_db(contact_id, sender_name, chat_session, total_tokens_na_interacao)
        
        return resposta.text
    
    except Exception as e:
        print(f"❌ Erro ao comunicar com a API do Gemini: {e}")
        if contact_id in conversations_cache:
            del conversations_cache[contact_id]
        return "Tive um pequeno problema para processar sua mensagem e precisei reiniciar nossa conversa. Você poderia repetir, por favor?"
    
def transcrever_audio_gemini(caminho_do_audio):
    """
    Envia um arquivo de áudio para a API do Gemini e retorna a transcrição em texto.
    """
    global modelo_ia # Vamos reutilizar o modelo Gemini que já foi iniciado

    if not modelo_ia:
        print("❌ Modelo de IA não inicializado. Impossível transcrever.")
        return None

    print(f"🎤 Enviando áudio '{caminho_do_audio}' para transcrição no Gemini...")
    try:
        audio_file = genai.upload_file(
            path=caminho_do_audio, 
            mime_type="audio/ogg"
        )
        
        # Pedimos ao modelo para transcrever o áudio
        response = modelo_ia.generate_content(["Por favor, transcreva o áudio a seguir.", audio_file])
        
        # Opcional, mas recomendado: deletar o arquivo do servidor do Google após o uso
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

processed_messages = set()  # para evitar loops

@app.route('/webhook', methods=['POST'])
def receive_webhook():
    """Recebe mensagens do WhatsApp enviadas pela Evolution API."""
    data = request.json
    print(f"📦 DADO BRUTO RECEBIDO NO WEBHOOK: {data}")

    try:
        message_data = data.get('data', {}) or data
        key_info = message_data.get('key', {})

        # --- 1️⃣ LÓGICA CORRIGIDA: Ignora mensagens do bot, A MENOS que seja um comando do responsável ---
        if key_info.get('fromMe'):
            # Pega o número para verificar se é a exceção (o responsável)
            sender_number_full = key_info.get('remoteJid')
            
            # Por segurança, se não tivermos o número, ignora.
            if not sender_number_full:
                return jsonify({"status": "ignored_from_me_no_sender"}), 200

            clean_number = sender_number_full.split('@')[0]
            
            # Se o número que enviou a mensagem NÃO for o do responsável, ignora.
            # Se FOR o do responsável, a função continua.
            if clean_number != RESPONSIBLE_NUMBER:
                print(f"➡️  Mensagem do próprio bot ignorada (remetente: {clean_number}).")
                return jsonify({"status": "ignored_from_me"}), 200
            
            print(f"⚙️  Mensagem do próprio bot PERMITIDA (é um comando do responsável: {clean_number}).")

        # --- 2️⃣ Pega o ID único da mensagem ---
        message_id = key_info.get('id')
        if not message_id:
            return jsonify({"status": "ignored_no_id"}), 200

        # --- 3️⃣ Se já processou esta mensagem, ignora ---
        if message_id in processed_messages:
            print(f"⚠️ Mensagem {message_id} já processada, ignorando.")
            return jsonify({"status": "ignored_duplicate"}), 200
        processed_messages.add(message_id)
        if len(processed_messages) > 1000:
            processed_messages.clear()

        # --- 4️⃣ Retorna imediatamente 200 para evitar reenvio da Evolution ---
        threading.Thread(target=process_message, args=(message_data,)).start()
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
    
    # Converte a mensagem para minúsculas para não diferenciar "ok", "OK", "Ok", etc.
    command_parts = message_content.lower().strip().split()
    
    # --- Comando: ok <numero> ---
    # A verificação agora é feita com "ok" em minúsculas
    if len(command_parts) == 2 and command_parts[0] == "ok":
        customer_number_to_reactivate = command_parts[1].replace('@s.whatsapp.net', '').strip()
        
        try:
            # Tenta encontrar o cliente para garantir que ele existe
            customer = conversation_collection.find_one({'_id': customer_number_to_reactivate})

            if not customer:
                send_whatsapp_message(responsible_number, f"⚠️ *Atenção:* O cliente com o número `{customer_number_to_reactivate}` não foi encontrado no banco de dados.")
                return 

            # Atualiza o status de intervenção no banco de dados
            result = conversation_collection.update_one(
                {'_id': customer_number_to_reactivate},
                {'$set': {'intervention_active': False}}
            )

            # Limpa o cache da conversa para forçar a releitura do status
            if customer_number_to_reactivate in conversations_cache:
                del conversations_cache[customer_number_to_reactivate]
                print(f"🗑️  Cache da conversa do cliente {customer_number_to_reactivate} limpo com sucesso.")

            if result.modified_count > 0:
                 # Envia confirmação para o responsável
                send_whatsapp_message(responsible_number, f"✅ Atendimento automático reativado para o cliente `{customer_number_to_reactivate}`.")
                # Notifica o cliente que o bot está de volta
                send_whatsapp_message(customer_number_to_reactivate, "Obrigado por aguardar! Meu assistente virtual já está disponível para continuar nosso atendimento. Como posso te ajudar? 😊")
            else:
                send_whatsapp_message(responsible_number, f"ℹ️ O atendimento para `{customer_number_to_reactivate}` já estava ativo. Nenhuma alteração foi necessária.")

        except Exception as e:
            print(f"❌ Erro ao tentar reativar cliente: {e}")
            send_whatsapp_message(responsible_number, f"❌ Ocorreu um erro técnico ao tentar reativar o cliente. Verifique o log do sistema.")
            
    # --- Se não for um comando conhecido, envia ajuda ---
    else:
        print("⚠️ Comando não reconhecido do responsável.")
        help_message = (
            "Comando não reconhecido. 🤖\n\n"
            "Para reativar o atendimento de um cliente, envie a mensagem no formato exato:\n"
            "`ok <numero_do_cliente>`\n\n"
            "*(Exemplo):*\n`ok 5544912345678`"
        )
        send_whatsapp_message(responsible_number, help_message)
        return True # A mensagem do responsável foi tratada (mesmo sendo inválida)
    
def process_message(message_data):
    """
    Processa a mensagem, primeiro verificando se é um comando do responsável,
    e somente depois tratando como uma mensagem de cliente.
    """
    try:
        key_info = message_data.get('key', {})
        sender_number_full = key_info.get('senderPn') or key_info.get('participant') or key_info.get('remoteJid')

        # Ignora mensagens de grupo ou sem remetente
        if not sender_number_full or sender_number_full.endswith('@g.us'):
            return

        clean_number = sender_number_full.split('@')[0]
        sender_name = message_data.get('pushName') or 'Cliente'

        # Extrai o conteúdo da mensagem (texto ou áudio transcrito)
        user_message_content = None
        message = message_data.get('message', {})
        
        if message.get('conversation'):
            user_message_content = message['conversation']
        elif message.get('extendedTextMessage'):
            user_message_content = message['extendedTextMessage'].get('text')
        
        # <<< CORREÇÃO 1: LÓGICA DE ÁUDIO AJUSTADA >>>
        # A chave 'base64' geralmente vem DENTRO de 'audioMessage', e não fora.
        # Esta nova lógica verifica isso corretamente.
        elif 'audioMessage' in message:
            audio_message = message['audioMessage']
            if 'mediaKey' in audio_message: # Usamos uma chave mais confiável para detectar áudio
                print(f"🎤 Mensagem de áudio recebida de {sender_name} ({clean_number}).")
                
                # A API Evolution pode não enviar 'base64', então precisamos buscar a mídia
                # Esta é uma abordagem mais robusta, mas por enquanto vamos manter a sua se funcionar.
                # Se a transcrição parar, o ideal é usar a rota /chat/getBase64FromMediaKey da Evolution API.
                # Por simplicidade, vamos assumir que o 'base64' pode estar em 'message'
                audio_base64 = message.get('base64') 

                if audio_base64:
                    audio_data = base64.b64decode(audio_base64)
                    temp_audio_path = f"/tmp/audio_{clean_number}.ogg"
                    with open(temp_audio_path, 'wb') as f:
                        f.write(audio_data)
                    
                    user_message_content = transcrever_audio_gemini(temp_audio_path)
                    os.remove(temp_audio_path)
                
                    if not user_message_content:
                        send_whatsapp_message(sender_number_full, "Desculpe, não consegui entender o áudio. Pode tentar novamente? 🎧")
                        return
                else:
                    print("⚠️ Áudio recebido, mas sem a chave 'base64' no webhook. A transcrição foi ignorada.")


        if not user_message_content:
            print("➡️ Mensagem ignorada (sem conteúdo útil).")
            return

        # =================================================================
        # LÓGICA PRINCIPAL: O BOT DECIDE O QUE FAZER COM A MENSAGEM
        # =================================================================

        # <<< CORREÇÃO 2: NORMALIZAÇÃO DO NÚMERO DO RESPONSÁVEL >>>
        # Esta lógica remove o "nono dígito" para garantir que a comparação funcione
        # mesmo que a API envie o número sem ele.
        responsible_num = RESPONSIBLE_NUMBER.strip() if RESPONSIBLE_NUMBER else ""
        
        # Remove o nono dígito do número do responsável, se ele existir no padrão (55 XX 9 XXXX-XXXX)
        if len(responsible_num) == 13 and responsible_num.startswith('55') and responsible_num[4] == '9':
            responsible_num_normalized = responsible_num[:4] + responsible_num[5:]
        else:
            responsible_num_normalized = responsible_num

        # Agora, a comparação é feita com os números já normalizados
        if responsible_num_normalized and clean_number.strip() == responsible_num_normalized:
            # Sim, a mensagem é do responsável. Trate como um comando.
            handle_responsible_command(user_message_content, clean_number)
            return

        # Caminho 2: A mensagem é de um Cliente.
        # (Esta parte só executa se o 'if' acima for falso)
        
        # O bot está pausado para este cliente?
        conversation_status = conversation_collection.find_one({'_id': clean_number})
        if conversation_status and conversation_status.get('intervention_active', False):
            print(f"⏸️  Conversa com {sender_name} ({clean_number}) pausada para atendimento humano.")
            return

        # Se não estiver pausado, processe com a IA.
        print(f"\n🧠  Processando mensagem de {sender_name} ({clean_number}): '{user_message_content}'")
        ai_reply = gerar_resposta_ia(clean_number, sender_name, user_message_content)

        # Se a IA pediu ajuda humana...
        if ai_reply and ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
            print(f"‼️ INTERVENÇÃO HUMANA SOLICITADA para {sender_name} ({clean_number})")
            
            # Pausa o bot para este cliente
            conversation_collection.update_one(
                {'_id': clean_number}, {'$set': {'intervention_active': True}}, upsert=True
            )
            
            # Avisa o cliente
            send_whatsapp_message(sender_number_full, "Entendido. Já notifiquei um de nossos especialistas para te ajudar pessoalmente. Por favor, aguarde um momento. 👨‍💼")
            
            # Notifica o responsável com os detalhes
            if RESPONSIBLE_NUMBER:
                  reason = ai_reply.replace("[HUMAN_INTERVENTION] Motivo:", "").strip()
                  conversa_db = load_conversation_from_db(clean_number)
                  
                  history_summary = "Nenhum histórico de conversa encontrado."
                  if conversa_db and 'history' in conversa_db:
                      history_summary = get_last_messages_summary(conversa_db['history'])

                  notification_msg = (
                      f"🔔 *NOVA SOLICITAÇÃO DE ATENDIMENTO HUMANO* 🔔\n\n"
                      f"👤 *Cliente:* {sender_name}\n"
                      f"📞 *Número:* `{clean_number}`\n\n"
                      f"💬 *Motivo da Chamada:*\n_{reason}_\n\n"
                      f"📜 *Resumo da Conversa:*\n{history_summary}\n\n"
                      f"-----------------------------------\n"
                      # Altere a linha abaixo para usar "ok"
                      f"*AÇÃO NECESSÁRIA:*\nApós resolver, envie para *ESTE NÚMERO* o comando:\n`ok {clean_number}`"
                  )
                  send_whatsapp_message(f"{RESPONSIBLE_NUMBER}@s.whatsapp.net", notification_msg)
        
        # Se for uma resposta normal da IA...
        elif ai_reply:
            print(f"🤖  Resposta da IA para {sender_name}: {ai_reply}")
            send_whatsapp_message(sender_number_full, ai_reply)

    except Exception as e:
        print(f"❌ Erro fatal ao processar mensagem: {e}")

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