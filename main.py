import google.generativeai as genai
import requests
import os
from flask import Flask, request, jsonify

# ==============================================================================
# ⚙️ CONFIGURAÇÕES
# ==============================================================================
# Dados fornecidos por você
RESPONSIBLE_NUMBER = "554898389781"
GEMINI_API_KEY = "AIzaSyB24rmQDo_NyAAH3Dtwzsd_CvzPbyX-kYo"

# Sua API no Fly.io
EVOLUTION_API_URL = "https://evolution-api-lucas.fly.dev"
EVOLUTION_API_KEY = "1234"
INSTANCE_NAME = "chatbot"

# Configuração da IA
genai.configure(api_key=GEMINI_API_KEY)

# ==============================================================================
# 🧠 CÉREBRO DA IA (FERRAMENTAS & PROMPT)
# ==============================================================================
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
Seja educado, breve e profissional.
Seu objetivo é conversar com o cliente.
SE E SOMENTE SE o cliente pedir para falar com o dono, humano ou suporte, CHAME a função `fn_solicitar_intervencao`.
Não invente números de telefone.
"""

model = genai.GenerativeModel('gemini-2.5-flash-lite', tools=tools, system_instruction=SYSTEM_PROMPT)

# Memória Simples (RAM)
memory = {} 

app = Flask(__name__)

# ==============================================================================
# 🛠️ FUNÇÕES AUXILIARES
# ==============================================================================
def log(msg):
    print(msg, flush=True)

def send_whatsapp_message(number, text):
    """Envia mensagem usando a estrutura estável do seu código de exemplo"""
    url = f"{EVOLUTION_API_URL}/message/sendText/{INSTANCE_NAME}"
    
    payload = {
        "number": number,
        "textMessage": {"text": text},
        "options": {
            "delay": 1200, 
            "presence": "composing", 
            "linkPreview": True
        }
    }
    headers = {
        "apikey": EVOLUTION_API_KEY,
        "Content-Type": "application/json"
    }
    
    try:
        # Timeout curto para não travar o servidor se a API demorar
        requests.post(url, json=payload, headers=headers, timeout=10)
        log(f"📤 [ENVIO] Enviado para {number}: {text}")
    except Exception as e:
        log(f"❌ [ERRO] Falha envio: {e}")

# ==============================================================================
# 📡 ROTA PRINCIPAL (WEBHOOK)
# ==============================================================================
@app.route('/', methods=['GET'])
def health():
    return "Bot Online e Estável", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    # 1. RESPOSTA RÁPIDA (CRÍTICO PARA NÃO CAIR A CONEXÃO)
    # O Flask precisa responder 200 OK imediatamente para a Evolution não achar que travou.
    # Por isso, vamos processar os dados mas garantir o retorno rápido.
    
    try:
        data = request.json
        if not data: return jsonify({"status": "no data"}), 200

        # Filtro de Evento (Só processa mensagens novas)
        if data.get('event') != 'messages.upsert':
            return jsonify({"status": "ignored"}), 200

        msg_data = data.get('data', {})
        key = msg_data.get('key', {})
        
        # Filtro de Origem (Ignora eu mesmo e grupos)
        if key.get('fromMe') or 'g.us' in key.get('remoteJid', ''):
            return jsonify({"status": "ignored"}), 200

        remote_jid = key.get('remoteJid')
        clean_number = remote_jid.split('@')[0]
        
        # Extração de Texto Segura
        user_msg = msg_data.get('message', {}).get('conversation') or \
                   msg_data.get('message', {}).get('extendedTextMessage', {}).get('text')

        if not user_msg:
            return jsonify({"status": "no_text"}), 200

        log(f"📩 [RECEBIDO] De: {clean_number} | Msg: {user_msg}")

        # --- PROCESSAMENTO DA IA ---
        # (Aqui está a lógica simplificada para não sobrecarregar)
        if clean_number not in memory:
            memory[clean_number] = []

        chat = model.start_chat(history=memory[clean_number])
        response = chat.send_message(user_msg)
        
        # Verifica Tool Call
        tool_call = None
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.function_call:
                    tool_call = part.function_call
                    break
        
        reply_text = ""
        
        if tool_call and tool_call.name == "fn_solicitar_intervencao":
            motivo = tool_call.args.get("motivo", "Não especificado")
            log(f"🚨 [INTERVENÇÃO] Cliente: {clean_number}")
            
            # Avisa Dono
            send_whatsapp_message(RESPONSIBLE_NUMBER, f"🚨 CHAMADO!\nCli: {clean_number}\nMotivo: {motivo}")
            
            # Avisa Cliente
            reply_text = "Entendi. Já chamei o responsável e ele vai entrar em contato com você em breve!"
        else:
            # Resposta IA Normal
            reply_text = response.text

        # Envia a resposta
        send_whatsapp_message(clean_number, reply_text)
        
        # Salva na memória
        memory[clean_number].append({'role': 'user', 'parts': [user_msg]})
        memory[clean_number].append({'role': 'model', 'parts': [reply_text]})

        return jsonify({"status": "processed"}), 200

    except Exception as e:
        log(f"❌ [ERRO GERAL] {e}")
        return jsonify({"status": "error"}), 500

if __name__ == '__main__':
    # Porta 8080 é padrão do Fly/Koyeb geralmente, mas deixei configurável
    port = int(os.environ.get("PORT", 8080)) 
    app.run(host='0.0.0.0', port=port)