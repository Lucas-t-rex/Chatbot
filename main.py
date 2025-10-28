import google.generativeai as genai
import requests
import os
import json  # <--- ADICIONADO: Necessário para processar o gabarito do pedido
import threading
from flask import Flask, request, jsonify
from datetime import datetime
from dotenv import load_dotenv
import base64
from pymongo import MongoClient
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from apscheduler.schedulers.background import BackgroundScheduler

CLIENT_NAME = "Neuro Soluções em Tecnologia"
load_dotenv()
EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_DB_URI = os.environ.get("MONGO_DB_URI")

# --- NOVO: Variáveis para o Plano de Bifurcação ---
# (Adicione estes no seu arquivo .env)
COZINHA_WPP_NUMBER = "554898389781"
MOTOBOY_WPP_NUMBER = "554499242532"

# Flag para saber se a funcionalidade está ativa
BIFURCACAO_ENABLED = bool(COZINHA_WPP_NUMBER and MOTOBOY_WPP_NUMBER)
if BIFURCACAO_ENABLED:
    print(f"✅ Plano de Bifurcação ATIVO. Cozinha: {COZINHA_WPP_NUMBER}, Motoboy: {MOTOBOY_WPP_NUMBER}")
else:
    print("⚠️ Plano de Bifurcação INATIVO. (Configure COZINHA_WPP_NUMBER e MOTOBOY_WPP_NUMBER no .env)")
# --- FIM DA NOVA SEÇÃO ---

try:
    client = MongoClient(MONGO_DB_URI)
    db_name = CLIENT_NAME.lower().replace(" ", "_").replace("-", "_")
    db = client[db_name]  # Conecta ao banco de dados específico do cliente
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

# Cache para conversas ativas (para evitar ler o DB a cada mensagem)
conversations_cache = {}

modelo_ia = None
try:
    modelo_ia = genai.GenerativeModel('gemini-2.5-flash') # Recomendo usar o 1.5-flash
    print("✅ Modelo do Gemini inicializado com sucesso.")
except Exception as e:
    print(f"❌ ERRO: Não foi possível inicializar o modelo do Gemini. Verifique sua API Key. Erro: {e}")


def save_conversation_to_db(contact_id, sender_name, customer_name, chat_session, tokens_used):
    """Salva o histórico, nomes e atualiza a contagem de tokens no MongoDB."""
    try:
        history_list = [
            {'role': msg.role, 'parts': [part.text for part in msg.parts]}
            for msg in chat_session.history
        ]

        update_payload = {
            'sender_name': sender_name,  # Nome do contato no WhatsApp
            'history': history_list,
            'last_interaction': datetime.now()
        }
        # Adiciona o nome real do cliente ao payload se ele for conhecido
        if customer_name:
            update_payload['customer_name'] = customer_name

        conversation_collection.update_one(
            {'_id': contact_id},
            {
                '$set': update_payload,
                '$inc': {
                    'total_tokens_consumed': tokens_used
                }
            },
            upsert=True
        )
    except Exception as e:
        print(f"❌ Erro ao salvar conversa no MongoDB para {contact_id}: {e}")


def load_conversation_from_db(contact_id):
    """Carrega o histórico de uma conversa do MongoDB, se existir."""
    try:
        result = conversation_collection.find_one({'_id': contact_id})
        if result:
            print(f"🧠 Documento da conversa encontrado e carregado para {contact_id}.")
            return result
    except Exception as e:
        print(f"❌ Erro ao carregar conversa do MongoDB para {contact_id}: {e}")
    return None


