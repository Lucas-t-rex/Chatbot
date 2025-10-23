
import google.generativeai as genai
import requests
import logging
import os
import json
from flask import Flask, request, jsonify
from datetime import datetime


# 1. Configurações da Evolution API para enviar mensagens
EVOLUTION_API_URL = "http://127.0.0.1:8080/message/sendText/chatgrupar"
EVOLUTION_API_KEY = "1234" # Sua chave da Evolution API

# 2. Chave de API do Google Gemini
# Lembre-se do aviso de segurança sobre expor a chave em código.
# Para produção, use variáveis de ambiente.
try:
    genai.configure(api_key="AIzaSyB24rmQDo_NyAAH3Dtwzsd_CvzPbyX-kYo") # <-- INSIRA SUA API KEY DO GOOGLE AQUI
except Exception as e:
    print(f"AVISO: A chave de API do Google não foi configurada. Erro: {e}")
    print("Por favor, insira sua chave na variável 'genai.configure(api_key=...)'.")



PASTA_DIARIO = r"C:\Users\Windows\Desktop\projetos\Chatbot\meu_diario"
ARQUIVO_CONVERSAS = os.path.join(PASTA_DIARIO, "conversations.json")

os.makedirs(PASTA_DIARIO, exist_ok=True)
os.makedirs(os.path.join(PASTA_DIARIO, "historicos"), exist_ok=True)


# --- INICIALIZAÇÃO DA IA E ESTRUTURAS DE DADOS ---

# Dicionário para armazenar as conversas e as sessões de chat da IA para cada contato
conversations = {}

# Inicializa o modelo da IA que será usado
modelo_ia = None
try:
    modelo_ia = genai.GenerativeModel('gemini-2.5-flash')
except Exception as e:
    print(f"ERRO: Não foi possível inicializar o modelo do Gemini. Verifique sua API Key. Erro: {e}")


# --- FUNÇÕES DA INTELIGÊCIA ARTIFICIAL ---

def carregar_historico_conversa(contact_id):
    """Lê o arquivo de histórico de um contato específico."""
    caminho_historico = os.path.join(PASTA_DIARIO, "historicos", f"{contact_id}.txt")
    if os.path.exists(caminho_historico):
        with open(caminho_historico, 'r', encoding='utf-8') as f:
            return f.read()
    return "" # Retorna vazio se não houver histórico

