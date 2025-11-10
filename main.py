import google.generativeai as genai
import requests
import os
import pytz 
from flask import Flask, request, jsonify
from datetime import datetime
from dotenv import load_dotenv
import base64
import threading
from pymongo import MongoClient
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from apscheduler.schedulers.background import BackgroundScheduler
import json 

# --- CONFIGURAÇÃO DO CLIENTE (DO CÓDIGO ANTIGO) ---
CLIENT_NAME = "Neuro Soluções em Tecnologia"
RESPONSIBLE_NUMBER = "554898389781" # <-- MANTIDO DO CÓDIGO ANTIGO
# --- FIM DA CONFIGURAÇÃO ---

load_dotenv()
EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_DB_URI = os.environ.get("MONGO_DB_URI")

# --- MELHORIA: Sistema de Buffer (DO CÓDIGO ATUAL) ---
message_buffer = {}
message_timers = {}
BUFFER_TIME_SECONDS = 8 
# --- FIM DA MELHORIA ---

try:
    client = MongoClient(MONGO_DB_URI)
    db_name = CLIENT_NAME.lower().replace(" ", "_").replace("-", "_")
    db = client[db_name] 
    conversation_collection = db.conversations
    
    print(f"✅ Conectado ao MongoDB para o cliente: '{CLIENT_NAME}' no banco de dados '{db_name}'")
except Exception as e:
    print(f"❌ ERRO: Não foi possível conectar ao MongoDB. Erro: {e}")

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"AVISO: A chave de API do Google não foi configurada corretamente. Erro: {e}")
else:
    print("AVISO: A variável de ambiente GEMINI_API_KEY não foi definida.")

modelo_ia = None
try:
    modelo_ia = genai.GenerativeModel('gemini-2.5-flash')
    print("✅ Modelo do Gemini (gemini-2.5-flash) inicializado com sucesso.")
except Exception as e:
    print(f"❌ ERRO: Não foi possível inicializar o modelo do Gemini. Verifique sua API Key. Erro: {e}")

# --- MELHORIA: Funções de DB 'Stateless' (DO CÓDIGO ATUAL) ---
def append_message_to_db(contact_id, role, text, message_id=None):
    """Salva uma única mensagem no histórico do DB."""
    try:
        tz = pytz.timezone('America/Sao_Paulo')
        now = datetime.now(tz)
        entry = {'role': role, 'text': text, 'ts': now.isoformat()}
        if message_id:
            entry['msg_id'] = message_id

        conversation_collection.update_one(
            {'_id': contact_id},
            {'$push': {'history': entry}, '$setOnInsert': {'created_at': now}},
            upsert=True
        )
        return True
    except Exception as e:
        print(f"❌ Erro ao append_message_to_db: {e}")
        return False

def save_conversation_to_db(contact_id, sender_name, customer_name, tokens_used):
    """Salva metadados (nomes, tokens) no MongoDB."""
    try:
        update_payload = {
            'sender_name': sender_name,
            'last_interaction': datetime.now()
        }
        if customer_name:
            update_payload['customer_name'] = customer_name

        conversation_collection.update_one(
            {'_id': contact_id},
            {
                '$set': update_payload,
                '$inc': {'total_tokens_consumed': tokens_used}
            },
            upsert=True
        )
    except Exception as e:
        print(f"❌ Erro ao salvar metadados da conversa no MongoDB para {contact_id}: {e}")

def load_conversation_from_db(contact_id):
    """Carrega o histórico de uma conversa do MongoDB, ordenando por timestamp."""
    try:
        result = conversation_collection.find_one({'_id': contact_id})
        if result:
            history = result.get('history', [])
            history_sorted = sorted(history, key=lambda m: m.get('ts', ''))
            result['history'] = history_sorted
            print(f"🧠 Histórico anterior encontrado e carregado para {contact_id} ({len(history_sorted)} entradas).")
            return result
    except Exception as e:
        print(f"❌ Erro ao carregar conversa do MongoDB para {contact_id}: {e}")
    return None
# --- FIM DAS FUNÇÕES DE DB ---

def get_last_messages_summary(history, max_messages=4):
    """Formata as últimas mensagens de um histórico para um resumo legível, ignorando prompts do sistema."""
    summary = []
    relevant_history = history[-max_messages:]
    
    for message in relevant_history:
        role = "Cliente" if message.get('role') == 'user' else "Bot"
        text = message.get('text', '').strip()

        if role == "Cliente" and text.startswith("A data e hora atuais são:"):
            continue 
        # --- ADAPTADO: Texto de 'ack' do bot da Neuro Soluções ---
        if role == "Bot" and text.startswith("Entendido. A Regra de Ouro"):
            continue 
            
        summary.append(f"*{role}:* {text}")
        
    if not summary:
        user_messages = [msg.get('text') for msg in history if msg.get('role') == 'user' and not msg.get('text', '').startswith("A data e hora atuais são:")]
        if user_messages:
            return f"*Cliente:* {user_messages[-1]}"
        else:
            return "Nenhum histórico de conversa encontrado."
            
    return "\n".join(summary)

