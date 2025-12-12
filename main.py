import google.generativeai as genai
import requests
import os
import sys
import threading
import time
from flask import Flask, request, jsonify

# ==============================================================================
# ⚙️ CONFIGURAÇÕES SEGURAS
# ==============================================================================
# Dados fornecidos por você
RESPONSIBLE_NUMBER = "554898389781"

# --- MUDANÇA AQUI: PEGAR DO AMBIENTE (SEGREDO) ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Sua API no Fly.io
EVOLUTION_API_URL = "https://evolution-api-lucas.fly.dev"
EVOLUTION_API_KEY = "1234"
INSTANCE_NAME = "chatbot"

# Verificação de segurança
if not GEMINI_API_KEY:
    print("❌ ERRO CRÍTICO: A chave GEMINI_API_KEY não foi configurada nos Secrets do Fly!", flush=True)
else:
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
CONFIGURAÇÕES:
    REGRA: Você deve sempre utilizar as funções no exato momento em que detectar, não deve fazer mais nada a nao ser chamar a tool.
    1. Sempre que notar que um cliente falar sobre valores, cotações, promoções chame a função ´fn_solicitar_intervencao´ IMEDIATAMENTE.

PERSONALIDADE: 
    REGRA:
        ROLE: Atendente, Vendedor e Especialista em peças de caminhão linha pesada. Você trabalha na empresa citada abaixo!
        NOME: Vitor (apelido Vitão). (Você deve encarnar um humano, nunca assuma que é um robô/IA).
        TOM: Humano, Educado, Profissional, Objetivo e Empático. Você deve tratar a pessoa como alguém da família — de forma acolhedora, próxima e natural, mas sem ser forçado.
        
        [REGRAS VISUAIS E DE ESTILO]
            1. QUEBRA DE LINHA: Pule uma linha entre sua reação e a próxima pergunta.
            2. EFEITO CAMALEÃO: Adapte o tom ao cliente (sério → formal; brincalhão → leve) e responda a piadas com “kkkkk” de forma natural.
            3. ANTI-REPETIÇÃO: Não fale o nome varias vezes na converssa, apenas em perguntas.
            4. ANTI-VALIDAÇÃO: Não use validações ("Entendi", "Interessante", "Compreendo") o cliente nao precisa saber que voce entendeu.
            4. NOME: Use no MÁXIMO 1 vez a cada 5 mensagens.
            5. EMOJIS: Máximo 1 a cada 3 mensagens inteiras. Use com moderação e sentido.
            6. DIREÇÃO: Sempre termine com PERGUNTA ou CTA (exceto despedidas).
            7. PING-PONG: Fale menos, ouça mais. Proibido perguntas duplas.
                ESCUTA GENUÍNA: 
                    Preste atenção no que o cliente disse, sempre tenha um interesse genuino sobre ele. 
                    Busque fazer amizade.
                    Não seja chato.
                DIREÇÃO DA CONVERSA: 
                    Não explique "para entender suas necessidades". O cliente não quer ser analisado, ele quer ser ajudado.
                    Faça UMA pergunta, espere a resposta, COMENTE a resposta dele com interesse genuíno, e só depois avance.
                    Não tenha pressa. Se o cliente quiser conversar, converse.
                    Não se explique ou explique uma ação ou pensamento pro cliente, você é humano. 
EMPRESA: 
    REGRA: Nunca invente informações desta empresa, se alguem perguntar algo sobre a empresa e não estiver aqui em baixo diga que não sabe.
        Informações:
            Empresa: Grupar
            Razão social: Parise Comércio e Distribuição de Peças Automotivas LTDA.
            Fundação: 12/03/2019.
            Local: Maringá-PR — Av. Joaquim Duarte Moleirinho, 4304 - Jardim Cidade Monções (CEP 87060-350). 
            Site:gruparautopecas.com.br
            Sobre nós:Atua no comércio atacadista e varejista de autopeças para linha pesada (caminhões) e implementos: Volvo, Scania, Mercedes-Benz, Iveco, MAN, DAF, entre outras. 
            Seguimento: Acabamentos, filtros, eletrica, suspenção e acessorios.
            Catálogo: amplo (milhares de SKUs) e seções de lançamentos, ofertas e produtos em destaque; aceita cotações via site e WhatsApp. 
            Estrutura logística: matriz no Paraná e filial em Santa Catarina, ~2.500 m² de armazenagem e mais de 50.000 clientes atendidos em todo o Brasil.
        Planos e produtos:
            Seguimento: Acabamentos, filtros, eletrica, suspenção e acessorios.
            Formas de pagamento: Pix, Boleto, Dinheiro, Cartão.
            Despacho: Envio para todo Brasil, parcerias com transportadoras.
FLUXO:
    REGRA:
        Você pode converssar a vontade com o cliente e fazer amizade, 
        Sempre termine com uma pergunta.

