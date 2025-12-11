import google.generativeai as genai
import requests
from flask import Flask, request, jsonify

# --- CONFIGURAÇÕES ---
# Chaves fornecidas por você
RESPONSIBLE_NUMBER = "554898389781"
GEMINI_API_KEY = "AIzaSyAhaTl7JDg_vzEteDSAIJwvGAhYAE95F24"

# Configure aqui os dados da sua Evolution API (Instância 'chatbot')
EVOLUTION_API_URL = "https://evolution-api-lucas.fly.dev" # <--- SUBSTITUA PELO SEU URL
EVOLUTION_API_KEY = "1234"         # <--- SUBSTITUA PELA KEY GLOBAL

# Configuração da IA
genai.configure(api_key=GEMINI_API_KEY)

# Definição da ÚNICA ferramenta (Intervenção)
tools = [
    {
        "function_declarations": [
            {
                "name": "fn_solicitar_intervencao",
                "description": "Use esta função quando o cliente pedir para falar com o dono, humano ou suporte.",
                "parameters": {
                    "type_": "OBJECT",
                    "properties": {
                        "motivo": {"type_": "STRING", "description": "O motivo do chamado."}
                    },
                    "required": ["motivo"]
                }
            }
        ]
    }
]

# Prompt do Sistema Simplificado
SYSTEM_PROMPT = """
Você é um assistente virtual de uma empresa.
Sempre que alguem falar de cabelo voce deve dizer "eu sou carequinha kkkk"
Seja educado, breve e profissional.
Seu objetivo é conversar com o cliente.
SE E SOMENTE SE o cliente pedir para falar com o dono, humano ou suporte, CHAME a função `fn_solicitar_intervencao`.
Não invente números de telefone.
"""

# Inicializa o Modelo
model = genai.GenerativeModel('gemini-2.5-flash-lite', tools=tools, system_instruction=SYSTEM_PROMPT)

# Memória VOLÁTIL (apaga se reiniciar o código, pois não estamos usando Banco de Dados)
# Formato: { 'numero_whatsapp': [historico_chat] }
memory = {} 

app = Flask(__name__)

def send_whatsapp_message(number, text):
    """Envia mensagem de texto via Evolution API"""
    url = f"{EVOLUTION_API_URL}/message/sendText/chatbot"
    
    payload = {
        "number": number,
        "textMessage": {"text": text},
        "options": {"delay": 1200, "presence": "composing"}
    }
    headers = {
        "apikey": EVOLUTION_API_KEY,
        "Content-Type": "application/json"
    }
    
    try:
        requests.post(url, json=payload, headers=headers)
        print(f"📤 Enviado para {number}: {text}")
    except Exception as e:
        print(f"❌ Erro ao enviar WhatsApp: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    
    # Filtros básicos para não processar lixo
    if data.get('event') != 'messages.upsert':
        return jsonify({"status": "ignored"}), 200
        
    msg_data = data.get('data', {})
    key = msg_data.get('key', {})
    
    # Ignora mensagens do próprio bot ou de grupos
    if key.get('fromMe') or 'g.us' in key.get('remoteJid', ''):
        return jsonify({"status": "ignored"}), 200

    remote_jid = key.get('remoteJid')
    clean_number = remote_jid.split('@')[0]
    
    # Pega o texto da mensagem
    user_msg = msg_data.get('message', {}).get('conversation') or \
               msg_data.get('message', {}).get('extendedTextMessage', {}).get('text')

    if not user_msg:
        return jsonify({"status": "no_text"}), 200

    print(f"📩 Recebido de {clean_number}: {user_msg}")

    # --- LÓGICA DO GEMINI ---
    try:
        # Inicia ou recupera histórico da memória RAM
        if clean_number not in memory:
            memory[clean_number] = []
        
        chat = model.start_chat(history=memory[clean_number])
        response = chat.send_message(user_msg)
        
        # Verifica se a IA chamou a ferramenta (Intervenção)
        tool_call = None
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.function_call:
                    tool_call = part.function_call
                    break
        
        if tool_call and tool_call.name == "fn_solicitar_intervencao":
            # 1. Avisa o Dono
            motivo = tool_call.args.get("motivo", "Não especificado")
            msg_dono = f"🚨 INTERVENÇÃO SOLICITADA!\nCliente: {clean_number}\nMotivo: {motivo}"
            send_whatsapp_message(RESPONSIBLE_NUMBER, msg_dono)
            
            # 2. Responde ao cliente
            reply_text = "Entendi. Já chamei o responsável e ele vai entrar em contato com você em breve!"
            send_whatsapp_message(clean_number, reply_text)
            
            # Atualiza memória com a resposta
            memory[clean_number].append({'role': 'user', 'parts': [user_msg]})
            memory[clean_number].append({'role': 'model', 'parts': [reply_text]})

        else:
            # Resposta normal (texto)
            reply_text = response.text
            send_whatsapp_message(clean_number, reply_text)
            
            # Atualiza memória
            memory[clean_number].append({'role': 'user', 'parts': [user_msg]})
            memory[clean_number].append({'role': 'model', 'parts': [reply_text]})

    except Exception as e:
        print(f"❌ Erro na IA: {e}")

    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)