def gerar_resposta_ia(contact_id, sender_name, user_message, known_customer_name): 
    """
    (VERSÃO FINAL - QUALIDADE MÁXIMA + MEMÓRIA TOTAL)
    Usa 'system_instruction' para inteligência E carrega o histórico completo para memória.
    """
    global modelo_ia # Pega o modelo global (gemini-1.5-flash)

    if not modelo_ia:
        return "Desculpe, estou com um problema interno (modelo IA não carregado)."

    print(f"🧠 Lendo o estado do DB para {contact_id}...")
    convo_data = load_conversation_from_db(contact_id)
    old_history = []
    
    if convo_data:
        # A lógica para buscar o nome (que não é do histórico) funciona perfeitamente
        known_customer_name = convo_data.get('customer_name', known_customer_name) 
        if 'history' in convo_data:
            
            # --- MEMÓRIA TOTAL (BOLA DE NEVE) ---
            # Carrega o histórico COMPLETO, sem truncamento.
            history_full = convo_data.get('history', []) 
            print(f"📜 MEMÓRIA LONGA ATIVA. Carregando histórico completo ({len(history_full)} msgs).")
            # --- FIM ---
            
            # Filtra o prompt antigo (boa prática, caso ainda exista no DB)
            history_from_db = [msg for msg in history_full if not msg.get('text', '').strip().startswith("A data e hora atuais são:")]
            
            for msg in history_from_db:
                role = msg.get('role', 'user')
                if role == 'assistant':
                    role = 'model'
                
                if 'text' in msg:
                    old_history.append({
                        'role': role,
                        'parts': [msg['text']]
                    })
    
    if known_customer_name:
        print(f"👤 Cliente já conhecido pelo DB: {known_customer_name}")

    # (Lógica de Fuso Horário)
    try:
        fuso_horario_local = pytz.timezone('America/Sao_Paulo')
        agora_local = datetime.now(fuso_horario_local)
        horario_atual = agora_local.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # --- PROMPT DE NOME DINÂMICO ---
    # Esta é a lógica que garante que ele pergunte o nome se não souber.
    prompt_name_instruction = ""
    if known_customer_name:
        # Se JÁ SABE o nome, a instrução é simples:
        final_user_name_for_prompt = known_customer_name
        prompt_name_instruction = f"O nome do usuário com quem você está falando é: {final_user_name_for_prompt}. Trate-o por este nome."
    else:
        # Se NÃO SABE o nome, a instrução é a regra de captura:
        final_user_name_for_prompt = sender_name
        prompt_name_instruction = f"""
            REGRA CRÍTICA - CAPTURA DE NOME INTELIGENTE (PRIORIDADE MÁXIMA):
              (Esta regra SÓ se aplica se a REGRA DE OURO de intervenção não for acionada primeiro)
              Seu nome é {{Lyra}} e você é atendente da {{Mengatto Estratégia Digital}}.
              Seu primeiro objetivo é sempre descobrir o nome real do cliente, pois o nome de contato ('{sender_name}') pode ser um apelido. No entanto, você deve fazer isso de forma natural.
              1. Se a primeira mensagem do cliente for um simples cumprimento (ex: "oi", "boa noite"), peça o nome dele de forma direta e educada.
              2. Se a primeira mensagem do cliente já contiver uma pergunta (ex: "oi, qual o preço?", "quero saber como funciona"), você deve:
                 - Primeiro, acalmar o cliente dizendo que já vai responder.
                 - Em seguida, peça o nome para personalizar o atendimento.
                 - *IMPORTANTE*: Você deve guardar a pergunta original do cliente na memória.
              3. Quando o cliente responder com o nome dele (ex: "Meu nome é Marcos"), sua próxima resposta DEVE OBRIGATORIAMENTE:
                 - Começar com a tag: [NOME_CLIENTE]O nome do cliente é: [Nome Extraído].
                 - Agradecer ao cliente pelo nome.
                 - *RESPONDER IMEDIATAMENTE à pergunta original que ele fez no início da conversa.* Não o faça perguntar de novo.
              4. Se não tiver historico de converssa anterior faça a aprensetação de forma amigavel e dinamica, se apresente, apresente a empresa, e continue para saber o nome. 
            """
    # --- FIM DO PROMPT DE NOME ---
    
    # --- SYSTEM INSTRUCTION (O "TREINAMENTO") ---
    # Aqui colocamos seu prompt gigante inteiro, incluindo a instrução de nome dinâmica
    prompt_inicial_de_sistema = f"""
            A data e hora atuais são: {horario_atual}.
            
            =====================================================
            🆘 REGRA DE OURO: ANÁLISE DE INTERVENÇÃO (PRIORIDADE ABSOLUTA)
            =====================================================
            - SUA TAREFA MAIS IMPORTANTE é identificar se o cliente quer falar com "Lucas" (o proprietário).
            - Se a mensagem do cliente contiver QUALQUER PEDIDO para falar com "Lucas" (ex: "quero falar com o Lucas", "falar com o dono", "chama o Lucas", "o Lucas está?"), esta regra ANULA TODAS AS OUTRAS.
            
            1.  **CENÁRIO 1: NOME + INTERVENÇÃO JUNTOS**
                - Se o nome AINDA NÃO FOI CAPTURADO.
                - E o cliente responder com o nome E o pedido de intervenção na MESMA FRASE (ex: "Meu nome é Marcos e quero falar com o Lucas").
                - Você DEVE capturar o nome E acionar a intervenção SIMULTANEAMENTE.
                - **Resposta Correta (EXATA):** `[NOME_CLIENTE]O nome do cliente é: Marcos. [HUMAN_INTERVENTION] Motivo: Cliente solicitou falar com o Lucas.`
                
            2.  **CENÁRIO 2: APENAS INTERVENÇÃO**
                - Se o cliente (com nome já conhecido ou não) pedir para falar com o Lucas.
                - **Resposta Correta (EXATA):** `[HUMAN_INTERVENTION] Motivo: Cliente solicitou falar com o Lucas.`

            3.  **CENÁRIO 3: EXCEÇÃO CRÍTICA (FALSO POSITIVO)**
                - Se o cliente APENAS se apresentar com o nome "Lucas" (ex: "Meu nome é Lucas", ou "Lucas").
                - ISSO **NÃO** É UMA INTERVENÇÃO. É uma apresentação.
                - **Resposta Correta (se o nome não foi capturado):** `[NOME_CLIENTE]O nome do cliente é: Lucas. Prazer em conhecê-lo, Lucas! Como posso te ajudar?`
            =====================================================
            
            {prompt_name_instruction}
            
            Dever : Potencializar os nossos planos entendendo como pode ajudar o clinte, se quer saber sobre a empresa ou falar com o Lucas(Proprietario).
            Missão : Agendar um horario para reunião com o proprietario. 
            
            =====================================================
            🏷️ IDENTIDADE DO ATENDENTE
            =====================================================
            nome: {{Lyra}}
            sexo: {{Feminina}}
            idade: {{40}}
            função: {{Atendente, vendedora, especialista em TI e machine learning}} 
            papel: {{Atender o cliente de forma profissional e amigável, entender sua necessidade, oferecer soluções personalizadas, tirar dúvidas, vender o plano ideal, enviar catálogos e agendar horários quando necessário.}} 
            =====================================================
            🏢 IDENTIDADE DA EMPRESA
            =====================================================
            nome da empresa: {{Neuro Soluções em Tecnologia}}
            setor: {{Tecnologia e Automação}} 
            missão: {{Facilitar e organizar as empresas de clientes por meio de soluções inteligentes e automação.}}
            valores: {{Organização, transparência, persistência e ascensão.}}
            horário de atendimento: {{De segunda a sexta, das 8:00 às 18:00.}}
            endereço: {{R. Pioneiro Alfredo José da Costa, 157 - Jardim Alvorada, Maringá - PR, 87035-270}}
            =====================================================
            🏛️ HISTÓRIA DA EMPRESA
            =====================================================
            {{Fundada em Maringá - PR, em 2025, a Neuro Soluções em Tecnologia nasceu com o propósito de unir inovação e praticidade. Criada por profissionais apaixonados por tecnologia e automação, a empresa cresceu ajudando empreendedores a otimizar processos, economizar tempo e aumentar vendas por meio de chatbots e sistemas inteligentes.}}
            =====================================================
            ℹ️ INFORMAÇÕES GERAIS
            =====================================================
            público-alvo: {{Empresas, empreendedores e prestadores de serviço que desejam automatizar atendimentos e integrar inteligência artificial ao seu negócio.}}
            diferencial: {{Atendimento personalizado, chatbots sob medida e integração total com o WhatsApp e ferramentas de IA.}}
            tempo de mercado: {{Desde 2025}}
            slogan: {{O futuro é agora!}}
            =====================================================
            💼 SERVIÇOS / CARDÁPIO
            =====================================================
            - Plano Atendente: {{Atendente personalizada, configurada conforme a necessidade do cliente. Pode atuar de forma autônoma, com intervenção humana ou bifurcação de mensagens.}}
            - Plano Secretário: {{Agendamento Inteligente, Avisos Automáticos e Agenda Integrada.}}
            - Plano Premium: {{Em construção.}}
            =====================================================
            💰 PLANOS E VALORES
            =====================================================
            Instalação: {{R$250,00 taxa única}} para setup inicial do projeto e requisitos da IA. 
            Plano Atendente: {{R$400,00 mensal}}
            Plano Secretário: {{R$700,00 mensal}}
            Plano Avançado: {{Em análise}}
            observações: {{Valores podem variar conforme personalização ou integrações extras.}}
            =====================================================
            🧭 COMPORTAMENTO E REGRAS DE ATENDIMENTO
            =====================================================
            - Ações: Seja profissional, empática, natural, objetiva e prestativa. Use frases curtas e diretas, mantenha tom positivo e humano. Entenda a necessidade do cliente, utilize técnicas de venda consultiva, aplique gatilhos mentais com sutileza (autoridade, escassez, reciprocidade, afinidade), e conduza a conversa para o fechamento de forma leve, parecendo que está ajudando, não vendendo.
            - Linguagem adaptável (modo camaleão): ajuste o tom conforme o cliente — simpático e leve com conversadores, direto com apressados, técnico com desconfiados, e descontraído com clientes informais.
            - Estratégia de venda: Sempre inicie entendendo a dor ou necessidade do cliente, recomende a melhor solução como um “especialista” que orienta com confiança (como um médico que indica o tratamento ideal), e finalize de forma natural e segura.
            - Não fazer: Não invente informações, não discuta, não compartilhe dados pessoais, não responda fora de contexto e evite encerrar sem oferecer uma solução. Como último recurso, ofereça uma semana grátis antes da despedida.
            - Use apenas 1 ou 2 paragrafos no maximo, evite blocos grandes, normalmete o ultimo sempre sera uma pergunta aberta!
            - Apenas fale o necessario sem ser rude. 
            =====================================================
            ⚙️ PERSONALIDADE DO ATENDENTE
            =====================================================
            - Tom de voz: {{profissional, alegre e amigável, ajustável conforme o cliente.}}
            - Estilo: {{Secretária inteligente, observadora, esperta e proativa — transmite segurança e conhecimento técnico, com leveza e empatia.}}
            - Emojis: {{usar com moderação — máximo 2 por mensagem.}}
            - Identidade comportamental: {{Parece humana e confiante. Mantém equilíbrio entre simpatia e eficiência. Atua como especialista que realmente quer ajudar.}}
            - Mentalidade: {{“Não vendo apenas um serviço; resolvo o problema do cliente de forma personalizada.”}}
            =====================================================
            📜 ABERTURA PADRÃO DE ATENDIMENTO
            =====================================================
            *Use apenas quando não tiver historico de converssa e for a primeira vez que entra em contato com o usuario 
            👋 Olá! Tudo bem?  
            Eu sou **Lyra**, da **Neuro Soluções em Tecnologia**.  
            Seja muito bem-vindo(a)! Pode me contar o que você está precisando hoje? Assim eu já te ajudo da melhor forma. Ou se quiser falar com o Lucas me avisa! 😊
            
            =====================================================
            🧩 TÉCNICAS DE OBJEÇÕES E CONVERSÃO
            =====================================================
            *Não fique repetindo as mesmas tecnicas para o mesmo cliente. 
            A função da Lyra é compreender o motivo da dúvida ou recusa e usar **técnicas inteligentes de objeção**, sempre de forma natural, empática e estratégica — nunca forçada ou mecânica.  
            Essas técnicas devem ser aplicadas apenas **quando fizerem sentido no contexto** da conversa, com base na necessidade e comportamento do cliente.
            🎯 **OBJETIVO:** Transformar objeções em diálogo e mostrar valor de forma consultiva, até o fechamento do agendameto .
            ---
            ### 💬 1. QUANDO O CLIENTE RECLAMA DO PREÇO
            - Mantenha calma e empatia, e pergunte com interesse genuíno:
            > “Entendo perfeitamente! Posso te perguntar, você achou o valor justo pelo que o sistema entrega?”
            - Depois, demonstre o valor agregado:
            > “Lembrando que aqui não é só um chatbot — é **atendimento, automação e venda 24h**, com suporte personalizado e tecnologia de ponta. Enquanto você trabalha, eu atendo sem erros. 😉”
            - Se o cliente ainda demonstrar resistência:
            > “Você investe em marketing? Porque o que mais acontece é pessoas chamarem fora do horário — e com a IA, **nenhum cliente fica sem resposta**.”
            ---
            ### 💡 2. QUANDO O CLIENTE DIZ “VOU PENSAR”
            - Não pressione, mas mantenha o interesse vivo:
            > “Perfeito, é bom pensar mesmo! Posso te perguntar o que você gostaria de analisar melhor? Assim vejo se consigo te ajudar com alguma dúvida antes.”
            - Se ele não souber responder:
            > “Muitos clientes me dizem isso quando ainda estão comparando valores, mas quando percebem o tempo que o sistema economiza e a credibilidade que passa, percebem que o retorno vem rápido.”
            - E complete com gatilho de valor:
            > “Se a gente dividir o valor do plano por 30 dias, ele sai menos que uma refeição por dia — e trabalha por você 24 horas.”  
            ---
            ### 🧠 3. QUANDO O CLIENTE DEMONSTRA DESINTERESSE OU DÚVIDA
            - Tente entender o motivo real:
            > “Posso te perguntar o que fez você achar que talvez não seja o momento certo? Assim vejo se faz sentido pra sua realidade.”  
            - Faça perguntas estratégicas:
            > “Você trabalha e atende sozinha?”  
            > “Já teve problemas com mal atendimento ou respostas atrasadas?”  
            > “Quanto tempo, em média, seus clientes esperam uma resposta quando você está ocupada ou fora do horário?”
            - Depois de ouvir, conecte com a solução:
            > “O sistema resolve exatamente isso — ele **atende rápido, sem erro e com empatia**, garantindo que nenhum cliente fique esperando.”
            ---
            ### ⚙️ 4. QUANDO O CLIENTE COMPARA COM OUTROS OU ACHA DESNECESSÁRIO
            - Mostre diferenciação técnica e valor:
            > “Entendo, mas vale destacar que aqui usamos **as tecnologias mais avançadas de IA e machine learning**, e o suporte é 100% personalizado — diferente dos sistemas prontos e genéricos do mercado.”
            - Se o cliente disser que outro é mais barato:
            > “Sim, pode até ter preço menor, mas não entrega o mesmo resultado. A diferença está na performance: nossos clientes fecham mais rápido, e seus concorrentes muitas vezes nem têm tempo de atender — porque **você já terá fechado com o seu cliente.** 😎”
            ---
            ### 💬 5. QUANDO O CLIENTE NÃO VÊ VALOR IMEDIATO
            - Reforce o retorno sobre o investimento:
            > “Pensa assim: se o sistema fechar apenas um cliente novo por mês, ele já se paga — e ainda sobra. É investimento, não gasto.”
            - Mostre o impacto real:
            > “Enquanto você dorme, ele continua atendendo. Enquanto você trabalha, ele já inicia novas conversas. Isso é **tempo transformado em resultado.**”
            ---
            ### ⚡ DICAS GERAIS DE CONDUTA
            - Use apenas **uma ou duas técnicas por conversa**, de forma natural.  
            - Evite repetir a mesma justificativa — varie conforme a reação do cliente.  
            - Mantenha o tom calmo, positivo e consultivo — nunca defensivo.  
            - Finalize sempre reforçando o valor e o benefício real.  
            💬 Exemplo de fechamento leve:
            > “Posso já reservar a sua vaga pra ativar hoje? Assim você já aproveita o suporte completo e começa a economizar tempo ainda essa semana. 😉”

            - Final : Se nada der certo antes de se despedir ofereça 1 semana gratis.

            =====================================================
            PRONTO PARA ATENDER O CLIENTE
            =====================================================
            Quando o cliente enviar uma mensagem, inicie o atendimento com essa apresentação profissional e amigável.  
            Adapte o tom conforme o comportamento do cliente, mantenha foco em entender a necessidade e conduza naturalmente até o fechamento da venda.  
            Lembre-se: o objetivo é vender ajudando — com empatia, segurança e inteligência.
        """

    try:
        # 1. Inicializa o modelo COM a instrução de sistema
        modelo_com_sistema = genai.GenerativeModel(
            modelo_ia.model_name, # Reutiliza o nome do modelo global ('gemini-1.5-flash')
            system_instruction=prompt_inicial_de_sistema 
        )
        
        # 2. Inicia o chat SÓ com o histórico (COMPLETO, para memória longa)
        chat_session = modelo_com_sistema.start_chat(history=old_history) 
        
        customer_name_to_save = known_customer_name

        print(f"Enviando para a IA: '{user_message}' (De: {sender_name})")
        
        # (O resto da função: contagem de tokens, envio, extração de nome, etc... é IDÊNTICO)
        
        try:
            # Conta tokens do (histórico completo + nova mensagem)
            input_tokens = modelo_com_sistema.count_tokens(chat_session.history + [{'role':'user', 'parts': [user_message]}]).total_tokens
        except Exception:
            input_tokens = 0

        resposta = chat_session.send_message(user_message)
        
        try:
            output_tokens = modelo_com_sistema.count_tokens(resposta.text).total_tokens
        except Exception:
            output_tokens = 0
            
        total_tokens_na_interacao = input_tokens + output_tokens
        
        if total_tokens_na_interacao > 0:
            print(f"📊 Consumo de Tokens (Nesta Interação): Total={total_tokens_na_interacao}")
        
        ai_reply = resposta.text

        if ai_reply.strip().startswith("[NOME_CLIENTE]"):
            print("📝 Tag [NOME_CLIENTE] detectada. Extraindo e salvando nome...")
            try:
                name_part = ai_reply.split("[HUMAN_INTERVENTION]")[0]
                full_response_part = name_part.split("O nome do cliente é:")[1].strip()
                extracted_name = full_response_part.split('.')[0].strip()
                extracted_name = extracted_name.split(' ')[0].strip() 
                
                conversation_collection.update_one(
                    {'_id': contact_id},
                    {'$set': {'customer_name': extracted_name}},
                    upsert=True
                )
                customer_name_to_save = extracted_name
                print(f"✅ Nome '{extracted_name}' salvo para o cliente {contact_id}.")

                if "[HUMAN_INTERVENTION]" in ai_reply:
                    ai_reply = "[HUMAN_INTERVENTION]" + ai_reply.split("[HUMAN_INTERVENTION]")[1]
                else:
                    start_of_message_index = full_response_part.find(extracted_name) + len(extracted_name)
                    ai_reply = full_response_part[start_of_message_index:].lstrip('.!?, ').strip()
            except Exception as e:
                print(f"❌ Erro ao extrair o nome da tag: {e}")
                ai_reply = ai_reply.replace("[NOME_CLIENTE]", "").strip()

        if not ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
             save_conversation_to_db(contact_id, sender_name, customer_name_to_save, total_tokens_na_interacao)
        
        return ai_reply
    
    except Exception as e:
        print(f"❌ Erro ao comunicar com a API do Gemini: {e}")
        return "Desculpe, estou com um problema técnico no momento (IA_GEN_FAIL). Por favor, tente novamente em um instante."
    
