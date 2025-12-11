import google.generativeai as genai
import requests
import sys
from flask import Flask, request, jsonify

# --- CONFIGURAÇÕES ---
RESPONSIBLE_NUMBER = "554898389781"
GEMINI_API_KEY = "AIzaSyB24rmQDo_NyAAH3Dtwzsd_CvzPbyX-kYo"

# URL da sua API no Fly.io
EVOLUTION_API_URL = "https://evolution-api-lucas.fly.dev" 
EVOLUTION_API_KEY = "1234"

# Configuração da IA
genai.configure(api_key=GEMINI_API_KEY)

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

SYSTEM_PROMPT = """
Você é um assistente virtual de uma empresa.
Sempre que alguem falar de cabelo voce deve dizer "eu sou carequinha kkkk"
Seja educado, breve e profissional.
Seu objetivo é conversar com o cliente.
SE E SOMENTE SE o cliente pedir para falar com o dono, humano ou suporte, CHAME a função `fn_solicitar_intervencao`.
Não invente números de telefone.
"""

model = genai.GenerativeModel('gemini-2.5-flash-lite', tools=tools, system_instruction=SYSTEM_PROMPT)

memory = {} 

app = Flask(__name__)

# Função para forçar o log aparecer na hora (sem delay)
def log(msg):
    print(msg, flush=True)

@app.route('/', methods=['GET'])
def health_check():
    return "Bot está rodando!", 200

def send_whatsapp_message(number, text):
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
        log(f"📤 [ENVIO] Tentando enviar para {number}...")
        requests.post(url, json=payload, headers=headers)
        log(f"✅ [SUCESSO] Mensagem enviada para {number}: {text}")
    except Exception as e:
        log(f"❌ [ERRO] Falha ao enviar WhatsApp: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    # LOG DE ENTRADA: Prova que a Evolution bateu na porta
    log("⚡ [WEBHOOK] Recebi um chamado!")

    data = request.json
    
    if data.get('event') != 'messages.upsert':
        return jsonify({"status": "ignored"}), 200
        
    msg_data = data.get('data', {})
    key = msg_data.get('key', {})
    
    if key.get('fromMe') or 'g.us' in key.get('remoteJid', ''):
        return jsonify({"status": "ignored"}), 200

    remote_jid = key.get('remoteJid')
    clean_number = remote_jid.split('@')[0]
    
    user_msg = msg_data.get('message', {}).get('conversation') or \
               msg_data.get('message', {}).get('extendedTextMessage', {}).get('text')

    if not user_msg:
        log("⚠️ [WEBHOOK] Mensagem sem texto recebida.")
        return jsonify({"status": "no_text"}), 200

    log(f"📩 [MENSAGEM] De: {clean_number} | Diz: {user_msg}")

    try:
        if clean_number not in memory:
            memory[clean_number] = []
        
        chat = model.start_chat(history=memory[clean_number])
        response = chat.send_message(user_msg)
        
        tool_call = None
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.function_call:
                    tool_call = part.function_call
                    break
        
        if tool_call and tool_call.name == "fn_solicitar_intervencao":
            motivo = tool_call.args.get("motivo", "Não especificado")
            log(f"🚨 [INTERVENÇÃO] Solicitada por {clean_number}. Motivo: {motivo}")
            
            msg_dono = f"🚨 INTERVENÇÃO SOLICITADA!\nCliente: {clean_number}\nMotivo: {motivo}"
            send_whatsapp_message(RESPONSIBLE_NUMBER, msg_dono)
            
            reply_text = "Entendi. Já chamei o responsável e ele vai entrar em contato com você em breve!"
            send_whatsapp_message(clean_number, reply_text)
            
            memory[clean_number].append({'role': 'user', 'parts': [user_msg]})
            memory[clean_number].append({'role': 'model', 'parts': [reply_text]})

        else:
            reply_text = response.text
            log(f"🤖 [IA] Resposta gerada: {reply_text}")
            send_whatsapp_message(clean_number, reply_text)
            
            memory[clean_number].append({'role': 'user', 'parts': [user_msg]})
            memory[clean_number].append({'role': 'model', 'parts': [reply_text]})

    except Exception as e:
        log(f"❌ [ERRO IA] Falha no Gemini: {e}")

    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)