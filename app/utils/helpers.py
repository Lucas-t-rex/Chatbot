import re
from datetime import datetime, time as dt_time
from dateutil import parser as dateparser
from typing import Optional, List
from app.core.config import config

def parse_data(data_str: str) -> Optional[datetime]:
    if not data_str or not isinstance(data_str, str):
        return None
    data_str = data_str.strip()
    if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', data_str):
        d, m, y = data_str.split('/')
        try:
            return datetime(int(y), int(m), int(d))
        except Exception:
            return None
    try:
        dt = dateparser.parse(data_str, dayfirst=True)
        if dt:
            return datetime(dt.year, dt.month, dt.day)
    except Exception:
        return None
    return None

def validar_hora(hora_str: str) -> Optional[str]:
    if not hora_str or not isinstance(hora_str, str):
        return None
    m = re.match(r'^\s*(\d{1,2}):(\d{1,2})\s*$', hora_str)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return f"{hh:02d}:{mm:02d}"
    return None

def str_to_time(time_str: str) -> dt_time:
    return datetime.strptime(time_str, '%H:%M').time()

def time_to_minutes(t: dt_time) -> int:
    return t.hour * 60 + t.minute

def minutes_to_str(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"

def gerar_slots_de_trabalho(intervalo_min: int, data_ref: datetime) -> List[str]:
    """Gera slots baseados no dia da semana específico da data informada."""
    dia_semana = data_ref.weekday() 
    blocos_hoje = config.BLOCOS_DE_TRABALHO.get(dia_semana, [])
    
    slots = []
    for bloco in blocos_hoje:
        inicio_min = time_to_minutes(str_to_time(bloco["inicio"]))
        fim_min = time_to_minutes(str_to_time(bloco["fim"]))
        current_min = inicio_min
        
        while current_min < fim_min:
            slots.append(minutes_to_str(current_min))
            current_min += intervalo_min
    return slots

def extrair_tokens_da_resposta(response):
    """
    Extrai separadamente tokens de entrada (prompt) e saída (resposta).
    Retorna uma tupla: (tokens_input, tokens_output)
    """
    try:
        if hasattr(response, 'usage_metadata'):
            usage = response.usage_metadata
            return (usage.prompt_token_count, usage.candidates_token_count)
        return (0, 0)
    except:
        return (0, 0)

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
