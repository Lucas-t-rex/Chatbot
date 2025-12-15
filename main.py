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
    "DELAY_ENTRE_MSG": (4, 7),    # Tempo de pausa entre uma mensagem e outra da sequência
    "DELAY_ENTRE_CLIENTES": (120, 180) # Tempo de descanso entre clientes
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

    def enviar_mensagem(self, numero: str, mensagem: str, delay_digitacao=None) -> bool:
        clean_number = self.limpar_telefone(numero)
        if not clean_number: return False

        # Verifica Intervenção
        if clean_number in CLIENTES_EM_INTERVENCAO and clean_number != CONFIG["RESPONSIBLE_NUMBER"]:
            print(f"      ⛔ [BLOQUEADO] Cliente {clean_number} em intervenção.")
            return False

        # Se não passar delay específico, usa o padrão da config
        if delay_digitacao is None:
            delay_digitacao = CONFIG["TEMPO_DIGITANDO"]

        api_path = f"/message/sendText/{self.instance}"
        final_url = self.base_url if self.base_url.endswith(api_path) else \
                    (self.base_url[:-1] + api_path if self.base_url.endswith('/') else self.base_url + api_path)

        payload = {
            "number": clean_number, 
            "textMessage": {"text": mensagem},
            "options": {
                "delay": delay_digitacao,
                "presence": "composing",
                "linkPreview": True
            }
        }

        try:
            response = requests.post(final_url, json=payload, headers=self.headers, timeout=25)
            if response.status_code < 400:
                print(f"      ✅ Enviado Texto: \"{mensagem[:30]}...")
                return True
            else:
                print(f"      ❌ Falha API Texto: {response.status_code}")
                return False
        except:
            return False

    def enviar_imagem_local(self, numero: str, caminho_imagem: str, caption: str = "") -> bool:
        clean_number = self.limpar_telefone(numero)
        if not clean_number: return False

        # Verifica arquivo
        if not os.path.exists(caminho_imagem):
            print(f"      ❌ Erro: Imagem '{caminho_imagem}' não encontrada.")
            return False

        # Verifica Intervenção
        if clean_number in CLIENTES_EM_INTERVENCAO and clean_number != CONFIG["RESPONSIBLE_NUMBER"]:
            return False

        try:
            # Converte imagem para Base64
            with open(caminho_imagem, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            
            # Detecta tipo (jpg, png, etc)
            mime_type, _ = mimetypes.guess_type(caminho_imagem)
            if not mime_type: mime_type = "image/jpeg"

            api_path = f"/message/sendMedia/{self.instance}"
            final_url = self.base_url if self.base_url.endswith(api_path) else \
                        (self.base_url[:-1] + api_path if self.base_url.endswith('/') else self.base_url + api_path)

            payload = {
                "number": clean_number,
                "mediaMessage": {
                    "mediatype": "image",
                    "caption": caption,
                    "media": encoded_string
                },
                "options": {
                    "delay": CONFIG["TEMPO_DIGITANDO"],
                    "presence": "composing"
                }
            }
            
            # Timeout maior (60s) para upload de imagem
            response = requests.post(final_url, json=payload, headers=self.headers, timeout=60)
            if response.status_code < 400:
                print(f"      📸 Enviado Imagem: {os.path.basename(caminho_imagem)}")
                return True
            else:
                print(f"      ❌ Falha API Imagem: {response.text}")
                return False
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
    
    print("\n🤖 DISPARADOR OTIMIZADO (COM PAUSE E IMAGENS)")
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
        
        # --- ⏸️ CHECK DE PAUSA ---
        while PAUSA_DO_SISTEMA:
            print("💤 ... Sistema PAUSADO pelo Admin (Aguardando 'bot play') ...")
            time.sleep(10)

        # --- VERIFICAÇÃO INICIAL ---
        telefone = str(row.get('telefone', '')).strip()
        if not telefone: continue
        
        clean_tel = sender_global.limpar_telefone(telefone)
        if clean_tel in CLIENTES_EM_INTERVENCAO:
            print(f"🔹 [{index + 1}/{total}] Pular {clean_tel}: Já está em intervenção.")
            continue

        nome_raw = str(row.get('nome', '')).strip()
        primeiro_nome = nome_raw.split()[0].title() if nome_raw else ""
        
        print(f"🔹 [{index + 1}/{total}] Iniciando sequência para: {nome_raw or 'Sem Nome'}...")

        # --- 1. MENSAGEM DE ABERTURA ---
        if primeiro_nome:
            msg1 = f"Boooom diiiaa, {primeiro_nome}! Beleza?\nFalamos uns dias atrás sobre sua frota, lembra?"
        else:
            msg1 = "Boooom diiiaa! Beleza?."

        if not sender_global.enviar_mensagem(telefone, msg1): continue # Se falhar ou estiver em intervenção, pula
        
        # Pausa para "escolher" as fotos
        time.sleep(random.randint(4, 6))

        # --- 2. ENVIO DAS 3 IMAGENS ---
        # Certifique-se que estas imagens estão na pasta do projeto no Git
        lista_imagens = ["promo1.jpeg", "promo2.jpeg", "promo3.jpeg"]
        
        abortar = False
        for imagem in lista_imagens:
            # Checa intervenção antes de cada imagem
            if clean_tel in CLIENTES_EM_INTERVENCAO:
                print(f"      🛑 PARE! Cliente {clean_tel} respondeu durante as fotos.")
                abortar = True
                break
            
            sucesso_img = sender_global.enviar_imagem_local(telefone, imagem)
            if sucesso_img:
                time.sleep(random.randint(2, 4)) # Pausa entre fotos
            else:
                pass 

        if abortar: continue # Pula pro próximo cliente

        # --- 3. MENSAGEM FINAL (DIVIDIDA EM 3 PARTES) ---
        msgs_finais = [
            "Escolhi umas promoções pra você bem top!",
            "Pra clientes inativos, a gente tá com condição especial de pagamento até o dia 18, antes das férias coletivas.",
            "Posso te enviar essa condição exclusiva?"
        ]
        
        for msg_parte in msgs_finais:
            # Checagem de segurança antes de cada balão de mensagem
            if clean_tel in CLIENTES_EM_INTERVENCAO:
                print(f"      🛑 PARE! Cliente {clean_tel} respondeu durante a finalização.")
                break
            
            # Envia a parte atual
            sender_global.enviar_mensagem(telefone, msg_parte)
            
            # Pequena pausa para simular que está digitando a próxima frase (2 a 4 segundos)
            time.sleep(random.randint(2, 4))

        # --- FIM DO CLIENTE ATUAL ---
        # (Removi o bloco errado que tentava enviar 'msgs_finais' de novo aqui)

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