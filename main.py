
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
from pymongo import errors

CLIENT_NAME = "Marmitaria Sabor do Dia" 

load_dotenv()
EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_DB_URI = os.environ.get("MONGO_DB_URI")

COZINHA_WPP_NUMBER = "554898389781"
MOTOBOY_WPP_NUMBER = "554499242532"
ADMIN_WPP_NUMBER = "554898389781"

message_buffer = {}
message_timers = {}
BUFFER_TIME_SECONDS = 8


BIFURCACAO_ENABLED = bool(COZINHA_WPP_NUMBER and MOTOBOY_WPP_NUMBER)
if BIFURCACAO_ENABLED:
    print(f"✅ Plano de Bifurcação ATIVO. Cozinha: {COZINHA_WPP_NUMBER}, Motoboy: {MOTOBOY_WPP_NUMBER}")
else:
    print("⚠️ Plano de Bifurcação INATIVO. (Configure COZINHA_WPP_NUMBER e MOTOBOY_WPP_NUMBER no .env)")

try:
    client = MongoClient(MONGO_DB_URI)
    db_name = CLIENT_NAME.lower().replace(" ", "_").replace("-", "_")
    db = client[db_name] 
    conversation_collection = db.conversations
    menu_collection = db.menu
    
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

def append_message_to_db(contact_id, role, text, message_id=None):
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
    
def save_conversation_to_db(contact_id, sender_name, customer_name, chat_session, tokens_used):

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
            # garante que 'history' exista e ordena
            history = result.get('history', [])
            history_sorted = sorted(history, key=lambda m: m.get('ts', ''))
            result['history'] = history_sorted
            print(f"🧠 Histórico anterior encontrado e carregado para {contact_id} ({len(history_sorted)} entradas).")
            return result
    except Exception as e:
        print(f"❌ Erro ao carregar conversa do MongoDB para {contact_id}: {e}")
    return None

def inicializar_menu_padrao():
    """Cria a estrutura do menu no DB, mas sem itens."""
    
    print("Verificando/Criando menu padrão no DB...")
    try:
 
        # Menu padrão agora está "em branco", só com a estrutura.
        menu_padrao = {
            '_id': 'menu_principal',
            'prato_do_dia': [], # Lista vazia
            'acompanhamentos': "", # String vazia
            'marmitas': [],        # Lista vazia
            'bebidas': [],         # Lista vazia
            'taxa_entrega': 0.00   # Padrão 0
        }

        resultado = menu_collection.update_one(
            {'_id': 'menu_principal'},
            {'$setOnInsert': menu_padrao},
            upsert=True
        )
        
        if resultado.upserted_id:
            print("✅✅✅ Menu padrão NÃO existia e foi CRIADO VAZIO. O admin deve preenchê-lo. ✅✅✅")
        else:
            print("✅ Menu 'menu_principal' já existia. Nenhuma alteração feita.")
        
    except Exception as e:
        print(f"❌ Erro ao inicializar menu: {e}")

def formatar_menu_para_prompt():
    """Busca o menu no DB e formata como string para a IA."""
    try:

        menu_data = menu_collection.find_one({"_id": "menu_principal"})
        if not menu_data:
            return "O cardápio não está disponível no momento."

        menu_string = "--- PRATO DO DIA ---\n"
        
        # --- LÓGICA ATUALIZADA PARA LISTA DE PRATOS ---
        prato_data = menu_data.get('prato_do_dia')
        
        if isinstance(prato_data, list):
            if len(prato_data) == 0:
                menu_string += "Prato do dia não informado.\n"
            elif len(prato_data) == 1:
                # Se for uma lista com UM item, trate como normal
                menu_string += f"Hoje temos: {{{prato_data[0]}}}\n"
            else:
                # Se for uma lista com VÁRIOS itens, liste como OPÇÕES
                opcoes_str = ", ".join(prato_data)
                menu_string += f"Hoje temos as seguintes OPÇÕES DE PRATO: [{{ {opcoes_str} }}]\n"
                menu_string += "(O cliente deve escolher UMA das opções para a marmita)\n"
        
        elif isinstance(prato_data, str):
             # Apenas para garantir compatibilidade se o dado for antigo (string)
             menu_string += f"Hoje temos: {{{prato_data}}}\n"
        else:
            menu_string += "Prato do dia não informado.\n"
        # --- FIM DA LÓGICA ATUALIZADA ---

        menu_string += f"Acompanhamentos: {{{menu_data.get('acompanhamentos', 'Não informado')}}}\n"

        menu_string += "--- TAMANHOS E VALORES (Marmitas) ---\n"
        for item in menu_data.get('marmitas', []):
            menu_string += f"- {item['nome']}: {{R${item['preco']:.2f}}}\n"

        menu_string += "--- 🥤 BEBIDAS ---\n"
        for item in menu_data.get('bebidas', []):
            menu_string += f"- {item['nome']}: {{R${item['preco']:.2f}}}\n"

        menu_string += "--- 🛵 TAXA DE ENTREGA ---\n"
        menu_string += f"- Taxa de Entrega Fixa: {{R${menu_data.get('taxa_entrega', 0.00):.2f}}} (Use este valor para CÁLCULO do valor total APENAS PARA ENTREGAS)\n"
        menu_string += "- Pedidos para Retirada no Local: {R$ 0,00} (não há taxa)\n"

        return menu_string

    except Exception as e:
        print(f"❌ Erro ao formatar menu: {e}")
        return "Erro ao carregar cardápio."

