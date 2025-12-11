import pandas as pd
import requests
import time
import random
import re
import os
import sys
import threading
import google.generativeai as genai
from flask import Flask, request, jsonify
from typing import Optional, List

# ==============================================================================
# ⚙️ CONFIGURAÇÕES
# ==============================================================================
CONFIG = {
    # --- EVOLUTION API ---
    "EVOLUTION_API_URL": "https://evolution-api-lucas.fly.dev",
    "EVOLUTION_API_KEY": "1234",
    "INSTANCE_NAME": "chatbot",
    
    # --- GOOGLE GEMINI AI ---
    "GEMINI_API_KEY": "AIzaSyB24rmQDo_NyAAH3Dtwzsd_CvzPbyX-kYo",
    "GEMINI_MODEL": "gemini-2.5-flash-lite",
    
    # --- CONFIGURAÇÕES DE NEGÓCIO ---
    "RESPONSIBLE_NUMBER": "554898389781", 
    "ARQUIVO_ALVO": "lista.xlsx",
    
    # --- TEMPOS (HUMANIZAÇÃO) ---
    "DELAY_ENTRE_MSG": (3, 6),
    "DELAY_ENTRE_CLIENTES": (15, 30)
}

# ==============================================================================
# 🚨 MEMÓRIA DE INTERVENÇÃO (VOLÁTIL)
# ==============================================================================
# Armazena APENAS os números que responderam. O resto continua recebendo.
CLIENTES_EM_INTERVENCAO = set()

app = Flask(__name__)

