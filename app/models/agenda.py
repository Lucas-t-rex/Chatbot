import re
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Optional, List, Dict, Any
from app.core.config import config
from app.core.db import db
from app.utils.helpers import parse_data, validar_hora, str_to_time, gerar_slots_de_trabalho
import logging

log = logging.getLogger(__name__)

def agrupar_horarios_em_faixas(lista_horarios, step=15):
    if not lista_horarios:
        return "Nenhum horário disponível."

    minutos = []
    for h in lista_horarios:
        try:
            h_split = h.split(':')
            m = int(h_split[0]) * 60 + int(h_split[1])
            minutos.append(m)
        except (ValueError, IndexError):
            continue

    if not minutos:
        return "Horários em formato inválido."

    minutos.sort()
    faixas = []
    if not minutos: return ""

    inicio_faixa = minutos[0]
    anterior = minutos[0]
    count_seq = 1

    def _formatar_bloco(inicio, fim, step, count):
        if count >= 3:
            fim_real = fim + step
            str_ini = f"{inicio // 60:02d}:{inicio % 60:02d}"
            str_fim = f"{fim_real // 60:02d}:{fim_real % 60:02d}"
            return f"das {str_ini} às {str_fim}"
        else:
            result = []
            temp = inicio
            while temp <= fim:
                result.append(f"{temp // 60:02d}:{temp % 60:02d}")
                temp += step
            return ", ".join(result)

    for atual in minutos[1:]:
        if atual == anterior + step:
            anterior = atual
            count_seq += 1
        else:
            faixas.append(_formatar_bloco(inicio_faixa, anterior, step, count_seq))
            inicio_faixa = atual
            anterior = atual
            count_seq = 1

    faixas.append(_formatar_bloco(inicio_faixa, anterior, step, count_seq))

    if len(faixas) == 1:
        return faixas[0]
    
    return ", ".join(faixas[:-1]) + " e " + faixas[-1]