def transcrever_audio_gemini(caminho_do_audio):
    global modelo_ia 
    if not modelo_ia:
        print("❌ Modelo de IA não inicializado. Impossível transcrever.")
        return None
    print(f"🎤 Enviando áudio '{caminho_do_audio}' para transcrição no Gemini...")
    try:
        audio_file = genai.upload_file(
            path=caminho_do_audio, 
            mime_type="audio/ogg"
        )
        response = modelo_ia.generate_content(["Por favor, transcreva o áudio a seguir.", audio_file])
        genai.delete_file(audio_file.name)
        
        if response.text:
            print(f"✅ Transcrição recebida: '{response.text}'")
            return response.text
        else:
            print("⚠️ A IA não retornou texto para o áudio. Pode ser um áudio sem falas.")
            return None
    except Exception as e:
        print(f"❌ Erro ao transcrever áudio com Gemini: {e}")
        return None

# --- MELHORIA: Função de envio robusta (DO CÓDIGO ATUAL) ---
def send_whatsapp_message(number, text_message):
    """Envia uma mensagem de texto via Evolution API, corrigindo a URL dinamicamente."""
    
    INSTANCE_NAME = "chatbot" # Nome da sua instância
    
    clean_number = number.split('@')[0]
    payload = {"number": clean_number, "textMessage": {"text": text_message}}
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}

    base_url = EVOLUTION_API_URL
    api_path = f"/message/sendText/{INSTANCE_NAME}"
    
    final_url = ""
    
    if not base_url:
        print("❌ ERRO: EVOLUTION_API_URL não está definida no .env")
        return

    if base_url.endswith(api_path):
        final_url = base_url
    elif base_url.endswith('/'):
        final_url = base_url[:-1] + api_path
    else:
        final_url = base_url + api_path

    try:
        print(f"✅ Enviando resposta para a URL: {final_url} (Destino: {clean_number})")
        response = requests.post(final_url, json=payload, headers=headers)
        
        if response.status_code < 400:
            print(f"✅ Resposta da IA enviada com sucesso para {clean_number}\n")
        else:
            print(f"❌ ERRO DA API EVOLUTION ao enviar para {clean_number}: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Erro de CONEXÃO ao enviar mensagem para {clean_number}: {e}")

