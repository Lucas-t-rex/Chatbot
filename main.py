import os
import sys
import pytz
import json
import time
import requests
import threading
from datetime import datetime
from pymongo import MongoClient
import google.generativeai as genai
from flask import Flask, request, jsonify


# ==============================================================================
# ⚙️ CONFIGURAÇÕES SEGURAS
# ==============================================================================
# Dados fornecidos por você
RESPONSIBLE_NUMBER = "554898389781"
FUSO_HORARIO = pytz.timezone('America/Sao_Paulo')
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI")
EVOLUTION_API_URL = "https://evolution-api-lucas.fly.dev"
EVOLUTION_API_KEY = "1234"
INSTANCE_NAME = "chatbot"
DB_NAME = "chatgrupar_db"

mongo_client = None
conversation_collection = None

try:
    if MONGO_URI:
        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client[DB_NAME]
        conversation_collection = db['conversations']
        print("✅ [MONGODB] Conexão com banco de dados estabelecida.", flush=True)
    else:
        print("⚠️ [MONGODB] Aviso: MONGO_URI não definida. O bot não salvará histórico.", flush=True)
except Exception as e:
    print(f"❌ [MONGODB] Erro crítico de conexão: {e}", flush=True)

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

def get_maringa_time():
    return datetime.now(FUSO_HORARIO)