# --- INÍCIO DA MUDANÇA (PASSO 4 CORRIGIDO) ---
def gerar_resposta_admin(contact_id, user_message):
    """Gera uma resposta para o ADMIN, focado em atualizar o menu."""
    global modelo_ia
    try:
        # 1. Carrega o menu ATUAL do DB para a IA saber o estado
        current_menu = menu_collection.find_one({"_id": "menu_principal"})
        if not current_menu:
            return "ERRO: Não encontrei o documento 'menu_principal' no banco de dados. A inicialização falhou."
        
        # 2. Carrega o histórico de conversa do ADMIN (só as últimas 10 msgs)
        convo_data = load_conversation_from_db(contact_id)
        old_history = []
        if convo_data and 'history' in convo_data:
            history_from_db = [msg for msg in convo_data['history']][-10:] 
            old_history = []
            for msg in history_from_db:
                role = msg.get('role', 'user')
                if role == 'assistant': role = 'model'
                if 'text' in msg:
                    old_history.append({'role': role, 'parts': [msg['text']]})

        # 3. Cria o prompt do ADMIN
        admin_prompt_text = f"""
        Você é um assistente de gerenciamento de cardápio.
        Sua única função é ajudar o dono da loja (o usuário) a ATUALIZAR o cardápio no banco de dados.
        O usuário NÃO é um programador. Ele vai falar em linguagem natural.

        REGRAS:
        1. ANALISE a mensagem do usuário.
        2. COMPARE com o "MENU ATUAL".
        3. DETERMINE a intenção: (adicionar, remover, alterar_preco, alterar_prato_dia, alterar_taxa).
        4. FAÇA PERGUNTAS se faltar informação (ex: "Qual o preço da Coca 2L?").
        5. QUANDO TIVER TUDO, sua resposta final DEVE conter a tag [CONFIRMAR_UPDATE] e o JSON VÁLIDO *seguido pelo* texto de confirmação.
        6. Se o usuário confirmar ("sim", "ok"), sua ÚNICA resposta deve ser a tag [EXECUTAR_UPDATE] seguida pelo JSON de antes.
        
        # --- REGRA CRÍTICA DO PRATO DO DIA ---
        O campo "prato_do_dia" DEVE ser sempre uma LISTA (um Array) de strings.
        - Se o admin disser que é SÓ UM prato (ex: "hoje é macarronada"), o JSON deve ser: {{"prato_do_dia": ["Macarronada"]}}
        - Se o admin disser que são DOIS ou MAIS pratos (ex: "hoje é carne e frango"), o JSON deve ser: {{"prato_do_dia": ["Carne de panela", "Frango frito"]}}
        
        # --- REGRA 7: VER O CARDÁPIO ---
        - Se o usuário pedir para "ver o cardápio", "ver o estoque", "o que temos hoje?", "qual o cardápio atual?" ou algo similar, 
        - Sua ÚNICA resposta deve ser a tag [VER_CARDAPIO].
        - NÃO tente atualizar nada, apenas envie a tag.
        
        MENU ATUAL (DO BANCO DE DADOS):
        {json.dumps(current_menu, indent=2, default=str)}
        
        EXEMPLO DE FLUXO 1 (Alterar Prato Único):
        Usuário: "oi, hoje o prato do dia é Macarronada e os acompanhamentos são arroz e feijão"
        Você: "[CONFIRMAR_UPDATE]{{{{\"prato_do_dia\": [\"Macarronada\"], \"acompanhamentos\": \"arroz e feijão\"}}}}Olá! Entendido. Vou alterar:
        - Prato do Dia: ['Macarronada']
        - Acompanhamentos: 'arroz e feijão'
        Confirma?"
        Usuário: "sim"
        Você: "[EXECUTAR_UPDATE]{{{{\"prato_do_dia\": [\"Macarronada\"], \"acompanhamentos\": \"arroz e feijão\"}}}}"
        
        EXEMPLO DE FLUXO 2 (Múltiplos Pratos):
        Usuário: "hoje os pratos sao carne de panela e frango frito"
        Você: "[CONFIRMAR_UPDATE]{{{{\"prato_do_dia\": [\"Carne de panela\", \"Frango frito\"]}}}}
        Certo! Entendido. O Prato do Dia terá 2 opções:
        1. Carne de panela
        2. Frango frito
        Confirma?"
        Usuário: "sim"
        Você: "[EXECUTAR_UPDATE]{{{{\"prato_do_dia\": [\"Carne de panela\", \"Frango frito\"]}}}}"
        
        EXEMPLO DE FLUXO 3 (Ver Cardápio):
        Usuário: "como está o cardápio agora?"
        Você: "[VER_CARDAPIO]"
        """

        admin_convo_start = [
            {'role': 'user', 'parts': [admin_prompt_text]},
            {'role': 'model', 'parts': ["Entendido. Estou no modo de gerenciamento. Vou analisar o pedido do admin, comparar com o JSON atual e pedir confirmação."]}
        ]
        chat_session = modelo_ia.start_chat(history=admin_convo_start + old_history)
        
        print(f"Enviando para a IA (Admin): '{user_message}'")
        resposta_ia_admin = chat_session.send_message(user_message)
        ai_reply = resposta_ia_admin.text
        
        # --- INÍCIO DO NOVO BLOCO DE LÓGICA DE RESPOSTA ---
        
        # 1. Verificando se é um COMANDO DE EXECUÇÃO
        if ai_reply.strip().startswith("[EXECUTAR_UPDATE]"):
            print("✅ Admin confirmou. Executando update no DB...")
            try:
                json_start = ai_reply.find('{')
                json_end = ai_reply.rfind('}') + 1
                if json_start == -1: raise ValueError("JSON de update não encontrado")
                
                update_json_string = ai_reply[json_start:json_end]
                update_data = json.loads(update_json_string)
                
                # Executa o update no MongoDB
                menu_collection.update_one(
                    {'_id': 'menu_principal'},
                    {'$set': update_data}
                )
                
                print("✅✅✅ MENU ATUALIZADO NO BANCO DE DADOS! ✅✅✅")
                return "Pronto! O menu foi atualizado com sucesso. Os próximos clientes já verão as mudanças."
                
            except Exception as e:
                print(f"❌ ERRO AO EXECUTAR UPDATE: {e}")
                return f"Tive um erro ao tentar salvar no banco: {e}. Por favor, tente de novo."
        
        # 2. Verificando se é um PEDIDO PARA VER O CARDÁPIO (Request 1)
        elif ai_reply.strip().startswith("[VER_CARDAPIO]"):
            print("ℹ️ Admin pediu para ver o cardápio atual.")
            try:
                # 'current_menu' já foi carregado no início desta função
                # Apenas formatamos para o admin
                menu_formatado = "--- 📋 CARDÁPIO / ESTOQUE ATUAL 📋 ---\n\n"
                
                pratos = current_menu.get('prato_do_dia', [])
                if not pratos:
                    menu_formatado += "Prato do Dia: (Vazio)\n"
                else:
                    menu_formatado += "Prato(s) do Dia:\n"
                    for prato in pratos:
                        menu_formatado += f" - {prato}\n"
                
                menu_formatado += f"\nAcompanhamentos: {current_menu.get('acompanhamentos') or '(Vazio)'}\n"
                
                marmitas = current_menu.get('marmitas', [])
                if not marmitas:
                    menu_formatado += "\nMarmitas: (Vazio)\n"
                else:
                    menu_formatado += "\nMarmitas:\n"
                    for item in marmitas:
                        menu_formatado += f" - {item.get('nome', '?')}: R${item.get('preco', 0.0):.2f}\n"
                
                bebidas = current_menu.get('bebidas', [])
                if not bebidas:
                    menu_formatado += "\nBebidas: (Vazio)\n"
                else:
                    menu_formatado += "\nBebidas:\n"
                    for item in bebidas:
                        menu_formatado += f" - {item.get('nome', '?')}: R${item.get('preco', 0.0):.2f}\n"
                
                menu_formatado += f"\nTaxa de Entrega: R${current_menu.get('taxa_entrega', 0.0):.2f}"
        
                return menu_formatado.strip()
            
            except Exception as e:
                print(f"❌ Erro ao formatar cardápio para admin: {e}")
                return "Erro ao tentar formatar o cardápio."

        # 3. Verificando se é uma MENSAGEM DE CONFIRMAÇÃO (Request 3)
        elif "[CONFIRMAR_UPDATE]" in ai_reply:
            print("ℹ️ IA gerou uma mensagem de confirmação para o admin.")
            
            # O admin NÃO deve ver a tag.
            # O prompt foi instruído a gerar: [TAG]{JSON}Texto amigável
            
            json_end_index = ai_reply.rfind('}')
            if json_end_index != -1:
                # Pega o texto DEPOIS do '}'
                visible_reply = ai_reply[json_end_index + 1:].strip()
                if visible_reply:
                    return visible_reply
            
            # Se falhou (ex: o texto veio antes, como no seu log),
            # vamos pegar o texto ANTES da tag [CONFIRMAR_UPDATE]
            tag_start_index = ai_reply.find("[CONFIRMAR_UPDATE]")
            if tag_start_index != -1:
                visible_reply = ai_reply[:tag_start_index].strip()
                if visible_reply:
                    return visible_reply
                    
            # Se ambas as lógicas falharem, é um erro de prompt
            print(f"❌ Erro de prompt admin: A IA gerou a tag [CONFIRMAR_UPDATE] mas não foi possível extrair o texto. Resposta: {ai_reply}")
            return "Entendi. Confirma a alteração? (Erro ao formatar JSON)"

        # 4. Se não for nenhum dos anteriores, é uma pergunta normal da IA (ex: "Qual o preço?")
        else:
            return ai_reply

        # --- FIM DO NOVO BLOCO DE LÓGICA DE RESPOSTA ---

    except Exception as e:
        print(f"❌ Erro em 'gerar_resposta_admin': {e}")
        return f"Desculpe, tive um erro no modo admin: {e}"