def gerar_e_enviar_relatorio_semanal():
    """Calcula um RESUMO do uso de tokens e envia por e-mail usando SendGrid."""
    print(f"🗓️ Gerando relatório semanal para o cliente: {CLIENT_NAME}...")
    
    SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY')
    EMAIL_RELATORIOS = os.environ.get('EMAIL_RELATORIOS')

    if not all([SENDGRID_API_KEY, EMAIL_RELATORIOS]):
        print("⚠️ Variáveis SENDGRID_API_KEY e EMAIL_RELATORIOS não configuradas. Relatório não pode ser enviado.")
        return

    hoje = datetime.now()
    
    try:
        usuarios_do_bot = list(conversation_collection.find({}))
        numero_de_contatos = len(usuarios_do_bot)
        total_geral_tokens = 0
        media_por_contato = 0

        if numero_de_contatos > 0:
            for usuario in usuarios_do_bot:
                total_geral_tokens += usuario.get('total_tokens_consumed', 0)
            media_por_contato = total_geral_tokens / numero_de_contatos
        
        corpo_email_texto = f"""
        Relatório de Consumo Acumulado do Cliente: '{CLIENT_NAME}'
        Data do Relatório: {hoje.strftime('%d/%m/%Y')}

        --- RESUMO GERAL DE USO ---

        👤 Número de Contatos Únicos: {numero_de_contatos}
        🔥 Consumo Total de Tokens (Acumulado): {total_geral_tokens}
        📊 Média de Tokens por Contato: {media_por_contato:.0f}

        ---------------------------
        Atenciosamente,
        Seu Sistema de Monitoramento.
        """

        message = Mail(
            from_email=EMAIL_RELATORIOS,
            to_emails=EMAIL_RELATORIOS,
            subject=f"Relatório Semanal de Tokens - {CLIENT_NAME} - {hoje.strftime('%d/%m')}",
            plain_text_content=corpo_email_texto
        )
        
        sendgrid_client = SendGridAPIClient(SENDGRID_API_KEY)
        response = sendgrid_client.send(message)
        
        if response.status_code == 202:
             print(f"✅ Relatório semanal para '{CLIENT_NAME}' enviado com sucesso via SendGrid!")
        else:
             print(f"❌ Erro ao enviar e-mail via SendGrid. Status: {response.status_code}. Body: {response.body}")

    except Exception as e:
        print(f"❌ Erro ao gerar ou enviar relatório para '{CLIENT_NAME}': {e}")

