#!/usr/bin/env python3
"""Web search for a small local LLM, over MCP (Streamable HTTP at :8765/mcp).

Two tools, sized for a 4B model. Output is capped here because Home Assistant truncates nothing:
- web_search(query): top results from the local SearXNG, about 1500 characters at most.
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
SEARCH_CAP = 1500
PAGE_CAP = 4000
PAGE_BYTES = 2_000_000
TIMEOUT = 10
FRENCH = re.compile(r"[àâçéèêëîïôûùüÿœ]|\b(le|la|les|des|du|est|qui|quel|quelle|quand|combien|"
                    r"comment|pourquoi|aujourd'hui|météo|résultat|match|hier|demain)\b", re.I)

kwargs = {"host": "0.0.0.0", "port": 8765, "stateless_http": True, "json_response": True}
try:  # internal-only server reached by hostname: no DNS-rebinding host check
    from mcp.server.transport_security import TransportSecuritySettings
    kwargs["transport_security"] = TransportSecuritySettings(enable_dns_rebinding_protection=False)
except ImportError:
    pass
mcp = FastMCP("web-search", **kwargs)


def language(query):
    if FRENCH.search(query):
        return "fr-FR"
    if re.search(r"\b(the|what|who|when|how|is|are|today|weather|score)\b", query, re.I):
        return "en-US"
    return "fr-FR" if OPTS.get("default_language", "fr") == "fr" else "en-US"


def cap(text, limit):
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


@mcp.tool()
async def web_search(query: str) -> str:
    """Search the web for current facts (news, scores, opening hours, events, recent data).
    Returns up to 5 results as "title — snippet — site"."""
    params = {"q": query, "format": "json", "language": language(query), "safesearch": 1}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(SEARXNG, params=params)
        r.raise_for_status()
        data = r.json()
    lines = [cap(a, SNIPPET) for a in data.get("answers", []) if isinstance(a, str)][:1]
    for box in data.get("infoboxes", [])[:1]:
        if box.get("content"):
            lines.append(f"{box.get('infobox', '')} — {cap(box['content'], SNIPPET)}")
    for res in data.get("results", [])[:MAX_RESULTS]:
        site = urlparse(res.get("url", "")).netloc.removeprefix("www.")
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


@mcp.tool()
async def read_page(url: str) -> str:
    """Read the main text of one web page found by web_search (first 4000 characters)."""
    try:
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
                return "Too many redirects."
    except (ValueError, httpx.HTTPError, OSError) as e:
        return f"Could not read the page: {e}"
    text = trafilatura.extract(body.decode("utf-8", errors="replace"), include_comments=False) or ""
    return cap(text.strip(), PAGE_CAP) if text.strip() else "No readable text on this page."


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