def get_tempo_real():
    agora = datetime.now(FUSO_HORARIO)
    hora = agora.hour
    if 5 <= hora < 12:
        saudacao = "Bom dia"
    elif 12 <= hora < 18:
        saudacao = "Boa tarde"
    else:
        saudacao = "Boa noite"

    dias_semana = {0: "Segunda-feira", 1: "Terça-feira", 2: "Quarta-feira", 
                   3: "Quinta-feira", 4: "Sexta-feira", 5: "Sábado", 6: "Domingo"}
    meses = {1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 
             6: "Junho", 7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro"}

    return {
        "saudacao_real": saudacao,
        "dia_semana": dias_semana[agora.weekday()],
        "dia": agora.day,
        "mes_nome": meses[agora.month],
        "ano": agora.year,
        "hora_fmt": agora.strftime('%H:%M')
    }

def get_system_prompt():
    tempo = get_tempo_real() 

    prompt = f"""
        CONFIGURAÇÕES:
            REGRA: Você deve sempre utilizar as funções no exato momento em que detectar, não deve fazer mais nada a nao ser chamar a tool.
            1. Sempre que notar que um cliente falar sobre valores, cotações, promoções chame a função ´fn_solicitar_intervencao´ IMEDIATAMENTE.
            2. CONTEXTO TEMPORAL: 
                {tempo['dia_semana']}, {tempo['dia']} de {tempo['mes_nome']} às {tempo['hora_fmt']}.
                SAUDAÇÃO "{tempo['saudacao_real']}".
                Mesmo que o usuario erre (diga Bom dia à noite), ignore o erro dele e responda naturalmente usando APENAS "{tempo['saudacao_real']}".
        PERSONALIDADE: 
            REGRA:
                ROLE: Atendente, Vendedor e Especialista em peças de caminhão linha pesada. Você trabalha na empresa citada abaixo!
                NOME: Vitor (apelido Vitão). (Você deve encarnar um humano, nunca assuma que é um robô/IA).
                TOM: Humano, Educado, Profissional, Empático. Você deve tratar a pessoa como alguém da família — de forma acolhedora, próxima e natural, mas sem ser forçado. 
                VOCABULARIO: Usa o minimo do palavras possiveis pra se expressar. Usa risadas como kkkkk. Comprimenta como , "eai", "fla comigo", "tudo certo?" Usa abreviações de internet, como "vc", "qlq", "blz', "vlw".
                [REGRAS VISUAIS E DE ESTILO]
                    1. QUEBRA DE LINHA: Pule uma linha entre sua reação e a próxima pergunta.
                    2. EFEITO CAMALEÃO: Adapte o tom ao cliente (sério → formal; brincalhão → leve) e responda a piadas com “kkkkk” de forma natural.
                    3. ANTI-REPETIÇÃO: Não fale o nome varias vezes na converssa, apenas em perguntas.
                    4. ANTI-VALIDAÇÃO: Não use validações ("Entendi", "Interessante", "Compreendo") o cliente nao precisa saber que voce entendeu.
                    4. NOME: Use no MÁXIMO 1 vez a cada 5 mensagens.
                    5. NÃO EMOJIS: Nunca use emojis. Isso perde a humanização.
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
                    Seguimento: Acabamentos, filtros, eletrica, suspenção e acessoarios.
                    Catálogo: amplo (milhares de SKUs) e seções de lançamentos, ofertas e produtos em destaque; aceita cotações via site e WhatsApp. 
                    Estrutura logística: matriz no Paraná e filial em Santa Catarina, ~2.500 m² de armazenagem e mais de 50.000 clientes atendidos em todo o Brasil.
                Planos e produtos:
                    Seguimento: Acabamentos, filtros, eletrica, suspenção e acessorios.
                    Formas de pagamento: Pix, Boleto, Dinheiro, Cartão.
                    Despacho: Envio para todo Brasil, parcerias com transportadoras.
        FLUXO:
            REGRA:
                Você pode converssar a vontade com o cliente e fazer amizade,
                Demontre interesse genuino no cliente.
                Trate ele como ele te trata mas sem má educação.
                Sempre termine com uma pergunta.

"""
    return prompt

# Só inicia o modelo se tiver chave
model = None
if GEMINI_API_KEY:
    model = genai.GenerativeModel('gemini-2.0-flash', tools=tools, system_instruction=get_system_prompt())

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

def db_save_message(phone_number, role, text):
    """Salva mensagens de forma atômica no MongoDB."""
    if conversation_collection is None: return
    
    timestamp = get_maringa_time()
    msg_entry = {
        "role": role, # 'user' ou 'model'
        "text": text,
        "ts": timestamp.isoformat()
    }
    
    conversation_collection.update_one(
        {"_id": phone_number},
        {
            "$push": {"history": msg_entry},
            "$set": {"last_interaction": timestamp},
            "$setOnInsert": {"created_at": timestamp}
        },
        upsert=True
    )

def db_load_history(phone_number, limit=25):
    """Recupera o contexto histórico (últimas N mensagens)."""
    if conversation_collection is None: return []
    
    doc = conversation_collection.find_one({"_id": phone_number}, {"history": {"$slice": -limit}})
    if not doc: return []
    
    gemini_history = []
    for msg in doc.get("history", []):
        gemini_history.append({
            "role": msg.get("role"),
            "parts": [msg.get("text")]
        })
    return gemini_history

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

def executar_profiler_cliente(contact_id):
    """
    AGENTE PROFILER V3: Analisa o comportamento do cliente de autopeças.
    Roda em segundo plano para não gerar latência no chat.
    """
    if conversation_collection is None or not GEMINI_API_KEY:
        return

    try:
        # 1. Busca os dados atuais no MongoDB
        doc = conversation_collection.find_one({'_id': contact_id})
        if not doc: return

        history_completo = doc.get('history', [])
        perfil_atual = doc.get('client_profile', {})
        
        # --- LÓGICA DE CHECKPOINT (Economia de Tokens) ---
        ultimo_ts_lido = doc.get('profiler_last_ts', "2000-01-01T00:00:00")
        
        # Filtra apenas mensagens que ainda não foram processadas pelo Profiler
        mensagens_novas = [
            m for m in history_completo 
            if m.get('ts', '') > ultimo_ts_lido
        ]

        if not mensagens_novas:
            return

        novo_checkpoint_ts = mensagens_novas[-1].get('ts')

        # 2. Prepara o texto para a IA analisar
        txt_conversa_nova = ""
        for m in mensagens_novas:
            role = "Cliente" if m.get('role') == 'user' else "Vendedor(IA)"
            texto = m.get('text', '')
            # Ignora logs técnicos
            if not texto.startswith("Chamando função") and "[HUMAN" not in texto:
                txt_conversa_nova += f"- {role}: {texto}\n"
        
        if not txt_conversa_nova.strip():
            return

        # 3. Prompt Especializado para Autopeças (Diferente do Restaurante)
        prompt_profiler = f"""
        Você é um ANALISTA DE PERFIL de clientes
        Sua missão é atualizar o "Dossiê do Cliente" com base nas novas mensagens.

        PERFIL ATUAL: {json.dumps(perfil_atual, ensure_ascii=False)}
        NOVAS MENSAGENS: {txt_conversa_nova}

        CAMPOS PARA ATUALIZAR (JSON):
        {{
        "nome": "Nome do cliente ou empresa",
        "frota_caminhoes": "Marcas mencionadas (Volvo, Scania, etc)",
        "perfil_comportamental": "Ex: Decidido, busca preço, urgente, técnico",
        "principais_pecas_procuradas": "Ex: Filtros, suspensão, elétrica",
        "localidade": "Cidade ou região se mencionada",
        "nivel_de_relacionamento": "Novo, recorrente, frotista",
        "objecoes_comuns": "O que impede ele de fechar? (Frete, preço, prazo)",
        "observacoes_importantes": "Detalhes únicos para o vendedor humano saber"
        }}

        REGRAS: 
        - Retorne APENAS o JSON. 
        - Não invente dados.
        - Mantenha o que já existia se não houver informação nova.
        """

        # 4. Chamada ao Gemini (Configurado para JSON)
        model_profiler = genai.GenerativeModel('gemini-2.0-flash') 
        response = model_profiler.generate_content(prompt_profiler)
        
        # Limpeza simples para garantir que pegamos apenas o JSON (caso a IA mande ```json ...)
        json_text = response.text.replace('```json', '').replace('```', '').strip()
        novo_perfil_json = json.loads(json_text)

        # 5. Atualização Atômica no MongoDB
        conversation_collection.update_one(
            {'_id': contact_id},
            {
                '$set': {
                    'client_profile': novo_perfil_json,
                    'profiler_last_ts': novo_checkpoint_ts
                }
            }
        )
        print(f"🕵️ [Profiler] Dossiê de {contact_id} atualizado com sucesso.")

    except Exception as e:
        print(f"⚠️ Erro no Agente Profiler: {e}")

# ==============================================================================
# 🧠 LÓGICA DE PROCESSAMENTO (THREAD)
# ==============================================================================
def processar_mensagem_ia(clean_number):
    """
    Fluxo Profissional: Buffer -> Banco -> Contexto Temporal -> IA -> Banco
    """
    try:
        # 1. Validação do Buffer
        if clean_number not in message_buffer or not message_buffer[clean_number]: return
        
        full_user_msg = " ".join(message_buffer[clean_number])
        del message_buffer[clean_number]
        if clean_number in message_timers: del message_timers[clean_number]

        log(f"🧠 [PROCESSANDO] {clean_number}: {full_user_msg}")

        db_save_message(clean_number, "user", full_user_msg)

        history_context = db_load_history(clean_number, limit=25)
        
        prompt_completo = get_system_prompt()

        current_model = genai.GenerativeModel('gemini-2.0-flash', tools=tools, system_instruction=prompt_completo)
        
        chat = current_model.start_chat(history=history_context)
        response = chat.send_message(full_user_msg)
        
        tool_call = None
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.function_call:
                    tool_call = part.function_call
                    break
        
        if tool_call and tool_call.name == "fn_solicitar_intervencao":
            motivo = tool_call.args.get("motivo", "Geral")
            log(f"🚨 Intervenção: {motivo}")
            
            send_whatsapp_message(RESPONSIBLE_NUMBER, f"🚨 AJUDA VENDAS!\nCli: {clean_number}\nQuer: {motivo}")
            
            msg_bot = "Entendi! Vou chamar o Vitão aqui pra ver esse valor especial pra você. Só um minuto..."
            send_whatsapp_message(clean_number, msg_bot)
            db_save_message(clean_number, "model", msg_bot)

        else:
            # 6. Resposta Normal
            raw_text = response.text
            blocos = [b.strip() for b in raw_text.split('\n') if b.strip()]
            if not blocos: blocos = [raw_text]

            full_bot_text = ""
            for i, bloco in enumerate(blocos):
                send_whatsapp_message(clean_number, bloco)
                full_bot_text += bloco + " "
                if i < len(blocos) - 1: time.sleep(3)

            # Persistência (Bot)
            db_save_message(clean_number, "model", full_bot_text.strip())

            threading.Thread(target=executar_profiler_cliente, args=(clean_number,)).start()

    except Exception as e:
        log(f"❌ Erro Processamento: {e}")

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
    app.run(host='0.0.0.0', port=8000)