# ==============================================================================
# 📡 SERVIDOR WEBHOOK
# ==============================================================================
@app.route('/webhook', methods=['POST'])
def receive_webhook():
    try:
        data = request.json
        if not data: return jsonify({"status": "no data"}), 200

        event_type = data.get('event')
        if event_type != 'messages.upsert': return jsonify({"status": "ignored"}), 200

        msg_data = data.get('data', {})
        key = msg_data.get('key', {})
        from_me = key.get('fromMe', False)
        remote_jid = key.get('remoteJid', '')

        # Ignora mensagens do próprio bot ou grupos
        if from_me or '@g.us' in remote_jid: return jsonify({"status": "ignored"}), 200

        clean_number = remote_jid.split('@')[0]
        
        # LÓGICA DE TRAVAMENTO INDIVIDUAL
        # Se quem mandou mensagem NÃO é o dono e NÃO está travado ainda:
        if clean_number != CONFIG["RESPONSIBLE_NUMBER"] and clean_number not in CLIENTES_EM_INTERVENCAO:
            print(f"\n🚨 [INTERVENÇÃO] Cliente {clean_number} respondeu! Pausando campanha APENAS para ele.")
            
            # 1. Adiciona na lista negra temporária
            CLIENTES_EM_INTERVENCAO.add(clean_number)
            
            # 2. Avisa o Lucas
            nome_cliente = f"Cliente {clean_number}" # Poderia buscar na lista, mas aqui é rapidez
            msg_aviso = (
                f"🔔 *INTERVENÇÃO HUMANA*\n"
                f"O número *{clean_number}* respondeu à campanha.\n"
                f"⏸️ O robô foi pausado para este contato.\n"
                f"Pode assumir o atendimento!"
            )
            sender_global.enviar_mensagem(CONFIG["RESPONSIBLE_NUMBER"], msg_aviso, delay_digitacao=0)

        return jsonify({"status": "processed"}), 200

    except Exception as e:
        print(f"❌ Erro no Webhook: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/', methods=['GET'])
def health():
    return "Sniper Bot Online", 200

# ==============================================================================
# 🧠 CÉREBRO DA IA (MODO FORMATADOR RIGOROSO)
# ==============================================================================
class GeradorMensagemIA:
    def __init__(self):
        try:
            genai.configure(api_key=CONFIG["GEMINI_API_KEY"])
            self.model = genai.GenerativeModel(CONFIG["GEMINI_MODEL"])
            print("✅ Inteligência Artificial (Gemini) Conectada!")
        except Exception as e:
            print(f"❌ Erro IA: {e}")
            self.model = None 

    def gerar_sequencia_exata(self, nome: str, empresa: str) -> List[str]:
        if not self.model: return self._fallback(nome)

        nome_str = nome if nome else ""
        empresa_str = empresa if empresa else ""
        
        # PROMPT "GABARITO" - BLINDADO CONTRA ALUCINAÇÃO
        prompt = f"""
        Você é um formatador de texto estrito.
        Sua única função é inserir o NOME e a EMPRESA do cliente nos textos abaixo, se existirem.

        DADOS:
        - Nome Cliente: {nome_str}
        - Empresa Cliente: {empresa_str}

        TEXTOS OBRIGATÓRIOS (GABARITO):
        1. "Oi [NOME], é o Lucas da Grupar auto peças tudo certo?"
        2. "Eu notei que ja faz um tempo que nao pede nada,"
        3. "e como vamos entrar de ferias agora to com umas promoçoes bem bacana , voce quer ver ?"

        REGRAS DE OURO:
        - Se não tiver Nome, a frase 1 deve ser: "Oi, é o Lucas da Grupar auto peças tudo certo?"
        - NÃO adicione datas (nunca fale dia 03/01).
        - NÃO adicione saudações extras.
        - NÃO mude o texto das frases 2 e 3.
        - Retorne as 3 frases separadas EXATAMENTE por "|||".
        - Não use aspas.
        """

        try:
            response = self.model.generate_content(prompt)
            texto_full = response.text.strip().replace('"', '')
            mensagens = [m.strip() for m in texto_full.split('|||') if m.strip()]
            
            if len(mensagens) < 3: 
                raise Exception("IA não retornou as 3 mensagens obrigatórias.")
            
            return mensagens

        except Exception as e:
            print(f"   ⚠️ Erro IA: {e}")
            return self._fallback(nome_str)

    def _fallback(self, nome):
        # Plano B caso o Google caia
        saudacao = f"Oi {nome}, é o Lucas da Grupar auto peças tudo certo?" if nome else "Oi, é o Lucas da Grupar auto peças tudo certo?"
        return [
            saudacao,
            "Eu notei que ja faz um tempo que nao pede nada,",
            "e como vamos entrar de ferias agora to com umas promoçoes bem bacana , voce quer ver ?"
        ]

# ==============================================================================
# 🛠️ DISPARADOR
# ==============================================================================
class EvolutionSender:
    def __init__(self):
        self.base_url = CONFIG["EVOLUTION_API_URL"]
        self.api_key = CONFIG["EVOLUTION_API_KEY"]
        self.instance = CONFIG["INSTANCE_NAME"]
        self.headers = {"apikey": self.api_key, "Content-Type": "application/json"}

    def limpar_telefone(self, telefone: str) -> Optional[str]:
        if not telefone: return None
        nums = re.sub(r'\D', '', str(telefone))
        if len(nums) < 10: return None
        return nums

    def enviar_mensagem(self, numero: str, mensagem: str, delay_digitacao=1200) -> bool:
        clean_number = self.limpar_telefone(numero)
        if not clean_number: return False

        # --- AQUI ESTÁ A LÓGICA DE PAUSA INDIVIDUAL ---
        # Antes de enviar, verifica se ESTE número específico está na lista de intervenção
        if clean_number in CLIENTES_EM_INTERVENCAO and clean_number != CONFIG["RESPONSIBLE_NUMBER"]:
            print(f"      ⛔ [BLOQUEADO] O envio para {clean_number} foi cancelado (Intervenção Ativa).")
            return False

        api_path = f"/message/sendText/{self.instance}"
        final_url = self.base_url if self.base_url.endswith(api_path) else \
                    (self.base_url[:-1] + api_path if self.base_url.endswith('/') else self.base_url + api_path)

        payload = {
            "number": clean_number, 
            "textMessage": {"text": mensagem},
            "options": {"delay": delay_digitacao, "presence": "composing", "linkPreview": True}
        }

        try:
            response = requests.post(final_url, json=payload, headers=self.headers, timeout=20)
            if response.status_code < 400:
                print(f"      ✅ Enviado: \"{mensagem[:30]}...\"")
                return True
            else:
                print(f"      ❌ Falha API: {response.status_code}")
                return False
        except:
            return False

sender_global = EvolutionSender()

class ProcessadorLista:
    def __init__(self, caminho_arquivo: str):
        self.caminho_arquivo = caminho_arquivo

    def carregar_dados(self):
        if not os.path.exists(self.caminho_arquivo):
            print(f"❌ Arquivo '{self.caminho_arquivo}' não encontrado.")
            return pd.DataFrame()
        try:
            ext = os.path.splitext(self.caminho_arquivo)[1].lower()
            if ext == '.csv': df = pd.read_csv(self.caminho_arquivo, dtype=str, keep_default_na=False)
            else: df = pd.read_excel(self.caminho_arquivo, dtype=str, keep_default_na=False)
            df.columns = df.columns.str.strip().str.lower()
            return df
        except Exception as e:
            print(f"❌ Erro leitura: {e}")
            return pd.DataFrame()

# ==============================================================================
# 🧵 LOOP PRINCIPAL
# ==============================================================================
def loop_disparo():
    print("⏳ Aguardando servidor iniciar (10s)...")
    time.sleep(10)
    
    print("\n🤖 SNIPER BOT INICIADO (IA + GABARITO RIGOROSO)")
    print("=" * 60)

    leitor = ProcessadorLista(CONFIG["ARQUIVO_ALVO"])
    cerebro = GeradorMensagemIA()
    
    df = leitor.carregar_dados()
    
    if df.empty:
        print("⚠️ Nenhuma lista encontrada. O sistema ficará online aguardando deploy com lista.")
        return

    for col in ['nome', 'empresa', 'telefone']:
        if col not in df.columns: df[col] = ""

    total = len(df)
    print(f"📋 Lista Carregada: {total} contatos. Iniciando campanha...")

    for index, row in df.iterrows():
        # --- VERIFICAÇÃO INICIAL (Nível Cliente) ---
        telefone = str(row.get('telefone', '')).strip()
        if not telefone: continue
        
        clean_tel = sender_global.limpar_telefone(telefone)
        
        # Se o cliente já respondeu antes mesmo de começarmos a mandar, PULA ele.
        if clean_tel in CLIENTES_EM_INTERVENCAO:
            print(f"🔹 [{index + 1}/{total}] Pular {clean_tel}: Já está em intervenção.")
            continue

        nome = str(row.get('nome', '')).strip()
        empresa = str(row.get('empresa', '')).strip()

        print(f"🔹 [{index + 1}/{total}] Processando: {nome or 'Sem Nome'}...")

        # 1. GERA MENSAGENS COM BASE NO GABARITO
        fila_mensagens = cerebro.gerar_sequencia_exata(nome, empresa)

        # 2. DISPARO EM CASCATA
        for i, msg in enumerate(fila_mensagens):
            # --- VERIFICAÇÃO SECUNDÁRIA (Nível Mensagem) ---
            # Se ele respondeu no meio das mensagens (ex: depois do "Oi"), para TUDO pra ele.
            if clean_tel in CLIENTES_EM_INTERVENCAO:
                print(f"      🛑 PARE! Cliente {clean_tel} respondeu. Abortando sequência.")
                break # Sai do loop de mensagens e vai para o próximo cliente do Excel

            enviou = sender_global.enviar_mensagem(telefone, msg)
            if not enviou: break
            
            if i < len(fila_mensagens) - 1:
                tempo = random.randint(CONFIG["DELAY_ENTRE_MSG"][0], CONFIG["DELAY_ENTRE_MSG"][1])
                time.sleep(tempo)

        # Delay antes de ir para o próximo cliente da lista
        delay_cliente = random.randint(CONFIG["DELAY_ENTRE_CLIENTES"][0], CONFIG["DELAY_ENTRE_CLIENTES"][1])
        print(f"   ⏳ Aguardando {delay_cliente}s para o próximo cliente...\n")
        time.sleep(delay_cliente)

    print("=" * 60)
    print("🏁 LISTA FINALIZADA. O bot continua online ouvindo intervenções.")

# ==============================================================================
# 🚀 START
# ==============================================================================
if not os.environ.get("WERKZEUG_RUN_MAIN"):
    t = threading.Thread(target=loop_disparo)
    t.daemon = True
    t.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)