# --- ATUALIZADO: Adicionado 'contact_phone' ---
def gerar_resposta_ia(contact_id, sender_name, user_message, known_customer_name, contact_phone):
    """
    Gera uma resposta usando a IA, com lógica para perguntar e salvar o nome do cliente.
    """
    global modelo_ia, conversations_cache

    if not modelo_ia:
        return "Desculpe, estou com um problema interno (modelo IA não carregado)."

    if contact_id not in conversations_cache:
        horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        prompt_name_instruction = ""
        final_user_name_for_prompt = ""

        if known_customer_name:
            final_user_name_for_prompt = known_customer_name
            prompt_name_instruction = f"O nome do usuário com quem você está falando é: {final_user_name_for_prompt}. Trate-o por este nome."
        else:
            final_user_name_for_prompt = sender_name
            prompt_name_instruction = f"""
            REGRA CRÍTICA - CAPTURA DE NOME (PRIORIDADE MÁXIMA):
            Seu nome nome: {{Lyra}} você é atendente da nome da empresa: {{Neuro Soluções em Tecnologia}}
            O nome real do cliente é DESCONHECIDO. O nome de contato '{sender_name}' é um apelido e NÃO deve ser usado.
            1. Sua primeira tarefa é perguntar o nome do cliente de forma educada.
            2. Se o cliente responder com o que parece ser um nome (ex: "Meu nome é João", "Pode me chamar de Maria", "Dani"), sua resposta DEVE, OBRIGATORIAMENTE E SEM EXCEÇÃO, seguir este formato exato:
               [NOME_CLIENTE]O nome do cliente é: [Nome Extraído]. (aqui você continua a conversa normalmente)
            3. Esta é sua prioridade máxima. Não responda a outras perguntas antes de ter o nome e ter usado a tag.

            EXEMPLO DE INTERAÇÃO CORRETA:
            Cliente: "oi"
            Você: "Olá! Como posso te chamar?"
            Cliente: "Meu nome é Carlos"
            Sua Resposta: "[NOME_CLIENTE]O nome do cliente é: Carlos. Prazer em conhecê-lo, Carlos! Como posso ajudar?"
            """

        # --- NOVO: Lógica do Prompt de Bifurcação ---
        prompt_bifurcacao = ""
        if BIFURCACAO_ENABLED:
            prompt_bifurcacao = f"""
            =====================================================
            ⚙️ MODO DE BIFURCAÇÃO DE PEDIDOS (PRIORIDADE ALTA)
            =====================================================
            Seu cliente ATUAL é a empresa: '{CLIENT_NAME}'.
            Esta empresa usa o plano "Bifurcação". Sua tarefa é ATIVAMENTE identificar se o cliente quer fazer um PEDIDO (ex: pizzaria, restaurante, marmitaria, etc.).

            Se o cliente quiser fazer um pedido, seu comportamento MUDA:
            
            1.  **MISSÃO:** Você DEVE preencher TODOS os campos do "Gabarito de Pedido" abaixo.
            2.  **COLETA:** Faça perguntas UMA de cada vez, de forma natural, até ter todos os dados. Seja persistente.
            3.  **TELEFONE:** O campo "telefone_contato" JÁ ESTÁ PREENCHIDO. É {contact_phone}. NÃO pergunte o telefone ao cliente.
            4.  **CONFIRMAÇÃO (LOOP OBRIGATÓRIO):** Ao ter TODOS os campos, você DEVE apresentar um RESUMO COMPLETO ao cliente (incluindo valor total) e perguntar "Confirma o pedido?".
            5.  **EDIÇÃO (LOOP OBRIGATÓRIO):** Se o cliente quiser alterar (ex: "quero tirar o feijao", "adicione uma coca"), você DEVE:
                a. Ajustar o gabarito (ex: adicionar em 'observacoes', alterar 'bebidas' ou 'valor_total').
                b. Apresentar o NOVO resumo completo e perguntar "Confirma o pedido?" novamente.
            6.  **ENVIO (AÇÃO CRÍTICA):** Quando o cliente responder "sim", "confirmo", "pode enviar", ou algo positivo, sua resposta DEVE, OBRIGATORIAMENTE E SEM EXCEÇÃO, começar com a tag [PEDIDO_CONFIRMADO] e ser seguida por um objeto JSON VÁLIDO contendo o gabarito.

            --- GABARITO DE PEDIDO (DEVE SER PREENCHIDO) ---
            {{
              "nome_cliente": "...", (Use o 'known_customer_name', se não tiver, pergunte)
              "endereco_completo": "...", (Rua, Número, Bairro, Cidade/Estado, Ponto de Referência se houver)
              "telefone_contato": "{contact_phone}", (JÁ PREENCHIDO)
              "pedido_completo": "...", (Lista de todos os itens, ex: "1 Marmita G, 2 Marmitas M (1 sem feijão), 1 Marmita P")
              "bebidas": "...", (ex: "1 Coca-Cola 2L", ou "Nenhuma")
              "forma_pagamento": "...", (ex: "Pix", "Cartão na entrega", "Dinheiro (troco para R$ 100)")
              "observacoes": "...", (ex: "1 das marmitas médias sem feijão", "Mandar sachês de ketchup", ou "Nenhuma")
              "valor_total": "..." (O valor total do pedido, incluindo entrega se houver)
            }}
            --- FIM DO GABARITO ---

            EXEMPLO DE INTERAÇÃO DE ENVIO CORRETA:
            Cliente: "Isso mesmo, pode confirmar."
            Sua Resposta: [PEDIDO_CONFIRMADO]{{
              "nome_cliente": "Gabriel",
              "endereco_completo": "Rua China, 0, Bairro X, Maringá-PR",
              "telefone_contato": "{contact_phone}",
              "pedido_completo": "1 Marmita G, 2 Marmitas M, 1 Marmita P",
              "bebidas": "1 Coca-Cola 2L",
              "forma_pagamento": "Pix",
              "observacoes": "1 das marmitas médias sem feijão.",
              "valor_total": "R$ 70,00"
            }}
            Pedido confirmado, Gabriel! Estou enviando para a cozinha. O tempo de entrega é de 40 minutos. Algo mais?
            """
        else:
            prompt_bifurcacao = "O plano de Bifurcação não está ativo."
        # --- FIM DA NOVA SEÇÃO ---

        prompt_inicial = f"""
                A data e hora atuais são: {horario_atual}.
                {prompt_name_instruction}
                Seu dever é atender e tirar todas as duvidas do cliente, vender nossos planos e produtos, e vangloriar a empresa sem parecer esnobe.
                =====================================================
                🏷️ IDENTIDADE DO ATENDENTE
                =====================================================
                nome: {{Lyra}}
                sexo: {{Feminina}}
                idade: {{40}}
                função: {{Atendente, vendedora, especialista em Ti e machine learning}} 
                papel: {{Você deve atender a pessoa, entender a necessidade da pessoa, vender o plano de acordo com a necessidade, tirar duvidas, ajudar.}}  (ex: tirar dúvidas, passar preços, enviar catálogos, agendar horários)

                =====================================================
                🏢 IDENTIDADE DA EMPRESA
                =====================================================
                nome da empresa: {{Neuro Soluções em Tecnologia}}
                setor: {{Tecnologia e Automação}} 
                missão: {{Facilitar e organizar as empresas de clientes.}}
                valores: {{Organização, trasparencia,persistencia e ascenção.}}
                horário de atendimento: {{De segunda-feira a sexta-feira das 8:00 as 18:00}}
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
                tempo de mercado: {{Desde de 2025}}
                slogan: {{O futuro é agora!}}

                =====================================================
                💼 SERVIÇOS / CARDÁPIO
                =====================================================
                - Plano Atendente: {{Atendente personalizada, configurada conforme a necessidade do cliente.
                                  Neste plano, o atendimento pode funcionar de três formas:

                                  Atendimento Autônomo:
                                  A atendente responde sozinha até o final da conversa, usando apenas as informações liberadas.

                                  Intervenção Humana:
                                  O responsável pode entrar na conversa quando quiser, para tomar decisões ou dar respostas mais específicas.

                                  Bifurcação de Mensagens:
                                  Permite enviar informações da conversa para outro número (por exemplo, repassar detalhes para o gestor ou outro atendente).}}
                - Plano Secretário: {{Agendamento Inteligente:
                                  Faz agendamentos, alterações e cancelamentos de horários ou serviços, conforme solicitado pelo cliente.

                                  🔔 Avisos Automáticos:
                                  Envia notificações e lembretes para o telefone do responsável sempre que houver mudança ou novo agredamento.

                                  💻 Agenda Integrada:
                                  Acompanha um software externo conectado ao WhatsApp, permitindo manter todos os dados organizados e atualizados exatamente como negociado.}}
                - Plano Premium: {{Em construção}}
                - {{}}

                {prompt_bifurcacao} 

                =====================================================
                💰 PLANOS E VALORES
                =====================================================
                Instalação: {{R$200,00 mensal}} todos os planos tem um fazer de setup inicial , para instalação do projeto e os requisitos da IA. 
                plano Atendente: {{R$300,00 mensal}}
                Plano Secretário: {{R$500,00 mensal}}
                plano avançado: {{Em analise}}
                observações: {{ex: valores podem variar conforme personalização ou integrações extras.}}

                =====================================================
                🧭 COMPORTAMENTO E REGRAS DE ATENDIMENTO
                =====================================================
                ações:
                - Responda sempre de forma profissional, empática e natural.
                - Use frases curtas, diretas e educadas.
                - Mantenha sempre um tom positivo e proativo.
                - Ajude o cliente a resolver dúvidas e tomar decisões.
                - Entender a nessecidade e vender o plano ideal para a pessoa.
                - Se a pessoa tiver objeção para não fechar tente enteder o porque.
                - Sempre usar quebra de objeções quando o cliente não querer fechar.
                - Se depois de tentar usar as quebras de objeção a pessoa ainda não querer fechar, ofereça uma semana gratis.
                - Se mesmo oferecendo uma semana gratis, use agradecimentos e despedidas.

                falas:
                - Use linguagem simples e amigável.
                - Evite termos técnicos, a menos que o cliente peça.
                - Não use emojis em excesso (máximo 2 por mensagem).

                saudações:
                - Sempre cumprimente com entusiasmo e simpatia.
                Exemplo: "Olá! 😊 Seja muito bem-vindo(a) à {{Neuro Soluções em Tecnologia}}, quer saber sobre a empresa ou falar com o Lucas?!"

                agradecimentos:
                - Agradeça de forma sincera e breve.
                Exemplo: "Agradeço o seu contato! Foi um prazer ajudar. 🙏"

                despedidas:
                - Despeça-se com elegância e positividade.
                Exemplo: "Tenha um ótimo dia! Ficamos à disposição sempre que precisar. 🌟
                Neuro Soluções em Tecnologia - O futuro é agora."

                não deve fazer:
                - Não inventar informações que não saiba.
                - Não discutir, nem responder de forma rude.
                - Não compartilhar dados pessoais.
                - Não responder perguntas fora do contexto da empresa.
                - Nunca deixe o cliente sair sem ter vendido.

                missão:
                - Ajudar o cliente a obter respostas rápidas e confiáveis.
                - Entender a nessecidade e vender o plano ideal para a pessoa.
                - Gerar uma boa experiência no atendimento.
                - Reforçar o nome e a credibilidade da empresa.
                - Sempre usar quebra de objeções quando o cliente não querer fechar.

                =====================================================
                ⚙️ PERSONALIDADE DO ATENDENTE
                =====================================================
                - Tom de voz: {{alegre, acolhedor, profissional, descontraído}} 
                - Ritmo de conversa: natural e fluido.
                - Estilo: humano, prestativo e simpático.
                - Emojis: usar com moderação, sempre com propósito.
                - Curiosidade: se o cliente parecer indecso, ofereça ajuda com sugestões.
                - Converssas: Não use muitas palavras para não ser cansativo.

                =====================================================
                🧩 EXEMPLO DE COMPORTAMENTO
                =====================================================
                Cliente: "Oi, quais são os horários de funcionamento?"
                Atendente: "Olá! 😊 A {{Neuro Soluções em Tecnologi}} funciona de {{De segunda-feira a sexta-feira das 8:00 as 18:00 }}. Quer que eu te ajude a agendar um horário?"

                Cliente: "Vocês têm planos mensais?"
                Atendente: "Temos sim! 🙌 Trabalhamos com diferentes planos adaptados ao seu perfil. Quer que eu te envie as opções?"

                =====================================================
                PRONTO PARA ATENDER O CLIENTE
                =====================================================
                Quando o cliente enviar uma mensagem, cumprimente e inicie o atendimento de forma natural, usando o nome do cliente se disponível, tente entender o que ele precisa e sempre coloque o cliente em primeiro lugar.
                """

        convo_start = [
            {'role': 'user', 'parts': [prompt_inicial]},
            {'role': 'model', 'parts': [f"Entendido. A Regra de Ouro e a captura de nome são prioridades. Se o plano Bifurcação estiver ativo e o cliente quiser um pedido, seguirei o gabarito. Estou pronta. Olá, {final_user_name_for_prompt}! Como posso te ajudar?"]}
        ]

        loaded_conversation = load_conversation_from_db(contact_id)
        if loaded_conversation and 'history' in loaded_conversation:
            print(f"Iniciando chat para {sender_name} com histórico anterior.")
            old_history = [msg for msg in loaded_conversation['history'] if not msg['parts'][0].strip().startswith("A data e hora atuais são:")]
            chat = modelo_ia.start_chat(history=convo_start + old_history)
        else:
            print(f"Iniciando novo chat para {sender_name}.")
            chat = modelo_ia.start_chat(history=convo_start)

        conversations_cache[contact_id] = {'ai_chat_session': chat, 'name': sender_name, 'customer_name': known_customer_name}

    chat_session = conversations_cache[contact_id]['ai_chat_session']
    customer_name_in_cache = conversations_cache[contact_id].get('customer_name')

    try:
        print(f"Enviando para a IA: '{user_message}' (De: {sender_name})")

        input_tokens = modelo_ia.count_tokens(chat_session.history + [{'role': 'user', 'parts': [user_message]}]).total_tokens
        resposta = chat_session.send_message(user_message)
        output_tokens = modelo_ia.count_tokens(resposta.text).total_tokens
        total_tokens_na_interacao = input_tokens + output_tokens

        print(f"📊 Consumo de Tokens: Entrada={input_tokens}, Saída={output_tokens}, Total={total_tokens_na_interacao}")

        ai_reply = resposta.text

        if ai_reply.strip().startswith("[NOME_CLIENTE]"):
            print("📝 Tag [NOME_CLIENTE] detectada. Extraindo e salvando nome...")
            try:
                # 1. Pega tudo que vem depois de "O nome do cliente é:"
                full_response_part = ai_reply.split("O nome do cliente é:")[1].strip()

                # 2. Divide essa parte no primeiro ponto final. A parte 0 é o nome.
                extracted_name = full_response_part.split('.')[0].strip()

                # 3. Pega o resto da mensagem de forma segura
                start_of_message_index = full_response_part.find(extracted_name) + len(extracted_name)
                ai_reply = full_response_part[start_of_message_index:].lstrip('.!?, ').strip()

                # 4. Salva o nome limpo no banco de dados e no cache
                conversation_collection.update_one(
                    {'_id': contact_id},
                    {'$set': {'customer_name': extracted_name}},
                    upsert=True
                )
                conversations_cache[contact_id]['customer_name'] = extracted_name
                customer_name_in_cache = extracted_name
                print(f"✅ Nome '{extracted_name}' salvo para o cliente {contact_id}.")

            except Exception as e:
                print(f"❌ Erro ao extrair o nome da tag: {e}")
                ai_reply = ai_reply.replace("[NOME_CLIENTE]", "").strip()

        # --- NOVO: Bloco de Processamento da Bifurcação ---
        if BIFURCACAO_ENABLED and ai_reply.strip().startswith("[PEDIDO_CONFIRMADO]"):
            print(f"📦 Tag [PEDIDO_CONFIRMADO] detectada. Processando e bifurcando pedido para {contact_id}...")
            try:
                # 1. Isolar o JSON do resto da mensagem
                json_start = ai_reply.find('{')
                json_end = ai_reply.rfind('}') + 1

                if json_start == -1 or json_end == 0:
                    raise ValueError("JSON de pedido não encontrado após a tag.")

                json_string = ai_reply[json_start:json_end]

                # 2. Isolar a mensagem de resposta para o cliente
                remaining_reply = ai_reply[json_end:].strip()
                if not remaining_reply:
                    remaining_reply = "Seu pedido foi confirmado e enviado para a cozinha! Algo mais?"  # Fallback

                # 3. Parsear o JSON
                order_data = json.loads(json_string)

                # 4. Formatar as mensagens de bifurcação

                # Mensagem para a COZINHA (Completa)
                msg_cozinha = f"""
                --- 🍳 NOVO PEDIDO (COZINHA) 🍳 ---
                
                Cliente: {order_data.get('nome_cliente', 'N/A')}
                Telefone: {order_data.get('telefone_contato', 'N/A')}
                Endereço: {order_data.get('endereco_completo', 'N/A')}
                
                --- PEDIDO ---
                {order_data.get('pedido_completo', 'N/A')}
                
                --- BEBIDAS ---
                {order_data.get('bebidas', 'N/A')}
                
                --- OBSERVAÇÕES ---
                {order_data.get('observacoes', 'N/A')}
                
                Forma de Pagto: {order_data.get('forma_pagamento', 'N/A')}
                Valor Total: {order_data.get('valor_total', 'N/A')}
                """

                # Mensagem para o MOTOBOY (Parcial)
                msg_motoboy = f"""
                --- 🛵 NOVA ENTREGA (MOTOBOY) 🛵 ---
                
                Cliente: {order_data.get('nome_cliente', 'N/A')}
                Telefone: {order_data.get('telefone_contato', 'N/A')}
                Endereço: {order_data.get('endereco_completo', 'N/A')}
                
                Forma de Pagto: {order_data.get('forma_pagamento', 'N/A')}
                Valor Total: {order_data.get('valor_total', 'N/A')}
                """

                # 5. Enviar as mensagens (em threads para não bloquear a resposta)
                threading.Thread(target=send_whatsapp_message, args=(COZINHA_WPP_NUMBER, msg_cozinha.strip())).start()
                threading.Thread(target=send_whatsapp_message, args=(MOTOBOY_WPP_NUMBER, msg_motoboy.strip())).start()

                print(f"✅ Pedido bifurcado com sucesso para {COZINHA_WPP_NUMBER} e {MOTOBOY_WPP_NUMBER}.")

                # 6. Atualiza a resposta para o cliente
                ai_reply = remaining_reply

            except Exception as e:
                print(f"❌ Erro ao processar bifurcação [PEDIDO_CONFIRMADO]: {e}")
                # Limpa a tag para não enviar o JSON bruto ao cliente
                ai_reply = ai_reply.replace("[PEDIDO_CONFIRMADO]", "").strip()
                if '{' in ai_reply and '}' in ai_reply:
                    ai_reply = "Tive um problema ao enviar seu pedido para a cozinha. Pode confirmar os dados novamente, por favor? (Erro interno: JSON_PARSE)"
                
                # Salva a conversa mesmo com erro, para a IA ter o contexto
                save_conversation_to_db(contact_id, sender_name, customer_name_in_cache, chat_session, total_tokens_na_interacao)
                return ai_reply  # Retorna a mensagem de erro

        # --- FIM DO NOVO BLOCO ---

        if not ai_reply.strip().startswith("[HUMAN_INTERVENTION]"):
            save_conversation_to_db(contact_id, sender_name, customer_name_in_cache, chat_session, total_tokens_na_interacao)

        return ai_reply

    except Exception as e:
        print(f"❌ Erro ao comunicar com a API do Gemini: {e}")
        if contact_id in conversations_cache:
            del conversations_cache[contact_id]
        return "Tive um pequeno problema para processar sua mensagem e precisei reiniciar nossa conversa. Você poderia repetir, por favor?"


