
import google.generativeai as genai
import requests
import os
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
from dotenv import load_dotenv
from urllib.parse import urlparse
import base64
import threading
from pymongo import MongoClient
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from apscheduler.schedulers.background import BackgroundScheduler


CLIENT_NAME = "Neuro Soluções em Tecnologia"
RESPONSIBLE_NUMBER = "5548998389781"

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
                - SUA TAREFA MAIS IMPORTANTE É ANALISAR A INTENÇÃO DO CLIENTE. Se a intenção for falar com um humano, sua única ação é acionar a intervenção. ESTA REGRA SOBREPÕE TODAS AS OUTRAS REGRAS DE COMPORTAMENTO.
                - CASOS PARA INTERVENÇÃO OBRIGATÓRIA:
                - Pedidos explícitos: "falar com o dono", "falar com o responsável", "quero falar com um humano", "falar com o proprietário", "quero fazer um investimento".
                - Perguntas complexas sem resposta: Pedidos de produtos/planos que não existem, reclamações graves, negociações de preços especiais.
                - COMO ACIONAR: Sua ÚNICA resposta DEVE ser a tag abaixo, sem saudações, sem explicações.
                [HUMAN_INTERVENTION] Motivo: [Resumo do motivo do cliente]
                - O QUE NÃO FAZER (ERRO CRÍTICO):
                - ERRADO: Cliente diz "Quero falar com o dono" e você responde "Compreendo, para isso, ligue para o número X...".
                - CORRETO: Cliente diz "Quero falar com o dono" e sua resposta é APENAS: [HUMAN_INTERVENTION] Motivo: Cliente solicitou falar com o dono.
                - Se a intenção do cliente NÃO se encaixar nos casos acima, você deve seguir as regras de atendimento normais abaixo.
                =====================================================
                🏷️ IDENTIDADE DO ATENDENTE
                =====================================================
                nome: {{Isaque}}
                sexo: {{Masculino}}
                idade: {{40}}
                função: {{Atendente, vendedor, especialista em Ti e machine learning}} 
                papel: {{Você deve atender a pessoa, entender a necessidade da pessoa, vender o plano de acordo com a  necessidade, tirar duvidas, ajudar.}}  (ex: tirar dúvidas, passar preços, enviar catálogos, agendar horários)

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
                - Plano Atendente: {{Atendente personalizada, configurada conforme a necessidade do cliente.
                                  Neste plano, o atendimento pode funcionar de três formas:

                                  Atendimento Autônomo:
                                  A atendente responde sozinha até o final da conversa, usando apenas as informações liberadas.

                                  Intervenção Humana:
                                  O responsável pode entrar na conversa quando quiser, para tomar decisões ou dar respostas mais específicas.

                                  Bifurcação de Mensagens:
                                  Permite enviar informações da conversa para outro número (por exemplo, repassar detalhes para o gestor ou outro atendente).}}
                - Plano Secretário: {{Agendamento Inteligente:
                                  Faz agendamentos, alterações e cancelamentos de horários ou serviços, conforme solicitado pelo cliente.

                                  🔔 Avisos Automáticos:
                                  Envia notificações e lembretes para o telefone do responsável sempre que houver mudança ou novo agendamento.

                                  💻 Agenda Integrada:
                                  Acompanha um software externo conectado ao WhatsApp, permitindo manter todos os dados organizados e atualizados exatamente como negociado.}}
                - Plano Premium: {{Em construção}}
                - {{}}

                =====================================================
                💰 PLANOS E VALORES
                =====================================================
                Instalação: {{R$200,00 mensal}} todos os planos tem um fazer de setup inicial , para instalação do projeto e os requisitos da IA. 
                plano Atendente: {{R$300,00 mensal}}
                Plano Secretário: {{R$500,00 mensal}}
                plano avançado: {{Em analise}}
                observações: {{ex: valores podem variar conforme personalização ou integrações extras.}}

                =====================================================
                🧭 COMPORTAMENTO E REGRAS DE ATENDIMENTO
                =====================================================
                ações:
                - Responda sempre de forma profissional, empática e natural.
                - Use frases curtas, diretas e educadas.
                - Mantenha sempre um tom positivo e proativo.
                - Ajude o cliente a resolver dúvidas e tomar decisões.
                - Entender a nessecidade e vender o plano ideal para a pessoa.
                - Se a pessoa tiver objeção para não fechar tente enteder o porque.
                - Sempre usar quebra de objeções quando o cliente não querer fechar.
                - Se depois de tentar usar as quebras de objeção a pessoa ainda não querer fechar, ofereça uma semana gratis.
                - Se mesmo oferecendo uma semana gratis, use agradecimentos e despedidas.

                falas:
                - Use linguagem simples e amigável.
                - Evite termos técnicos, a menos que o cliente peça.
                - Não use emojis em excesso (máximo 2 por mensagem).

                saudações:
                - Sempre cumprimente com entusiasmo e simpatia.
                Exemplo: "Olá! 😊 Seja muito bem-vindo(a) à {{Neuro Soluções em Tecnologia}}!"

                agradecimentos:
                - Agradeça de forma sincera e breve.
                Exemplo: "Agradeço o seu contato! Foi um prazer ajudar. 🙏"

                despedidas:
                - Despeça-se com elegância e positividade.
                Exemplo: "Tenha um ótimo dia! Ficamos à disposição sempre que precisar. 🌟
                Neuro Soluções em Tecnologia - O futuro é agora."

                não deve fazer:
                - Não inventar informações que não saiba.
                - Não discutir, nem responder de forma rude.
                - Não compartilhar dados pessoais.
                - Não responder perguntas fora do contexto da empresa.
                - Nunca deixe o cliente sair sem ter vendido.

                missão:
                - Ajudar o cliente a obter respostas rápidas e confiáveis.
                - Entender a nessecidade e vender o plano ideal para a pessoa.
                - Gerar uma boa experiência no atendimento.
                - Reforçar o nome e a credibilidade da empresa.
                - Sempre usar quebra de objeções quando o cliente não querer fechar.

                =====================================================
                ⚙️ PERSONALIDADE DO ATENDENTE
                =====================================================
                - Tom de voz: {{alegre, acolhedor, profissional, descontraído}} 
                - Ritmo de conversa: natural e fluido.
                - Estilo: humano, prestativo e simpático.
                - Emojis: usar com moderação, sempre com propósito.
                - Curiosidade: se o cliente parecer indeciso, ofereça ajuda com sugestões.
                - Converssas: Não use muitas palavras para não ser cansativo.

                =====================================================
                🧩 EXEMPLO DE COMPORTAMENTO
                =====================================================
                Cliente: "Oi, quais são os horários de funcionamento?"
                Atendente: "Olá! 😊 A {{Neuro Soluções em Tecnologi}} funciona de {{De segunda-feira a sexta-feira das 8:00 as 18:00 }}. Quer que eu te ajude a agendar um horário?"

                Cliente: "Vocês têm planos mensais?"
                Atendente: "Temos sim! 🙌 Trabalhamos com diferentes planos adaptados ao seu perfil. Quer que eu te envie as opções?"

                =====================================================
                PRONTO PARA ATENDER O CLIENTE
                =====================================================
                Quando o cliente enviar uma mensagem, cumprimente e inicie o atendimento de forma natural, usando o nome do cliente se disponível, tente entender o que ele precisa e sempre coloque o cliente em primeiro lugar.
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

        # --- 1️⃣ Ignora mensagens enviadas por você mesmo ---
        if key_info.get('fromMe'):
            return jsonify({"status": "ignored_from_me"}), 200

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