"""

# Só inicia o modelo se tiver chave
model = None
if GEMINI_API_KEY:
    model = genai.GenerativeModel('gemini-2.5-flash-lite', tools=tools, system_instruction=SYSTEM_PROMPT)

# ==============================================================================
# 🗄️ MEMÓRIA & BUFFER (VOLÁTIL)
# ==============================================================================
memory = {} 
message_buffer = {}  # Armazena as mensagens temporárias
message_timers = {}  # Armazena os timers ativos

app = Flask(__name__)

# ==============================================================================
# 🛠️ FUNÇÕES AUXILIARES
# ==============================================================================
def log(msg):
    print(msg, flush=True)

def send_whatsapp_message(number, text, delay_extra=0):
    """Envia mensagem usando a estrutura estável"""
    url = f"{EVOLUTION_API_URL}/message/sendText/{INSTANCE_NAME}"
    
    # O delay aqui é o tempo que aparece "digitando..." no WhatsApp
    delay_digitando = 3000  # 3 segundos digitando para cada bloco
    
    payload = {
        "number": number,
        "textMessage": {"text": text},
        "options": {
            "delay": delay_digitando, 
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
        log(f"📤 [ENVIO] Enviado para {number}: {text[:30]}...")
    except Exception as e:
        log(f"❌ [ERRO] Falha envio: {e}")

# ==============================================================================
# 🧠 LÓGICA DE PROCESSAMENTO (THREAD)
# ==============================================================================
def processar_mensagem_ia(clean_number):
    """
    Função executada após o tempo de buffer (8s) acabar.
    Ela processa o texto acumulado, chama a IA e envia a resposta em blocos.
    """
    try:
        # 1. Recupera todas as mensagens do buffer e junta
        if clean_number not in message_buffer or not message_buffer[clean_number]:
            return
            
        full_user_msg = " ".join(message_buffer[clean_number])
        del message_buffer[clean_number] # Limpa o buffer
        if clean_number in message_timers: del message_timers[clean_number]

        log(f"🧠 [IA INICIADA] Processando para {clean_number}: {full_user_msg}")

        # 2. Inicia Chat com IA
        if clean_number not in memory:
            memory[clean_number] = []

        chat = model.start_chat(history=memory[clean_number])
        response = chat.send_message(full_user_msg)
        
        # 3. Verifica Tool Call (Intervenção)
        tool_call = None
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.function_call:
                    tool_call = part.function_call
                    break
        
        if tool_call and tool_call.name == "fn_solicitar_intervencao":
            motivo = tool_call.args.get("motivo", "Não especificado")
            log(f"🚨 [INTERVENÇÃO] Cliente: {clean_number}")
            
            # Avisa Dono
            send_whatsapp_message(RESPONSIBLE_NUMBER, f"🚨 CHAMADO!\nNumero: {clean_number}\nMotivo: {motivo}")
            # Não envia nada pro cliente, pois o humano vai assumir (ou envia msg de espera se quiser)
            
        else:
            # 4. TRATAMENTO DE BLOCOS (PARÁGRAFOS)
            raw_text = response.text
            
            # Divide o texto onde houver quebra de linha
            # Remove linhas vazias ou apenas com espaço
            blocos = [b.strip() for b in raw_text.split('\n') if b.strip()]
            
            # Se a IA mandou tudo junto, vira um bloco só
            if not blocos: 
                blocos = [raw_text]

            # 5. ENVIO SEQUENCIAL COM PAUSA
            for i, bloco in enumerate(blocos):
                send_whatsapp_message(clean_number, bloco)
                
                # Salva no histórico (parte por parte)
                memory[clean_number].append({'role': 'model', 'parts': [bloco]})
                
                # Se ainda tiver blocos para enviar, espera 4 segundos
                if i < len(blocos) - 1:
                    log(f"⏳ [PAUSA] Esperando 4s para enviar o próximo bloco...")
                    time.sleep(4) 

            # Salva a mensagem do usuário no histórico no final
            memory[clean_number].append({'role': 'user', 'parts': [full_user_msg]})

    except Exception as e:
        log(f"❌ [ERRO PROCESSAMENTO] {e}")


# ==============================================================================
# 📡 ROTA PRINCIPAL (WEBHOOK)
# ==============================================================================
@app.route('/', methods=['GET'])
def health():
    return "Bot Online e Protegido", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    # Proteção: Se não tiver chave, nem tenta processar
    if not model:
        log("❌ [ERRO] Tentativa de uso sem chave de API configurada.")
        return jsonify({"status": "error_no_key"}), 200

    try:
        data = request.json
        if not data: return jsonify({"status": "no data"}), 200

        # Filtro de Evento
        if data.get('event') != 'messages.upsert':
            return jsonify({"status": "ignored"}), 200

        msg_data = data.get('data', {})
        key = msg_data.get('key', {})
        
        # Filtro de Origem
        if key.get('fromMe') or 'g.us' in key.get('remoteJid', ''):
            return jsonify({"status": "ignored"}), 200

        remote_jid = key.get('remoteJid')
        clean_number = remote_jid.split('@')[0]
        
        # Extração de Texto
        user_msg = msg_data.get('message', {}).get('conversation') or \
                   msg_data.get('message', {}).get('extendedTextMessage', {}).get('text')

        if not user_msg:
            return jsonify({"status": "no_text"}), 200

        log(f"📩 [BUFFER] Recebido de {clean_number}: {user_msg}")

        # --- LÓGICA DE BUFFER (ESPERA 8 SEGUNDOS) ---
        
        # 1. Adiciona mensagem na lista temporária
        if clean_number not in message_buffer:
            message_buffer[clean_number] = []
        message_buffer[clean_number].append(user_msg)
        
        # 2. Se já tinha um timer rodando, cancela (o cliente digitou mais coisa)
        if clean_number in message_timers:
            message_timers[clean_number].cancel()
            
        # 3. Cria um novo timer de 8 segundos
        # Se passar 8s sem novas mensagens, ele roda a função 'processar_mensagem_ia'
        timer = threading.Timer(8.0, processar_mensagem_ia, args=[clean_number])
        timer.start()
        message_timers[clean_number] = timer

        # Retorna OK na hora para a Evolution não travar
        return jsonify({"status": "buffered"}), 200

    except Exception as e:
        log(f"❌ [ERRO GERAL] {e}")
        return jsonify({"status": "error"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080)) 
    app.run(host='0.0.0.0', port=port)