class Agenda:
    def __init__(self):
        # Using connection pooled singleton from app.core.db
        if db.client_agenda:
            self.collection = db.client_agenda[config.DB_NAME][config.MONGO_AGENDA_COLLECTION]
            self.is_connected = True
            log.info("✅ [App/Models/Agenda] Usando conexão singleton com DB_AGENDA.")
        else:
            self.collection = None
            self.is_connected = False
            log.warning("⚠️ [App/Models/Agenda] Sem conexão com BD. Agendamentos falharão.")
            
    # As demais funções usam self.collection, já estão prontas no original.
    def _is_dia_bloqueado_admin(self, dt: datetime) -> bool:
        if not self.is_connected: return False
        try:
            inicio_dia = datetime.combine(dt.date(), dt_time.min)
            fim_dia = datetime.combine(dt.date(), dt_time.max)
            bloqueio = self.collection.find_one({
                "inicio": {"$gte": inicio_dia, "$lte": fim_dia},
                "$or": [{"servico": "Folga"}, {"status": "bloqueado"}]
            })
            return bloqueio is not None
        except Exception as e:
            log.error(f"Erro ao checar bloqueio administrativo: {e}")
            return False
            
    def _checar_dia_de_folga(self, dt: datetime) -> Optional[str]:
        dia_semana_num = dt.weekday()
        if dia_semana_num in config.FOLGAS_DIAS_SEMANA:
            return config.MAPA_DIAS_SEMANA_PT.get(dia_semana_num, "dia de folga")
            
        if self._is_dia_bloqueado_admin(dt):
            return "dia de folga administrativa (feriado ou recesso)"
        return None

    def _get_duracao_servico(self, servico_str: str) -> Optional[int]:
        servico_key = servico_str.strip().lower()
        if servico_key in config.MAPA_SERVICOS_DURACAO:
             return config.MAPA_SERVICOS_DURACAO.get(servico_key)
        for chave_oficial in config.MAPA_SERVICOS_DURACAO.keys():
            if chave_oficial in servico_key or servico_key in chave_oficial:
                return config.MAPA_SERVICOS_DURACAO[chave_oficial]
        if len(config.MAPA_SERVICOS_DURACAO) == 1:
            unica_chave = list(config.MAPA_SERVICOS_DURACAO.keys())[0]
            return config.MAPA_SERVICOS_DURACAO[unica_chave]
        return None

    def _cabe_no_bloco(self, data_base: datetime, inicio_str: str, duracao_min: int) -> bool:
        dia_semana = data_base.weekday()
        blocos_hoje = config.BLOCOS_DE_TRABALHO.get(dia_semana, [])
        inicio_dt = datetime.combine(data_base.date(), str_to_time(inicio_str))
        fim_dt = inicio_dt + timedelta(minutes=duracao_min)
        for bloco in blocos_hoje:
            bloco_inicio_dt = datetime.combine(data_base.date(), str_to_time(bloco["inicio"]))
            bloco_fim_dt = datetime.combine(data_base.date(), str_to_time(bloco["fim"]))
            if inicio_dt >= bloco_inicio_dt and fim_dt <= bloco_fim_dt:
                return True
        return False

    def _checar_horario_passado(self, dt_agendamento: datetime, hora_str: str) -> bool:
        try:
            agendamento_dt = datetime.combine(dt_agendamento.date(), str_to_time(hora_str))
            agora_sp_com_fuso = datetime.now(config.FUSO_HORARIO)
            agora_sp_naive = agora_sp_com_fuso.replace(tzinfo=None)
            return agendamento_dt < agora_sp_naive
        except Exception:
            return False

    def _contar_conflitos_no_banco(self, novo_inicio_dt: datetime, novo_fim_dt: datetime, excluir_id: Optional[Any] = None) -> int:
        query = {"inicio": {"$lt": novo_fim_dt}, "fim": {"$gt": novo_inicio_dt}}
        if excluir_id: query["_id"] = {"$ne": excluir_id}
        try:
            return self.collection.count_documents(query)
        except Exception as e:
            log.error(f"❌ Erro ao contar conflitos no Mongo: {e}")
            return 999 

    def _buscar_agendamentos_do_dia(self, dt: datetime) -> List[Dict[str, Any]]:
        try:
            inicio_dia = datetime.combine(dt.date(), dt_time.min)
            fim_dia = inicio_dia + timedelta(days=1)
            query = {"inicio": {"$gte": inicio_dia, "$lt": fim_dia}}
            return list(self.collection.find(query))
        except Exception as e:
            log.error(f"❌ Erro ao buscar agendamentos do dia: {e}")
            return []

    def _contar_conflitos_em_lista(self, agendamentos_do_dia: List[Dict], novo_inicio_dt: datetime, novo_fim_dt: datetime) -> int:
        conflitos_encontrados = 0
        for ag in agendamentos_do_dia:
            if (novo_inicio_dt < ag["fim"]) and (novo_fim_dt > ag["inicio"]):
                conflitos_encontrados += 1
        return conflitos_encontrados

    def buscar_por_telefone(self, telefone: str) -> Dict[str, Any]:
        if not self.is_connected: return {"erro": "Conexão com BD falhou."}
        tel_limpo = re.sub(r'\D', '', str(telefone)) if telefone else ""
        if not tel_limpo: return {"erro": "Telefone inválido."}
        try:
            agora_sp = datetime.now(config.FUSO_HORARIO).replace(tzinfo=None)
            query = {"telefone": tel_limpo, "inicio": {"$gte": agora_sp}}
            resultados_db = self.collection.find(query).sort("inicio", 1)
            resultados = []
            for ag in resultados_db:
                inicio_dt_local = ag["inicio"]
                resultados.append({
                    "data": inicio_dt_local.strftime('%d/%m/%Y'),
                    "hora": inicio_dt_local.strftime('%H:%M'),
                    "nome": ag.get("nome"),
                    "telefone": ag.get("telefone"),
                    "servico": ag.get("servico"),
                    "duracao_minutos": ag.get("duracao_minutos")
                })
            if not resultados:
                return {"sucesso": True, "resultados": [], "info": "Nenhum agendamento futuro encontrado para este telefone."}
            return {"sucesso": True, "resultados": resultados}
        except Exception as e:
            return {"erro": f"Falha ao buscar telefone no banco de dados: {e}"}

    def salvar(self, nome: str, telefone: str, servico: str, data_str: str, hora_str: str, owner_id: str = None, observacao: str = "") -> Dict[str, Any]:
        if not self.is_connected: return {"erro": "Conexão com BD falhou."}
        tel_limpo = re.sub(r'\D', '', str(telefone)) if telefone else ""
        if not tel_limpo: return {"erro": "Telefone inválido."}
        dt = parse_data(data_str)
        if not dt: return {"erro": "Data inválida."}
        hora = validar_hora(hora_str)
        if not hora: return {"erro": "Hora inválida."}

        folga = self._checar_dia_de_folga(dt)
        if folga: return {"erro": f"Não é possível agendar. O dia {data_str} é um {folga} e não trabalhamos."}
        if self._checar_horario_passado(dt, hora): return {"erro": f"Não é possível agendar. O horário {data_str} às {hora} já passou."}

        duracao_minutos = self._get_duracao_servico(servico)
        servico_key = servico.lower().strip()
        if servico_key in config.GRADE_HORARIOS_SERVICOS:
            dia_semana = dt.weekday()
            horarios_permitidos = config.GRADE_HORARIOS_SERVICOS[servico_key].get(dia_semana, [])
            if hora_str not in horarios_permitidos:
                msg_grade = ", ".join(horarios_permitidos) if horarios_permitidos else "não tem aula neste dia"
                return {"erro": f"Impossível agendar {servico} às {hora_str}. A grade oficial para esta data é: {msg_grade}."}
        
        if duracao_minutos is None:
            return {"erro": f"Serviço '{servico}' não reconhecido. Os serviços válidos são: {config.LISTA_SERVICOS_PROMPT}"}

        if dt.weekday() in [5, 6]:
            return {"erro": "Não realizamos agendamentos de aulas aos finais de semana. Por favor, escolha um dia entre segunda e sexta."}
            
        if not self._cabe_no_bloco(dt, hora, duracao_minutos):
            return {"erro": f"O horário {hora} é inválido ou ultrapassa o fechamento para agendamentos."}

        try:
            inicio_dt = datetime.combine(dt.date(), str_to_time(hora))
            fim_dt = inicio_dt + timedelta(minutes=duracao_minutos)
            already_booked = self.collection.find_one({
                "telefone": tel_limpo, 
                "inicio": inicio_dt,
                "nome": {"$regex": f"^{re.escape(nome.strip())}$", "$options": "i"}
            })
            if already_booked:
                return {"sucesso": False, "msg": f"Atenção: A pessoa informada ({nome.strip()}) já possui um agendamento EXATAMENTE neste dia e horário ({data_str} às {hora}) sob este número de telefone. Se quiser agendar para uma segunda pessoa no mesmo celular e horário, por favor cadastre com o NOME COMPLETO dessa outra pessoa."}

            conflitos_atuais = self._contar_conflitos_no_banco(inicio_dt, fim_dt)
            if conflitos_atuais >= config.NUM_ATENDENTES:
                return {"erro": f"Horário {hora} indisponível (Lotação máxima atingida)."}
            
            obs_limpa = str(observacao).strip()[:200] if observacao else ""
            novo_documento = {
                "owner_whatsapp_id": owner_id,  
                "nome": nome.strip(),
                "telefone": tel_limpo,
                "servico": servico.strip(),
                "observacao": obs_limpa,
                "duracao_minutos": duracao_minutos,
                "inicio": inicio_dt, 
                "fim": fim_dt,
                "reminder_sent": False, 
                "created_at": datetime.now(timezone.utc)
            }
            result = self.collection.insert_one(novo_documento)
            if result.inserted_id:
                log.info(f"💾 [DB SALVO COM SUCESSO] ID: {result.inserted_id} | Cliente: {nome} | Serviço: {servico}")
                return {"sucesso": True, "msg": f"Agendamento salvo com sucesso para {nome} em {data_str} às {hora}."}
            else:
                return {"erro": "Erro crítico: O banco de dados não retornou o ID de confirmação."}
        except Exception as e:
            return {"erro": f"Falha técnica ao salvar no banco de dados: {e}"}
        
    def excluir(self, telefone: str, data_str: str, hora_str: str) -> Dict[str, Any]:
        if not self.is_connected: return {"erro": "Conexão com BD falhou."}
        tel_limpo = re.sub(r'\D', '', str(telefone)) if telefone else ""
        if not tel_limpo: return {"erro": "Telefone inválido."}
        dt = parse_data(data_str)
        if not dt: return {"erro": "Data inválida."}
        hora = validar_hora(hora_str)
        if not hora: return {"erro": "Hora inválida."}
        if self._checar_horario_passado(dt, hora):
            return {"erro": f"Não é possível excluir. O agendamento em {data_str} às {hora} já passou."}

        try:
            inicio_dt = datetime.combine(dt.date(), str_to_time(hora))
            documento_removido = self.collection.find_one_and_delete({"telefone": tel_limpo, "inicio": inicio_dt})
            if not documento_removido:
                return {"erro": "Agendamento não encontrado com os dados fornecidos."}
            nome_cliente = documento_removido.get('nome', 'Cliente')
            return {"sucesso": True, "msg": f"Agendamento de {nome_cliente} em {data_str} às {hora} removido."}
        except Exception as e:
            return {"erro": f"Falha ao excluir do banco de dados: {e}"}

    def excluir_todos_por_telefone(self, telefone: str) -> Dict[str, Any]:
        if not self.is_connected: return {"erro": "Conexão com BD falhou."}
        tel_limpo = re.sub(r'\D', '', str(telefone)) if telefone else ""
        if not tel_limpo: return {"erro": "Telefone inválido."}
        try:
            query = {"telefone": tel_limpo, "inicio": {"$gte": datetime.now()}}
            resultado = self.collection.delete_many(query)
            if resultado.deleted_count == 0:
                return {"erro": "Nenhum agendamento futuro encontrado para este telefone."}
            return {"sucesso": True, "msg": f"{resultado.deleted_count} agendamento(s) futuros foram removidos com sucesso."}
        except Exception as e:
            return {"erro": f"Falha ao excluir agendamentos do banco de dados: {e}"}

    def alterar(self, telefone: str, data_antiga: str, hora_antiga: str, data_nova: str, hora_nova: str) -> Dict[str, Any]:
        if not self.is_connected: return {"erro": "Conexão com BD falhou."}
        tel_limpo = re.sub(r'\D', '', str(telefone)) if telefone else ""
        if not tel_limpo: return {"erro": "Telefone inválido."}
        dt_old = parse_data(data_antiga)
        dt_new = parse_data(data_nova)
        if not dt_old or not dt_new: return {"erro": "Data antiga ou nova inválida."}
        h_old = validar_hora(hora_antiga)
        h_new = validar_hora(hora_nova)
        if not h_old or not h_new: return {"erro": "Hora antiga ou nova inválida."}

        folga = self._checar_dia_de_folga(dt_new)
        if folga: return {"erro": f"Não é possível alterar para {data_nova}, pois é um {folga} e não trabalhamos."}
        if self._checar_horario_passado(dt_old, h_old): return {"erro": f"Não é possível alterar. O agendamento original já passou."}
        if self._checar_horario_passado(dt_new, h_new): return {"erro": f"Não é possível agendar. O novo horário já passou."}

        try:
            inicio_antigo_dt = datetime.combine(dt_old.date(), str_to_time(h_old))
            item = self.collection.find_one({"telefone": tel_limpo, "inicio": inicio_antigo_dt})
            if not item: return {"erro": "Agendamento antigo não encontrado."}

            duracao_minutos = item.get("duracao_minutos", self._get_duracao_servico(item.get("servico", "")))
            if duracao_minutos is None: return {"erro": f"O serviço original não é mais válido."}
            if not self._cabe_no_bloco(dt_new, h_new, duracao_minutos):
                return {"erro": f"O novo horário {h_new} ultrapassa o horário de atendimento."}

            novo_inicio_dt = datetime.combine(dt_new.date(), str_to_time(h_new))
            novo_fim_dt = novo_inicio_dt + timedelta(minutes=duracao_minutos)
            
            if self._contar_conflitos_no_banco(novo_inicio_dt, novo_fim_dt, excluir_id=item["_id"]) >= config.NUM_ATENDENTES:
                return {"erro": f"Novo horário {h_new} indisponível."}

            resultado = self.collection.update_one({"_id": item["_id"]}, {"$set": {"inicio": novo_inicio_dt, "fim": novo_fim_dt}})
            if resultado.matched_count == 0: return {"erro": "Falha ao encontrar o documento para atualizar."}

            return {
                "sucesso": True, 
                "msg": f"Agendamento alterado para {dt_new.strftime('%d/%m/%Y')} às {h_new}.",
                "nome_cliente": item.get("nome", "Cliente"),
                "telefone_cliente": item.get("telefone", "Não informado")
            }
        except Exception as e:
            return {"erro": f"Falha ao alterar no banco de dados: {e}"}
        
    def listar_horarios_disponiveis(self, data_str: str, servico_str: str) -> Dict[str, Any]:
        if not self.is_connected: return {"erro": "Conexão com BD falhou."}
        dt = parse_data(data_str)
        if not dt: return {"erro": "Data inválida."}
        folga = self._checar_dia_de_folga(dt)
        if folga: return {"erro": f"Desculpe, o dia {data_str} está indisponível ({folga})."}

        servico_key = servico_str.lower().strip()
        dia_semana = dt.weekday()
        if servico_key in config.GRADE_HORARIOS_SERVICOS:
            slots_para_testar = config.GRADE_HORARIOS_SERVICOS[servico_key].get(dia_semana, [])
            if not slots_para_testar: return {"erro": f"Não temos aula de {servico_str} disponível neste dia da semana."}
        else:
            slots_para_testar = gerar_slots_de_trabalho(config.INTERVALO_SLOTS_MINUTOS, dt)

        agora = datetime.now(config.FUSO_HORARIO).replace(tzinfo=None)
        duracao_minutos = self._get_duracao_servico(servico_key) or 60
        agendamentos_do_dia = self._buscar_agendamentos_do_dia(dt)
        horarios_disponiveis = []

        for slot_hora_str in slots_para_testar:
            slot_dt_completo = datetime.combine(dt.date(), str_to_time(slot_hora_str))
            if slot_dt_completo < agora: continue
            if not self._cabe_no_bloco(dt, slot_hora_str, duracao_minutos): continue
            
            slot_fim_dt = slot_dt_completo + timedelta(minutes=duracao_minutos)
            if self._contar_conflitos_em_lista(agendamentos_do_dia, slot_dt_completo, slot_fim_dt) < config.NUM_ATENDENTES:
                horarios_disponiveis.append(slot_hora_str)
        
        if not horarios_disponiveis:
            resumo_humanizado = "Não há horários livres para este serviço nesta data."
        else:
            texto_faixas = agrupar_horarios_em_faixas(horarios_disponiveis, config.INTERVALO_SLOTS_MINUTOS)
            resumo_humanizado = f"Para {servico_str}, tenho estes horários: {texto_faixas}."
            
        return {
            "sucesso": True,
            "data": dt.strftime('%d/%m/%Y'),
            "servico_consultado": servico_str,
            "resumo_humanizado": resumo_humanizado,
            "horarios_disponiveis": horarios_disponiveis
        }

agenda_instance = Agenda()
