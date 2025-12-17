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
import base64
import mimetypes

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
    "DELAY_ENTRE_MSG": (7, 14),    # Tempo de pausa entre uma mensagem e outra da sequência
    "DELAY_ENTRE_CLIENTES": (300, 420) # Tempo de descanso entre clientes
}

# ==============================================================================
# 🚨 MEMÓRIA DE INTERVENÇÃO (VOLÁTIL)
# ==============================================================================
CLIENTES_EM_INTERVENCAO = set()
PAUSA_DO_SISTEMA = False

app = Flask(__name__)

# ==============================================================================
# 📡 SERVIDOR WEBHOOK (INTERVENÇÃO)
# ==============================================================================
@app.route('/webhook', methods=['POST'])
def receive_webhook():
    global PAUSA_DO_SISTEMA  # Variável global para controlar o pause

    try:
        data = request.json
        if not data: return jsonify({"status": "no data"}), 200

        event_type = data.get('event')
        if event_type != 'messages.upsert': return jsonify({"status": "ignored"}), 200

        msg_data = data.get('data', {})
        key = msg_data.get('key', {})
        from_me = key.get('fromMe', False)
        
        # --- LÓGICA DE EXTRAÇÃO DE NÚMERO ---
        raw_number = key.get('senderPn') or key.get('participant') or key.get('remoteJid')
        
        if not raw_number: return jsonify({"status": "no_number"}), 200

        # Limpeza final
        clean_number = raw_number.split('@')[0].split(':')[0]

        # --- 👑 COMANDOS DO ADMINISTRADOR (Seu Número) ---
        if clean_number == CONFIG["RESPONSIBLE_NUMBER"]:
            # Extrai o texto da mensagem com segurança
            content = msg_data.get('message', {})
            text_body = content.get('conversation') or content.get('extendedTextMessage', {}).get('text') or ""
            comando = text_body.strip().lower()

            if comando == "bot pause":
                PAUSA_DO_SISTEMA = True
                sender_global.enviar_mensagem(CONFIG["RESPONSIBLE_NUMBER"], "⏸️ *SISTEMA PAUSADO!* Envios interrompidos. Intervenções continuam ativas.", delay_digitacao=0)
                return jsonify({"status": "paused_command"}), 200
            
            elif comando == "bot play":
                PAUSA_DO_SISTEMA = False
                sender_global.enviar_mensagem(CONFIG["RESPONSIBLE_NUMBER"], "▶️ *SISTEMA RETOMADO!* Voltando a enviar a lista.", delay_digitacao=0)
                return jsonify({"status": "play_command"}), 200

        # Ignora mensagens do próprio bot ou grupos (se não for comando)
        if from_me or '@g.us' in raw_number: return jsonify({"status": "ignored"}), 200
        
        # --- TRAVAMENTO DE INTERVENÇÃO ---
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

    def tratar_erro_api(self, response):
        """🛡️ FREIO DE SEGURANÇA: Se a API cair, avisa admin e dorme."""
        
        # --- SE O ERRO FOR 500 (ERRO NO SERVIDOR) ---
        if response.status_code >= 500:
            print(f"      🚨 ERRO CRÍTICO API ({response.status_code}). Servidor instável.")

            # >>>> NOVA PARTE: AVISA O DONO <<<<
            try:
                print("      📣 Tentando enviar alerta para o número responsável...")
                aviso_url = f"{self.base_url}/message/sendText/{self.instance}"
                aviso_payload = {
                    "number": CONFIG["RESPONSIBLE_NUMBER"],
                    "textMessage": {"text": f"🚨 *ALERTA CRÍTICO DO BOT*\n\nA API retornou erro *{response.status_code}*.\nO sistema entrará em pausa de segurança por 2 minutos."},
                    "options": {"delay": 1000, "presence": "composing"}
                }
                # Fazemos um request direto aqui para não gerar loop infinito
                requests.post(aviso_url, json=aviso_payload, headers=self.headers, timeout=10)
            except Exception as e_aviso:
                print(f"      ❌ Falha ao tentar avisar o admin (A API deve estar muito ruim): {e_aviso}")
            # >>>> FIM DA NOVA PARTE <<<<

            print("      ⏳ Pausando por 120 segundos para evitar bloqueio...")
            time.sleep(120) # <--- AQUI ESTÁ A PROTEÇÃO
            return False

        elif response.status_code == 429:
            print("      ⚠️ Rate Limit. Esperando 30s...")
            time.sleep(30)
            return False
        else:
            print(f"      ❌ Falha API: {response.status_code}")
            return False

    def enviar_mensagem(self, numero: str, mensagem: str, delay_digitacao=None) -> bool:
        clean_number = self.limpar_telefone(numero)
        if not clean_number: return False

        if clean_number in CLIENTES_EM_INTERVENCAO and clean_number != CONFIG["RESPONSIBLE_NUMBER"]:
            print(f"      ⛔ [BLOQUEADO] Cliente {clean_number} em intervenção.")
            return False

        if delay_digitacao is None: delay_digitacao = CONFIG["TEMPO_DIGITANDO"]

        api_path = f"/message/sendText/{self.instance}"
        final_url = self.base_url if self.base_url.endswith(api_path) else \
                    (self.base_url[:-1] + api_path if self.base_url.endswith('/') else self.base_url + api_path)

        payload = {
            "number": clean_number, 
            "textMessage": {"text": mensagem},
            "options": {"delay": delay_digitacao, "presence": "composing", "linkPreview": True}
        }

        try:
            response = requests.post(final_url, json=payload, headers=self.headers, timeout=30)
            if response.status_code < 400:
                print(f"      ✅ Enviado Texto: \"{mensagem[:20]}...\"")
                return True
            else:
                return self.tratar_erro_api(response)
        except Exception as e:
            print(f"      ❌ Erro Conexão: {e}")
            time.sleep(10)
            return False

    def enviar_imagem_local(self, numero: str, caminho_imagem: str, caption: str = "") -> bool:
        clean_number = self.limpar_telefone(numero)
        if not clean_number: return False

        if not os.path.exists(caminho_imagem):
            print(f"      ❌ Erro: Imagem '{caminho_imagem}' não encontrada.")
            return False

        if clean_number in CLIENTES_EM_INTERVENCAO and clean_number != CONFIG["RESPONSIBLE_NUMBER"]:
            return False

        try:
            with open(caminho_imagem, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            
            mime_type, _ = mimetypes.guess_type(caminho_imagem)
            if not mime_type: mime_type = "image/jpeg"

            api_path = f"/message/sendMedia/{self.instance}"
            final_url = self.base_url if self.base_url.endswith(api_path) else \
                        (self.base_url[:-1] + api_path if self.base_url.endswith('/') else self.base_url + api_path)

            payload = {
                "number": clean_number,
                "mediaMessage": {"mediatype": "image", "caption": caption, "media": encoded_string},
                "options": {"delay": CONFIG["TEMPO_DIGITANDO"], "presence": "composing"}
            }
            
            response = requests.post(final_url, json=payload, headers=self.headers, timeout=90)
            
            if response.status_code < 400:
                print(f"      📸 Enviado Imagem: {os.path.basename(caminho_imagem)}")
                return True
            else:
                return self.tratar_erro_api(response)
        except Exception as e:
            print(f"      ❌ Erro processamento imagem: {e}")
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
# 🧵 LOOP PRINCIPAL (CORRIGIDO)
# ==============================================================================
def loop_disparo():
    print("⏳ Aguardando servidor iniciar (10s)...")
    time.sleep(10)
    
    print("\n🤖 DISPARADOR OTIMIZADO (Variação + Segurança)")
    print(f"🕒 Intervalo entre Clientes: {CONFIG['DELAY_ENTRE_CLIENTES'][0]}-{CONFIG['DELAY_ENTRE_CLIENTES'][1]}s")
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
        
        while PAUSA_DO_SISTEMA:
            print("💤 ... Sistema PAUSADO (Aguardando 'bot play') ...")
            time.sleep(10)

        telefone = str(row.get('telefone', '')).strip()
        if not telefone: continue
        
        clean_tel = sender_global.limpar_telefone(telefone)
        if clean_tel in CLIENTES_EM_INTERVENCAO:
            print(f"🔹 [{index + 1}/{total}] Pular {clean_tel}: Já está em intervenção.")
            continue

        nome_raw = str(row.get('nome', '')).strip()
        primeiro_nome = nome_raw.split()[0].title() if nome_raw else ""
        
        print(f"🔹 [{index + 1}/{total}] Iniciando sequência para: {nome_raw or 'Sem Nome'}...")

        # --- 1. MENSAGEM DE ABERTURA COM VARIAÇÃO ---
        if primeiro_nome:
            opcoes_saudacao = [
                f"Boooom diiiaa, {primeiro_nome}! Beleza?\nFalamos uns dias atrás sobre sua frota, lembra?",
                f"Boooom diiiaa, Beleza {primeiro_nome}!? \nFalamos alguns dias atrás sobre sua frota, certo?"
            ]
            msg1 = random.choice(opcoes_saudacao)
        else:
            msg1 = "Boooom diiiaa! Beleza?."

        if not sender_global.enviar_mensagem(telefone, msg1): continue 
        
        time.sleep(random.randint(4, 6))

        # --- 2. ENVIO DAS 3 IMAGENS (JPEG) ---
        lista_imagens = ["promo1.jpeg", "promo2.jpeg"] 
        
        abortar = False
        for imagem in lista_imagens:
            if clean_tel in CLIENTES_EM_INTERVENCAO:
                print(f"      🛑 PARE! Cliente {clean_tel} respondeu.")
                abortar = True
                break
            
            sucesso_img = sender_global.enviar_imagem_local(telefone, imagem)
            if sucesso_img:
                time.sleep(random.randint(6, 12))
            else:
                pass 

        if abortar: continue

        msgs_finais = [
            "Escolhi umas promoções pra você bem top!",
            "Pra clientes inativos, a gente tá com condição especial de pagamento até o dia 18, antes das férias coletivas.",
            "Posso te enviar essa condição exclusiva?"
        ]
        
        for msg_parte in msgs_finais:
            if clean_tel in CLIENTES_EM_INTERVENCAO:
                print(f"      🛑 PARE! Cliente {clean_tel} respondeu.")
                break
            sender_global.enviar_mensagem(telefone, msg_parte)
            time.sleep(random.randint(4, 8))

        # --- DELAY ALEATÓRIO DE 3 A 5 MINUTOS ---
        delay_cliente = random.randint(CONFIG["DELAY_ENTRE_CLIENTES"][0], CONFIG["DELAY_ENTRE_CLIENTES"][1])
        print(f"   ⏳ Aguardando {delay_cliente}s para o próximo cliente...\n")
        time.sleep(delay_cliente)

    print("=" * 60)
    print("🏁 LISTA FINALIZADA.")
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