def process_message(message_data):
    """Processa a mensagem (texto ou áudio) com a nova lógica de intervenção humana."""
    try:
        key_info = message_data.get('key', {})
        
        # Lógica final para pegar o número de telefone correto
        sender_number_full = key_info.get('senderPn') or key_info.get('participant') or key_info.get('remoteJid')

        if not sender_number_full or sender_number_full.endswith('@g.us'):
            return

        clean_number = sender_number_full.split('@')[0]
        sender_name = message_data.get('pushName') or 'Desconhecido'
        message = message_data.get('message', {})
        user_message_content = None

        if message.get('conversation') or message.get('extendedTextMessage'):
            user_message_content = message.get('conversation') or message.get('extendedTextMessage', {}).get('text')
        elif message.get('audioMessage') and message.get('base64'):
            print(f"🎤 Mensagem de áudio recebida de {sender_name}.")
            audio_base64 = message['base64']
            audio_data = base64.b64decode(audio_base64)
            temp_audio_path = f"/tmp/audio_{clean_number}.ogg"
            with open(temp_audio_path, 'wb') as f:
                f.write(audio_data)
            transcribed_text = transcrever_audio_gemini(temp_audio_path)
            os.remove(temp_audio_path)
            if not transcribed_text:
                send_whatsapp_message(sender_number_full, "Desculpe, não consegui entender o áudio. Pode tentar novamente? 🎧")
                return 
            user_message_content = transcribed_text

        if not user_message_content:
            print("➡️ Mensagem ignorada (sem conteúdo útil).")
            return

        # <<< CORREÇÃO DEFINITIVA: LÓGICA DE COMANDO DO RESPONSÁVEL (BLOQUEIO ADMINISTRATIVO) >>>
        if RESPONSIBLE_NUMBER and clean_number == RESPONSIBLE_NUMBER:
            print(f"⚙️ Mensagem recebida do número do responsável: '{user_message_content}'")
            command_parts = user_message_content.lower().strip().split()
            
            if len(command_parts) == 2 and command_parts[0] == "reativar":
                customer_number_to_reactivate = command_parts[1].replace('@s.whatsapp.net', '').strip()
                print(f"⚙️ Executando comando para reativar: {customer_number_to_reactivate}")
                
                # Atualiza o banco de dados
                conversation_collection.update_one(
                    {'_id': customer_number_to_reactivate},
                    {'$set': {'intervention_active': False}},
                )
                
                # Envia confirmações
                send_whatsapp_message(RESPONSIBLE_NUMBER, f"✅ Atendimento automático reativado para o cliente {customer_number_to_reactivate}.")
                send_whatsapp_message(customer_number_to_reactivate, "Obrigado por aguardar! Meu assistente virtual já está disponível para continuar nosso atendimento. Como posso te ajudar? 😊")
            else:
                # Se for qualquer outra mensagem do responsável, avisa sobre o comando correto.
                print("⚠️ Comando não reconhecido do responsável. Enviando instrução.")
                send_whatsapp_message(RESPONSIBLE_NUMBER, f"Comando não reconhecido. Para reativar um cliente, por favor, envie a mensagem no formato exato: `reativar <numero_do_cliente>`")

            # A linha mais importante: para a execução AQUI para qualquer mensagem do responsável, quebrando o loop.
            return

        # --- LÓGICA NORMAL PARA CLIENTES (SÓ EXECUTA SE NÃO FOR O RESPONSÁVEL) ---
        conversation_status = conversation_collection.find_one({'_id': clean_number})
        if conversation_status and conversation_status.get('intervention_active', False):
            print(f"⏸️ Conversa com {sender_name} ({clean_number}) está em modo de intervenção humana. Mensagem ignorada.")
            return

        print(f"\n🧠 Processando mensagem de {sender_name}: {user_message_content}")
        ai_reply = gerar_resposta_ia(clean_number, sender_name, user_message_content)

        if ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
            # (O resto da sua lógica de intervenção permanece exatamente a mesma)
            print(f"‼️ INTERVENÇÃO HUMANA SOLICITADA para {sender_name} ({clean_number})")
            
            conversation_collection.update_one(
                {'_id': clean_number},
                {'$set': {'intervention_active': True}},
                upsert=True
            )
            
            send_whatsapp_message(sender_number_full, "Entendido. Já notifiquei um de nossos especialistas para te ajudar pessoalmente. Por favor, aguarde um momento. 👨‍💼")
            
            if RESPONSIBLE_NUMBER:
                reason = ai_reply.replace("[HUMAN_INTERVENTION] Motivo:", "").strip()
                conversa_db = load_conversation_from_db(clean_number)
                
                if conversa_db and 'history' in conversa_db:
                    history_summary = get_last_messages_summary(conversa_db['history'])
                else:
                    history_summary = "Nenhum histórico de conversa encontrado."

                notification_msg = (
                    f"🔔 *NOVA SOLICITAÇÃO DE ATENDIMENTO HUMANO* 🔔\n\n"
                    f"👤 *Cliente:* {sender_name}\n"
                    f"📞 *Número:* `{clean_number}`\n\n"
                    f"💬 *Motivo da Chamada:*\n_{reason}_\n\n"
                    f"📜 *Resumo da Conversa:*\n{history_summary}\n\n"
                    f"-----------------------------------\n"
                    f"*AÇÃO NECESSÁRIA:*\nEntre em contato com o cliente. Após resolver, envie para *ESTE NÚMERO* o comando:\n`reativar {clean_number}`"
                )
                
                send_whatsapp_message(f"{RESPONSIBLE_NUMBER}@s.whatsapp.net", notification_msg)
            else:
                print("⚠️ RESPONSIBLE_NUMBER não definido. Não é possível notificar.")
        else:
            print(f"🤖 Resposta: {ai_reply}")
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