# --- MELHORIA: Inicialização Global (DO CÓDIGO ATUAL) ---
scheduler = BackgroundScheduler(daemon=True, timezone='America/Sao_Paulo')
scheduler.start()

app = Flask(__name__)
processed_messages = set() 

@app.route('/webhook', methods=['POST'])
def receive_webhook():
    """
    (VERSÃO MELHORADA - DO CÓDIGO ATUAL)
    Recebe mensagens do WhatsApp e as coloca no buffer.
    """
    data = request.json
    print(f"📦 DADO BRUTO RECEBIDO NO WEBHOOK: {data}")

    event_type = data.get('event')
    
    if event_type and event_type != 'messages.upsert':
        print(f"➡️  Ignorando evento: {event_type} (não é uma nova mensagem)")
        return jsonify({"status": "ignored_event_type"}), 200

    try:
        message_data = data.get('data', {}) 
        if not message_data:
             message_data = data
             
        key_info = message_data.get('key', {})
        if not key_info:
            print("➡️ Evento sem 'key'. Ignorando.")
            return jsonify({"status": "ignored_no_key"}), 200

        if key_info.get('fromMe'):
            sender_number_full = key_info.get('remoteJid')
            if not sender_number_full:
                return jsonify({"status": "ignored_from_me_no_sender"}), 200
            
            clean_number = sender_number_full.split('@')[0]
            
            if clean_number != RESPONSIBLE_NUMBER:
                print(f"➡️  Mensagem do próprio bot ignorada (remetente: {clean_number}).")
                return jsonify({"status": "ignored_from_me"}), 200
            
            print(f"⚙️  Mensagem do próprio bot PERMITIDA (é um comando do responsável: {clean_number}).")

        message_id = key_info.get('id')
        if not message_id:
            return jsonify({"status": "ignored_no_id"}), 200

        if message_id in processed_messages:
            print(f"⚠️ Mensagem {message_id} já processada, ignorando.")
            return jsonify({"status": "ignored_duplicate"}), 200
        processed_messages.add(message_id)
        if len(processed_messages) > 1000:
            processed_messages.clear()

        handle_message_buffering(message_data)
        
        return jsonify({"status": "received"}), 200

    except Exception as e:
        print(f"❌ Erro inesperado no webhook: {e}")
        print("DADO QUE CAUSOU ERRO:", data)
        return jsonify({"status": "error"}), 500

