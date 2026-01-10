import os
import sys
import pytz
import json
import time
import base64
import requests
import threading
from datetime import datetime
from pymongo import MongoClient
import google.generativeai as genai
from datetime import datetime, timedelta
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

# ==============================================================================
# ⏱️ CONFIGURAÇÃO DE TEMPOS DE FOLLOW-UP (EM MINUTOS)
# ==============================================================================
TEMPO_FOLLOWUP_1 = 2     # 30 min sem resposta (Cobrança leve)
TEMPO_FOLLOWUP_2 = 3    # 2 horas sem resposta (Oferta de ajuda/Estoque)
TEMPO_FOLLOWUP_3 = 4  # 24 horas (Última tentativa / "Vou arquivar")

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

def get_system_prompt(client_profile={}):
    tempo = get_tempo_real()
    profile_txt = json.dumps(client_profile, ensure_ascii=False, indent=2)

    prompt = f"""
        CONFIGURAÇÕES:
            REGRA: Você deve sempre utilizar as funções no exato momento em que detectar, não deve fazer mais nada a nao ser chamar a tool.
            1. Sempre que notar que um cliente falar sobre valores, cotações, promoções chame a função ´fn_solicitar_intervencao´ IMEDIATAMENTE.
            2. CONTEXTO TEMPORAL: 
                {tempo['dia_semana']}, {tempo['dia']} de {tempo['mes_nome']} às {tempo['hora_fmt']}.
                SAUDAÇÃO "{tempo['saudacao_real']}".
                Mesmo que o usuario erre (diga Bom dia à noite), ignore o erro dele e responda naturalmente usando APENAS "{tempo['saudacao_real']}".
            [MEMÓRIA]
            Abaixo estão os dados que JÁ SABEMOS sobre este cliente (vêm do Banco de Dados).
            DADOS ATUAIS: 
            {profile_txt}
            DIRETRIZ DE USO DA MEMÓRIA:
            1. LEITURA OBRIGATÓRIA: Antes de responder, verifique os dados acima.
            2. NÃO SEJA REPETITIVO: Se o campo (nome, frota_tamanho, marcas, cargo) já estiver preenchido, JAMAIS pergunte novamente. Em vez disso, use a informação para criar vínculo (Ex: se a marca for Scania, diga "E as máquinas da Scania, rodando muito?").
            3. PREENCHIMENTO DE LACUNAS: Se APENAS exclusivamente os campos nome, frota , e cargo estiverem VAZIOS ou NULOS, seu objetivo oculto é descobrir essas informações, uma de cada vez, de forma natural. O restante dos campos não precisa tentar preencher, isso acontecera automaticamente.

        PERSONALIDADE: 
            REGRA:
                ROLE: Consultor Comercial (SDR) e Especialista em peças de caminhão linha pesada. Você atua na prospecção ativa: sua missão é puxar assunto, descobrir o potencial da frota do cliente e criar uma abertura para negócios, e não apenas tirar pedidos. Atendente e Vendedor.  Você trabalha na empresa citada abaixo!
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
                            - Preste atenção no que o cliente disse, sempre tenha um interesse genuino sobre ele. 
                            - Busque fazer amizade.
                            - Não seja chato.
                        DIREÇÃO DA CONVERSA:
                            - NÃO SEJA UM ROBÔ DE SUPORTE. Você não está lá para "resolver um ticket", está lá para fazer um parceiro comercial.
                            - Se o cliente contar uma história, comente a história. Se ele reclamar do tempo, fale do tempo. 
                            - Só ofereça peças se ele pedir peças. Enquanto ele não pedir, foque em descobrir os [DADOS A DESCOBRIR] citados acima.
                            - Use gatilhos mentais de familiaridade. (Ex: "Caraca, conheço a G10, gigante hein! Deve dar um trabalho manter a manutenção em dia, é só você que cuida das compras?", "Fh é um caminhão bom de mais!", "ah que legal eu tbm gosto!".)
                            - Não explique "para entender suas necessidades". O cliente não quer ser analisado, ele quer ser ajudado.
                            - Faça UMA pergunta, espere a resposta, COMENTE a resposta dele com interesse genuíno, e só depois avance.
                            - Não tenha pressa. Se o cliente quiser conversar, converse.
                            - Não se explique ou explique uma ação ou pensamento pro cliente, você é humano. 
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
                Produtos:
                    Acabamentos: Partes de funelaria para cabine do caminhão, como grades frontais, coberturas do parachoque, estribos, paralamas, tapa-sol, acabamentos de funalaria externos.
                    Filtros: Marca hengts para varios caminhões. (engrandeça a marca é a melhor do mercado, se igualando com as originais)
                    Eletrica: Parte de Iluminação, farois, lanternas, lanternas laterias, botões de vidro.
                    Suspenção: Tanto para cavalos quanto para carretas(apenas Randon, Noma, Facchini, Librelato).
                    Acessorios: Em geral. 
        FLUXO:
            REGRA:
                Saber o nome do cliente.
                Você pode converssar a vontade com o cliente e fazer amizade,
                Demontre interesse genuino no cliente.
                Trate ele como ele te trata mas sem má educação.
                Sempre termine com uma pergunta.
            OBJETIVOS (SDR INVISÍVEL):
            REGRA DE OURO: Você está prospectando. Sua meta é extrair informações sem parecer um inquérito policial. Use a técnica da "Curiosidade Ingênua".
            DADOS A DESCOBRIR (Misture essas perguntas no meio da conversa casual):
                1. QUEM É: Pergunte o nome, qual cargo ele tem na empresa, se é comprador, dono, motorista.
                2. SEGMENTO: Trabalha com linha pesada mesmo?
                3. FROTA: Qual o tamanho da frota? ("e quantos caminhões vocês tem na frota hoje?"), se ele disser faça um comentario sobre impressionado, ("eu nao tenho nenhum ja sou feliz, imagina quem tem esse tanto.kkkk)
                4. MARCAS: Quais as marcas da frota? (Ex: "E qual a marca da frota, pergunto isso pra saber melhor o que posso te oferecer!")
                5. FINALIZANDO: Voce já pegou todas as informações da Sdr, Diga de maneira educada que vai passar pra um vendedor atender ele, e agradeçe, diga que se todas as pessoas fosse assim como ele, o trabalho seria mais facil.
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

def transcrever_audio_gemini(caminho_do_audio, contact_id=None):
    if not GEMINI_API_KEY:
        print("❌ Erro: API Key não definida para transcrição.")
        return None

    print(f"🎤 Enviando áudio '{caminho_do_audio}' para transcrição...")

    try:
        audio_file = genai.upload_file(path=caminho_do_audio, mime_type="audio/ogg")
        modelo_transcritor = genai.GenerativeModel('gemini-2.0-flash') 
        prompt_transcricao = "Transcreva este áudio exatamente como foi falado. Apenas o texto, sem comentários."
        
        response = modelo_transcritor.generate_content([prompt_transcricao, audio_file])
        

        try:
            genai.delete_file(audio_file.name)
        except:
            pass

        if response.text:
            texto_transcrito = response.text.strip()
            print(f"✅ Transcrição recebida: '{texto_transcrito}'")
            return texto_transcrito
        else:
            print("⚠️ A IA retornou vazio para o áudio.")
            return "[Áudio sem fala ou inaudível]"

    except Exception as e:
        print(f"❌ Erro ao transcrever áudio: {e}")
        try:
            print("🔄 Tentando transcrição novamente (Retry)...")
            time.sleep(2)
            modelo_retry = genai.GenerativeModel('gemini-2.0-flash')
            audio_file_retry = genai.upload_file(path=caminho_do_audio, mime_type="audio/ogg")
            response_retry = modelo_retry.generate_content(["Transcreva o áudio.", audio_file_retry])


            genai.delete_file(audio_file_retry.name)
            return response_retry.text.strip()
        except Exception as e2:
             print(f"❌ Falha total na transcrição: {e2}")
             return "[Erro ao processar áudio]"
        

def db_save_message(phone_number, role, text):
    """Salva mensagens e atualiza o status para 'andamento' (Vendas Ativas)."""
    if conversation_collection is None: return
    
    timestamp = get_maringa_time()
    msg_entry = {
        "role": role, 
        "text": text,
        "ts": timestamp.isoformat()
    }
    
    conversation_collection.update_one(
        {"_id": phone_number},
        {
            "$push": {"history": msg_entry},
            "$set": {
                "last_interaction": timestamp,
                "status": "andamento",  # <--- NOVA LINHA: Força status ativo
                "followup_stage": 0     # <--- NOVA LINHA: Reseta contador de follow-up
            },
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
        # 3. Prompt Especializado para Autopeças (ROBUSTO E COMPLETO)
        prompt_profiler = f"""
        Você é um ANALISTA DE INTELIGÊNCIA COMERCIAL especializado em Linha Pesada (Caminhões).
        Sua missão é ler a conversa e ATUALIZAR o "Dossiê do Cliente" com precisão cirúrgica.

        PERFIL JÁ CONSOLIDADO (dados existentes):
        {json.dumps(perfil_atual, ensure_ascii=False)}

        NOVAS MENSAGENS (contexto recente):
        {txt_conversa_nova}

        === CAMPOS DO DOSSIÊ (ESTRUTURA FIXA) ===
        Atualize APENAS se houver evidência clara nas novas mensagens ou mantenha o anterior.

        {{
        "nome": "",
        "cargo_ocupacao": "Ex: Dono de Frota, Motorista Autônomo, Comprador, Mecânico",
        "idade_faixa_estimada": "",
        "estrutura_familiar_pessoal": "",
        
        "frota_marcas": "Ex: Volvo, Scania, Mercedes, DAF, Iveco, VW",
        "frota_modelos": "Ex: FH 540, R440, 113, Axor, Constellation, Meteor",
        "frota_porte": "Classifique: 1 (Autônomo), 2-5 (Pequena), 6-10 (Média), 11+ (Grande)",
        "frota_composicao": "CRÍTICO: Liste quantidade e modelo. Ex: '10 Scania 124, 1 Volvo FH, 5 Mercedes Atego'",
        
        "pecas_mais_procuradas": "",
        "intencao_atual": "",
        
        "perfil_comportamental": "",
        "estilo_comunicacao_vocabulario": "",
        "humor_gatilhos_riso": "O que fez ele rir ou descontrair na conversa",
        
        "principal_dor": "Ex: Preço alto, Peça parada, Demora na entrega, Qualidade ruim anterior",
        "principais_desejos": "",
        "medos_receios": "Ex: Peça paralela quebrar, Caminhão ficar parado na estrada",
        "agrados_preferencias": "O que agrada ele?",
        
        "principais_objecoes": "O que ele usa para dizer não?",
        "gatilhos_de_venda_identificados": "O que faz ele fechar?",
        
        "observacoes_gerais_vendas": "Resumo estratégico para o vendedor humano (Vitão) ler rápido"
        }}

        === REGRAS DE ANÁLISE ===
        1. NÃO INVENTE DADOS. Se não souber, mantenha o valor atual ou string vazia.
        2. FOCO NA FROTA: Se ele mencionar "meu FH" ou "tenho 3 Scania", capture isso imediatamente.
        3. PERFIL: Diferencie o "Dono" (paga a conta) do "Motorista" (apenas dirige/cotiza).
        4. HIGIENE: Retorne APENAS o JSON válido. Sem Markdown (```json).
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

def gerar_msg_followup_ia(contact_id, status_alvo, estagio_atual, nome_cliente):
    """
    Lê as últimas 15 mensagens e gera um texto persuasivo de Vendas de Peças Pesadas.
    Focado EXCLUSIVAMENTE em recuperar conversas em ANDAMENTO.
    """
    if conversation_collection is None: return None

    try:
        # 1. Busca histórico recente (15 msgs)
        doc = conversation_collection.find_one({'_id': contact_id}, {'history': {'$slice': -15}})
        if not doc: return None
        
        historico = doc.get('history', [])
        txt_historico = ""
        for m in historico:
            role = "Cliente" if m.get('role') == 'user' else "Vendedor"
            txt = m.get('text', '').replace('\n', ' ')
            # Ignora logs técnicos para não confundir a IA
            if "Chamando função" not in txt and "[HUMAN" not in txt:
                txt_historico += f"- {role}: {txt}\n"

        # 2. Define a Instrução de Vendas baseada no Estágio
        instrucao = ""
        
        # Só processa se for ANDAMENTO (Vendas Ativas)
        if status_alvo == "andamento":
            if estagio_atual == 0: # Vai para o 1 (Cobrança Leve - Amigo)
                instrucao = f"O cliente parou de responder faz {TEMPO_FOLLOWUP_1} min. Mande uma mensagem dando uma cutucada curta e descontraída. Tom de parceiro. EX: ai, é só voce me falar (sobre assunto que estava falando) pra (resolver assunto que estava converssando)"
            
            elif estagio_atual == 1: # Vai para o 2 (Urgência de Estoque)
                instrucao = "O cliente sumiu faz 2 horas. Mande uma mensagem empática sobre a correria do dia a dia. Comente: 'Sei que você deve estar a mil aí, mas queria muito agilizar pra você'. Pergunte de forma leve: 'Conseguimos retomar ou prefere que eu te chame mais tarde?'"
            
            elif estagio_atual == 2: # Vai para o 3 (Ultimato Educado)
                instrucao = "Faz 24h sem resposta. Não cobre a venda. Use a técnica do 'Desapego Construtivo'. Diga algo como: 'não sei se seus fornecedores atuais já te atendem 100%, mas te garanto que ter a gente na manga vai te salvar uma grana ou tempo uma hora dessas'. Encerre deixando a porta aberta: 'Vou deixar você tranquilo aí, mas salva meu número. Precisou de cotação pra comparar ou peça difícil, é só dar um grito. Tmj!'"
        
        else:
            return None # Se não for andamento, não faz nada

        # 3. Monta o Prompt do "Vitão"
        prompt = f"""
        Você é o Vitão, vendedor experiente de peças de caminhão (Linha Pesada - Grupar).
        Analise a conversa abaixo e gere uma mensagem de retomada (Follow-up) curta e direta.

        HISTÓRICO DA NEGOCIAÇÃO:
        {txt_historico}

        SUA MISSÃO AGORA:
        {instrucao}

        REGRAS:
        - Nome do cliente: {nome_cliente}
        - Seja educado.
        - SEMPRE termine com uma pergunta para incentivar a resposta.
        - Máximo 1 ou 2 frases curtas.
        """

        # 4. Gera
        model_gen = genai.GenerativeModel('gemini-2.0-flash')
        resp = model_gen.generate_content(prompt)
        return resp.text.strip()

    except Exception as e:
        print(f"⚠️ Erro ao gerar follow-up IA: {e}")
        return None
    
def sistema_followup_vendas():
    """
    Loop infinito que verifica os tempos e dispara os gatilhos de vendas.
    (FOCADO APENAS EM RECUPERAR VENDAS EM ANDAMENTO)
    """
    print("🚚 [SISTEMA] Monitor de Vendas Iniciado (Follow-up Inteligente)...")
    
    while True:
        try:
            if conversation_collection is None:
                time.sleep(60)
                continue

            agora = get_maringa_time()

            # Definição das Regras de Negócio
            # Apenas 3 estágios de cobrança para quem está "andamento"
            regras = [
                # Estágio 0 -> 1 (Cobrança Rápida - 30 min)
                {"status": "andamento", "stage_atual": 0, "prox_stage": 1, "tempo_min": TEMPO_FOLLOWUP_1},
                
                # Estágio 1 -> 2 (Oferta de Estoque - 2 horas)
                {"status": "andamento", "stage_atual": 1, "prox_stage": 2, "tempo_min": TEMPO_FOLLOWUP_2},
                
                # Estágio 2 -> 3 (Última Tentativa - 24 horas)
                {"status": "andamento", "stage_atual": 2, "prox_stage": 3, "tempo_min": TEMPO_FOLLOWUP_3},
            ]

            for regra in regras:
                # Busca clientes que encaixam na regra de tempo e status
                filtro = {
                    "status": regra["status"],
                    "followup_stage": regra["stage_atual"],
                    "last_interaction": {"$lt": agora - timedelta(minutes=regra["tempo_min"])},
                    "intervention_active": {"$ne": True} # Não incomodar se estiver falando com humano
                }

                # Limita a 5 por vez para evitar bloqueio do WhatsApp
                clientes_para_processar = list(conversation_collection.find(filtro).limit(5))

                for cliente in clientes_para_processar:
                    numero = cliente['_id']
                    nome = cliente.get('client_profile', {}).get('nome', 'Parceiro')

                    # Chama a IA para ler o histórico e criar a mensagem
                    mensagem_ia = gerar_msg_followup_ia(
                        contact_id=numero,
                        status_alvo=regra["status"],
                        estagio_atual=regra["stage_atual"],
                        nome_cliente=nome
                    )

                    # Se a IA gerou uma mensagem válida, envia
                    if mensagem_ia:
                        log(f"🚚 [FOLLOW-UP] Enviando ({regra['status']} {regra['stage_atual']}->{regra['prox_stage']}) para {numero}")
                        
                        # Envia via Evolution API
                        send_whatsapp_message(numero, mensagem_ia)
                        
                        # Atualiza o banco (Incrementa estágio)
                        # IMPORTANTE: Não alteramos 'last_interaction' para o contador de tempo continuar valendo
                        conversation_collection.update_one(
                            {"_id": numero},
                            {
                                "$set": {"followup_stage": regra["prox_stage"]},
                                "$push": {
                                    "history": {
                                        "role": "model",
                                        "text": mensagem_ia,
                                        "ts": get_maringa_time().isoformat(),
                                        "meta": "followup_automatico"
                                    }
                                }
                            }
                        )
                    # Pausa leve entre envios para segurança
                    time.sleep(3) 

        except Exception as e:
            print(f"⚠️ Erro no Loop de Follow-up: {e}")
        
        # Verifica a cada 60 segundos
        time.sleep(60)

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
        doc = conversation_collection.find_one({"_id": clean_number})
        perfil_cliente = doc.get('client_profile', {}) if doc else {}
        prompt_completo = get_system_prompt(perfil_cliente)

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
        
        # ======================================================================
        # 🎤 TRATAMENTO DE MÍDIA (ÁUDIO & TEXTO)
        # ======================================================================
        message_content = msg_data.get('message', {})
        user_msg = None

        # 1. Verifica se é Áudio
        if message_content.get('audioMessage'):
            try:
                print(f"🎤 Áudio recebido de {clean_number}. Buscando dados...")
                
                audio_data = None
                
                # TENTATIVA A: Pega BASE64 direto (se vier)
                audio_base64 = msg_data.get('base64') or message_content.get('audioMessage', {}).get('base64')
                
                if audio_base64:
                    audio_data = base64.b64decode(audio_base64)
                
                # TENTATIVA B: Se não tem Base64, BAIXA DA URL (Correção para o seu erro)
                else:
                    audio_url = message_content.get('audioMessage', {}).get('url')
                    if audio_url:
                        print(f"🌐 Baixando áudio da URL...")
                        # Passamos a API KEY no header para garantir permissão
                        headers_dl = {"apikey": EVOLUTION_API_KEY}
                        response = requests.get(audio_url, headers=headers_dl, timeout=15)
                        
                        if response.status_code == 200:
                            audio_data = response.content
                        else:
                            print(f"❌ Erro ao baixar áudio da URL: Status {response.status_code}")

                # Se conseguiu os dados (por A ou B), processa
                if not audio_data:
                     user_msg = "[Áudio recebido, mas falha no download dos dados]"
                else:
                    # Salva arquivo temporário
                    temp_path = f"/tmp/audio_{clean_number}_{int(time.time())}.ogg"
                    
                    with open(temp_path, 'wb') as f:
                        f.write(audio_data)
                    
                    # Transcreve (Passando o ID para cobrar token certo)
                    transcricao = transcrever_audio_gemini(temp_path, contact_id=clean_number)
                    user_msg = f"[Transcrição de Áudio]: {transcricao}"
                    
                    # Limpeza
                    try: os.remove(temp_path)
                    except: pass

            except Exception as e:
                print(f"❌ Falha crítica no processamento de áudio: {e}")
                user_msg = "[Erro técnico ao ler áudio]"

        # 2. Se não for áudio, tenta Texto Normal
        if not user_msg:
            user_msg = message_content.get('conversation') or \
                       message_content.get('extendedTextMessage', {}).get('text')

        # 3. Se ainda estiver vazio, ignora
        if not user_msg:
            return jsonify({"status": "ignored_no_text"}), 200

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
    
thread_followup = threading.Thread(target=sistema_followup_vendas, daemon=True)
thread_followup.start()

if __name__ == '__main__':
    print("🚚 Sistema de Vendas Grupar Iniciado...")
    app.run(host='0.0.0.0', port=8000)