def gerar_resposta_ia(contact_id, sender_name, user_message, contact_phone):
    """
    Gera uma resposta usando a IA.
    Esta versão é STATELESS: ela não usa cache de memória e lê o histórico
    do MongoDB a cada chamada, garantindo consistência entre os workers.
    """
    global modelo_ia
    if not modelo_ia:
        return "Desculpe, estou com um problema interno (modelo IA não carregado)."
    print(f"🧠 Lendo o estado do DB para {contact_id}...")
    convo_data = load_conversation_from_db(contact_id)
    known_customer_name = None
    old_history = []
    
    if convo_data:
        known_customer_name = convo_data.get('customer_name')
        if 'history' in convo_data:
            history_from_db = [msg for msg in convo_data['history'] if not msg['text'].strip().startswith("A data e hora atuais são:")]
            old_history = []
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
    try:
        fuso_horario_local = pytz.timezone('America/Sao_Paulo')
        agora_local = datetime.now(fuso_horario_local)
        horario_atual = agora_local.strftime("%Y-%m-%d %H:%M:%S")
        menu_dinamico_string = formatar_menu_para_prompt()
        print(f"⏰ Hora local (America/Sao_Paulo) definida para: {horario_atual}")
    except Exception as e:
        print(f"⚠️ Erro ao definir fuso horário, usando hora do servidor. Erro: {e}")
        horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt_name_instruction = ""
    final_user_name_for_prompt = ""
    
    if known_customer_name:
        final_user_name_for_prompt = known_customer_name
        prompt_name_instruction = f"""
        REGRA DE NOME: O nome do cliente JÁ FOI CAPTURADO. O nome dele é {final_user_name_for_prompt}.
        NÃO pergunte o nome dele novamente.
        (IMPORTANTE: Use o nome dele UMA VEZ por saudação, não em toda frase. Ex: "Certo, {final_user_name_for_prompt}!" e não "Certo, {final_user_name_for_prompt}! Seu pedido, {final_user_name_for_prompt}, é...")
        """
    else:
        final_user_name_for_prompt = sender_name
        prompt_name_instruction =  f"""
        REGRA CRÍTICA - CAPTURA DE NOME (PRIORIDADE MÁXIMA):
         Seu nome é {{Lyra}}. Seu primeiro objetivo é descobrir o nome real do cliente ('{sender_name}' é um apelido).
         1. Se a mensagem for "oi", "bom dia", etc., se apresente e peça o nome.
         2. Se a mensagem for uma pergunta (ex: "quero uma marmita"), diga que já vai ajudar, mas primeiro peça o nome para personalizar o atendimento. Guarde a pergunta original.
         3. Quando o cliente responder o nome (ex: "marcelo"), sua resposta DEVE começar com a tag: `[NOME_CLIENTE]O nome do cliente é: [Nome Extraído].`
         4. Imediatamente após a tag, agradeça e RESPONDA A PERGUNTA ORIGINAL que ele fez (ex: "Obrigada, Marcelo! Sobre a marmita, nosso cardápio é...").
         5. (IMPORTANTE: Ao extrair o nome, NÃO o repita no resto da sua resposta. Agradeça UMA VEZ. Ex: "Obrigada, Marcelo! Sobre a marmita...")
        """

    prompt_bifurcacao = ""
    if BIFURCACAO_ENABLED:
        prompt_bifurcacao = f"""
        =====================================================
        ⚙️ MODO DE BIFURCAÇÃO DE PEDIDOS (PRIORIDADE ALTA)
        =====================================================
        Esta é a sua principal função. Você DEVE seguir este fluxo com extrema precisão, passo a passo.

        1.  **MISSÃO:** Preencher TODOS os campos do "Gabarito de Pedido" abaixo.
        2.  **PERSISTÊNCIA:** Você deve ser um robô persistente. Se o cliente não fornecer uma informação (ex: Bairro), pergunte novamente até conseguir.
        3.  **COLETA DE DADOS (SEQUENCIAL E OBRIGATÓRIA):**
            a. **Item:** Pergunte o(s) item(ns) e tamanho(s).
            b. **Observações:** Pergunte se há modificações (ex: "sem salada").
            c. **Bebida:** Ofereça bebidas.
            d. **Tipo de Pedido:** Pergunte se é "Entrega" ou "Retirada".
            e. **Endereço (CRÍTICO):** Se for "Entrega", você DEVE obter "Rua", "Número" e "Bairro".
            f. **Pagamento:** Pergunte a forma de pagamento.
        4.  **TELEFONE:** O campo "telefone_contato" JÁ ESTÁ PREENCHIDO. É {contact_phone}. NÃO pergunte o telefone.
        5.  **CÁLCULO:** Calcule o `valor_total` somando itens, bebidas e a `taxa_entrega` (APENAS se for 'Entrega'. Se for 'Retirada', a taxa é R$ 0,00).
        6.  **CONFIRMAÇÃO FINAL:**
            - Após ter TODOS os dados, você DEVE apresentar um RESUMO COMPLETO.
            - O resumo deve ter TODOS os campos: Cliente, Pedido, Obs, Bebidas, Endereço, Pagamento, Valor Total.
            - Você DEVE terminar perguntando "Confirma o pedido?".
        
        # --- CORREÇÃO 2 (Vazamento de JSON) ---
        7.  **REGRA DE SIGILO (NÃO MOSTRE O GABARITO):**
            - O "Gabarito de Pedido" e o JSON são seus pensamentos internos e ferramentas de sistema.
            - O cliente NUNCA deve ver o JSON, a palavra "Gabarito", ou chaves `{{ }}`.
            - Para o cliente, você escreve apenas o RESUMO formatado de forma amigável (como no Passo 6).
        # --- FIM DA CORREÇÃO 2 ---
        
        8.  **REGRA MESTRA (A MAIS IMPORTANTE DE TODAS):**
            - QUANDO o cliente enviar uma mensagem de confirmação (como "isso mesmo", "sim", "confirmo", "pode ser") LOGO APÓS você apresentar o resumo (Passo 6),
            - Sua ÚNICA E EXCLUSIVA AÇÃO deve ser gerar a tag `[PEDIDO_CONFIRMADO]` seguida pelo JSON VÁLIDO.
            - **IMPORTANTE:** A tag `[PEDIDO_CONFIRMADO]` é um comando de sistema. O cliente não a verá.
            - **APÓS** a tag e o JSON, você *DEVE* adicionar uma curta mensagem de despedida (ex: "Pedido confirmado, Mateus! Agradecemos a preferência!").
            - **NÃO GERE ` ``` `.**
            - Se o cliente pedir para editar (ex: "tira o suco"), você DEVE editar o gabarito e voltar ao passo 6 (apresentar novo resumo).

        --- GABARITO DE PEDIDO (DEVE SER PREENCHIDO, NÃO MOSTRADO) ---
        {{
          "nome_cliente": "...", (Use o nome que você já sabe)
          "tipo_pedido": "...", (Deve ser "Entrega" ou "Retirada")
          "endereco_completo": "...", (Deve conter Rua, Número e Bairro. Se 'Retirada', preencha com 'Retirada no Local')
          "telefone_contato": "{contact_phone}", (JÁ PREENCHIDO)
          "pedido_completo": "...", (Ex: "1 Marmita M, 2 Marmitas P")
          "bebidas": "...", (Ex: "2 Coca-Cola Lata, 1 Suco de Laranja")
          "forma_pagamento": "...", (ex: "Pix")
          "observacoes": "...", (CRÍTICO: Deve incluir "sem salada", "as 2 P sem salada", etc.)
          "valor_total": "..." (O valor total calculado por você)
        }}
        --- FIM DO GABARITO ---
        
        EXEMPLO DE FALHA (ERRADO):
        Cliente: isso mesmo
        Você: Pedido confirmado, Mateus! Agradecemos a preferência!
        (ERRADO! Faltou a tag [PEDIDO_CONFIRMADO] e o JSON)

        EXEMPLO DE SUCESSO (CORRETO):
        Cliente: isso mesmo
        Você: [PEDIDO_CONFIRMADO]{{"nome_cliente": "Mateus", "tipo_pedido": "Retirada", ...}}Pedido confirmado, Mateus! Agradecemos a preferência e até logo!
        """
    else:
        prompt_bifurcacao = "O plano de Bifurcação (envio para cozinha) não está ativo."
    
    prompt_inicial = f"""
        A data e hora atuais são: {horario_atual}.
        {prompt_name_instruction}
        =====================================================
        🏷️ IDENTIDADE DO ATENDENTE
        =====================================================
        nome: {{Lyra}}
        função: {{Atendente de restaurante (delivery)}} 
        papel: {{Você deve atender o cliente, apresentar o cardápio, anotar o pedido completo (Gabarito de Pedido), calcular o valor total e confirmar a entrega.}}
        =====================================================
        🏢 IDENTIDADE DA EMPRESA
        =====================================================
        nome da empresa: {{Marmitaria Sabor do Dia}}
        
        # (Horário está desabilitado para testes)
        # horário de atendimento: {{Segunda a Sábado, das 11:00 às 14:00}}
        
        =====================================================
        🍲 CARDÁPIO E PREÇOS (BASE DO PEDIDO)
        =====================================================
        {menu_dinamico_string}
        {prompt_bifurcacao} 
        =====================================================
        🧭 COMPORTAMENTO E REGRAS DE ATENDIMENTO
        =====================================================
        - FOCO TOTAL: Seu primeiro objetivo é capturar o nome do cliente (se ainda não souber). Seu segundo objetivo é preencher o "Gabarito de Pedido" e confirmar.

        # --- INÍCIO DA NOVA REGRA (MÚLTIPLOS SABORES) ---
        - **REGRA DE ESCOLHA (PRIORIDADE ALTA):**
          - Se a seção "PRATO DO DIA" do cardápio mostrar o texto "OPÇÕES DE PRATO: [Opção A, Opção B, ...]",
          - Isso significa que o cliente DEVE escolher UMA dessas opções.
          - Se o cliente pedir uma marmita (ex: "quero 1 M"), sua primeira pergunta DEVE ser qual dos sabores ele prefere.
          - **Exemplo de fala:** "Certo, Dani! A marmita M hoje pode ser de Carne de panela ou Frango frito. Qual das duas você prefere?"
          - Ao preencher o "Gabarito de Pedido", o campo "pedido_completo" deve conter o sabor escolhido (ex: "1 Marmita M (Frango Frito)").
        # --- FIM DA NOVA REGRA ---

        - MEMÓRIA: Você deve lembrar do que o cliente já disse (nome, itens) e não perguntar de novo.
        - NÃO FAZER: Não inventar pratos ou preços. Não falar sobre "IA" ou "Chatbot". Você é uma ATENDENTE HUMANA (Lyra).

        # --- CORREÇÃO 3 (Confusão de Pedido: 1 coca + 1 agua) ---
        - ATENÇÃO MÁXIMA: Leia as ÚLTIMAS mensagens do cliente com muito cuidado. Se ele enviar duas mensagens seguidas (ex: "1 coca" e logo depois "1 agua"), ele quer OS DOIS ITENS. Não ignore a segunda mensagem. Preste atenção no histórico recente.
        =====================================================
        PRONTO PARA ATENDER O CLIENTE
        =====================================================
        """

    convo_start = [
        {'role': 'user', 'parts': [prompt_inicial]},
        {'role': 'model', 'parts': [f"Entendido. Eu sou Lyra. Minha prioridade é capturar o nome do cliente (se eu ainda não souber) e depois anotar o pedido rigorosamente, seguindo a REGRA MESTRA. Estou pronta."]}
    ]
    
    chat_session = modelo_ia.start_chat(history=convo_start + old_history)
    
    try:
        print(f"Enviando para a IA: '{user_message}' (De: {sender_name})")
        
        try:
            input_tokens = modelo_ia.count_tokens(chat_session.history + [{'role':'user', 'parts': [user_message]}]).total_tokens
        except Exception:
            input_tokens = 0

        resposta = chat_session.send_message(user_message)
        
        try:
            output_tokens = modelo_ia.count_tokens(resposta.text).total_tokens
        except Exception:
            output_tokens = 0
            
        total_tokens_na_interacao = input_tokens + output_tokens
        
        if total_tokens_na_interacao > 0:
             print(f"📊 Consumo de Tokens: Total={total_tokens_na_interacao}")
        
        ai_reply = resposta.text
        
        customer_name_to_save = known_customer_name 

        if ai_reply.strip().startswith("[NOME_CLIENTE]"):
            print("📝 Tag [NOME_CLIENTE] detectada. Extraindo e salvando nome...")
            try:
                full_response_part = ai_reply.split("O nome do cliente é:")[1].strip()
                
                # Pega o nome e remove qualquer ponto final
                extracted_name = full_response_part.split('.')[0].strip()
                
                # --- CORREÇÃO 1 (Evitar "DaniDani") ---
                # Garante que estamos pegando apenas o primeiro nome se houver lixo
                extracted_name = extracted_name.split(' ')[0].strip() 
                
                start_of_message_index = full_response_part.find(extracted_name) + len(extracted_name)
                ai_reply = full_response_part[start_of_message_index:].lstrip('.!?, ').strip()

                customer_name_to_save = extracted_name
                
                conversation_collection.update_one(
                    {'_id': contact_id},
                    {'$set': {'customer_name': extracted_name}},
                    upsert=True
                )
                print(f"✅ Nome '{extracted_name}' salvo no DB para o cliente {contact_id}.")

            except Exception as e:
                print(f"❌ Erro ao extrair o nome da tag: {e}")
                ai_reply = ai_reply.replace("[NOME_CLIENTE]", "").strip()

        save_conversation_to_db(contact_id, sender_name, customer_name_to_save, chat_session, total_tokens_na_interacao)
        
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

