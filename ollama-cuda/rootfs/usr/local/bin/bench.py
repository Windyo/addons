#!/usr/bin/env python3
"""Phase-3 benchmark for the Ollama CUDA add-on (option `benchmark`).

Matrix: Qwen3-4B with flash attention off/on and a f16/q8_0 KV cache (q8_0 needs FA), plus
Qwen3-8B at 4k context for reference (option `benchmark_8b`). For each config: load time, VRAM
(the model's and the whole card's, Whisper included), then 6 chats with a fixed ~400-token
chat-only system prompt: the first is cold, the rest reuse the cached prompt. Thermal gates as in
the Whisper benchmark: start each config at <= 75 C, pause at 87 C until 80 C.
Results: /data/bench/results.json and sensor.ollama_cuda_benchmark.
"""
import json
import os
import subprocess
import time

import main as m

MODEL_4B = m.OPTS["model"]
MODEL_8B = "qwen3:8b"
START_C, PAUSE_C, RESUME_C = 75, 87, 80

SYSTEM = (
    "You are the voice assistant of a family home in France. You answer spoken questions from "
    "the people who live here. Answer in the language of the question: French or English. Keep "
    "every answer to one or two short sentences that sound natural when read aloud by a speech "
    "synthesiser: no lists, no markdown, no emoji, no URLs, and spell out numbers and units the "
    "way people say them. You cannot control the house: lights, heating, doors, the alarm, music "
    "and every device are handled by another system before you are asked. If someone asks you to "
    "do something in the house, say briefly that the house did not understand the command and "
    "that they can rephrase it. Be warm and a little playful, never sarcastic. If you do not know "
    "something, say so in one sentence instead of guessing. Do not invent facts about the family, "
    "their plans, or the weather. Questions can be general knowledge, definitions, small "
    "calculations, conversions, cooking help, word games, jokes or riddles for children, or "
    "simple advice. Vous êtes l'assistant vocal d'une maison familiale en France. Répondez dans "
    "la langue de la question, en une ou deux phrases courtes et naturelles à l'oral, sans liste "
    "ni mise en forme. Vous ne pilotez pas la maison : si on vous demande d'agir sur un appareil, "
    "dites simplement que la maison n'a pas compris la commande et qu'on peut la reformuler. "
    "Soyez chaleureux et un peu joueur, jamais sarcastique. Si vous ne savez pas, dites-le."
)
QUESTIONS = [
    "Pourquoi le ciel est bleu ?",
    "How many tablespoons are in a cup?",
    "Raconte-moi une devinette pour un enfant de six ans.",
    "What's the difference between a crocodile and an alligator?",
    "Combien de temps faut-il pour cuire un oeuf mollet ?",
    "Give me a word that rhymes with orange.",
]


def gpu():
    out = subprocess.run(["nvidia-smi", "--format=csv,noheader,nounits",
                          "--query-gpu=temperature.gpu,memory.used,memory.total"],
                         capture_output=True, text=True, timeout=10).stdout
    t, used, total = (int(x) for x in out.split(","))
    return t, used, total


def cool_to(limit, why):
    t = gpu()[0]
    if t <= limit:
        return 0
    m.log(f"bench: GPU {t} C, waiting for {limit} C ({why})")
    t0 = time.monotonic()
    while gpu()[0] > limit:
        time.sleep(5)
    return round(time.monotonic() - t0)


def run_config(name, model, fa, kv, ctx):
    env = m.ollama_env()
    env.update({"OLLAMA_FLASH_ATTENTION": "1" if fa else "0", "OLLAMA_KV_CACHE_TYPE": kv,
                "OLLAMA_CONTEXT_LENGTH": str(ctx)})
    res = {"config": name, "model": model, "flash_attention": fa, "kv_cache": kv, "context": ctx}
    res["cooling_wait_s"] = cool_to(START_C, "config start")
    m.start_ollama(env)
    try:
        m.ensure_model(model)
        full = m.warm_up(model)
        with m.lock:
            res.update({k: m.status.get(k) for k in ("load_s", "model_size_mib", "model_vram_mib")})
        res["fully_on_gpu"] = full
        res["card_vram_used_mib"], res["card_vram_total_mib"] = gpu()[1:]
        chats = []
        for q in QUESTIONS:
            res["cooling_wait_s"] += cool_to(RESUME_C, "pause") if gpu()[0] >= PAUSE_C else 0
            t0 = time.monotonic()
            r = m.api("POST", "/api/chat", {
                "model": model, "stream": False, "keep_alive": -1,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": q}],
            }, timeout=300)
            wall = time.monotonic() - t0
            pe, pd = r.get("prompt_eval_count", 0), r.get("prompt_eval_duration", 0)
            ec, ed = r.get("eval_count", 0), r.get("eval_duration", 0)
            chats.append({
                "q": q, "wall_ms": round(wall * 1000), "prompt_tokens": pe,
                "pp_tps": round(pe / (pd / 1e9), 1) if pd else None,
                "out_tokens": ec, "tg_tps": round(ec / (ed / 1e9), 1) if ed else None,
                "temp_c": gpu()[0], "answer": r.get("message", {}).get("content", "")[:200],
            })
        warm = sorted(c["wall_ms"] for c in chats[1:])
        res.update({
            "cold_ms": chats[0]["wall_ms"],
            "warm_median_ms": warm[len(warm) // 2],
            "warm_max_ms": warm[-1],
            "tg_tps_median": sorted(c["tg_tps"] or 0 for c in chats)[len(chats) // 2],
            "max_temp_c": max(c["temp_c"] for c in chats),
            "chats": chats,
        })
    except Exception as e:  # recorded, the next config still runs
        res["error"] = f"{type(e).__name__}: {e}"
    finally:
        m.stop_ollama()
    m.log(f"bench: {name}: " + json.dumps({k: v for k, v in res.items() if k != "chats"}))
    return res


def main():
    configs = [
        ("4b fa-off f16", MODEL_4B, False, "f16", m.OPTS["context_length"]),
        ("4b fa-on f16", MODEL_4B, True, "f16", m.OPTS["context_length"]),
        ("4b fa-on q8_0", MODEL_4B, True, "q8_0", m.OPTS["context_length"]),
    ]
    if m.OPTS.get("benchmark_8b"):
        configs.append(("8b fa-on q8_0 4k", MODEL_8B, True, "q8_0", 4096))
    results = [run_config(*c) for c in configs]
    os.makedirs("/data/bench", exist_ok=True)
    with open("/data/bench/results.json", "w") as f:
        json.dump({"at": m.now(), "results": results}, f, indent=1)
    summary = [{k: v for k, v in r.items() if k != "chats"} for r in results]
    m.publish("sensor.ollama_cuda_benchmark", "done", {
        "friendly_name": "Ollama CUDA benchmark", "icon": "mdi:speedometer", "at": m.now(),
        "results": summary, "answers": {r["config"]: [c["answer"] for c in r.get("chats", [])]
                                        for r in results}})


if __name__ == "__main__":
    main()
