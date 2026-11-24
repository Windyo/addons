# Web Search

Gives the local LLM (Ollama CUDA) a way to look things up, and nothing else: no house access.

- **SearXNG** runs inside the add-on (not reachable from outside it) and queries DuckDuckGo,
  Brave, Bing, Qwant, Wikipedia, Wikidata and news engines. Google is off (CAPTCHAs).
- An **MCP server** on port 8765 offers one tool, `web_search(query)`: a live quote for stock
  questions, the sentences of the top 3 pages that match the query, then the top 5 results as
  "title — snippet — site", 2600 characters at most. Pages are only fetched from public http(s)
  addresses, never the internal network.

## Home Assistant

Add the **Model Context Protocol** integration with the URL `http://8634c18d-web-search:8765/mcp`,
then select only that API under "Control Home Assistant" in the "+ web" Ollama agent.