@app.route('/', methods=['GET'])
def health_check():
    return f"Estou vivo! ({CLIENT_NAME} Bot)", 200 # <-- Nome do cliente adaptado

# --- MELHORIA: Funções de Buffer Otimizadas (DO CÓDIGO ATUAL) ---
def handle_message_buffering(message_data):
    """
    Agrupa mensagens de texto e processa áudio imediatamente.
    """
    global message_buffer, message_timers, BUFFER_TIME_SECONDS
    
    try:
        key_info = message_data.get('key', {})
        sender_number_full = key_info.get('senderPn') or key_info.get('participant') or key_info.get('remoteJid')
        if not sender_number_full or sender_number_full.endswith('@g.us'):
            return

        clean_number = sender_number_full.split('@')[0]
        
        message = message_data.get('message', {})
        user_message_content = None
        
        # --- Processa ÁUDIO imediatamente ---
        if message.get('audioMessage'):
            print("🎤 Áudio recebido, processando imediatamente (sem buffer)...")
            threading.Thread(target=process_message_logic, args=(message_data, None)).start()
            return
        
        # --- Processa TEXTO no buffer ---
        if message.get('conversation'):
            user_message_content = message['conversation']
        elif message.get('extendedTextMessage'):
            user_message_content = message['extendedTextMessage'].get('text')
        
        if not user_message_content:
            print("➡️  Mensagem sem conteúdo de texto ignorada pelo buffer.")
            return

        if clean_number not in message_buffer:
            message_buffer[clean_number] = []
        message_buffer[clean_number].append(user_message_content)
        
        print(f"📥 Mensagem adicionada ao buffer de {clean_number}: '{user_message_content}'")

        if clean_number in message_timers:
            message_timers[clean_number].cancel()

        timer = threading.Timer(
            BUFFER_TIME_SECONDS, 
            _trigger_ai_processing, 
            args=[clean_number, message_data] 
        )
        message_timers[clean_number] = timer
        timer.start()
        print(f"⏰ Buffer de {clean_number} resetado. Aguardando {BUFFER_TIME_SECONDS}s...")

    except Exception as e:
        print(f"❌ Erro no 'handle_message_buffering': {e}")
            
def _trigger_ai_processing(clean_number, last_message_data):
    """
    Função chamada pelo Timer. Junta as mensagens e chama a 'process_message_logic'.
    """
    global message_buffer, message_timers
    
    if clean_number not in message_buffer:
        return 

    messages_to_process = message_buffer.pop(clean_number, [])
    if clean_number in message_timers:
        del message_timers[clean_number]
        
    if not messages_to_process:
        return

    full_user_message = ". ".join(messages_to_process)
    
    print(f"⚡️ DISPARANDO IA para {clean_number} com mensagem agrupada: '{full_user_message}'")

    threading.Thread(target=process_message_logic, args=(last_message_data, full_user_message)).start()
# --- FIM DAS FUNÇÕES DE BUFFER ---

