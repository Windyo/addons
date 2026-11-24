# Changelog

## 0.2.0

- `web_search` reads the top 3 result pages in parallel (4 s each) and adds the sentences that
  match the query, numbers preferred: landing-page snippets alone had no facts for the LLM to use.
  Duplicate results are dropped. Output cap 2600 chars.
- Stock questions (stock, share, quote, cours, action, bourse…) get a live quote line first
  (Yahoo Finance search + chart, no key): pages gave a price target, never the price.
- Tool description names prices, stock quotes, weather and anything recent, and tells the model
  never to answer that it has no real-time data.
- English queries without "what/who/the…" were searched in French (default language); detection
  now counts French and English words.
- Wikipedia and Wikidata engines were failing to load (no `contact_url`); fixed.

## 0.1.0

- First version: SearXNG (commit a7128d5d1, Google disabled, safe search on) and an MCP server
  with `web_search` (5 results, ≤ 1500 chars) and `read_page` (≤ 4000 chars, public addresses
  only).
