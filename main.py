
import google.generativeai as genai
import requests
import os
from flask import Flask, request, jsonify
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "1234") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"AVISO: A chave de API do Google não foi configurada corretamente. Erro: {e}")
else:
    print("AVISO: A variável de ambiente GEMINI_API_KEY não foi definida.")

conversations = {}

modelo_ia = None
try:
    modelo_ia = genai.GenerativeModel('gemini-2.5-flash') # Recomendo usar o 1.5-flash
    print("✅ Modelo do Gemini inicializado com sucesso.")
except Exception as e:
    print(f"❌ ERRO: Não foi possível inicializar o modelo do Gemini. Verifique sua API Key. Erro: {e}")


def gerar_resposta_ia(contact_id, sender_name, user_message):
    """
    Gera uma resposta usando a IA do Gemini, mantendo o histórico da conversa
    em memória para cada contato.
    """
    global modelo_ia, conversations

    if not modelo_ia:
        return "Desculpe, estou com um problema interno (modelo IA não carregado) e não consigo responder agora."

    if contact_id not in conversations:
        print(f"Iniciando nova sessão de chat para o contato: {sender_name} ({contact_id})")
        
        horario_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        historico_anterior = "Nenhum histórico encontrado para esta sessão."
        
        prompt_inicial = f"""
            A data e hora atuais são: {horario_atual}.
            O nome do usuário com quem você está falando é: {sender_name}.
            Histórico anterior: {historico_anterior}.
            Voce é o atendente.
            =====================================================
            🏷️ IDENTIDADE DO ATENDENTE
            =====================================================
            nome: {{Isaque}}
            sexo: {{Masculino}}
            idade: {{40}}
            função: {{Atendente, vendedor, especialista em Ti e machine learning}} 
            papel: {{Você deve atender a pessoa, entender a necessidade da pessoa, vender o plano de acordo com a  necessidade, tirar duvidas, ajudar.}}  (ex: tirar dúvidas, passar preços, enviar catálogos, agendar horários)

            =====================================================
            🏢 IDENTIDADE DA EMPRESA
            =====================================================
            nome da empresa: {{Neuro Soluções em Tecnologia}}
            setor: {{Tecnologia e Automação}} 
            missão: {{Facilitar e organizar as empresas de clientes.}}
            valores: {{Organização, trasparencia,persistencia e ascenção.}}
            horário de atendimento: {{De segunda-feira a sexta-feira das 8:00 as 18:00}}
            contatos: {{44991676564}} 
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
                                Envia notificações e lembretes para o telefone do responsável sempre que houver mudança ou novo agendamento.

                                💻 Agenda Integrada:
                                Acompanha um software externo conectado ao WhatsApp, permitindo manter todos os dados organizados e atualizados exatamente como negociado.}}
            - Plano Premium: {{Em construção}}
            - {{}}

            =====================================================
            💰 PLANOS E VALORES
            =====================================================
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

            falas:
            - Use linguagem simples e amigável.
            - Evite termos técnicos, a menos que o cliente peça.
            - Não use emojis em excesso (máximo 2 por mensagem).

            saudações:
            - Sempre cumprimente com entusiasmo e simpatia.
            Exemplo: "Olá! 😊 Seja muito bem-vindo(a) à {{Neuro Soluções em Tecnologia}}!"

            agradecimentos:
            - Agradeça de forma sincera e breve.
            Exemplo: "Agradeço o seu contato! Foi um prazer ajudar. 🙏"

            despedidas:
            - Despeça-se com elegância e positividade.
            Exemplo: "Tenha um ótimo dia! Ficamos à disposição sempre que precisar. 🌟"

            não deve fazer:
            - Não inventar informações que não saiba.
            - Não discutir, nem responder de forma rude.
            - Não compartilhar dados pessoais.
            - Não responder perguntas fora do contexto da empresa.

            missão:
            - Ajudar o cliente a obter respostas rápidas e confiáveis.
            - Gerar uma boa experiência no atendimento.
            - Reforçar o nome e a credibilidade da empresa.

            =====================================================
            ⚙️ PERSONALIDADE DO ATENDENTE
            =====================================================
            - Tom de voz: {{alegre, acolhedor, profissional, descontraído}} 
            - Ritmo de conversa: natural e fluido.
            - Estilo: humano, prestativo e simpático.
            - Emojis: usar com moderação, sempre com propósito.
            - Curiosidade: se o cliente parecer indeciso, ofereça ajuda com sugestões.

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
        
        chat = modelo_ia.start_chat(history=[
            {'role': 'user', 'parts': [prompt_inicial]},
            {'role': 'model', 'parts': [f"Entendido. Perfil de personalidade e todas as regras assimiladas. Olá, {sender_name}! Como posso te ajudar?"]}
        ])
        
        conversations[contact_id] = {'ai_chat_session': chat, 'name': sender_name}

    chat_session = conversations[contact_id]['ai_chat_session']
    
    try:
        print(f"Enviando para a IA: '{user_message}' (De: {sender_name})")
        resposta = chat_session.send_message(user_message)
        return resposta.text
    except Exception as e:
        print(f"❌ Erro ao comunicar com a API do Gemini: {e}")

        del conversations[contact_id]
        return "Tive um pequeno problema para processar sua mensagem e precisei reiniciar nossa conversa. Você poderia repetir, por favor?"

def send_whatsapp_message(number, text_message):
    """Envia uma mensagem de texto para um número via Evolution API."""
    clean_number = number.split('@')[0]
    payload = {"number": clean_number, "textMessage": {"text": text_message}}
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    
    try:
        response = requests.post(EVOLUTION_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        print(f"✅ Resposta da IA enviada com sucesso para {clean_number}\n")
    except requests.exceptions.RequestException as e:
        print(f"❌ Erro ao enviar mensagem para {clean_number}: {e}")

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def receive_webhook():
    """Recebe as mensagens do WhatsApp enviadas pela Evolution API."""
    data = request.json
    try:
        message_data = data.get('data', {})
        key_info = message_data.get('key', {})

        if key_info.get('fromMe'):
            return jsonify({"status": "ignored_from_me"}), 200

        sender_number_full = key_info.get('senderPn') or key_info.get('remoteJid')
        if not sender_number_full:
            print("Ignorando webhook sem 'remoteJid'")
            return jsonify({"status": "ignored_no_sender"}), 200
        
        clean_number = sender_number_full.split('@')[0]

        message_text = (
            message_data.get('message', {}).get('conversation') or
            message_data.get('message', {}).get('extendedTextMessage', {}).get('text')
        )

        if message_text:
            sender_name = message_data.get('pushName') or 'Desconhecido'
            
            print("\n----------- NOVA MENSAGEM RECEBIDA -----------")
            print(f"De: {sender_name} ({clean_number})")
            print(f"Mensagem: {message_text}")
            print("----------------------------------------------")

            print("🤖 Processando com a Inteligência Artificial...")
            ai_reply = gerar_resposta_ia(clean_number, sender_name, message_text)
            print(f"🤖 Resposta gerada: {ai_reply}")

            send_whatsapp_message(sender_number_full, ai_reply)

    except Exception as e:
        print(f"❌ Erro inesperado no webhook: {e}")
        # Logar o dado recebido para depuração
        print("DADO RECEBIDO QUE CAUSOU ERRO:", data)

    return jsonify({"status": "success"}), 200

if __name__ == '__main__':
    if modelo_ia:
        print("\n=============================================")
        print("   CHATBOT WHATSAPP COM IA INICIADO")
        print("=============================================")
        print("Servidor aguardando mensagens no webhook...")
        
        app.run(host='0.0.0.0', port=8000)
    else:
        print("\n encerrando o programa devido a erros na inicialização.")