# --- MELHORIA: Comando do Responsável (DO CÓDIGO ATUAL) ---
# (Substitua sua função 'handle_responsible_command' inteira por esta)
def handle_responsible_command(message_content, responsible_number):
    """
    Processa comandos enviados pelo número do responsável.
    INCLUI: 'bot on', 'bot off' e 'ok <numero>'
    """
    print(f"⚙️  Processando comando do responsável: '{message_content}'")
    
    command_lower = message_content.lower().strip()
    command_parts = command_lower.split()

    # --- COMANDO LIGA/DESLIGA ---
    if command_lower == "bot off":
        try:
            conversation_collection.update_one(
                {'_id': 'BOT_STATUS'},
                {'$set': {'is_active': False}},
                upsert=True
            )
            send_whatsapp_message(responsible_number, "✅ *Bot PAUSADO.* O bot não responderá a nenhum cliente até você enviar 'bot on'.")
            return True
        except Exception as e:
            send_whatsapp_message(responsible_number, f"❌ Erro ao pausar o bot: {e}")
            return True

    elif command_lower == "bot on":
        try:
            conversation_collection.update_one(
                {'_id': 'BOT_STATUS'},
                {'$set': {'is_active': True}},
                upsert=True
            )
            send_whatsapp_message(responsible_number, "✅ *Bot REATIVADO.* O bot está respondendo aos clientes normally.")
            return True
        except Exception as e:
            send_whatsapp_message(responsible_number, f"❌ Erro ao reativar o bot: {e}")
            return True
    # --- FIM DO COMANDO LIGA/DESLIGA ---

    # --- Comando 'ok <numero>' ---
    if len(command_parts) == 2 and command_parts[0] == "ok":
        customer_number_to_reactivate = command_parts[1].replace('@s.whatsapp.net', '').strip()
        
        try:
            customer = conversation_collection.find_one({'_id': customer_number_to_reactivate})

            if not customer:
                send_whatsapp_message(responsible_number, f"⚠️ *Atenção:* O cliente com o número `{customer_number_to_reactivate}` não foi encontrado no banco de dados.")
                return True 

            result = conversation_collection.update_one(
                {'_id': customer_number_to_reactivate},
                {'$set': {'intervention_active': False}}
            )

            # O cache de sessão não é mais usado, então não precisamos limpá-lo

            if result.modified_count > 0:
                send_whatsapp_message(responsible_number, f"✅ Atendimento automático reativado para o cliente `{customer_number_to_reactivate}`.")
                # --- MENSAGEM ADAPTADA (DO CÓDIGO 2) ---
                send_whatsapp_message(customer_number_to_reactivate, "Oi sou eu a Lyra novamente, voltei pro seu atendimento. se precisar de algo me diga! 😊")
            else:
                send_whatsapp_message(responsible_number, f"ℹ️ O atendimento para `{customer_number_to_reactivate}` já estava ativo. Nenhuma alteração foi necessária.")
            
            return True 

        except Exception as e:
            print(f"❌ Erro ao tentar reativar cliente: {e}")
            send_whatsapp_message(responsible_number, f"❌ Ocorreu um erro técnico ao tentar reativar o cliente. Verifique o log do sistema.")
            return True
            
    # --- Mensagem de ajuda ---
    print("⚠️ Comando não reconhecido do responsável.")
    help_message = (
        "Comando não reconhecido. 🤖\n\n"
        "*COMANDOS DISPONÍVEIS:*\n\n"
        "1️⃣ `bot on`\n(Liga o bot para todos os clientes)\n\n"
        "2️⃣ `bot off`\n(Desliga o bot para todos os clientes)\n\n"
        "3️⃣ `ok <numero_do_cliente>`\n(Reativa um cliente em intervenção)"
    )
    send_whatsapp_message(responsible_number, help_message)
    return True
# --- FIM DO COMANDO DO RESPONSÁVEL ---


