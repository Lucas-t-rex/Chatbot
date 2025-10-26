
import google.generativeai as genai
import requests
import os
from flask import Flask, request, jsonify
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse
import base64
import threading
from pymongo import MongoClient

load_dotenv()
EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_DB_URI = os.environ.get("MONGO_DB_URI")


try:
    client = MongoClient(MONGO_DB_URI)
    db = client.chatbot_db # Nome do banco de dados
    conversation_collection = db.conversations # Nome da "tabela"
    print("✅ Conectado ao MongoDB Atlas com sucesso.")
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

# ADICIONE ESTE BLOCO NOVO
def save_conversation_to_db(contact_id, sender_name, chat_session):
    """Salva ou atualiza o histórico da conversa no MongoDB."""
    try:
        history_list = [
            {'role': msg.role, 'parts': [part.text for part in msg.parts]}
            for msg in chat_session.history
        ]
        # O comando update_one com upsert=True é perfeito:
        # ele atualiza se o contato existir, ou insere um novo se não existir.
        conversation_collection.update_one(
            {'_id': contact_id},
            {'$set': {
                'sender_name': sender_name,
                'history': history_list,
                'last_interaction': datetime.now()
            }},
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
            return result['history']
    except Exception as e:
        print(f"❌ Erro ao carregar conversa do MongoDB para {contact_id}: {e}")
    return None

def gerar_resposta_ia(contact_id, sender_name, user_message):
    """
    Gera uma resposta usando a IA, carregando/salvando o histórico no banco de dados
    e usando um cache para conversas ativas.
    """
    global modelo_ia, conversations_cache # Alterado de 'conversations' para 'conversations_cache'

    if not modelo_ia:
        return "Desculpe, estou com um problema interno (modelo IA não carregado)."

    # --- LÓGICA DE CARREGAMENTO (LOADING LOGIC) ---
    # 1. Se a conversa não está no cache de memória RAM, vamos buscá-la no banco de dados.
    if contact_id not in conversations_cache:
        loaded_history = load_conversation_from_db(contact_id)
        
        # 2. Se um histórico foi encontrado no banco de dados...
        if loaded_history:
            # ...iniciamos o chat da IA com esse histórico antigo. A IA "se lembrará" de tudo.
            chat = modelo_ia.start_chat(history=loaded_history)
        # 3. Se não há histórico no banco de dados, é um usuário completamente novo.
        else:
            print(f"Iniciando nova sessão de chat para o contato: {sender_name} ({contact_id})")
            
            # Mantivemos seu prompt inicial exatamente como estava.
            horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            historico_anterior = "Nenhum histórico encontrado para esta sessão."
            prompt_inicial = f"""
                A data e hora atuais são: {horario_atual}.
                O nome do usuário com quem você está falando é: {sender_name}.
                Histórico anterior: {historico_anterior}.
                Voce é o atendente.
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
                contatos: {{44991676564}} 
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
            
            chat = modelo_ia.start_chat(history=[
                {'role': 'user', 'parts': [prompt_inicial]},
                {'role': 'model', 'parts': [f"Entendido. Perfil de personalidade e todas as regras assimiladas. Olá, {sender_name}! Como posso te ajudar?"]}
            ])
        
        # 4. Adicionamos a conversa (nova ou carregada) ao cache para acesso rápido.
        conversations_cache[contact_id] = {'ai_chat_session': chat, 'name': sender_name}

    # A partir daqui, o código usa a sessão que está no cache.
    chat_session = conversations_cache[contact_id]['ai_chat_session']
    
    try:
        print(f"Enviando para a IA: '{user_message}' (De: {sender_name})")
        resposta = chat_session.send_message(user_message)

        # --- LÓGICA DE SALVAMENTO (SAVING LOGIC) ---
        # 5. Após a IA responder, salvamos IMEDIATAMENTE o histórico atualizado no banco de dados.
        save_conversation_to_db(contact_id, sender_name, chat_session)
        
        return resposta.text
    except Exception as e:
        print(f"❌ Erro ao comunicar com a API do Gemini: {e}")

        # Se der erro na comunicação com a IA, limpamos o cache para forçar
        # o recarregamento do banco de dados na próxima tentativa.
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
    """Processa a mensagem real (texto ou áudio)."""
    try:
        sender_number_full = message_data.get('key', {}).get('senderPn') or message_data.get('key', {}).get('remoteJid')
        if not sender_number_full:
            return

        clean_number = sender_number_full.split('@')[0]
        sender_name = message_data.get('pushName') or 'Desconhecido'
        message = message_data.get('message', {})

        user_message_content = None

        # --- TEXTO ---
        if message.get('conversation') or message.get('extendedTextMessage'):
            user_message_content = message.get('conversation') or message.get('extendedTextMessage', {}).get('text')

        # --- ÁUDIO ---
        elif message.get('audioMessage') and message.get('base64'):
            print(f"🎤 Mensagem de áudio recebida de {sender_name}.")
            audio_base64 = message['base64']
            audio_data = base64.b64decode(audio_base64)
            temp_audio_path = f"/tmp/audio_{clean_number}.ogg"
            with open(temp_audio_path, 'wb') as f:
                f.write(audio_data)
            transcribed_text = transcrever_audio_gemini(temp_audio_path)
            os.remove(temp_audio_path)
            user_message_content = transcribed_text or "Desculpe, não consegui entender o áudio. Pode tentar novamente? 🎧"

        if user_message_content:
            print(f"\n🧠 Processando mensagem de {sender_name}: {user_message_content}")
            ai_reply = gerar_resposta_ia(clean_number, sender_name, user_message_content)
            print(f"🤖 Resposta: {ai_reply}")
            send_whatsapp_message(sender_number_full, ai_reply)
        else:
            print("➡️ Mensagem ignorada (sem conteúdo útil).")

    except Exception as e:
        print(f"❌ Erro ao processar mensagem: {e}")

if __name__ == '__main__':
    if modelo_ia:
        print("\n=============================================")
        print("   CHATBOT WHATSAPP COM IA INICIADO")
        print("=============================================")
        print("Servidor aguardando mensagens no webhook...")
        
        app.run(host='0.0.0.0', port=8000)
    else:
        print("\n encerrando o programa devido a erros na inicialização.")