def send_whatsapp_message(number, text_message):
    """Envia uma mensagem de texto via Evolution API, corrigindo a URL dinamicamente."""
    
    INSTANCE_NAME = "chatbot" 
    
    clean_number = number.split('@')[0]
    payload = {"number": clean_number, "textMessage": {"text": text_message}}
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}

    base_url = EVOLUTION_API_URL
    api_path = f"/message/sendText/{INSTANCE_NAME}"
    
    final_url = ""
    
    # Caso 1: A variável de ambiente JÁ é a URL completa
    if base_url.endswith(api_path):
        final_url = base_url
    elif base_url.endswith('/'):
        final_url = base_url[:-1] + api_path
    else:
        final_url = base_url + api_path
    # --- FIM DA LÓGICA ---

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

scheduler = BackgroundScheduler(daemon=True, timezone='America/Sao_Paulo')
scheduler.start()

app = Flask(__name__)
processed_messages = set() 


@app.route('/webhook', methods=['POST'])
def receive_webhook():
    """Recebe mensagens do WhatsApp e as coloca no buffer."""
    data = request.json
    print(f"📦 DADO BRUTO RECEBIDO NO WEBHOOK: {data}")

    event_type = data.get('event')
    
    if event_type != 'messages.upsert':
        print(f"➡️  Ignorando evento: {event_type} (não é uma nova mensagem)")
        return jsonify({"status": "ignored_event_type"}), 200

    try:
        message_data = data.get('data', {}) 
        if not message_data:
            print("➡️  Evento 'messages.upsert' sem 'data'. Ignorando.")
            return jsonify({"status": "ignored_no_data"}), 200
        
        key_info = message_data.get('key', {})

        if key_info.get('fromMe'):
            print(f"➡️  Mensagem do próprio bot ignorada.")
            return jsonify({"status": "ignored_from_me"}), 200

        message_id = key_info.get('id')
        if not message_id:
            return jsonify({"status": "ignored_no_id"}), 200

        if message_id in processed_messages:
            print(f"⚠️ Mensagem {message_id} já processada, ignorando.")
            return jsonify({"status": "ignored_duplicate"}), 200
        processed_messages.add(message_id)
        if len(processed_messages) > 1000:
            processed_messages.clear()

        # --- LÓGICA DE BUFFER ---
        handle_message_buffering(message_data)
        
        return jsonify({"status": "received"}), 200

    except Exception as e:
        print(f"❌ Erro inesperado no webhook: {e}")
        print("DADO QUE CAUSOU ERRO:", data)
        return jsonify({"status": "error"}), 500

