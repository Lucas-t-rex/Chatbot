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
# ⚙️ CONFIGURAÇÕES (PREENCHA AQUI)
# ==============================================================================
CONFIG = {
    # --- EVOLUTION API ---
    "EVOLUTION_API_URL": "https://evolution-api-lucas.fly.dev", # Sua URL correta
    "EVOLUTION_API_KEY": "1234",
    "INSTANCE_NAME": "chatbot",
    
    # --- GOOGLE GEMINI AI ---
    "GEMINI_API_KEY": "AIzaSyB24rmQDo_NyAAH3Dtwzsd_CvzPbyX-kYo",
    "GEMINI_MODEL": "gemini-2.5-flash-lite",
    
    # --- INTERVENÇÃO HUMANA ---
    "RESPONSIBLE_NUMBER": "554898389781", 
    
    # --- ARQUIVO ---
    "ARQUIVO_ALVO": "lista.xlsx",

    # --- TEMPOS ---
    "DELAY_ENTRE_MSG": (3, 6),
    "DELAY_ENTRE_CLIENTES": (15, 30)
}

# ==============================================================================
# 🚨 SISTEMA DE INTERVENÇÃO
# ==============================================================================
CLIENTES_EM_INTERVENCAO = set()
app = Flask(__name__)

# ==============================================================================
# 📡 SERVIDOR WEBHOOK (OUVIDO DO ROBÔ)
# ==============================================================================
@app.route('/webhook', methods=['POST'])
def receive_webhook():
    try:
        data = request.json
        if not data: return jsonify({"status": "no data"}), 200

        # Verifica se é mensagem recebida (upsert)
        event_type = data.get('event')
        if event_type != 'messages.upsert': return jsonify({"status": "ignored"}), 200

        msg_data = data.get('data', {})
        key = msg_data.get('key', {})
        from_me = key.get('fromMe', False)
        remote_jid = key.get('remoteJid', '')

        # Ignora mensagens enviadas pelo próprio bot ou grupos
        if from_me or '@g.us' in remote_jid: return jsonify({"status": "ignored"}), 200

        # Pega o número limpo
        clean_number = remote_jid.split('@')[0]
        
        # Se NÃO for o dono mandando mensagem e NÃO estiver já travado
        if clean_number != CONFIG["RESPONSIBLE_NUMBER"] and clean_number not in CLIENTES_EM_INTERVENCAO:
            print(f"\n🚨 [INTERVENÇÃO] Cliente {clean_number} respondeu! Travando bot.")
            CLIENTES_EM_INTERVENCAO.add(clean_number)
            
            # Avisa o dono
            msg_aviso = f"🔔 *INTERVENÇÃO HUMANA DETECTADA*\nO cliente {clean_number} respondeu sua campanha.\nO envio automático para ele foi pausado."
            sender_global.enviar_mensagem(CONFIG["RESPONSIBLE_NUMBER"], msg_aviso, delay_digitacao=0)

        return jsonify({"status": "processed"}), 200

    except Exception as e:
        print(f"❌ Erro no Webhook: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/', methods=['GET'])
def health():
    return "Disparador Online", 200

# ==============================================================================
# 🧠 CÉREBRO DA IA
# ==============================================================================
class GeradorMensagemIA:
    def __init__(self):
        try:
            genai.configure(api_key=CONFIG["GEMINI_API_KEY"])
            self.model = genai.GenerativeModel(CONFIG["GEMINI_MODEL"])
            print("✅ Inteligência Artificial (Gemini) Conectada!")
        except Exception as e:
            print(f"❌ Erro IA: {e}")
            # Não damos exit aqui para não derrubar o servidor Flask no Fly
            self.model = None 

    def gerar_sequencia_campanha(self, nome: str, empresa: str) -> List[str]:
        if not self.model: return self._fallback(nome)

        nome_str = nome if nome else ""
        empresa_str = empresa if empresa else ""
        
        prompt = f"""
        Aja como o Lucas da "Grupar Auto Peças".
        Crie uma sequência de 3 mensagens curtas de WhatsApp.
        DADOS: Nome: {nome_str} | Empresa: {empresa_str}
        
        ESTRUTURA (Use ||| para separar):
        1: Saudação casual + Identificação.
        2: Comente que notou que faz um tempo que ele não pede nada.
        3: Fale das férias coletivas e pergunte se quer ver promoções.
        
        REGRAS: NÃO use "parceiro". NÃO use aspas.
        """
        try:
            response = self.model.generate_content(prompt)
            texto_full = response.text.strip().replace('"', '')
            mensagens = [m.strip() for m in texto_full.split('|||') if m.strip()]
            if len(mensagens) < 2: raise Exception("Formato inválido")
            return mensagens
        except Exception as e:
            print(f"   ⚠️ Erro IA: {e}")
            return self._fallback(nome_str)

    def _fallback(self, nome):
        saudacao = f"Oi {nome}, aqui é o Lucas da Grupar auto peças tudo certo?" if nome else "Oi, aqui é o Lucas da Grupar auto peças tudo certo?"
        return [
            saudacao,
            "Eu notei que ja faz um tempo que nao pede nada,",
            "e como vamos entrar de ferias agora to com umas promoçoes bem bacana , voce quer ver ?"
        ]

# ==============================================================================
# 🛠️ DISPARADOR (Evolution API)
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

        if clean_number in CLIENTES_EM_INTERVENCAO and clean_number != CONFIG["RESPONSIBLE_NUMBER"]:
            print(f"      ⛔ Bloqueado: Cliente {clean_number} está em Intervenção.")
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

# ==============================================================================
# 📂 PROCESSADOR
# ==============================================================================
class ProcessadorLista:
    def __init__(self, caminho_arquivo: str):
        self.caminho_arquivo = caminho_arquivo

    def carregar_dados(self):
        if not os.path.exists(self.caminho_arquivo):
            print(f"❌ Arquivo '{self.caminho_arquivo}' não encontrado.")
            return pd.DataFrame() # Retorna vazio se não achar
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
# 🧵 LOOP DE DISPARO
# ==============================================================================
def loop_disparo():
    print("⏳ Aguardando servidor iniciar (5s)...")
    time.sleep(5) 
    print("\n🤖 DISPARADOR INICIADO NO FLY.IO")
    print("=" * 60)

    leitor = ProcessadorLista(CONFIG["ARQUIVO_ALVO"])
    cerebro = GeradorMensagemIA()
    
    df = leitor.carregar_dados()
    
    if df.empty:
        print("⚠️ Nenhuma lista encontrada ou lista vazia. Aguardando...")
        return

    for col in ['nome', 'empresa', 'telefone']:
        if col not in df.columns: df[col] = ""

    total = len(df)
    print(f"📋 Lista: {total} contatos. Iniciando...")

    for index, row in df.iterrows():
        nome = str(row.get('nome', '')).strip()
        empresa = str(row.get('empresa', '')).strip()
        telefone = str(row.get('telefone', '')).strip()

        if not telefone: continue

        clean_tel = sender_global.limpar_telefone(telefone)
        if clean_tel in CLIENTES_EM_INTERVENCAO:
            print(f"🔹 [{index + 1}/{total}] Pular {nome}: Já está em intervenção.")
            continue

        print(f"🔹 [{index + 1}/{total}] IA gerando para: {nome or 'Sem Nome'}...")

        # 1. IA GERA TUDO
        fila_mensagens = cerebro.gerar_sequencia_campanha(nome, empresa)

        # 2. DISPARO
        for i, msg in enumerate(fila_mensagens):
            if clean_tel in CLIENTES_EM_INTERVENCAO:
                print(f"      🛑 PARE! Cliente respondeu. Abortando.")
                break

            enviou = sender_global.enviar_mensagem(telefone, msg)
            if not enviou: break
            
            if i < len(fila_mensagens) - 1:
                tempo = random.randint(CONFIG["DELAY_ENTRE_MSG"][0], CONFIG["DELAY_ENTRE_MSG"][1])
                time.sleep(tempo)

        delay_cliente = random.randint(CONFIG["DELAY_ENTRE_CLIENTES"][0], CONFIG["DELAY_ENTRE_CLIENTES"][1])
        print(f"   ⏳ Cliente finalizado. Aguardando {delay_cliente}s...\n")
        time.sleep(delay_cliente)

    print("=" * 60)
    print("🏁 FIM DA LISTA.")

# ==============================================================================
# 🚀 MAIN (ADAPTADO PARA FLY.IO)
# ==============================================================================
if __name__ == "__main__":
    # Inicia a Thread do Disparador
    thread_disparo = threading.Thread(target=loop_disparo)
    thread_disparo.daemon = True 
    thread_disparo.start()

    # Inicia servidor na porta correta do Fly
    port = int(os.environ.get("PORT", 8080))
    # 0.0.0.0 é obrigatório para o Fly te enxergar
    app.run(host='0.0.0.0', port=port)