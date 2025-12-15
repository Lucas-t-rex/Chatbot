import pandas as pd
import requests
import time
import random
import re
import os
import sys
import threading
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
    
    # --- CONFIGURAÇÕES DE NEGÓCIO ---
    "RESPONSIBLE_NUMBER": "554498716404", 
    "ARQUIVO_ALVO": "lista.xlsx",
    
    # --- TEMPOS (HUMANIZAÇÃO) ---
    "TEMPO_DIGITANDO": 5000,      # 5 Segundos de "digitando..." (Balaozinho)
    "DELAY_ENTRE_MSG": (4, 7),    # Tempo de pausa entre uma mensagem e outra da sequência
    "DELAY_ENTRE_CLIENTES": (80, 120) # Tempo de descanso entre clientes
}

# ==============================================================================
# 🚨 MEMÓRIA DE INTERVENÇÃO (VOLÁTIL)
# ==============================================================================
CLIENTES_EM_INTERVENCAO = set()

app = Flask(__name__)

# ==============================================================================
# 📡 SERVIDOR WEBHOOK (INTERVENÇÃO)
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
        
        # --- LÓGICA DE EXTRAÇÃO DE NÚMERO (CAÇA AO NÚMERO REAL) ---
        raw_number = key.get('senderPn') or key.get('participant') or key.get('remoteJid')
        
        if not raw_number: return jsonify({"status": "no_number"}), 200

        # Ignora mensagens do próprio bot ou grupos
        if from_me or '@g.us' in raw_number: return jsonify({"status": "ignored"}), 200

        # Limpeza final
        clean_number = raw_number.split('@')[0].split(':')[0]
        
        # TRAVAMENTO
        if clean_number != CONFIG["RESPONSIBLE_NUMBER"] and clean_number not in CLIENTES_EM_INTERVENCAO:
            print(f"\n🚨 [INTERVENÇÃO] Cliente {clean_number} respondeu! Pausando campanha.")
            
            CLIENTES_EM_INTERVENCAO.add(clean_number)
            
            msg_aviso = (
                f"🔔 *INTERVENÇÃO HUMANA*\n"
                f"O número *{clean_number}* respondeu.\n"
                f"⏸️ Robô pausado para ele."
            )
            sender_global.enviar_mensagem(CONFIG["RESPONSIBLE_NUMBER"], msg_aviso, delay_digitacao=0)

        return jsonify({"status": "processed"}), 200

    except Exception as e:
        print(f"❌ Erro no Webhook: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/', methods=['GET'])
def health():
    return "Disparador Manual Online", 200

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

    def enviar_mensagem(self, numero: str, mensagem: str, delay_digitacao=None) -> bool:
        clean_number = self.limpar_telefone(numero)
        if not clean_number: return False

        # Verifica Intervenção
        if clean_number in CLIENTES_EM_INTERVENCAO and clean_number != CONFIG["RESPONSIBLE_NUMBER"]:
            print(f"      ⛔ [BLOQUEADO] Cliente {clean_number} em intervenção.")
            return False

        # Se não passar delay específico, usa o padrão de 5s da config
        if delay_digitacao is None:
            delay_digitacao = CONFIG["TEMPO_DIGITANDO"]

        api_path = f"/message/sendText/{self.instance}"
        final_url = self.base_url if self.base_url.endswith(api_path) else \
                    (self.base_url[:-1] + api_path if self.base_url.endswith('/') else self.base_url + api_path)

        payload = {
            "number": clean_number, 
            "textMessage": {"text": mensagem},
            "options": {
                "delay": delay_digitacao,   # <--- AQUI ESTÁ O SEGREDO DO 5 SEGUNDOS
                "presence": "composing",    # <--- ISSO FAZ APARECER O "DIGITANDO..."
                "linkPreview": True
            }
        }

        try:
            response = requests.post(final_url, json=payload, headers=self.headers, timeout=25) # Timeout maior pq o delay é longo
            if response.status_code < 400:
                print(f"      ✅ Enviado: \"{mensagem[:30]}...")
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
# 🧵 LOOP PRINCIPAL (AGORA COM MENSAGENS FIXAS)
# ==============================================================================
def loop_disparo():
    print("⏳ Aguardando servidor iniciar (10s)...")
    time.sleep(10)
    
    print("\n🤖 DISPARADOR PROGRAMADO (SEM IA)")
    print(f"🕒 Tempo de Digitação Configurado: {CONFIG['TEMPO_DIGITANDO']}ms")
    print("=" * 60)

    leitor = ProcessadorLista(CONFIG["ARQUIVO_ALVO"])
    df = leitor.carregar_dados()
    
    if df.empty:
        print("⚠️ Nenhuma lista encontrada.")
        return

    for col in ['nome', 'empresa', 'telefone']:
        if col not in df.columns: df[col] = ""

    total = len(df)
    print(f"📋 Lista Carregada: {total} contatos. Iniciando...")

    for index, row in df.iterrows():
        # --- VERIFICAÇÃO INICIAL ---
        telefone = str(row.get('telefone', '')).strip()
        if not telefone: continue
        
        clean_tel = sender_global.limpar_telefone(telefone)
        if clean_tel in CLIENTES_EM_INTERVENCAO:
            print(f"🔹 [{index + 1}/{total}] Pular {clean_tel}: Já está em intervenção.")
            continue

        nome_raw = str(row.get('nome', '')).strip()
        
        # --- LÓGICA DE NOME (PROGRAMADA) ---
        if nome_raw:
            # Pega o primeiro nome e deixa a primeira letra maiúscula
            primeiro_nome = nome_raw.split()[0].title() 
            msg1 = f"Boooom diiiaa, {primeiro_nome}! Beleza?.\nFalamos uns dias atrás sobre sua frota, lembra?"
        else:
            msg1 = "Boooom diiiaa! Beleza?."

        # --- SEQUÊNCIA FIXA ---
        msg2 = "A Grupar entra de férias dia 18 e tem condição especial pra clientes que estão inativos!"
        msg3 = "Quer que eu te mande o que dá pra aproveitar antes das férias?"

        fila_mensagens = [msg1, msg2, msg3]

        print(f"🔹 [{index + 1}/{total}] Enviando para: {nome_raw or 'Sem Nome'}...")

        # 2. DISPARO EM CASCATA
        for i, msg in enumerate(fila_mensagens):
            # Checagem de intervenção no meio
            if clean_tel in CLIENTES_EM_INTERVENCAO:
                print(f"      🛑 PARE! Cliente {clean_tel} respondeu. Abortando sequência.")
                break 

            enviou = sender_global.enviar_mensagem(telefone, msg)
            if not enviou: break
            
            # Pausa entre mensagens (simulando pensar na próxima)
            if i < len(fila_mensagens) - 1:
                tempo = random.randint(CONFIG["DELAY_ENTRE_MSG"][0], CONFIG["DELAY_ENTRE_MSG"][1])
                time.sleep(tempo)

        # Delay entre clientes
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