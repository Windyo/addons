"""Search router for chats that offer a `web_search` tool (the Home Assistant web agent).

On 2026-11-24, with the tool listed and instructions saying to search, Qwen3-4B searched only for
stock prices. For weather, news, "controversies" and unknown names it answered "I couldn't find…",
"I can't provide…", even "I cannot search" when asked to search (hass-ops voice-eval runs). So the
search decision is made here, not by the model:

- pass:   a tool result already follows the last user message. The model is answering from
          results, so its "couldn't find" is genuine. Also every chat without web_search.
- search: the question asks for something fresh (current, news, price, weather, search…). The
          proxy replies at once, without the model: "Let me check." plus a web_search call with the
          question. Home Assistant speaks the phrase, runs the search and asks the model again.
- guard:  anything else goes to the model, holding its first sentence. If it reads as a
          deflection, the proxy drops it and sends the search reply instead (never spoken).
"""
import json
import re
from datetime import datetime, timezone

FRESH = re.compile(
    r"\b(current(ly)?|latest|today|tonight|tomorrow|yesterday|right now|now|this (week|month|year|"
    r"morning|afternoon|evening)|news|headlines?|prices?|stocks?|shares?|share price|quote|exchange rate|"
    r"weather|temperature|forecast|scores?|who won|results?|standings|opening hours|open (on|until|today)|"
    r"search|look up|google|controvers\w*|scandals?|recent(ly)?|last (match|game|race|election|night)|"
    r"elections?|"
    r"actuel(le)?s?|actuellement|derni(er|ère)s?|aujourd'?hui|ce soir|demain|hier|maintenant|en ce moment|"
    r"cette (semaine|année|nuit)|actualités?|nouvelles|infos?|prix|cours|bourse|cotation|météo|"
    r"température|prévisions?|quel temps|résultats?|qui a gagné|classement|horaires?|ouvert|cherche|"
    r"recherche|polémiques?|controverses?|scandales?|récemment|élections?)\b", re.I)

DEFLECT = re.compile(
    r"\b(couldn'?t find|could not find|can'?t find|cannot find|can'?t provide|cannot provide|"
    r"unable to (find|provide|access|search|browse|look)|not able to (find|provide|access|search|browse)|"
    r"(don'?t|do not) have (access|the (ability|capability)|real-?time|current|up-to-date|recent|any information|information)|"
    r"(can'?t|cannot) (search|browse|access|look up)|no (recent |current |up-to-date )?information|"
    r"real-?time (data|information|updates)|check(ing)? (a|an|the) [\w ]{0,30}(website|app|site)|"
    r"je n'ai pas (trouvé|accès|d'informations?|pu|la (possibilité|capacité))|"
    r"je ne (trouve|dispose) pas|je ne peux pas (chercher|rechercher|accéder|fournir|consulter|naviguer)|"
    r"aucune information|je ne suis pas en mesure|(données|informations) en temps réel|"
    r"consulte[rz]? (un|une|le|la) [\w' ]{0,30}(site|application))", re.I)

# A question that names the pipeline's language in the instructions ("Reply in French.").
FILLER = {"fr": "Je regarde.", "en": "Let me check."}
POLITE = re.compile(
    r"^\s*((hey|ok|okay|dis)\b[ ,]*)?((can|could|would|will) you (please )?|please |"
    r"(est-ce que )?(tu peux|peux-tu|pourrais-tu|vous pouvez|pouvez-vous) )?"
    r"((search|look up|find|check|google)( (online|on the (internet|web)|the web))?( for)?|"
    r"(chercher|rechercher|regarder|trouver)( (sur internet|en ligne|sur le web))?)\s+", re.I)
HOLD_MAX = 120  # characters held before releasing the model's answer


def _tool_names(req):
    for t in req.get("tools") or []:
        if isinstance(t, dict):
            yield (t.get("function") or {}).get("name")


def clean_query(text):
    q = POLITE.sub("", text.strip()).strip().rstrip("?.!¿ ").strip()
    return q or text.strip()


def plan(req):
    """-> ("pass" | "search" | "guard", info or None)."""
    if not isinstance(req, dict) or "web_search" not in _tool_names(req):
        return "pass", None
    msgs = [m for m in req.get("messages") or [] if isinstance(m, dict)]
    users = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
    if not users:
        return "pass", None
    last = users[-1]
    if any(m.get("role") == "tool" for m in msgs[last + 1:]):
        return "pass", None
    text = msgs[last].get("content") or ""
    system = next((m.get("content") or "" for m in msgs if m.get("role") == "system"), "")
    if "Reply in French" in system:
        lang = "fr"
    elif "Reply in English" in system:
        lang = "en"
    else:
        lang = "fr" if re.search(r"[àâçéèêëîïôûùüÿœ]|\b(le|la|les|est|quel|quelle)\b", text, re.I) else "en"
    info = {"query": clean_query(text), "lang": lang, "model": req.get("model", ""),
            "stream": req.get("stream", True) is not False}
    return ("search" if FRESH.search(text) else "guard"), info


def release(text):
    """True once enough of the answer is held to judge it (first sentence, or HOLD_MAX)."""
    t = text.strip()
    return len(t) >= HOLD_MAX or (len(t) >= 15 and re.search(r"[.!?…](\s|$)", t) is not None)


def is_deflection(text):
    return DEFLECT.search(text) is not None


def search_reply(info):
    """Ollama /api/chat reply: the filler, then a web_search call, then done. Bytes, plus content type."""
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    base = {"model": info["model"], "created_at": created}
    call = {"function": {"name": "web_search", "arguments": {"query": info["query"]}}}
    end = {"done": True, "done_reason": "stop", "total_duration": 0, "load_duration": 0,
           "prompt_eval_count": 0, "prompt_eval_duration": 0, "eval_count": 0, "eval_duration": 0}
    filler = FILLER[info["lang"]]
    if not info["stream"]:
        msg = {"role": "assistant", "content": filler, "tool_calls": [call]}
        return json.dumps({**base, "message": msg, **end}).encode(), "application/json; charset=utf-8"
    lines = [
        {**base, "message": {"role": "assistant", "content": filler + " "}, "done": False},
        {**base, "message": {"role": "assistant", "content": "", "tool_calls": [call]}, "done": False},
        {**base, "message": {"role": "assistant", "content": ""}, **end},
    ]
    return b"".join(json.dumps(x).encode() + b"\n" for x in lines), "application/x-ndjson"
