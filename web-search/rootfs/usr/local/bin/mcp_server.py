#!/usr/bin/env python3
"""Web search for a small local LLM, over MCP (Streamable HTTP at :8765/mcp).

Two tools, sized for a 4B model. Output is capped here because Home Assistant truncates nothing:
- web_search(query): top results from the local SearXNG plus the passages of the top pages that
  match the query (a 4B model can't answer from landing-page snippets), each result with its URL,
  2600 characters at most. An empty search is retried once with the key words only.
- read_page(url, question): the passages of one page about the question (or its start), 2000
  characters at most: the second step when the first passages didn't hold the answer.
Pages are only fetched from public http(s) addresses: the LLM reads untrusted web text, so it must
never be steered into the internal network (Supervisor, Home Assistant, other add-ons).
One log line per call (engines that answered or failed, timings), for diagnosis.
"""
import asyncio
import ipaddress
import json
import re
import socket
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import httpx
import trafilatura
from mcp.server.fastmcp import FastMCP

SEARXNG = "http://127.0.0.1:8888/search"
OPTS = json.load(open("/data/options.json"))
MAX_RESULTS = 5
SNIPPET = 180
SEARCH_CAP = 2600
READ_CAP = 2000
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
# A stock question gets a live quote first: finance pages put the price in a table row that names
# neither the company nor "stock", so page excerpts miss it (CI 2026-11-24 got only a price target).
STOCK = re.compile(r"\b(stocks?|shares?|share price|quote|nasdaq|nyse|ticker|cours|actions?|bourse)\b", re.I)
YAHOO = "https://query1.finance.yahoo.com"
# Query words that say nothing about which passage holds the answer.
NOT_TERMS = set("les des une est sont qui quel quelle quels quelles quand combien comment pourquoi "
                "aujourd'hui hier demain dans sur pour avec cette actuel actuelle dernier dernière "
                "the was were what what's who who's when where why how which today yesterday tomorrow "
                "current latest now and with this that about from there their into over "
                "derniers dernières autour quoi chez vers depuis".split())

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


def log(msg):
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] {msg}", flush=True)


def clean_url(url):
    """Without tracking parameters, and short: the model reads it, and may pass it to read_page."""
    u = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
         if not k.lower().startswith(("utm_", "fbclid", "gclid", "mc_", "ref", "src"))]
    out = u._replace(query=urlencode(q), fragment="").geturl()
    return out if len(out) <= 140 else u._replace(query="", fragment="").geturl()


def key_terms(text):
    return [w for w in dict.fromkeys(re.findall(r"\w+", text.lower())) if len(w) > 2 and w not in NOT_TERMS]


def excerpt(text, terms, limit=EXCERPT_CAP):
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
        if size + len(part) > limit:
            continue
        picked.append((i, part))
        size += len(part) + 1
    return " ".join(part for _, part in sorted(picked))


async def stock_quote(query, terms):
    """'Live quote NVDA (NVIDIA Corporation, NasdaqGS): 165.03 USD, +1.20 % on that session, last trade …', or ''."""
    name = " ".join(sorted(terms - {"stock", "stocks", "share", "shares", "price", "quote", "cours",
                                    "action", "actions", "bourse", "prix", "valeur"}, key=query.lower().find))
    if not name:
        return ""
    try:
        async with httpx.AsyncClient(timeout=EXCERPT_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}) as client:
            r = await client.get(f"{YAHOO}/v1/finance/search", params={"q": name, "quotesCount": 3, "newsCount": 0})
            r.raise_for_status()
            # Only a quote whose name or symbol is in the question ("actions pour économiser" ≠ shares).
            hits = [q for q in r.json().get("quotes", []) if q.get("quoteType") in ("EQUITY", "ETF", "INDEX")
                    and any(w in f"{q.get('symbol', '')} {q.get('longname', '')} {q.get('shortname', '')}".lower()
                            for w in name.split())]
            if not hits:
                return ""
            symbol = hits[0]["symbol"]
            r = await client.get(f"{YAHOO}/v8/finance/chart/{symbol}", params={"range": "1d", "interval": "1d"})
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
    except Exception:  # best effort, like the page excerpts
        return ""
    price, prev = meta.get("regularMarketPrice"), meta.get("chartPreviousClose") or meta.get("previousClose")
    if price is None:
        return ""
    change = f", {100 * (price - prev) / prev:+.2f} % on that session" if prev else ""
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(meta["regularMarketTime"])) if meta.get("regularMarketTime") else ""
    label = hits[0].get("longname") or hits[0].get("shortname") or ""
    return f"Live quote {symbol} ({label}, {meta.get('exchangeName', '')}): {price} {meta.get('currency', '')}{change}" + \
        (f", last trade {when}" if when else "")


