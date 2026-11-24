#!/usr/bin/env python3
"""Search router, end to end without a GPU: the real `ollama` client Home Assistant uses (0.6.2 in
HA 2026.11) -> the add-on's proxy (main.py Gate) -> a fake Ollama replaying answers Qwen3-4B really
gave on 2026-11-24. Run: python3 test_search_router.py (needs `pip install ollama==0.6.2`)."""
import asyncio
import http.server
import json
import pathlib
import sys
import tempfile
import threading
import time
import types

import ollama

BIN = pathlib.Path(__file__).resolve().parent.parent / "rootfs" / "usr" / "local" / "bin"
sys.path.insert(0, str(BIN))
import search_router  # noqa: E402

# main.py reads add-on paths at import; point them at temp files.
tmp = pathlib.Path(tempfile.mkdtemp())
(tmp / "VERSION").write_text("580.196.07\n")
(tmp / "options.json").write_text(json.dumps({"model": "qwen3:4b-instruct-2507-q4_K_M", "search_router": True,
                                              "max_answer_tokens": 400}))
src = (BIN / "main.py").read_text().replace('"/opt/nvidia/VERSION"', repr(str(tmp / "VERSION"))) \
    .replace('"/data/options.json"', repr(str(tmp / "options.json")))
main = types.ModuleType("main")
exec(compile(src, "main.py", "exec"), main.__dict__)

