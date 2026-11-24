# Changelog

## 0.3.0

- Each result now ends with its URL (tracking parameters stripped), and `read_page(url, question)`
  returns the parts of that page about the question (≤ 2000 chars) instead of the first 4000
  characters: the model can read further when the first passages lack the answer.
- An empty search is retried once with the key words only, in any language: on 2026-11-24 the same
  questions gave results in the morning and "No results." at noon.
- One log line per call: query, language, retry, results, excerpts, quote, size, timings, engines
  that answered and engines that failed.

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