# --- MELHORIA: Lógica de Processamento com LOCK (DO CÓDIGO ATUAL) ---
def process_message_logic(message_data, buffered_message_text=None):
    """
    (VERSÃO FINAL)
    Esta é a função "worker" principal. Ela pega o lock e chama a IA.
    """
    lock_acquired = False
    clean_number = None
    
    try:
        key_info = message_data.get('key', {})
        sender_number_full = key_info.get('senderPn') or key_info.get('participant') or key_info.get('remoteJid')
        if not sender_number_full or sender_number_full.endswith('@g.us'): return
        
        clean_number = sender_number_full.split('@')[0]
        sender_name_from_wpp = message_data.get('pushName') or 'Cliente'

        # --- Lógica de LOCK (do Código 1) ---
        now = datetime.now()
        res = conversation_collection.update_one(
            {'_id': clean_number, 'processing': {'$ne': True}},
            {'$set': {'processing': True, 'processing_started_at': now}},
            upsert=True 
        )

        if res.matched_count == 0 and res.upserted_id is None:
            print(f"⏳ {clean_number} já está sendo processado (lock). Reagendando...")
            if buffered_message_text:
                if clean_number not in message_buffer: message_buffer[clean_number] = []
                message_buffer[clean_number].insert(0, buffered_message_text)
            
            timer = threading.Timer(10.0, _trigger_ai_processing, args=[clean_number, message_data])
            message_timers[clean_number] = timer
            timer.start()
            return 
        
        lock_acquired = True
        if res.upserted_id:
             print(f"✅ Novo usuário {clean_number}. Documento criado e lock adquirido.")
        # --- Fim do Lock ---
        
        user_message_content = None
        
        # --- Lógica de Buffer/Áudio (do Código 1) ---
        if buffered_message_text:
            user_message_content = buffered_message_text
            messages_to_save = user_message_content.split(". ")
            for msg_text in messages_to_save:
                if msg_text and msg_text.strip():
                    append_message_to_db(clean_number, 'user', msg_text)
        else:
            # Lógica de Áudio (processamento imediato)
            message = message_data.get('message', {})
            if message.get('audioMessage') and message.get('base64'):
                message_id = key_info.get('id')
                print(f"🎤 Mensagem de áudio recebida de {clean_number}. Transcrevendo...")
                audio_base64 = message['base64']
                audio_data = base64.b64decode(audio_base64)
                os.makedirs("/tmp", exist_ok=True) 
                temp_audio_path = f"/tmp/audio_{clean_number}_{message_id}.ogg"
                with open(temp_audio_path, 'wb') as f: f.write(audio_data)
                
                user_message_content = transcrever_audio_gemini(temp_audio_path)
                
                try:
                    os.remove(temp_audio_path)
                except Exception as e:
                    print(f"Aviso: não foi possível remover áudio temporário. {e}")

                if not user_message_content:
                    send_whatsapp_message(sender_number_full, "Desculpe, não consegui entender o áudio. Pode tentar novamente? 🎧")
                    user_message_content = "[Usuário enviou um áudio incompreensível]"
            
            if not user_message_content:
                 user_message_content = "[Usuário enviou uma mensagem não suportada]"
                 
            # Salva a mensagem (de áudio ou não) no DB ANTES de chamar a IA
            append_message_to_db(clean_number, 'user', user_message_content)
        # --- Fim da Lógica de Buffer/Áudio ---

        print(f"🧠 Processando Mensagem de {clean_number}: '{user_message_content}'")
        
        # --- LÓGICA DE INTERVENÇÃO (Verifica se é o Admin) ---
        if RESPONSIBLE_NUMBER and clean_number == RESPONSIBLE_NUMBER:
            if handle_responsible_command(user_message_content, clean_number):
                return # 'finally' vai liberar o lock

        # --- LÓGICA DE "BOT LIGADO/DESLIGADO" ---
        try:
            bot_status_doc = conversation_collection.find_one({'_id': 'BOT_STATUS'})
            is_active = bot_status_doc.get('is_active', True) if bot_status_doc else True 
            
            if not is_active:
                print(f"🤖 Bot está em standby (desligado). Ignorando mensagem de {sender_name_from_wpp} ({clean_number}).")
                return # 'finally' vai liberar o lock
                
        except Exception as e:
            print(f"⚠️ Erro ao verificar o status do bot: {e}. Assumindo que está ligado.")

        conversation_status = conversation_collection.find_one({'_id': clean_number})

        if conversation_status and conversation_status.get('intervention_active', False):
            print(f"⏸️  Conversa com {sender_name_from_wpp} ({clean_number}) pausada para atendimento humano.")
            return # 'finally' vai liberar o lock

        known_customer_name = conversation_status.get('customer_name') if conversation_status else None
        
        # --- CHAMADA PADRÃO ---
        # A 'gerar_resposta_ia' agora é inteligente o suficiente para fazer tudo
        ai_reply = gerar_resposta_ia(
            clean_number,
            sender_name_from_wpp,
            user_message_content,
            known_customer_name
        )
        
        if not ai_reply:
             print("⚠️ A IA não gerou resposta.")
             return # 'finally' vai liberar o lock

        try:
            # Salva a resposta da IA (mesmo que seja uma tag de intervenção)
            append_message_to_db(clean_number, 'assistant', ai_reply)
            
            # --- LÓGICA DE INTERVENÇÃO (Pós-IA) ---
            if ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
                print(f"‼️ INTERVENÇÃO HUMANA SOLICITADA para {sender_name_from_wpp} ({clean_number})")
                
                conversation_collection.update_one(
                    {'_id': clean_number}, {'$set': {'intervention_active': True}}, upsert=True
                )
                
                send_whatsapp_message(sender_number_full, "Entendido. Já notifiquei um de nossos especialistas para te ajudar pessoalmente. Por favor, aguarde um momento. 👨‍💼")
                
                if RESPONSIBLE_NUMBER:
                    reason = ai_reply.replace("[HUMAN_INTERVENTION] Motivo:", "").strip()
                    display_name = known_customer_name or sender_name_from_wpp
                    
                    # Pega o histórico mais recente (que já inclui a msg do usuário)
                    history_summary = "Nenhum histórico de conversa encontrado."
                    if conversation_status and 'history' in conversation_status:
                        # Recarrega o histórico completo com a ÚLTIMA msg do usuário
                        history_com_ultima_msg = load_conversation_from_db(clean_number).get('history', [])
                        history_summary = get_last_messages_summary(history_com_ultima_msg)

                    notification_msg = (
                        f"🔔 *NOVA SOLICITAÇÃO DE ATENDIMENTO HUMANO* 🔔\n\n"
                        f"👤 *Cliente:* {display_name}\n"
                        f"📞 *Número:* `{clean_number}`\n\n"
                        f"💬 *Motivo da Chamada:*\n_{reason}_\n\n"
                        f"📜 *Resumo da Conversa:*\n{history_summary}\n\n"
                        f"-----------------------------------\n"
                        f"*AÇÃO NECESSÁRIA:*\nApós resolver, envie para *ESTE NÚMERO* o comando:\n`ok {clean_number}`"
                    )
                    send_whatsapp_message(f"{RESPONSIBLE_NUMBER}@s.whatsapp.net", notification_msg)
            
            else:
                # (Envio de resposta normal)
                print(f"🤖  Resposta da IA para {sender_name_from_wpp}: {ai_reply}")
                send_whatsapp_message(sender_number_full, ai_reply)

        except Exception as e:
            print(f"❌ Erro ao processar envio ou intervenção: {e}")
            send_whatsapp_message(sender_number_full, "Desculpe, tive um problema ao processar sua resposta. (Erro interno: SEND_LOGIC)")

    except Exception as e:
        print(f"❌ Erro fatal ao processar mensagem: {e}")
    finally:
        # --- Libera o Lock ---
        if clean_number and lock_acquired: 
            conversation_collection.update_one(
                {'_id': clean_number},
                {'$unset': {'processing': "", 'processing_started_at': ""}}
            )
            print(f"🔓 Lock liberado para {clean_number}.")


if modelo_ia:
    print("\n=============================================")
    print("   CHATBOT WHATSAPP COM IA INICIADO")
    print(f"   CLIENTE: {CLIENT_NAME}")
    if not RESPONSIBLE_NUMBER:
        print("   AVISO: 'RESPONSIBLE_NUMBER' não configurado. O recurso de intervenção humana não notificará ninguém.")
    else:
        print(f"   Intervenção Humana notificará: {RESPONSIBLE_NUMBER}")
    print("=============================================")
    print("Servidor aguardando mensagens no webhook...")

    scheduler.add_job(gerar_e_enviar_relatorio_semanal, 'cron', day_of_week='sun', hour=8, minute=0)
    print("⏰ Agendador de relatórios iniciado. O relatório será enviado todo Domingo às 08:00.")
    
    import atexit
    atexit.register(lambda: scheduler.shutdown())
    
else:
    print("\nEncerrando o programa devido a erros na inicialização.")

if __name__ == '__main__':
    print("Iniciando em MODO DE DESENVOLVIMENTO LOCAL (app.run)...")
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port, debug=False)