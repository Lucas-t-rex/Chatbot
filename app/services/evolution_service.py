import base64
import requests
import re
import logging
import os
import secrets
from app.core.config import config

log = logging.getLogger(__name__)

BOT_WEBHOOK_URL = os.environ.get(
    "BOT_WEBHOOK_URL",
    "https://chatbot-python-lucas.fly.dev/webhook"
)

def evento_da_instancia(data, instancia_esperada) -> bool:
    """True se o evento do webhook veio da instância esperada (ou se o payload não informa a instância)."""
    instancia = (data or {}).get('instance')
    if isinstance(instancia, dict):
        instancia = instancia.get('instanceName') or instancia.get('name')
    return not instancia or instancia == instancia_esperada


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

    def _inst(self, instance=None) -> str:
        """Nome da instância: a informada ou a padrão (config.EVOLUTION_INSTANCE)."""
        return instance or config.EVOLUTION_INSTANCE

    def is_evolution_online(self, instance=None) -> bool:
        try:
            url = f"{self.base_url}/instance/connectionState/{self._inst(instance)}"
            response = self._session.get(url, timeout=5)
            if response.status_code == 200 and "open" in response.text.lower():
                return True
            return False
        except Exception:
            return False

    def is_webhook_configurado(self, instance=None) -> bool:
        try:
            url = f"{self.base_url}/webhook/find/{self._inst(instance)}"
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

    def configurar_webhook(self, instance=None) -> bool:
        """Registra/atualiza o webhook na Evolution API. Chamado no boot e periodicamente."""
        try:
            url = f"{self.base_url}/webhook/set/{self._inst(instance)}"
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

    def get_connection_state(self, instance=None) -> str:
        """Retorna o estado atual da conexão WhatsApp: 'open', 'connecting', 'close' ou 'unknown'."""
        try:
            url = f"{self.base_url}/instance/connectionState/{self._inst(instance)}"
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

    def esta_conectada(self, instance=None) -> bool:
        """True se a instância está com o WhatsApp vinculado (estado 'open')."""
        return self.get_connection_state(instance) == "open"

    def instancia_para_relatorios(self):
        """
        Instância que envia os relatórios: só a de relatórios, e só se estiver 'open'.
        Sem fallback: a principal é o número das atendentes e não pode receber/mandar
        relatório. Devolve None se não houver instância pronta (quem chama não envia).
        """
        rel = config.EVOLUTION_INSTANCE_RELATORIO
        if rel and rel != config.EVOLUTION_INSTANCE and self.esta_conectada(rel):
            return rel
        log.error(f"❌ [Relatórios] Instância '{rel}' não está conectada; relatório NÃO enviado "
                  f"(não usamos '{config.EVOLUTION_INSTANCE}' como alternativa).")
        return None

    def numero_da_instancia(self, instance=None) -> str:
        """Número (só dígitos) vinculado à instância, ou '' se não conseguir descobrir."""
        try:
            nome = self._inst(instance)
            response = self._session.get(f"{self.base_url}/instance/fetchInstances",
                                         params={"instanceName": nome}, timeout=10)
            if response.status_code != 200:
                return ""
            for item in response.json():
                dados = item.get("instance", item)
                if dados.get("instanceName") == nome:
                    return re.sub(r"\D", "", str(dados.get("owner") or "").split("@")[0])
        except Exception:
            pass
        return ""

    def criar_instancia(self, nome: str) -> bool:
        """Cria uma instância sem QR code e sem webhook (ex.: a de relatórios)."""
        try:
            # Token próprio: sem ele a Evolution v1.8.7 responde "Token already exists".
            payload = {"instanceName": nome, "token": secrets.token_hex(16).upper(),
                       "qrcode": False, "integration": "WHATSAPP-BAILEYS"}
            response = self._session.post(f"{self.base_url}/instance/create", json=payload, timeout=15)
            if response.status_code in [200, 201]:
                log.info(f"✅ [Instância] '{nome}' criada.")
                return True
            log.error(f"❌ [Instância] Falha ao criar '{nome}'. Status: {response.status_code}")
            return False
        except Exception as e:
            log.error(f"❌ [Instância] Erro de conexão ao criar '{nome}': {e}")
            return False

    def obter_qrcode(self, instance=None) -> dict:
        """Pede o QR code de pareamento. Devolve o JSON da Evolution ({} se falhar)."""
        try:
            url = f"{self.base_url}/instance/connect/{self._inst(instance)}"
            response = self._session.get(url, timeout=15)
            if response.status_code in [200, 201]:
                return response.json()
            return {}
        except Exception:
            return {}

    def tentar_reconectar(self, instance=None) -> bool:
        """
        Tenta reconectar a instância WhatsApp quando o estado não é 'open'.
        Chama o endpoint de connect/restart da Evolution API.
        Retorna True se a chamada foi aceita.
        """
        try:
            url = f"{self.base_url}/instance/connect/{self._inst(instance)}"
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
        # A instância de relatórios só tem o estado registrado em log: enquanto
        # nunca foi vinculada, não faz sentido reconectar (geraria QR codes à toa).
        rel = config.EVOLUTION_INSTANCE_RELATORIO
        if rel and rel != config.EVOLUTION_INSTANCE:
            estado_rel = self.get_connection_state(rel)
            if estado_rel == "open":
                log.info(f"[Saúde] Instância de relatórios '{rel}' conectada.")
            else:
                log.warning(f"⚠️ [Saúde] Instância de relatórios '{rel}' não está 'open' "
                            f"(estado: '{estado_rel}'). Os relatórios saem pela '{config.EVOLUTION_INSTANCE}'.")

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

    def enviar_simulacao_digitacao(self, number: str, instance=None) -> bool:
        clean_number = number.split('@')[0]
        payload = {
            "number": clean_number,
            "options": {
                "presence": "composing",
                "delay": 12000
            }
        }
        url = f"{self.base_url}/chat/sendPresence/{self._inst(instance)}"
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

    def send_whatsapp_message(self, number: str, text_message: str, delay_ms: int = 3000,
                              manter_emojis: bool = False, instance=None) -> bool:
        """
        manter_emojis=False (padr\u00e3o): o bot fala com clientes sem emoji.
        manter_emojis=True: usado nos relat\u00f3rios do dono, que usam \ud83d\udcca \u26a0\ufe0f \u2705 de prop\u00f3sito.
        """
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

        mensagem_limpa = (text_message or "").strip() if manter_emojis else remove_emojis_func(text_message)
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

        url = f"{self.base_url}/message/sendText/{self._inst(instance)}"
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

    def send_whatsapp_document(self, number: str, conteudo: bytes, nome_arquivo: str, legenda: str = "", instance=None) -> bool:
        """Envia um arquivo (ex.: a planilha .xlsx) como documento do WhatsApp."""
        clean_number = number.split('@')[0]
        payload = {
            "number": clean_number,
            "mediaMessage": {
                "mediatype": "document",
                "fileName": nome_arquivo,
                "caption": legenda,
                "media": base64.b64encode(conteudo).decode("ascii"),
            },
        }
        url = f"{self.base_url}/message/sendMedia/{self._inst(instance)}"
        try:
            response = self._session.post(url, json=payload, timeout=90)
            if response.status_code < 400:
                log.info(f"✅ Documento '{nome_arquivo}' enviado para {clean_number}")
                try:
                    msg_id = response.json().get('key', {}).get('id')
                    if msg_id and self._sent_ids_callback:
                        self._sent_ids_callback(msg_id)
                except Exception:
                    pass
                return True
            log.error(f"❌ ERRO DA API EVOLUTION ao enviar documento para {clean_number}: {response.status_code}")
            return False
        except requests.exceptions.RequestException as e:
            log.error(f"❌ Erro de CONEXÃO ao enviar documento para {clean_number}: {e}")
            return False

    def send_whatsapp_contact(self, number: str, contact_name: str, contact_number: str, instance=None) -> bool:
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
        url = f"{self.base_url}/message/sendContact/{self._inst(instance)}"
        try:
            response = self._session.post(url, json=payload, timeout=20)
            if response.status_code < 400:
                print(f"✅ Contato ({contact_name}) enviado com sucesso para {clean_number}")
                # Igual ao sendText: sem isso o eco do VCard volta pelo webhook
                # e é gravado como se o atendente tivesse mandado.
                try:
                    msg_id = response.json().get('key', {}).get('id')
                    if msg_id and self._sent_ids_callback:
                        self._sent_ids_callback(msg_id)
                except Exception:
                    pass
                return True
            else:
                print(f"❌ ERRO API EVOLUTION VCard ({response.status_code}): {response.text}")
                return False
        except requests.exceptions.RequestException as e:
            print(f"❌ Erro de CONEXÃO ao enviar contato para {clean_number}: {e}")
            return False

evolution_api = EvolutionService()