def salvar_historico_conversa(contact_id, user_message, ai_reply):
    """Salva a nova interação no arquivo de histórico do contato com data e hora."""
    caminho_historico = os.path.join(PASTA_DIARIO, "historicos", f"{contact_id}.txt")
    os.makedirs(os.path.dirname(caminho_historico), exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(caminho_historico, 'a', encoding='utf-8') as f:
        f.write(f"[{timestamp}] Pessoa: {user_message}\n")
        f.write(f"[{timestamp}] IA: {ai_reply}\n")

def carregar_dados_conversas():
    """Carrega os dados das conversas do arquivo JSON no início do programa."""
    if os.path.exists(ARQUIVO_CONVERSAS):
        try:
            with open(ARQUIVO_CONVERSAS, 'r', encoding='utf-8') as f:
                print("✅ Dados de conversas anteriores carregados de conversations.json")
                return json.load(f)
        except Exception as e:
            print(f"❌ Erro ao carregar conversations.json: {e}. Começando do zero.")
            return {}
    return {}

def salvar_dados_conversas():
    """Salva o dicionário 'conversations' no arquivo JSON."""
    # Criamos uma cópia para não salvar objetos complexos como a sessão de chat
    dados_para_salvar = {}
    for contact_id, data in conversations.items():
        dados_para_salvar[contact_id] = {
            'name': data.get('name'),
            'messages': data.get('messages', [])
        }
    
    try:
        with open(ARQUIVO_CONVERSAS, 'w', encoding='utf-8') as f:
            json.dump(dados_para_salvar, f, indent=4)
    except Exception as e:
        print(f"❌ Erro ao salvar dados em conversations.json: {e}")



# Estas variáveis seriam definidas em outra parte do seu código
# conversations = {} 
# modelo_ia = None 

def gerar_resposta_ia(contact_id, sender_name, user_message): # <-- SEU CÓDIGO ORIGINAL
    """
    Gera uma resposta usando a IA do Gemini, mantendo o histórico da conversa
    para cada contato.
    """
    global modelo_ia, conversations # Adicionado 'conversations' para o exemplo funcionar

    if not modelo_ia:
        return "Desculpe, estou com um problema interno (modelo IA não carregado) e não consigo responder agora."

    historico_anterior = carregar_historico_conversa(contact_id)
    if not historico_anterior:
        historico_anterior = "Nenhum histórico encontrado."
    else:
        print(f"🧠 Histórico carregado para {sender_name} ({contact_id})")
    
    # Verifica se já existe uma sessão de chat para este contato
    if 'ai_chat_session' not in conversations.get(contact_id, {}):
        print(f"Iniciando nova sessão de chat para o contato: {sender_name} ({contact_id})")

        horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # --- INÍCIO DO PROMPT DETALHADO PARA A ACADEMIA ---
        prompt_inicial = f"""
        A data e hora atuais são: {horario_atual}.
        O nome do usuário com quem você está falando é: {sender_name}.
        Histórico anterior: {historico_anterior}

        ## PERFIL ##
        Você é um profissional de ti muito inteligente e legal, feito pra converssar com quem te chama e tirar duvidas 
        pessoa que voce converssa, sempre em poucas palavras no maximo uma frase.
        """
        
        # Cria um objeto 'conversations[contact_id]' se ele não existir
        if contact_id not in conversations:
            conversations[contact_id] = {}

        # Inicia um novo chat com o histórico pré-definido pelo prompt
        chat = modelo_ia.start_chat(history=[
            {'role': 'user', 'parts': [prompt_inicial]},
            {'role': 'model', 'parts': [f"Entendido. Perfil de personalidade e todas as regras assimiladas. Eu sou o assistente virtual da Dorean Fight. Olá, {sender_name}! Bem-vindo(a) à Dorean Fight! Como posso te ajudar a começar sua jornada no mundo das artes marciais hoje?"]}
        ])
        conversations[contact_id]['ai_chat_session'] = chat

    # Recupera a sessão de chat e envia a nova mensagem
    chat_session = conversations[contact_id]['ai_chat_session']
    
    try:
        print(f"Enviando para a IA: '{user_message}' (De: {sender_name})")
        resposta = chat_session.send_message(user_message)
        return resposta.text
    except Exception as e:
        print(f"❌ Erro ao comunicar com a API do Gemini: {e}")
        return "Tive um pequeno problema para processar sua mensagem. Você poderia repetir, por favor?"
# --- FUNÇÕES DO WHATSAPP (EVOLUTION API) ---

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

# --- SERVIDOR WEB (FLASK) PARA RECEBER MENSAGENS ---

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def receive_webhook():
    """Recebe as mensagens do WhatsApp enviadas pela Evolution API."""
    data = request.json
    try:
        message_data = data.get('data', {})
        key_info = message_data.get('key', {})

        if key_info.get('fromMe'):
            return jsonify({"status": "ignored_from_me"}), 200

        sender_number_full = key_info.get('senderPn') or key_info.get('remoteJid')
        if not sender_number_full:
            return jsonify({"status": "ignored_no_sender"}), 200
        clean_number = sender_number_full.split('@')[0]

        message_text = (
            message_data.get('message', {}).get('conversation') or
            message_data.get('message', {}).get('extendedTextMessage', {}).get('text')
        )

        if message_text:
            sender_name = message_data.get('pushName') or 'Desconhecido'
            sender_name = sender_name.split()[0]

            print("\n----------- NOVA MENSAGEM RECEBIDA -----------")
            print(f"De: {sender_name} ({clean_number})")
            print(f"Mensagem: {message_text}")
            print("----------------------------------------------")

            # Lógica da conversa
            if clean_number not in conversations:
                conversations[clean_number] = {}
            conversations[clean_number]['name'] = sender_name

            # Passo 2: Gerar a resposta da IA
            print("🤖 Processando com a Inteligência Artificial...")
            ai_reply = gerar_resposta_ia(clean_number, sender_name, message_text)
            print(f"🤖 Resposta gerada: {ai_reply}")

            # Passo 5: Enviar a mensagem final
            send_whatsapp_message(clean_number, ai_reply)
            
            # Salvar o histórico
            salvar_historico_conversa(clean_number, message_text, ai_reply)
            salvar_dados_conversas()

    except Exception as e:
        print(f"❌ Erro inesperado no webhook: {e}")

    return jsonify({"status": "success"}), 200

# --- EXECUÇÃO PRINCIPAL ---

if __name__ == '__main__':

    conversations = carregar_dados_conversas()

    if modelo_ia:
        print("\n=============================================")
        print("   CHATBOT WHATSAPP COM IA INICIADO")
        print("=============================================")
        print("Servidor aguardando mensagens no webhook...")
        
        # Inicia o servidor Flask para receber as mensagens
        # O log do Werkzeug (servidor Flask) é desativado para um console mais limpo
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
        app.run(host='0.0.0.0', port=5000)
    else:
        print("\n encerrando o programa devido a erros na inicialização.")