@app.route('/', methods=['GET'])
def health_check():
    return "Estou vivo! (Marmitaria Bot)", 200

def handle_message_buffering(message_data):
    """
    Agrupa mensagens de um mesmo usuário que chegam rápido
    e dispara o processamento após um 'delay'.
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

        # Adiciona a mensagem de texto ao buffer
        if clean_number not in message_buffer:
            message_buffer[clean_number] = []
        message_buffer[clean_number].append(user_message_content)
        
        print(f"📥 Mensagem adicionada ao buffer de {clean_number}: '{user_message_content}'")

        # Se já existe um timer, cancela ele (vamos esperar mais)
        if clean_number in message_timers:
            message_timers[clean_number].cancel()

        # Inicia um NOVO timer
        timer = threading.Timer(
            BUFFER_TIME_SECONDS, 
            _trigger_ai_processing, 
            args=[clean_number, message_data] # Passa o 'message_data' da ÚLTIMA mensagem
        )
        message_timers[clean_number] = timer
        timer.start()
        print(f"⏰ Buffer de {clean_number} resetado. Aguardando {BUFFER_TIME_SECONDS}s...")

    except Exception as e:
        print(f"❌ Erro no 'handle_message_buffering': {e}")
            
def _trigger_ai_processing(clean_number, last_message_data):
    """
    Função chamada pelo Timer. Junta as mensagens e chama a IA.
    """
    global message_buffer, message_timers
    
    if clean_number not in message_buffer:
        return 

    # 1. Pega todas as mensagens agrupadas e limpa o buffer
    messages_to_process = message_buffer.pop(clean_number, [])
    if clean_number in message_timers:
        del message_timers[clean_number]
        
    if not messages_to_process:
        return

    # 2. Junta as mensagens
    # Ex: ["Quero 1 p", "E", "Uma m"] -> "Quero 1 p. E. Uma m"
    full_user_message = ". ".join(messages_to_process)
    
    print(f"⚡️ DISPARANDO IA para {clean_number} com mensagem agrupada: '{full_user_message}'")

    # 3. Chama a função de processamento principal
    threading.Thread(target=process_message_logic, args=(last_message_data, full_user_message)).start()


def process_message_logic(message_data, buffered_message_text=None):
    """
    Esta é a função "worker" principal. Ela pega o lock e chama a IA.
    (Versão corrigida com 'upsert=True' para novos usuários)
    """
    lock_acquired = False
    clean_number = None
    
    try:
        key_info = message_data.get('key', {})
        sender_number_full = key_info.get('senderPn') or key_info.get('participant') or key_info.get('remoteJid')
        if not sender_number_full or sender_number_full.endswith('@g.us'): return
        
        clean_number = sender_number_full.split('@')[0]
        sender_name_from_wpp = message_data.get('pushName') or 'Cliente'

        IS_ADMIN = bool(BIFURCACAO_ENABLED and clean_number == ADMIN_WPP_NUMBER)

        # --- Pega o Lock ---
        now = datetime.now()
        
        # --- CORREÇÃO APLICADA AQUI ---
        res = conversation_collection.update_one(
            {'_id': clean_number, 'processing': {'$ne': True}},
            {'$set': {'processing': True, 'processing_started_at': now}},
            upsert=True  # <--- ADICIONADO: Cria o documento se for um novo usuário
        )

        # Nova lógica de verificação:
        # Se não deu "match" E também não criou um novo doc (upsert),
        # então o doc já existia e estava com 'processing: True'.
        if res.matched_count == 0 and res.upserted_id is None:
            # Isso agora é a ÚNICA condição de "lock" real
            print(f"⏳ {clean_number} já está sendo processado (lock). Reagendando...")
            
            # (Lógica de reagendamento)
            if buffered_message_text:
                if clean_number not in message_buffer: message_buffer[clean_number] = []
                message_buffer[clean_number].insert(0, buffered_message_text)
            
            timer = threading.Timer(10.0, _trigger_ai_processing, args=[clean_number, message_data])
            message_timers[clean_number] = timer
            timer.start()
            return # Sai da função. O 'finally' será executado, mas lock_acquired é False.
        
        # --- TEMOS O LOCK! ---
        lock_acquired = True
        if res.upserted_id:
             print(f"✅ Novo usuário {clean_number}. Documento criado e lock adquirido.")
        # --- FIM DA CORREÇÃO ---
        
        user_message_content = None
        
        if buffered_message_text:
            user_message_content = buffered_message_text
            messages_to_save = user_message_content.split(". ")
            for msg_text in messages_to_save:
                if msg_text and msg_text.strip():
                    append_message_to_db(clean_number, 'user', msg_text)
        else:
            message = message_data.get('message', {})
            if message.get('audioMessage') and message.get('base64'):
                message_id = key_info.get('id')
                print(f"🎤 Mensagem de áudio recebida de {clean_number}. Transcrevendo...")
                audio_base64 = message['base64']
                audio_data = base64.b64decode(audio_base64)
                temp_audio_path = f"/tmp/audio_{clean_number}_{message_id}.ogg"
                with open(temp_audio_path, 'wb') as f: f.write(audio_data)
                user_message_content = transcrever_audio_gemini(temp_audio_path)
                os.remove(temp_audio_path)
                if not user_message_content:
                    send_whatsapp_message(sender_number_full, "Desculpe, não consegui entender o áudio. Pode tentar novamente? 🎧")
                    user_message_content = "[Usuário enviou um áudio incompreensível]"
            
            if not user_message_content:
                 user_message_content = "[Usuário enviou uma mensagem não suportada]"
                 
            append_message_to_db(clean_number, 'user', user_message_content)

        print(f"🧠 Processando Mensagem de {clean_number}: '{user_message_content}'")
        
        ai_reply = None
        if IS_ADMIN:
            ai_reply = gerar_resposta_admin(clean_number, user_message_content)
        else:
            ai_reply = gerar_resposta_ia(
                clean_number,
                sender_name_from_wpp,
                user_message_content,
                clean_number
            )
        
        if not ai_reply:
             print("⚠️ A IA não gerou resposta.")
             return

        try:
            append_message_to_db(clean_number, 'assistant', ai_reply)
            
            if BIFURCACAO_ENABLED and ai_reply.strip().startswith("[PEDIDO_CONFIRMADO]"):
                print(f"📦 Tag [PEDIDO_CONFIRMADO] detectada. Processando e bifurcando pedido para {clean_number}...")
                json_start = ai_reply.find('{')
                json_end = ai_reply.rfind('}') + 1
                if json_start == -1 or json_end == 0: raise ValueError("JSON de pedido não encontrado após a tag.")

                json_string = ai_reply[json_start:json_end]
                remaining_reply = ai_reply[json_end:].strip()
                if not remaining_reply: remaining_reply = "Seu pedido foi confirmado e enviado para a cozinha! 😋"

                order_data = json.loads(json_string)

                msg_cozinha = f"""
                --- 🍳 NOVO PEDIDO (COZINHA) 🍳 ---
                Cliente: {order_data.get('nome_cliente', 'N/A')}
                Telefone: {order_data.get('telefone_contato', 'N/A')}
                Tipo: {order_data.get('tipo_pedido', 'N/A')}
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

                msg_motoboy = f"""
                --- 🛵 NOVA ENTREGA (MOTOBOY) 🛵 ---
                Cliente: {order_data.get('nome_cliente', 'N/A')}
                Telefone: {order_data.get('telefone_contato', 'N/A')}
                Tipo: {order_data.get('tipo_pedido', 'N/A')}
                Endereço: {order_data.get('endereco_completo', 'N/A')}
                Forma de Pagto: {order_data.get('forma_pagamento', 'N/A')}
                Valor Total: {order_data.get('valor_total', 'N/A')}
                """

                threading.Thread(target=send_whatsapp_message, args=(f"{COZINHA_WPP_NUMBER}@s.whatsapp.net", msg_cozinha.strip())).start()
                if order_data.get('tipo_pedido') == "Entrega":
                    threading.Thread(target=send_whatsapp_message, args=(f"{MOTOBOY_WPP_NUMBER}@s.whatsapp.net", msg_motoboy.strip())).start()
                
                send_whatsapp_message(sender_number_full, remaining_reply)

            else:
                print(f"🤖 Resposta (normal) da IA para {sender_name_from_wpp}: {ai_reply}")
                send_whatsapp_message(sender_number_full, ai_reply)

        except Exception as e:
            print(f"❌ Erro ao processar bifurcação ou envio: {e}")
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
    inicializar_menu_padrao() 
    
    print("\n=============================================")
    print(f"   CHATBOT WHATSAPP COM IA INICIADO")
    print(f"   CLIENTE: {CLIENT_NAME}")
    
    if not BIFURCACAO_ENABLED:
        print("   AVISO: 'COZINHA_WPP_NUMBER' ou 'MOTOBOY_WPP_NUMBER' não configurados. O recurso de bifurcação está DESATIVADO.")
    else:
        print(f"   Bifurcação ATIVA. Cozinha: {COZINHA_WPP_NUMBER} | Motoboy: {MOTOBOY_WPP_NUMBER}")
    
    print("=============================================")
    print("Servidor aguardando mensagens no webhook...")

    # Inicia o agendador de relatórios
    scheduler.add_job(gerar_e_enviar_relatorio_semanal, 'cron', day_of_week='sun', hour=8, minute=0)
    print("⏰ Agendador de relatórios iniciado. O relatório será enviado todo Domingo às 08:00.")
    
    import atexit
    atexit.register(lambda: scheduler.shutdown())
    
else:
    print("\nEncerrando o programa devido a erros na inicialização (modelo_ia falhou).")


if __name__ == '__main__':
    # Esta parte só roda se você executar 'python main.py'
    # Gunicorn NÃO executa isso.
    print("Iniciando em MODO DE DESENVOLVIMENTO LOCAL (app.run)...")
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)