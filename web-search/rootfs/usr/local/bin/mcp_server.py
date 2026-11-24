#!/usr/bin/env python3
"""Web search for a small local LLM, over MCP (Streamable HTTP at :8765/mcp).

Two tools, sized for a 4B model. Output is capped here because Home Assistant truncates nothing:
- web_search(query): top results from the local SearXNG plus the passages of the top pages that
  match the query (a 4B model can't answer from landing-page snippets), 2600 characters at most.
- read_page(url): the main text of one public web page, 4000 characters at most.
read_page only fetches public http(s) addresses: the LLM reads untrusted web text, so it must
never be steered into the internal network (Supervisor, Home Assistant, other add-ons).
"""
import asyncio
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from mcp.server.fastmcp import FastMCP

SEARXNG = "http://127.0.0.1:8888/search"
OPTS = json.load(open("/data/options.json"))
MAX_RESULTS = 5
SNIPPET = 240
SEARCH_CAP = 2600
PAGE_CAP = 4000
PAGE_BYTES = 2_000_000
TIMEOUT = 10
EXCERPT_PAGES = 3     # top results read by web_search, in parallel
EXCERPT_TIMEOUT = 4   # per page; SearXNG's own timeout is 4 s, HA's MCP call timeout 30 s
EXCERPT_CAP = 450     # per page
FR_WORDS = set("le la les des du de un une est sont qui quel quelle quels quelles quand combien comment "
               "pourquoi où aujourd'hui hier demain météo résultat prix cours action et dans sur pour "
               "avec ce cette".split())
EN_WORDS = set("the an of in at for to is are was were what what's who who's when where why how which "
               "today yesterday tomorrow weather score price stock current latest news and with this "
               "that".split())
# Query words that say nothing about which passage holds the answer.
NOT_TERMS = set("les des une est sont qui quel quelle quels quelles quand combien comment pourquoi "
                "aujourd'hui hier demain dans sur pour avec cette actuel actuelle dernier dernière "
                "the was were what what's who who's when where why how which today yesterday tomorrow "
                "current latest now and with this that".split())

kwargs = {"host": "0.0.0.0", "port": 8765, "stateless_http": True, "json_response": True}
try:  # internal-only server reached by hostname: no DNS-rebinding host check
    from mcp.server.transport_security import TransportSecuritySettings
    kwargs["transport_security"] = TransportSecuritySettings(enable_dns_rebinding_protection=False)
except ImportError:
    pass
mcp = FastMCP("web-search", **kwargs)


def language(query):
    if re.search(r"[àâçéèêëîïôûùüÿœ]", query, re.I):
        return "fr-FR"
    words = re.findall(r"[a-z']+", query.lower())
    fr, en = sum(w in FR_WORDS for w in words), sum(w in EN_WORDS for w in words)
    if fr != en:
        return "fr-FR" if fr > en else "en-US"
    return "fr-FR" if OPTS.get("default_language", "fr") == "fr" else "en-US"


def cap(text, limit):
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def excerpt(text, terms):
    """The sentences of a page that mention the query's terms, best first, numbers preferred.
    A sentence with a number right after a matching one is kept too ("NVIDIA stock." "Closed at 182")."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if 15 <= len(p.strip()) <= 400]
    hits = [sum(t in part.lower() for t in terms) for part in parts]
    scored = []
    for i, part in enumerate(parts):
        number = bool(re.search(r"\d", part))
        if hits[i]:
            scored.append((2 * hits[i] + number, i, part))
        elif number and any(hits[max(0, i - 2):i]):
            scored.append((1, i, part))
    picked, size = [], 0
    for _, i, part in sorted(scored, key=lambda x: (-x[0], x[1])):
        if size + len(part) > EXCERPT_CAP:
            continue
        picked.append((i, part))
        size += len(part) + 1
    return " ".join(part for _, part in sorted(picked))


async def page_excerpt(url, terms):
    try:  # best effort: a slow, blocked or unparsable page just adds nothing
        text = await asyncio.wait_for(page_text(url), EXCERPT_TIMEOUT)
    except Exception:
        return ""
    return excerpt(text, terms)


@mcp.tool()
async def web_search(query: str) -> str:
    """Search the web. Use it for anything current or that you may not know: news, prices, stock
    quotes, exchange rates, weather anywhere, sports results, opening hours, events, people, and
    anything that happened after your training. Never answer that you have no real-time data: call
    this tool instead. Returns passages read from the top pages, then the results as
    "title — snippet — site"."""
    params = {"q": query, "format": "json", "language": language(query), "safesearch": 1}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(SEARXNG, params=params)
        r.raise_for_status()
        data = r.json()
    results, seen = [], set()
    for res in data.get("results", []):
        u = urlparse(res.get("url", ""))
        key = (u.netloc.removeprefix("www."), u.path.rstrip("/"))
        if u.scheme in ("http", "https") and u.netloc and key not in seen:
            seen.add(key)
            results.append(res)
    results = results[:MAX_RESULTS]
    terms = {w for w in re.findall(r"\w+", query.lower()) if len(w) > 2 and w not in NOT_TERMS}
    excerpts = await asyncio.gather(*(page_excerpt(res["url"], terms) for res in results[:EXCERPT_PAGES]))

    lines = [cap(a, SNIPPET) for a in data.get("answers", []) if isinstance(a, str)][:1]
    for box in data.get("infoboxes", [])[:1]:
        if box.get("content"):
            lines.append(f"{box.get('infobox', '')} — {cap(box['content'], SNIPPET)}")
    for res, text in zip(results, excerpts):
        if text:
            lines.append(f"From {urlparse(res['url']).netloc.removeprefix('www.')}: {text}")
    for res in results:
        site = urlparse(res["url"]).netloc.removeprefix("www.")
        lines.append(f"{res.get('title', '').strip()} — {cap((res.get('content') or '').strip(), SNIPPET)} — {site}")
    return cap("\n".join(lines), SEARCH_CAP) if lines else "No results."


async def public_url(url):
    """Raise ValueError unless url is http(s) and every address of its host is public."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("only http(s) URLs")
    infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or 0, type=socket.SOCK_STREAM)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("not a public address")


async def page_text(url):
    """Main text of a public page. Raises ValueError / httpx.HTTPError / OSError."""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                                 headers={"User-Agent": "Mozilla/5.0 (home assistant voice)"}) as client:
        for _ in range(4):  # follow redirects by hand, checking every hop
            await public_url(url)
            async with client.stream("GET", url) as r:
                if r.is_redirect and "location" in r.headers:
                    url = urljoin(url, r.headers["location"])
                    continue
                r.raise_for_status()
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > PAGE_BYTES:
                        break
                break
        else:
            raise ValueError("too many redirects")
    html = body.decode("utf-8", errors="replace")
    return (await asyncio.to_thread(trafilatura.extract, html, include_comments=False) or "").strip()


@mcp.tool()
async def read_page(url: str) -> str:
    """Read the main text of one web page found by web_search (first 4000 characters)."""
    try:
        text = await page_text(url)
    except (ValueError, httpx.HTTPError, OSError) as e:
        return f"Could not read the page: {e}"
    return cap(text, PAGE_CAP) if text else "No readable text on this page."


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
