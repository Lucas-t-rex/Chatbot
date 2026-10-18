"""
Cliente de IA do Analista: UMA função, gerar_json(), que funciona com Gemini,
Claude (Anthropic) ou OpenAI. Quem escolhe é a configuração (ANALISTA_IA_PROVEDOR).

Usa só "requests" (já instalado): nada de biblioteca nova. A chave nunca vai
para o log. Erros temporários (429, 5xx, timeout) tentam de novo com espera.
"""
import json
import re
import time

import requests

from app.core.config import config


class IAIndisponivel(Exception):
    """Sem chave configurada: a IA do analista está desligada."""


class IAErro(Exception):
    """A chamada falhou (ou a resposta veio inutilizável)."""


TENTATIVAS = 3
ESPERAS_SEG = (2, 6)
_STATUS_TEMPORARIOS = (408, 429, 500, 502, 503, 504)


def ia_ligada():
    return bool(config.ANALISTA_IA_API_KEY)


def _dormir(segundos):
    time.sleep(segundos)


# ---------------------------------------------------------------------------
# Leitura tolerante do JSON que a IA devolve
# ---------------------------------------------------------------------------
def extrair_json(texto):
    """Aceita JSON puro, dentro de ```json ... ``` ou com texto em volta."""
    if not texto or not texto.strip():
        raise IAErro("resposta vazia da IA")
    t = texto.strip()
    cerca = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S | re.I)
    if cerca:
        t = cerca.group(1).strip()
    candidatos = [t]
    ini, fim = t.find("{"), t.rfind("}")
    if 0 <= ini < fim:
        candidatos.append(t[ini:fim + 1])
    for c in candidatos:
        try:
            dado = json.loads(c)
        except ValueError:
            continue
        if isinstance(dado, dict):
            return dado
    raise IAErro("a IA não devolveu um JSON válido")


# ---------------------------------------------------------------------------
# Um provedor = uma função que devolve (texto, {"entrada": n, "saida": n})
# ---------------------------------------------------------------------------
def _gemini(sistema, usuario, max_tokens, temperatura):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{config.ANALISTA_IA_MODELO}:generateContent")
    corpo = {
        "systemInstruction": {"parts": [{"text": sistema}]},
        "contents": [{"role": "user", "parts": [{"text": usuario}]}],
        "generationConfig": {
            "temperature": temperatura,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(
        url, json=corpo, timeout=config.ANALISTA_IA_TIMEOUT_SEG,
        headers={"x-goog-api-key": config.ANALISTA_IA_API_KEY, "Content-Type": "application/json"},
    )
    return resp, _ler_gemini


def _ler_gemini(dado):
    candidatos = dado.get("candidates") or []
    if not candidatos:
        raise IAErro(f"Gemini sem resposta ({dado.get('promptFeedback') or 'sem detalhe'})")
    partes = (candidatos[0].get("content") or {}).get("parts") or []
    texto = "".join(p.get("text", "") for p in partes if not p.get("thought"))
    uso = dado.get("usageMetadata") or {}
    saida = (uso.get("candidatesTokenCount") or 0) + (uso.get("thoughtsTokenCount") or 0)
    return texto, {"entrada": uso.get("promptTokenCount") or 0, "saida": saida}


def _anthropic(sistema, usuario, max_tokens, temperatura):
    corpo = {
        "model": config.ANALISTA_IA_MODELO,
        "max_tokens": max_tokens,
        "temperature": temperatura,
        # cache nas instruções fixas: nas conversas seguintes elas custam bem menos
        "system": [{"type": "text", "text": sistema, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": usuario}],
    }
    resp = requests.post(
        "https://api.anthropic.com/v1/messages", json=corpo, timeout=config.ANALISTA_IA_TIMEOUT_SEG,
        headers={"x-api-key": config.ANALISTA_IA_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    return resp, _ler_anthropic


def _ler_anthropic(dado):
    texto = "".join(b.get("text", "") for b in dado.get("content") or [] if b.get("type") == "text")
    uso = dado.get("usage") or {}
    entrada = ((uso.get("input_tokens") or 0) + (uso.get("cache_read_input_tokens") or 0)
               + (uso.get("cache_creation_input_tokens") or 0))
    return texto, {"entrada": entrada, "saida": uso.get("output_tokens") or 0}


def _openai(sistema, usuario, max_tokens, temperatura):
    corpo = {
        "model": config.ANALISTA_IA_MODELO,
        "max_completion_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": usuario}],
    }
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions", json=corpo, timeout=config.ANALISTA_IA_TIMEOUT_SEG,
        headers={"Authorization": f"Bearer {config.ANALISTA_IA_API_KEY}", "Content-Type": "application/json"},
    )
    return resp, _ler_openai


def _ler_openai(dado):
    escolhas = dado.get("choices") or []
    if not escolhas:
        raise IAErro("OpenAI sem resposta")
    texto = (escolhas[0].get("message") or {}).get("content") or ""
    uso = dado.get("usage") or {}
    return texto, {"entrada": uso.get("prompt_tokens") or 0, "saida": uso.get("completion_tokens") or 0}


_PROVEDORES = {"gemini": _gemini, "anthropic": _anthropic, "openai": _openai}


def gerar_json(sistema, usuario, max_tokens=4096, temperatura=0.2):
    """
    Manda as instruções (sistema) e o conteúdo (usuario) para a IA configurada e
    devolve (dict, {"entrada": tokens, "saida": tokens}).
    Levanta IAIndisponivel (sem chave) ou IAErro (falha / resposta inutilizável).
    """
    if not ia_ligada():
        raise IAIndisponivel("ANALISTA_IA_API_KEY não configurada")
    chamar = _PROVEDORES.get(config.ANALISTA_IA_PROVEDOR)
    if chamar is None:
        raise IAErro(f"provedor desconhecido: {config.ANALISTA_IA_PROVEDOR}")

    ultimo_erro = None
    for tentativa in range(TENTATIVAS):
        try:
            resp, ler = chamar(sistema, usuario, max_tokens, temperatura)
        except (requests.Timeout, requests.ConnectionError) as e:
            ultimo_erro = f"rede: {type(e).__name__}"
        else:
            if resp.status_code == 200:
                try:
                    dado = resp.json()
                except ValueError:
                    raise IAErro("resposta da IA não é JSON")
                texto, uso = ler(dado)
                return extrair_json(texto), uso
            ultimo_erro = f"HTTP {resp.status_code}: {resp.text[:300]}"
            if resp.status_code not in _STATUS_TEMPORARIOS:
                raise IAErro(ultimo_erro)
        if tentativa < TENTATIVAS - 1:
            _dormir(ESPERAS_SEG[tentativa])
    raise IAErro(f"IA indisponível após {TENTATIVAS} tentativas ({ultimo_erro})")
