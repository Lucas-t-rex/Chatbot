"""
PRÉVIA do Analista com IA — roda os textos e a planilha REAIS em cima de dados
FICTÍCIOS (nenhum dado de cliente, nenhuma chamada à IA, nada de banco).

Uso (dentro de chatbot-python/):
    python scripts/previa_analista.py --saida PASTA
    python scripts/previa_analista.py --saida PASTA --enviar-para 554491586345

Sem --enviar-para só grava os arquivos (diario_22h.txt, semanal.txt,
planilha_semana.xlsx). Com --enviar-para, manda tudo para esse número, marcado
como PRÉVIA. Recusa o número do dono.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import pytz  # noqa: E402

from app.services import analista, analista_excel, analista_ia as aia, analista_relatorios as rel  # noqa: E402

TZ = pytz.timezone("America/Sao_Paulo")
SEGUNDA = datetime(2026, 10, 12)          # a semana fictícia: seg 12/10 a sáb 17/10/2026
DIA_DO_DIARIO = 14                        # o resumo das 22h mostrado é o de quarta 14/10


def ts(dia, hora, minuto=0):
    return TZ.localize(datetime(2026, 10, dia, hora, minuto))


# nome, dia, hora, desfecho, nota, nova, interesse, resumo, ponto_forte, pode_melhorar, proxima_acao
CONVERSAS = [
    ("Maria Souza", 12, 10, "em_andamento", 5.4, True, "musculação e muay thai; iniciante, receio de lesão",
     "Maria, iniciante de 40 anos, pediu valores e aula teste; vai falar com o marido.",
     "Trouxe segurança quando ela contou do receio: \"Temos professores que acompanham sim\".",
     "Quando ela pediu a aula teste, dá para já propor dia e hora.",
     "Oferecer aula teste para o casal, com dia e horário."),
    ("João Lima", 12, 14, "sucesso", 8.9, True, "muay thai, quer emagrecer",
     "João quer emagrecer com muay thai e já marcou aula experimental para quarta.",
     "Perguntou o objetivo antes do preço e fechou com dia e hora: \"Quarta às 18h30 fica bom?\".",
     "Dá para propor uma ligação rápida logo no começo.", "Confirmar a aula de quarta na véspera."),
    ("Carla Mendes", 12, 19, "em_andamento", 6.8, False, "dança, noite",
     "Carla voltou perguntando sobre dança à noite; falta confirmar a turma.",
     "Chamou pelo nome e lembrou da conversa anterior.", "Falar da turma de terça às 19h logo na primeira resposta.",
     "Mandar o horário da turma de dança."),
    ("Pedro Alves", 13, 9, "perdido", 6.1, True, "musculação",
     "Pedro achou a mensalidade alta e disse que vai ver em outra academia.",
     "Respondeu rápido e com educação.",
     "Antes de falar o preço, contar o que está incluso: \"Aqui você tem professor acompanhando o treino\".",
     "Retomar com o plano trimestral."),
    ("Ana Paula", 13, 11, "sucesso", 9.2, True, "jiu-jitsu kids para o filho",
     "Ana Paula matriculou o filho no jiu-jitsu kids depois de uma aula experimental.",
     "Acolheu a preocupação da mãe: \"Ele vai ser muito bem cuidado\".", "Nada a ajustar nesta conversa.",
     "Dar as boas-vindas ao aluno."),
    ("Lucas Rocha", 13, 16, "em_andamento", 7.3, True, "muay thai",
     "Lucas quer começar muay thai na semana que vem; pediu o endereço.",
     "Enviou o endereço e a localização no mapa.",
     "Convidar com dia e hora: \"Quer passar aqui na segunda às 18h30?\".", "Convidar para aula experimental."),
    ("Fernanda Dias", 14, 8, "em_andamento", 7.9, True, "musculação, quer ganhar massa",
     "Fernanda quer ganhar massa e perguntou sobre acompanhamento de treino.",
     "Perguntou a experiência dela antes de falar de planos.",
     "Propor uma ligação rápida para tirar as dúvidas de treino.", "Ligar para explicar o acompanhamento."),
    ("Rafael Costa", 14, 12, "nao_lead", None, True, "pagamento da mensalidade",
     "Aluno atual perguntando a data do vencimento da mensalidade.", None, None, "Responder com a data de vencimento."),
    ("Juliana Prado", 14, 18, "sucesso", 8.4, False, "capoeira",
     "Juliana voltou e fechou a matrícula na capoeira de sexta.",
     "Retomou a conversa de ontem sem repetir perguntas: \"Como combinamos, a turma de sexta é às 20h\".",
     "Contar o que está incluso junto com o valor.", "Enviar o contrato e as boas-vindas."),
    ("Bianca Torres", 14, 9, "sucesso", 9.4, True, "musculação, emagrecer 8 kg",
     "Bianca quer emagrecer, falou por telefone com a equipe e agendou aula experimental para quinta.",
     "Seguiu o script do começo ao fim: \"Posso te ligar agora ou prefere outro horário?\".",
     "Nada a ajustar nesta conversa.", "Confirmar a aula de quinta na véspera."),
    ("Marcos Vieira", 14, 10, "em_andamento", 6.9, True, "muay thai, comparando academias",
     "Marcos comparou preços com outra academia e disse que vai pensar.",
     "Explicou o que está incluso antes do valor: \"Aqui as aulas têm professor e avaliação\".",
     "Quando ele disse \"Vou ver e te falo\", dá para convidar com dia e hora.",
     "Convidar para uma aula experimental com dia e hora."),
    ("Renata Lopes", 14, 15, "em_andamento", 5.1, True, "quer voltar a treinar, dor nas costas",
     "Renata pediu informações gerais e contou que tem dor nas costas.",
     "Respondeu em poucos minutos.",
     "Começar se apresentando e dando as boas-vindas: \"Aqui é da Brooklyn Academia, seja bem-vinda!\".",
     "Retomar com boas-vindas e propor uma ligação."),
    ("Eduardo Cunha", 14, 20, "perdido", 6.3, True, "jiu-jitsu",
     "Eduardo recebeu a grade de horários e parou de responder.",
     "Perguntou se ele já tinha treinado antes.",
     "Quando ele sumir, chamar de novo no dia seguinte, no estilo da tentativa 1.",
     "Mandar a tentativa 1 amanhã de manhã."),
    ("Bruno Teixeira", 15, 7, "em_andamento", 6.2, True, "jiu-jitsu",
     "Bruno perguntou os horários de jiu-jitsu e ainda não respondeu à proposta de aula.",
     "Mandou a grade completa de forma organizada.",
     "Depois de mandar a grade, perguntar qual horário combina com a rotina dele.",
     "Perguntar qual horário combina com ele."),
    ("Camila Ramos", 15, 13, "perdido", 5.8, True, "dança",
     "Camila achou longe da casa dela e preferiu uma academia perto.",
     "Foi simpática e agradeceu o contato.",
     "Perguntar o bairro e falar do horário flexível: \"Aqui você treina das 6h às 21h\".",
     "Deixar a porta aberta com uma mensagem leve."),
    ("Diego Nunes", 15, 19, "em_andamento", 7.1, True, "musculação",
     "Diego quer começar na segunda e pediu os planos.", "Respondeu em poucos minutos com os planos organizados.",
     "Convidar para visitar a academia antes de decidir.", "Convidar para uma visita."),
    ("Patrícia Melo", 16, 9, "sucesso", 8.7, True, "muay thai para a filha adolescente",
     "Patrícia agendou aula experimental de muay thai para a filha no sábado.",
     "Perguntou a idade e o objetivo da filha: \"Ela já praticou alguma luta?\".",
     "Poderia citar o cuidado com adolescentes na turma.", "Confirmar a aula na sexta."),
    ("Thiago Barros", 16, 15, "em_andamento", 4.6, True, "musculação",
     "Thiago perguntou o preço e ficou sem resposta por um tempo.", "Foi educado ao pedir desculpas pela demora.",
     "Responder com o valor e perguntar o objetivo dele: \"Qual a sua meta com o treino?\".",
     "Responder hoje com o plano e convite."),
    ("Larissa Freitas", 17, 9, "em_andamento", 7.6, True, "jiu-jitsu kids",
     "Larissa pediu informações do jiu-jitsu kids para os dois filhos.",
     "Mesmo no sábado, respondeu com carinho e detalhes.", "Convidar com dia e hora para a turma de terça às 18h.",
     "Enviar a grade das turmas kids."),
]


def M(tecnica, cliente, equipe, script):
    return {"tecnica": tecnica, "cliente_disse": cliente, "equipe_respondeu": equipe, "pelo_script": script}


# nome: (termômetro, etapa do script, passos feitos, passos que faltaram, momentos)
SCRIPT = {
    "Maria Souza": ("comparador", "descoberta", ["S06"], ["S01", "S02", "S04", "S07", "S08", "S09"], [
        M("S07", "Queria saber o valor da mensalidade", "Bom dia! Musculação R$ 120 e Muay Thai R$ 150",
          "Bom dia! Aqui é da Brooklyn Academia, seja bem-vinda! Te adianto sim: como cada plano é montado para o "
          "seu objetivo, me conta rapidinho o que você busca? Aí te passo o valor certinho e o que está incluso."),
        M("S09", "Posso fazer uma aula teste?", "Pode sim, quando quiser passar aqui",
          "Pode sim, Maria! Tenho horário amanhã às 18h ou quinta às 9h. Qual fica melhor para você?")]),
    "João Lima": ("comprador", "agendamento", ["S01", "S04", "S08", "S09"], ["S02"], [
        M("S02", "Perder uns 10kg", "Perfeito! Quarta às 18h30 fica bom para uma aula experimental?",
          "Perfeito, João! Posso te ligar 2 minutinhos agora para entender sua rotina e já deixar a aula de "
          "quarta às 18h30 reservada?")]),
    "Carla Mendes": ("pesquisador", "foco", ["S01", "S04"], ["S09"], [
        M("S09", "Vocês têm dança à noite?", "Temos sim, vou ver os horários e te passo",
          "Temos sim, Carla! A turma de terça às 19h tem vaga. Quer fazer uma aula experimental nesta terça?")]),
    "Pedro Alves": ("comparador", "acolhimento", ["S01"], ["S04", "S07", "S09"], [
        M("S07", "Quanto é a mensalidade da musculação?", "R$ 120 por mês",
          "Te passo sim, Pedro! Aqui você treina com professor acompanhando e avaliação no começo. Me conta: qual "
          "o seu objetivo? Assim te indico o melhor plano. Consegue vir conhecer hoje?")]),
    "Ana Paula": ("comprador", "agendamento", ["S01", "S02", "S04", "S06", "S09", "S11"], [], []),
    "Lucas Rocha": ("pesquisador", "descoberta", ["S01", "S04"], ["S08", "S09"], [
        M("S09", "Qual o endereço?", "Rua das Flores, 120. Segue a localização",
          "Rua das Flores, 120! Quer passar aqui na segunda às 18h30 para uma aula experimental de muay thai?")]),
    "Fernanda Dias": ("incomodado", "descoberta", ["S01", "S04", "S05"], ["S02", "S06"], [
        M("S02", "Nunca treinei, vou ter acompanhamento?", "Temos professores no salão o dia todo",
          "Tem sim, Fernanda! Posso te ligar 2 minutinhos agora para te explicar como funciona o "
          "acompanhamento de quem está começando?")]),
    "Juliana Prado": ("comprador", "agendamento", ["S01", "S08", "S09", "S10"], ["S07"], [
        M("S07", "E quanto fica a capoeira?", "R$ 90 por mês",
          "Fica R$ 90 por mês, com aula às sextas às 20h e o professor acompanhando sua evolução. "
          "Quer que eu já reserve sua vaga para sexta?")]),
    "Bianca Torres": ("comprador", "agendamento", ["S01", "S02", "S04", "S07", "S09"], [], []),
    "Marcos Vieira": ("comparador", "foco", ["S01", "S04", "S07"], ["S09"], [
        M("S09", "Vou ver e te falo", "Ok, fico no aguardo",
          "Combinado, Marcos! Para você decidir com calma, que tal uma aula experimental amanhã às 19h?")]),
    "Renata Lopes": ("incomodado", "acolhimento", [], ["S01", "S02", "S04"], [
        M("S01", "Oi, queria informações", "Oi, o que gostaria de saber?",
          "Oi, Renata! Aqui é da Brooklyn Academia, seja muito bem-vinda! Parabéns por decidir se cuidar. "
          "Posso te ligar agora para te explicar tudo, ou prefere outro horário?")]),
    "Eduardo Cunha": ("pesquisador", "descoberta", ["S01", "S04"], ["S10"], [
        M("S10", None, "Segue a grade de horários do jiu-jitsu",
          "Oi, Eduardo! Não consegui falar com você ontem. Aqui é da Brooklyn Academia. Quero te explicar melhor "
          "como funcionam as turmas. Qual o melhor horário para falarmos ainda hoje?")]),
    "Bruno Teixeira": ("pesquisador", "foco", ["S01", "S08"], ["S04", "S09"], [
        M("S04", "Quais os horários do jiu-jitsu?", "Segunda e quarta às 19h30",
          "Segunda e quarta às 19h30! Você já treinou antes ou vai começar agora? Assim te indico a melhor turma.")]),
    "Camila Ramos": ("tranquilo", "acolhimento", ["S01"], ["S04", "S06"], []),
    "Diego Nunes": ("comparador", "descoberta", ["S01", "S04", "S07"], ["S09"], []),
    "Patrícia Melo": ("comprador", "agendamento", ["S01", "S04", "S06", "S09"], ["S02"], []),
    "Thiago Barros": ("comparador", "nenhuma", [], ["S01", "S07"], []),
    "Larissa Freitas": ("pesquisador", "descoberta", ["S01", "S04"], ["S09"], [
        M("S09", "Tem turma kids para dois irmãos?", "Temos sim, terça e quinta às 18h",
          "Temos sim, Larissa! Terça e quinta às 18h. Quer trazer os dois para uma aula experimental nesta terça?")]),
}
ATENCAO = {"Maria Souza": "Quer aula teste e ficou sem data marcada.",
           "Thiago Barros": "Pediu o preço e ainda não teve retorno.",
           "Marcos Vieira": "Disse que vai pensar e ficou sem convite com data.",
           "Renata Lopes": "Pediu informações e ainda não recebeu um convite."}
ESPERANDO = {"Thiago Barros"}
OBJECOES = {"Maria Souza": ["preco", "vou_pensar"], "Pedro Alves": ["preco"], "Camila Ramos": ["distancia"],
            "Bruno Teixeira": ["horario"], "Marcos Vieira": ["preco", "vou_pensar"], "Renata Lopes": ["saude"]}
MENSAGEM = {
    "Maria Souza": "Oi, Maria! Aqui é da Brooklyn Academia. Conseguiu conversar com seu marido? Posso reservar uma "
                   "aula experimental para vocês dois amanhã às 18h ou quinta às 9h. Qual fica melhor?",
    "Eduardo Cunha": "Oi, Eduardo! Não consegui falar com você ontem. Aqui é da Brooklyn Academia. Quero te explicar "
                     "melhor como funcionam as turmas. Qual o melhor horário para falarmos ainda hoje?",
    "Marcos Vieira": "Oi, Marcos! Aqui é da Brooklyn Academia. Para você comparar com calma, que tal fazer uma aula "
                     "experimental amanhã às 19h? Assim você conhece o professor e a estrutura.",
}


def dados_ficticios():
    """Monta sessões, análises e mensagens fictícias no formato que o banco devolve."""
    sessoes, analises, mensagens = [], [], []
    for i, (nome, dia, hora, desfecho, nota, nova, interesse, resumo, forte, melhorar, prox) in enumerate(CONVERSAS):
        tel = f"55449{80000000 + i * 111111}"[:12]
        inicio, fim = ts(dia, hora, 5 + i), ts(dia, hora, 45 + i % 10)
        primeira = (180 + i * 97) % 1500 if nome != "Thiago Barros" else None
        sessoes.append({
            "telefone": tel, "nome": nome, "inicio": inicio, "fim": fim, "duracao_seg": 2400, "nova": nova,
            "iniciada_por": "cliente", "msgs_cliente": 3 + i % 4, "msgs_atendente": 2 + i % 3, "msgs_bot": 0,
            "primeira_resposta_util_seg": primeira, "primeira_resposta_seg": primeira,
            "tempo_medio_resposta_util_seg": (primeira or 0) + 60 if primeira else None,
            "tempo_medio_resposta_seg": (primeira or 0) + 60 if primeira else None,
            "tempo_max_resposta_util_seg": (primeira or 0) + 200 if primeira else None,
            "respondida_por": "atendente" if primeira else "ninguem", "respostas": 2,
            "ultima_msg_de": "cliente" if nome in ESPERANDO else "atendente",
            "aguardando_desde": ts(dia, hora, 40) if nome in ESPERANDO else None,
            "sinal_saudacao": True, "sinal_pediu_nome": i % 2 == 0, "sinal_cta": desfecho == "sucesso",
            "sinal_followup": False, "media_caracteres_equipe": 45 + i, "maior_msg_equipe": 90 + i,
        })
        eh_lead = desfecho != "nao_lead"
        temperatura, etapa, feitas, faltaram, momentos = SCRIPT.get(nome, (None, None, [], [], []))
        notas = {}
        if eh_lead:
            base = nota
            for k, chave in enumerate(aia.CRITERIOS_IA):
                notas[chave] = max(0.0, min(10.0, round(base + ((k * 7 + i * 3) % 5 - 2) * 0.8, 1)))
            notas["objecoes"] = notas["objecoes"] if OBJECOES.get(nome) else None
            notas["followup"] = notas["followup"] if not nova else None
            notas["intuito_ligacao"] = notas["intuito_ligacao"] if ("S02" in feitas or "S02" in faltaram) else None
            tempo = aia.notas_de_tempo(sessoes[-1], agora=fim + timedelta(hours=2))
            notas = {**{k: v for k, v in tempo.items()}, **notas}
        analises.append({
            "telefone": tel, "sessao_inicio": inicio, "inicio": inicio, "fim": fim, "nome": nome, "nova": nova,
            "eh_lead": eh_lead, "tipo_contato": "lead" if eh_lead else "aluno",
            "genero": "Feminino" if nome.split()[0][-1] == "a" else "Masculino", "interesse": interesse,
            "desfecho": desfecho, "detalhe_desfecho": resumo,
            "motivo_perda": {"Pedro Alves": "preco", "Camila Ramos": "distancia", "Eduardo Cunha": "sumiu"}.get(nome),
            "temperatura": temperatura, "precisa_atencao": nome in ATENCAO,
            "motivo_atencao": ATENCAO.get(nome),
            "nota_final": aia.media_das_notas(notas) if eh_lead else None,
            "nota_intuito_venda": notas.get("intuito_venda"), "nota_ligacao": notas.get("intuito_ligacao"),
            "propos_ligacao": ("S02" in feitas) if eh_lead else None,
            "resumo": resumo, "ponto_forte": forte if eh_lead else None, "pode_melhorar": melhorar if eh_lead else None,
            "proxima_acao": prox,
            "mensagem_sugerida": MENSAGEM.get(nome, f"Oi, {nome.split()[0]}! Aqui é da Brooklyn Academia. "
                                                    "Posso te ligar rapidinho hoje para combinarmos sua aula "
                                                    "experimental?") if eh_lead else None,
            "aguardando_desde": ts(dia, hora, 40) if nome in ESPERANDO else None,
            "primeira_resposta_util_seg": primeira,
            "dados": {"notas": notas, "objecoes": [{"tipo": t, "trecho": "..."} for t in OBJECOES.get(nome, [])],
                      "tecnicas_aplicadas": list(feitas), "tecnicas_a_reforcar": list(faltaram),
                      "etapa_script": etapa, "momentos": list(momentos),
                      "como_poderia_ser": None, "trecho_ligacao": None, "metodo_fonte": "curso"},
        })
    transcricoes = {
        "Maria Souza": [("cliente", "Oi, bom dia! Queria saber o valor da mensalidade"),
                        ("atendente", "Bom dia! Musculação R$ 120 e Muay Thai R$ 150"),
                        ("cliente", "Nossa, ta meio caro. Tem algum plano mais em conta?"),
                        ("atendente", "Temos o trimestral por R$ 99 por mês"),
                        ("cliente", "Posso fazer uma aula teste?"),
                        ("atendente", "Pode sim, quando quiser passar aqui"),
                        ("cliente", "Vou ver com meu marido e te aviso")],
        "João Lima": [("cliente", "Oi! Quero emagrecer, vocês têm muay thai?"),
                      ("atendente", "Olá, João! Aqui é a Ana, da Brooklyn Academia. Temos sim! Qual o seu objetivo hoje?"),
                      ("cliente", "Perder uns 10kg"),
                      ("atendente", "Perfeito! Quarta às 18h30 fica bom para uma aula experimental?"),
                      ("cliente", "Fica sim, obrigado!")],
    }
    for s in sessoes:
        for k, (quem, texto) in enumerate(transcricoes.get(s["nome"], [])):
            mensagens.append({"telefone": s["telefone"], "nome": s["nome"], "ts": s["inicio"] + timedelta(minutes=k * 3),
                              "origem": quem, "tipo": "texto", "text": texto})
    return sessoes, analises, mensagens


class IAFalsa:
    """Responde como a IA responderia (textos fixos): só para a prévia."""
    def ia_ligada(self):
        return True

    def gerar_json(self, sistema, usuario, max_tokens=0):
        if "SEMANA" in sistema:
            return {
                "destaques": ["A Bianca seguiu o script do começo ao fim: ligação, objetivo e aula marcada.",
                              "O João e a Patrícia saíram com aula marcada: perguntar o objetivo antes do preço funcionou."],
                "treino": {"tema": "Convidar com dia e hora quando o cliente diz \"vou ver\"",
                           "exemplo": "\"Combinado! Que tal uma aula experimental amanhã às 19h?\""},
                "objecoes": [{"tipo": "preco", "como_responder": "Contar o que está incluso antes do valor e convidar para conhecer."},
                             {"tipo": "vou_pensar", "como_responder": "Oferecer uma aula experimental com dia e hora para decidir com calma."},
                             {"tipo": "distancia", "como_responder": "Perguntar o bairro e falar do horário flexível."}],
                "metodo": {"ja_faz": "A equipe já se apresenta bem e descobre o objetivo do cliente.",
                           "reforcar": "Vale fechar sempre com convite com dia e hora e propor a ligação logo no começo."}}, \
                {"entrada": 1, "saida": 1}
        return {"destaque": "A conversa com a Bianca seguiu o script do começo ao fim e terminou com aula marcada.",
                "dica": "Quando o cliente disser \"vou ver\", convide com dia e hora: \"Que tal uma aula experimental amanhã às 19h?\""}, \
            {"entrada": 1, "saida": 1}


def instalar_banco_falso():
    """Troca as leituras do banco por dados fictícios (respeitando a janela pedida)."""
    sessoes, analises, mensagens = dados_ficticios()

    def na_janela(item, inicio, fim):
        return item["inicio"] < fim and item["fim"] >= inicio

    def do_periodo_sessoes(inicio, fim, excluir=None):
        return [s for s in sessoes if na_janela(s, inicio, fim)]

    def do_periodo_analises(inicio, fim, excluir=None):
        return [a for a in analises if na_janela(a, inicio, fim)]

    def resumo_periodo(inicio, fim, agora=None):
        ss = do_periodo_sessoes(inicio, fim)
        com = [s for s in ss if s["primeira_resposta_util_seg"] is not None]
        pend = [{"telefone": s["telefone"], "nome": s["nome"], "esperando_util_seg": 2400, "nova": s["nova"],
                 "esperando_seg": 2400, "aguardando_desde": s["aguardando_desde"].isoformat(),
                 "ultima_msg": "Oi, qual o valor da mensalidade?"} for s in ss if s["aguardando_desde"]]
        return {
            "periodo": {"inicio": inicio.isoformat(), "fim": fim.isoformat()}, "conversas": len(ss),
            "msgs_cliente": sum(s["msgs_cliente"] for s in ss), "msgs_equipe": sum(s["msgs_atendente"] for s in ss),
            "com_primeira_resposta": len(com), "respondidas_no_limite": sum(1 for s in com if s["primeira_resposta_util_seg"] <= 900),
            "novas": sum(1 for s in ss if s["nova"]), "retomadas": sum(1 for s in ss if not s["nova"]),
            "followups_da_equipe": 0,
            "primeira_resposta_media_util_seg": int(sum(s["primeira_resposta_util_seg"] for s in com) / len(com)) if com else None,
            "pior_espera_util_seg": 1500 if com else None,
            "respondidas_por": {"atendente": len(com), "bot": 0, "ambos": 0, "ninguem": len(ss) - len(com)},
            "sem_resposta_total": len(pend), "sem_resposta": pend,
        }

    analista.resumo_periodo = resumo_periodo
    analista._ignorados = lambda: []
    rel.sessoes_repo.do_periodo = do_periodo_sessoes
    rel.analises_repo.do_periodo = do_periodo_analises
    rel.analises_repo.em_aberto_anteriores = lambda inicio, dias=7, excluir=None: 3
    # o diário de "ontem" (terça 13/10), para a comparação
    rel.relatorios_repo.diario_anterior = lambda ref: {
        "referencia": ref - timedelta(days=1), "hora": 22,
        "dados": {"conversas": 3, "nota_media": 7.0, "script": {"aderencia": 55}}}
    rel.relatorios_repo.semanal_da_semana = lambda segunda: {
        "dados": {"sucesso": 1, "nota_media": 6.6, "script": {"aderencia": 48}}}
    return sessoes, analises, mensagens


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", required=True, help="pasta onde gravar os arquivos")
    ap.add_argument("--enviar-para", help="número (com 55) que recebe a prévia no WhatsApp")
    args = ap.parse_args()
    saida = Path(args.saida)
    saida.mkdir(parents=True, exist_ok=True)

    sessoes, analises, mensagens = instalar_banco_falso()
    ia = IAFalsa()

    d22 = rel.montar_diario(22, agora=ts(DIA_DO_DIARIO, 22), ia=ia)      # quarta 22h (compara com terça)
    semana = rel.montar_semanal(agora=ts(17, 18), ia=ia)                  # sábado 18h
    xlsx = analista_excel.construir(semana["dados"], analises, sessoes, mensagens, semana["inicio"], semana["fim"])

    arquivos = {"diario_22h.txt": d22["texto"], "semanal.txt": semana["texto"]}
    for nome, texto in arquivos.items():
        (saida / nome).write_text(texto, encoding="utf-8")
    (saida / "planilha_semana.xlsx").write_bytes(xlsx)
    print(f"Arquivos gravados em {saida}")
    for nome, texto in arquivos.items():
        print(f"\n===== {nome} =====\n{texto}")

    if args.enviar_para:
        from app.services.evolution_service import evolution_api
        destino = "".join(c for c in args.enviar_para if c.isdigit())
        if destino in analista.variantes_numero("554491216103"):
            sys.exit("Recusado: esse é o número do dono. A prévia só vai para o celular de teste.")
        aviso = "🧪 *PRÉVIA - dados fictícios* (não são conversas reais)\n\n"
        resultados = []
        for texto in (d22["texto"], semana["texto"]):
            resultados.append(evolution_api.send_whatsapp_message(destino, aviso + texto, delay_ms=1000, manter_emojis=True))
        resultados.append(evolution_api.send_whatsapp_document(
            destino, xlsx, "previa_planilha_semana.xlsx", "PRÉVIA - dados fictícios"))
        if not all(resultados):
            sys.exit(f"ATENÇÃO: nem tudo foi aceito pela Evolution (texto diário, texto semanal, planilha): {resultados}")
        print(f"\nPrévia enviada para {destino}: 2 mensagens + planilha, todas aceitas pela Evolution.")


if __name__ == "__main__":
    main()