async def page_excerpt(url, terms):
    try:  # best effort: a slow, blocked or unparsable page just adds nothing
        text = await asyncio.wait_for(page_text(url), EXCERPT_TIMEOUT)
    except Exception:
        return ""
    return excerpt(text, terms)


async def searx(query, lang):
    params = {"q": query, "format": "json", "language": lang, "safesearch": 1}
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
    return data, results


def engines(data, results):
    ok = sorted({e for r in data.get("results", []) for e in r.get("engines", [])})
    failed = [f"{n}: {why}" for n, why in data.get("unresponsive_engines", [])]
    return ok, failed


@mcp.tool()
async def web_search(query: str) -> str:
    """Search the web. Use it for anything current or that you may not know: news, prices, stock
    quotes, exchange rates, weather anywhere, sports results, opening hours, events, people, and
    anything that happened after your training. Never answer that you have no real-time data: call
    this tool instead. Returns passages read from the top pages, then the results as
    "title — snippet — URL"."""
    t0 = time.monotonic()
    lang = language(query)
    data, results = await searx(query, lang)
    ok, failed = engines(data, results)
    retry = ""
    if not results:
        # Seen 2026-11-24: the same questions gave results in the morning and "No results." at
        # noon (engines failing). The key words alone, any language, usually still find something.
        retry = " ".join(key_terms(query)) or query
        data, results = await searx(retry, "all")
        ok2, failed2 = engines(data, results)
        ok, failed = sorted(set(ok) | set(ok2)), failed + [f"retry {f}" for f in failed2]
    t_search = time.monotonic() - t0
    results = results[:MAX_RESULTS]
    terms = set(key_terms(query))
    quote, *excerpts = await asyncio.gather(
        stock_quote(query, terms) if STOCK.search(query) else asyncio.sleep(0, ""),
        *(page_excerpt(res["url"], terms) for res in results[:EXCERPT_PAGES]))

    lines = [quote] if quote else []
    lines += [cap(a, SNIPPET) for a in data.get("answers", []) if isinstance(a, str)][:1]
    for box in data.get("infoboxes", [])[:1]:
        if box.get("content"):
            lines.append(f"{box.get('infobox', '')} — {cap(box['content'], SNIPPET)}")
    for res, text in zip(results, excerpts):
        if text:
            lines.append(f"From {urlparse(res['url']).netloc.removeprefix('www.')}: {text}")
    for res in results:
        lines.append(f"{res.get('title', '').strip()} — {cap((res.get('content') or '').strip(), SNIPPET)}"
                     f" — {clean_url(res['url'])}")
    out = cap("\n".join(lines), SEARCH_CAP) if lines else "No results."
    log(f"search {query!r} [{lang}]{f' retry {retry!r}' if retry else ''}: {len(results)} results, "
        f"{sum(bool(e) for e in excerpts)} excerpts, quote={'yes' if quote else 'no'}, {len(out)} chars, "
        f"search {t_search:.1f} s, total {time.monotonic() - t0:.1f} s; engines {', '.join(ok) or 'none'}"
        f"{'; failed ' + '; '.join(failed) if failed else ''}")
    return out


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
async def read_page(url: str, question: str = "") -> str:
    """Read one page from web_search's results when its passages didn't contain the answer.
    Give the page's URL and the user's question; returns the parts of the page about the question."""
    t0 = time.monotonic()
    try:
        text = await page_text(url)
    except (ValueError, httpx.HTTPError, OSError) as e:
        log(f"read {url} failed: {e}")
        return f"Could not read the page: {e}"
    if not text:
        log(f"read {url}: no readable text")
        return "No readable text on this page."
    part = excerpt(text, set(key_terms(question)), READ_CAP) if question else ""
    out = part or cap(text, READ_CAP)
    log(f"read {url} q={question!r}: {len(text)} chars on page, returned {len(out)}"
        f"{' (start of page)' if not part else ''}, {time.monotonic() - t0:.1f} s")
    return out


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
