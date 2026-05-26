import requests
import re
import logging
import os
from app.core.config import config

log = logging.getLogger(__name__)

BOT_WEBHOOK_URL = os.environ.get(
    "BOT_WEBHOOK_URL",
    "https://chatbot-python-lucas.fly.dev/webhook"
)

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

            # Callback injetado pelo main.py para rastrear ids das mensagens enviadas pelo bot
            cls._instance._sent_ids_callback = None

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

    def configurar_webhook(self) -> bool:
        """Registra/atualiza o webhook na Evolution API. Chamado no boot e periodicamente."""
        try:
            url = f"{self.base_url}/webhook/set/chatbot"
            payload = {
                "url": BOT_WEBHOOK_URL,
                "webhook_by_events": False,
                "webhook_base64": True,
                "events": ["MESSAGES_UPSERT"],
                "enabled": True
            }
            response = self._session.post(url, json=payload, timeout=10)
            if response.status_code in [200, 201]:
                log.info(f"✅ [Webhook] Registrado com sucesso: {BOT_WEBHOOK_URL}")
                return True
            else:
                log.error(f"❌ [Webhook] Falha ao registrar. Status: {response.status_code} | {response.text}")
                return False
        except Exception as e:
            log.error(f"❌ [Webhook] Erro de conexão ao registrar: {e}")
            return False

    def verificar_e_reconfigurar_webhook(self) -> None:
        """Verifica se o webhook está ativo; se não, registra automaticamente."""
        if not self.is_webhook_configurado():
            log.warning("⚠️ [Webhook] Não encontrado ou desativado. Tentando registrar...")
            self.configurar_webhook()

    def get_connection_state(self) -> str:
        """Retorna o estado atual da conexão WhatsApp: 'open', 'connecting', 'close' ou 'unknown'."""
        try:
            url = f"{self.base_url}/instance/connectionState/chatbot"
            response = self._session.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                # A Evolution API v1/v2 retorna o estado em campos diferentes
                state = (
                    data.get('instance', {}).get('state')
                    or data.get('state')
                    or ""
                ).lower()
                return state if state else ("open" if "open" in response.text.lower() else "unknown")
            return "unknown"
        except Exception:
            return "unknown"

    def tentar_reconectar(self) -> bool:
        """
        Tenta reconectar a instância WhatsApp quando o estado não é 'open'.
        Chama o endpoint de connect/restart da Evolution API.
        Retorna True se a chamada foi aceita.
        """
        try:
            url = f"{self.base_url}/instance/connect/chatbot"
            response = self._session.get(url, timeout=10)
            if response.status_code in [200, 201]:
                log.info("🔄 [Reconexão] Solicitação de reconexão enviada com sucesso.")
                return True
            else:
                log.warning(f"⚠️ [Reconexão] Falha na solicitação. Status: {response.status_code}")
                return False
        except Exception as e:
            log.error(f"❌ [Reconexão] Erro de conexão: {e}")
            return False

    def verificar_e_reconectar_whatsapp(self) -> None:
        """
        Job periódico: verifica se o WhatsApp está conectado (estado 'open').
        Se não estiver, tenta reconectar automaticamente e reconfigura o webhook.
        Isso evita o problema de mensagens presas em 'Aguardando mensagem'
        após um restart da Evolution API.
        """
        state = self.get_connection_state()
        if state == "open":
            return  # Tudo certo, nada a fazer

        log.warning(f"⚠️ [Saúde] WhatsApp não está 'open' (estado atual: '{state}'). Tentando reconectar...")
        reconectou = self.tentar_reconectar()
        if reconectou:
            # Após reconectar, garante que o webhook ainda está registrado
            self.verificar_e_reconfigurar_webhook()
        else:
            log.error("❌ [Saúde] Reconexão falhou. Será tentado novamente no próximo ciclo.")

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

        # Limita o delay a no m\u00e1ximo 5s para evitar timeout na Evolution API
        delay_seguro = min(delay_ms, 5000)

        # linkPreview s\u00f3 faz sentido quando h\u00e1 URL; ativ\u00e1-lo em textos comuns
        # faz o WhatsApp ficar no estado "aguardando mensagem" quando o servi\u00e7o
        # de preview do Meta est\u00e1 lento ou indispon\u00edvel.
        tem_link = "http://" in mensagem_limpa or "https://" in mensagem_limpa

        payload = {
            "number": clean_number,
            "textMessage": {
                "text": mensagem_limpa
            },
            "options": {
                "delay": delay_seguro,
                "presence": "composing",
                "linkPreview": tem_link
            }
        }

        url = f"{self.base_url}/message/sendText/chatbot"
        try:
            response = self._session.post(url, json=payload, timeout=40)
            if response.status_code < 400:
                log.info(f"✅ Resposta da IA enviada com sucesso para {clean_number}")
                # Registra o msg_id para que o webhook saiba que essa msg foi enviada pelo código
                try:
                    resp_data = response.json()
                    msg_id = resp_data.get('key', {}).get('id')
                    if msg_id and self._sent_ids_callback:
                        self._sent_ids_callback(msg_id)
                except Exception:
                    pass
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
                    "wuid": contact_number,
                    "phoneNumber": contact_number
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
