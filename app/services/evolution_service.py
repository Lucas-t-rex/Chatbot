import requests
import re
import logging
from app.core.config import config

log = logging.getLogger(__name__)

class EvolutionService:
    _instance = None
    _session = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EvolutionService, cls).__new__(cls)
            # Create a global session for connection pooling
            # This drastically reduces TCP/SSL handshake latency
            cls._instance._session = requests.Session()
            cls._instance._session.headers.update({"apikey": config.EVOLUTION_API_KEY, "Content-Type": "application/json"})
            
            adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
            cls._instance._session.mount('http://', adapter)
            cls._instance._session.mount('https://', adapter)
            
        return cls._instance

    @property
    def base_url(self):
        url = config.EVOLUTION_API_URL or ""
        return url[:-1] if url.endswith('/') else url

    def is_evolution_online(self) -> bool:
        try:
            url = f"{self.base_url}/instance/connectionState/chatbot"
            response = self._session.get(url, timeout=5)
            if response.status_code == 200 and "open" in response.text.lower():
                return True
            return False
        except Exception:
            return False

    def is_webhook_configurado(self) -> bool:
        try:
            url = f"{self.base_url}/webhook/find/chatbot"
            response = self._session.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                webhook_info = data.get('webhook', data)
                enabled = webhook_info.get('enabled', False)
                url_conf = webhook_info.get('url', '')
                return bool(enabled and url_conf)
            return False
        except Exception:
            return False

    def enviar_simulacao_digitacao(self, number: str) -> bool:
        clean_number = number.split('@')[0]
        payload = {
            "number": clean_number,
            "options": {
                "presence": "composing",
                "delay": 12000
            }
        }
        url = f"{self.base_url}/chat/sendPresence/chatbot"
        try:
            response = self._session.post(url, json=payload, timeout=20)
            if response.status_code in [200, 201]:
                return True
            else:
                log.warning(f"⚠️ Falha ao enviar 'Digitando'. {response.status_code}")
                return False
        except Exception as e:
            log.warning(f"⚠️ Erro de conexão no 'Digitando': {e}")
            return False

    def send_whatsapp_message(self, number: str, text_message: str, delay_ms: int = 3000) -> bool:
        clean_number = number.split('@')[0]
        
        # I didn't put remove_emojis in helpers.py yet. I will define it here for encapsulation.
        def remove_emojis_func(text):
            if not text: return ""
            return re.sub(
                r'[\U00010000-\U0010ffff'
                r'\u2600-\u26ff'
                r'\u2700-\u27bf'
                r'\ufe0f]'
                , '', text).strip()
                
        mensagem_limpa = remove_emojis_func(text_message)
        if not mensagem_limpa:
            return False
        
        payload = {
            "number": clean_number, 
            "textMessage": {
                "text": mensagem_limpa
            },
            "options": {
                "delay": delay_ms,    
                "presence": "composing", 
                "linkPreview": True
            }
        }
        
        url = f"{self.base_url}/message/sendText/chatbot"
        try:
            response = self._session.post(url, json=payload, timeout=40)
            if response.status_code < 400:
                log.info(f"✅ Resposta da IA enviada com sucesso para {clean_number}")
                return True
            else:
                log.error(f"❌ ERRO DA API EVOLUTION ao enviar para {clean_number}: {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            log.error(f"❌ Erro de CONEXÃO ao enviar mensagem para {clean_number}: {e}")
            return False

    def send_whatsapp_contact(self, number: str, contact_name: str, contact_number: str) -> bool:
        clean_number = number.split('@')[0]
        payload = {
            "number": clean_number,
            "contactMessage": [
                {
                    "fullName": contact_name,
                    "wuid": contact_number
                }
            ]
        }
        url = f"{self.base_url}/message/sendContact/chatbot"
        try:
            response = self._session.post(url, json=payload, timeout=20)
            if response.status_code < 400:
                print(f"✅ Contato ({contact_name}) enviado com sucesso para {clean_number}")
                return True
            else:
                print(f"❌ ERRO API EVOLUTION VCard ({response.status_code}): {response.text}")
                return False
        except requests.exceptions.RequestException as e:
            print(f"❌ Erro de CONEXÃO ao enviar contato para {clean_number}: {e}")
            return False

evolution_api = EvolutionService()