TOOLS = [{"type": "function", "function": {"name": "web_search", "description": "Search the web.",
                                           "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                                                          "required": ["query"]}}}]
SYSTEM_EN = "You are the voice assistant of a home in France.\n\nReply in English.\n\nToday's date is 2026-11-24."
SYSTEM_FR = SYSTEM_EN.replace("Reply in English.", "Reply in French.")

upstream = {"calls": 0, "script": [], "cut": False}


class FakeOllama(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        upstream["calls"] += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for part in upstream["script"]:
                line = json.dumps(part).encode() + b"\n"
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                self.wfile.flush()
                time.sleep(0.01)
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            upstream["cut"] = True


def serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


main.UPSTREAM = ("127.0.0.1", serve(FakeOllama))
main.status["state"] = "ready"
GATE = f"http://127.0.0.1:{serve(main.Gate)}"


def tokens(text, tool_query=None):
    """Ollama-style stream: the text word by word, optional tool call, then done."""
    words = text.split(" ")
    parts = [{"model": "m", "created_at": "2026-11-24T00:00:00Z",
              "message": {"role": "assistant", "content": w + (" " if i < len(words) - 1 else "")}, "done": False}
             for i, w in enumerate(words) if text]
    if tool_query:
        parts.append({"model": "m", "created_at": "2026-11-24T00:00:00Z", "done": False,
                      "message": {"role": "assistant", "content": "", "tool_calls": [
                          {"function": {"name": "web_search", "arguments": {"query": tool_query}}}]}})
    parts.append({"model": "m", "created_at": "2026-11-24T00:00:00Z", "message": {"role": "assistant", "content": ""},
                  "done": True, "done_reason": "stop"})
    return parts


async def ask(question, model_says="", model_tool=None, system=SYSTEM_EN, tools=TOOLS, history=(), stream=True):
    """-> (spoken content, [tool queries], upstream called?, upstream cut?) as Home Assistant sees it."""
    upstream.update(calls=0, script=tokens(model_says, model_tool), cut=False)
    msgs = [{"role": "system", "content": system}, *history, {"role": "user", "content": question}]
    client = ollama.AsyncClient(host=GATE)
    content, queries = "", []
    if stream:
        async for r in await client.chat(model="m", messages=msgs, tools=tools, stream=True):
            m = r["message"]  # same access as homeassistant/components/ollama/entity.py _transform_stream
            if (calls := m.get("tool_calls")) is not None:
                queries += [c["function"]["arguments"]["query"] for c in calls]
            content += m.get("content") or ""
    else:
        r = await client.chat(model="m", messages=msgs, tools=tools, stream=False)
        content = r["message"].get("content") or ""
        queries = [c["function"]["arguments"]["query"] for c in r["message"].get("tool_calls") or []]
    await asyncio.sleep(0.1)
    return content.strip(), queries, upstream["calls"] > 0, upstream["cut"]


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    return cond


async def run():
    ok = True
    # Fresh questions: answered by the proxy, the model is never asked.
    c, q, called, _ = await ask("What is the latest news about Jordan Bardella?")
    ok &= check("fresh EN -> Let me check + search", c == "Let me check." and q == ["What is the latest news about Jordan Bardella"] and not called, (c, q, called))
    c, q, called, _ = await ask("Quelles sont les dernières polémiques autour de Jordan Bardella ?", system=SYSTEM_FR)
    ok &= check("fresh FR -> Je regarde + search", c == "Je regarde." and q == ["Quelles sont les dernières polémiques autour de Jordan Bardella"] and not called, (c, q))
    c, q, called, _ = await ask("Can you search online for Jordan Bardella and list any controversies?")
    ok &= check("explicit search request -> polite part stripped", q == ["Jordan Bardella and list any controversies"] and not called, q)
    c, q, called, _ = await ask("What temperature it is in New Mexico.")
    ok &= check("weather elsewhere -> search", q == ["What temperature it is in New Mexico"], q)
    c, q, _, _ = await ask("What's the latest news?", stream=False)
    ok &= check("fresh, non-streaming chat -> search", c == "Let me check." and q == ["What's the latest news"], (c, q))

    # Guard: the model answers; a deflection is dropped (never reaches HA) and becomes a search.
    c, q, called, cut = await ask("Who was Charles the Gull?",
                                  "I couldn't find information about Charles the Gull. That name does not appear in known historical records.")
    ok &= check("deflection -> replaced by search, not spoken", c == "Let me check." and q == ["Who was Charles the Gull"] and called, (c, q, called, cut))
    c, q, _, _ = await ask("Who is Jordan Bardella?", "I cannot search for Jordan Bardella. I do not have the capability to perform such searches.")
    ok &= check("'I cannot search' -> search", q == ["Who is Jordan Bardella"], (c, q))
    c, q, _, _ = await ask("Qui est Jordan Bardella ?", "Je n'ai pas d'information récente sur Jordan Bardella. Pourrais-tu préciser ?", system=SYSTEM_FR)
    ok &= check("FR deflection -> Je regarde + search", c == "Je regarde." and q == ["Qui est Jordan Bardella"], (c, q))
    good = "Charles de Gaulle was a French general and statesman. He became the first President of the Fifth Republic in 1959."
    c, q, called, _ = await ask("Who is Charles de Gaulle?", good)
    ok &= check("known answer -> passes whole, no search", c == good and q == [] and called, (c, q))
    c, q, _, _ = await ask("What is two plus two?", "Four.")
    ok &= check("very short answer -> passes", c == "Four." and q == [], (c, q))
    c, q, _, _ = await ask("Who is Charles de Gaulle?", "I'll check that for you.", model_tool="Charles de Gaulle")
    ok &= check("model's own tool call -> passes with its text", c == "I'll check that for you." and q == ["Charles de Gaulle"], (c, q))

    # After a search, the answer is the model's, even "couldn't find".
    hist = [{"role": "user", "content": "Who was Charles the Gull?"},
            {"role": "assistant", "content": "Let me check.", "tool_calls": [
                {"function": {"name": "web_search", "arguments": {"query": "Who was Charles the Gull"}}}]},
            {"role": "tool", "content": "No results."}]
    upstream.update(script=tokens("I couldn't find anyone called Charles the Gull."))
    c, q, called, _ = await ask_with_history(hist, "I couldn't find anyone called Charles the Gull.")
    ok &= check("after a tool result -> passes untouched", c == "I couldn't find anyone called Charles the Gull." and q == [] and called, (c, q))

    # Chats without web_search (AI Task, benchmark) are never touched.
    c, q, called, _ = await ask("What is the latest news?", "I couldn't find any.", tools=None)
    ok &= check("no web_search tool -> untouched", c == "I couldn't find any." and q == [] and called, (c, q))

    r = main.status["search_router"]
    ok &= check("counters", r["fresh"] == 5 and r["deflection"] == 3 and r["model"] >= 3, r)
    return ok


async def ask_with_history(hist, model_says):
    upstream.update(calls=0, script=tokens(model_says), cut=False)
    msgs = [{"role": "system", "content": SYSTEM_EN}, *hist]
    content, queries = "", []
    async for r in await ollama.AsyncClient(host=GATE).chat(model="m", messages=msgs, tools=TOOLS, stream=True):
        if (calls := r["message"].get("tool_calls")) is not None:
            queries += [c["function"]["arguments"]["query"] for c in calls]
        content += r["message"].get("content") or ""
    return content.strip(), queries, upstream["calls"] > 0, upstream["cut"]


def unit():
    ok = True
    # Every deflection the model produced on 2026-11-24 (voice traces + voice-eval).
    for said in ["I don't have real-time data on stock prices or financial markets.",
                 "The current temperature in New Mexico is not specified in the provided results. For accurate and real-time weather data, I recommend checking a dedicated weather website like AccuWeather or Weather.com.",
                 "I can't provide the current temperature in New Mexico. Please check a weather website or app for real-time updates.",
                 "I couldn't find information about Charles the Gull.",
                 "I couldn't find recent news about Jordan Bardella.",
                 "I cannot search for Jordan Bardella or list any controversies related to them.",
                 "Je n'ai pas d'information récente sur les polémiques autour de Jordan Bardella."]:
        ok &= check(f"deflection: {said[:50]}", search_router.is_deflection(said))
    for said in ["Charles de Gaulle was a French general and statesman.",
                 "The current stock price of Nvidia is about 165 dollars.",
                 "Jordan Bardella is a French politician."]:
        ok &= check(f"not a deflection: {said[:50]}", not search_router.is_deflection(said))
    for text, route in [("Who is Charles de Gaulle?", "guard"), ("What's the current stock of Nvidia?", "search"),
                        ("Turn on the jacuzzi", "guard"), ("Quel temps fait-il à Montréal ?", "search"),
                        ("Tell me a joke", "guard"), ("Who won the last Formula 1 race?", "search")]:
        got, _ = search_router.plan({"tools": TOOLS, "messages": [{"role": "user", "content": text}]})
        ok &= check(f"route {route}: {text}", got == route, got)
    return ok


if __name__ == "__main__":
    good = unit() & asyncio.run(run())
    print("ALL PASS" if good else "SOME FAILED")
    sys.exit(0 if good else 1)