def transcrever_audio_gemini(caminho_do_audio):
    """
    Envia um arquivo de áudio para a API do Gemini e retorna a transcrição em texto.
    """
    global modelo_ia  # Vamos reutilizar o modelo Gemini que já foi iniciado

    if not modelo_ia:
        print("❌ Modelo de IA não inicializado. Impossível transcrever.")
        return None

    print(f"🎤 Enviando áudio '{caminho_do_audio}' para transcrição no Gemini...")
    try:
        audio_file = genai.upload_file(
            path=caminho_do_audio,
            mime_type="audio/ogg"
        )

        # Pedimos ao modelo para transcrever o áudio
        response = modelo_ia.generate_content(["Por favor, transcreva o áudio a seguir.", audio_file])

        # Opcional, mas recomendado: deletar o arquivo do servidor do Google após o uso
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


def send_whatsapp_message(number, text_message):
    """Envia uma mensagem de texto para um número via Evolution API."""
    clean_number = number.split('@')[0]
    payload = {"number": clean_number, "textMessage": {"text": text_message}}
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}

    try:
        response = requests.post(EVOLUTION_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        print(f"✅ Mensagem (Texto) enviada com sucesso para {clean_number}\n")
    except requests.exceptions.RequestException as e:
        print(f"❌ Erro ao enviar mensagem para {clean_number}: {e}")


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


app = Flask(__name__)

processed_messages = set()  # para evitar loops


@app.route('/webhook', methods=['POST'])
def receive_webhook():
    """Recebe mensagens do WhatsApp enviadas pela Evolution API."""
    data = request.json
    print(f"📦 DADO BRUTO RECEBIDO NO WEBHOOK: {data}")

    try:
        message_data = data.get('data', {}) or data
        key_info = message_data.get('key', {})

        # --- 1️⃣ Ignora mensagens enviadas por você mesmo ---
        if key_info.get('fromMe'):
            return jsonify({"status": "ignored_from_me"}), 200

        # --- 2️⃣ Pega o ID único da mensagem ---
        message_id = key_info.get('id')
        if not message_id:
            return jsonify({"status": "ignored_no_id"}), 200

        # --- 3️⃣ Se já processou esta mensagem, ignora ---
        if message_id in processed_messages:
            print(f"⚠️ Mensagem {message_id} já processada, ignorando.")
            return jsonify({"status": "ignored_duplicate"}), 200
        processed_messages.add(message_id)
        if len(processed_messages) > 1000:
            processed_messages.clear()

        # --- 4️⃣ Retorna imediatamente 200 para evitar reenvio da Evolution ---
        threading.Thread(target=process_message, args=(message_data,)).start()
        return jsonify({"status": "received"}), 200

    except Exception as e:
        print(f"❌ Erro inesperado no webhook: {e}")
        print("DADO QUE CAUSOU ERRO:", data)
        return jsonify({"status": "error"}), 500


def process_message(message_data):
    """Processa a mensagem, buscando dados do cliente antes de chamar a IA."""
    try:
        sender_number_full = message_data.get('key', {}).get('remoteJid')

        if not sender_number_full or sender_number_full.endswith('@g.us'):
            return

        clean_number = sender_number_full.split('@')[0]
        sender_name_from_wpp = message_data.get('pushName') or 'Cliente'
        message = message_data.get('message', {})

        user_message_content = None
        if message.get('conversation'):
            user_message_content = message['conversation']
        elif message.get('extendedTextMessage'):
            user_message_content = message['extendedTextMessage'].get('text')
        elif message.get('audioMessage') and message.get('base64'):
            print(f"🎤 Mensagem de áudio recebida de {sender_name_from_wpp} ({clean_number}).")
            audio_base64 = message['base64']
            audio_data = base64.b64decode(audio_base64)
            temp_audio_path = f"/tmp/audio_{clean_number}.ogg"
            with open(temp_audio_path, 'wb') as f:
                f.write(audio_data)

            user_message_content = transcrever_audio_gemini(temp_audio_path)
            os.remove(temp_audio_path)

            if not user_message_content:
                send_whatsapp_message(sender_number_full, "Desculpe, não consegui entender o áudio. Pode tentar novamente? 🎧")
                return

        if not user_message_content:
            print("➡️ Mensagem ignorada (sem conteúdo útil).")
            return

        # --- LÓGICA ADICIONADA ---
        # Busca os dados do cliente no banco ANTES de chamar a IA
        conversation_status = load_conversation_from_db(clean_number)
        known_customer_name = conversation_status.get('customer_name') if conversation_status else None

        if known_customer_name:
            print(f"👤 Cliente já conhecido: {known_customer_name} ({clean_number})")
        else:
            print(f"👤 Novo cliente ou nome desconhecido. Usando nome do WPP: {sender_name_from_wpp} ({clean_number})")

        print(f"\n🧠 Processando mensagem de {sender_name_from_wpp} ({clean_number}): '{user_message_content}'")

        # --- ATUALIZADO: Passa o 'clean_number' como 'contact_phone' ---
        ai_reply = gerar_resposta_ia(clean_number, sender_name_from_wpp, user_message_content, known_customer_name, clean_number)

        if ai_reply:
            print(f"🤖 Resposta da IA para {sender_name_from_wpp}: {ai_reply}")
            send_whatsapp_message(sender_number_full, ai_reply)

    except Exception as e:
        print(f"❌ Erro fatal ao processar mensagem: {e}")


if __name__ == '__main__':
    if modelo_ia:
        print("\n=============================================")
        print("   CHATBOT WHATSAPP COM IA INICIADO")
        print(f"   CLIENTE: {CLIENT_NAME}")  # Mostra para qual cliente este bot está rodando
        print("=============================================")
        print("Servidor aguardando mensagens no webhook...")

        scheduler = BackgroundScheduler(daemon=True, timezone='America/Sao_Paulo')
        # Agenda a função para rodar todo Domingo às 08:00 da manhã
        scheduler.add_job(gerar_e_enviar_relatorio_semanal, 'cron', day_of_week='sun', hour=8, minute=0)
        scheduler.start()
        print("⏰ Agendador de relatórios iniciado. O relatório será enviado todo Domingo às 08:00.")

        # Garante que o agendador seja desligado corretamente ao sair
        import atexit
        atexit.register(lambda: scheduler.shutdown())

        app.run(host='0.0.0.0', port=8000)
    else:
        print("\nEncerrando o programa devido a erros na inicialização.")
