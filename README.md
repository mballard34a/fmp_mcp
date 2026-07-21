# BCNA FMP MCP Server

A Model Context Protocol (MCP) connector that gives Claude direct access to the
**entire Financial Modeling Prep `stable` API** using your own API key — including
the **bulk** datasets the stock FMP connector can't reach (scores-bulk, dcf-bulk,
ratios-ttm-bulk, key-metrics-ttm-bulk, price-target-summary-bulk, and the rest).

The same file runs two ways:

- **Local (Claude Desktop)** — stdio transport, key stays on your machine.
- **Remote (claude.ai web + mobile)** — HTTPS transport behind a public URL.

Pick whichever surface you actually use. Steps for both are below.

---

## What's in the box

| File | Purpose |
|------|---------|
| `fmp_mcp_server.py` | The server. 15 tools. |
| `requirements.txt` | Python dependencies. |
| `.env.example` | Local environment-variable template. |
| `asgi.py` | ASGI entry point, hosted/remote deploys only. |
| `render.yaml` | Reproducible private Render deployment. |
| `.codex-plugin/plugin.json` | ChatGPT/Codex plugin manifest. |
| `.mcp.json` | Remote MCP connection configuration. |
| `README.md` | This file. |

### Tools

- **`fmp_request(endpoint, params)`** — universal passthrough. Reaches *any* FMP
  stable endpoint, present or future. Pass the path after `/stable/`
  (e.g. `"profile"`, `"ratios-ttm"`, `"economic-indicators"`,
  `"insider-trading/search"`) and a params dict. The `apikey` is added for you.
- **`fmp_bulk(dataset, symbols, params, max_rows)`** — pulls a bulk CSV dataset
  and **filters it to your symbol list** so a 69 MB file never lands in the chat.
  Datasets: `profile, rating, dcf, scores, price_target_summary,
  key_metrics_ttm, ratios_ttm, peers, upgrades_downgrades_consensus, etf_holder`.
- Convenience tools: `search_symbol, quote, batch_quote, company_profile,
  key_metrics_ttm, ratios_ttm, financial_scores, dcf, price_target_summary,
  analyst_estimates, grades_consensus, insider_trading_search, stock_screener`.

---

## Security — read this first

The server reads your key from the **`FMP_API_KEY`** environment variable. It is
never written into the code and never sent back to the model. Hosted requests
also require **`FMP_MCP_ACCESS_TOKEN`** as an HTTP bearer token. Keep both out of
source control and store both as Render secrets.

---

## Option A — Local, with Claude Desktop (simplest, key never leaves your computer)

1. Install Python 3.10+ if you don't have it.
2. In this folder:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Tell Claude Desktop about the server. Open its config file:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`

   Add (adjust the two absolute paths to this folder):
   ```json
   {
     "mcpServers": {
       "fmp": {
         "command": "/ABSOLUTE/PATH/fmp-mcp/.venv/bin/python",
         "args": ["/ABSOLUTE/PATH/fmp-mcp/fmp_mcp_server.py"],
         "env": { "FMP_API_KEY": "your-fmp-key-here" }
       }
     }
   }
   ```
4. Restart Claude Desktop. The `fmp` tools appear in the connectors list.

That's it — no cloud account, no public URL.

---

## Option B — Remote, for claude.ai web + mobile (needs a public HTTPS URL)

claude.ai's web and mobile apps connect to MCP servers by **URL**, so the server
has to be reachable on the internet. Any Python host works (Render, Railway,
Fly.io, a small VPS). General shape:

1. Push this folder to a Git repo (or upload it to the host).
2. Set the start command to:
   ```bash
   FMP_MCP_TRANSPORT=http python fmp_mcp_server.py
   ```
   or, equivalently, run it under uvicorn via the included `asgi.py`:
   ```bash
   uvicorn asgi:app --host 0.0.0.0 --port $PORT
   ```
3. In the host's dashboard, add environment secrets `FMP_API_KEY` and
   `FMP_MCP_ACCESS_TOKEN`. Use two different values.
4. The host gives you an HTTPS URL. Your MCP endpoint is that URL plus the
   streamable-http path, typically:
   ```
   https://your-app.onrender.com/mcp
   ```
5. In claude.ai → **Settings → Connectors → Add custom connector**, paste that
   URL. Name it "FMP".

> Note: most hosts inject a `$PORT` they expect you to bind. The server reads
> `FMP_MCP_PORT`, so set `FMP_MCP_PORT=$PORT` in the host's env if needed, or use
> the uvicorn command above which takes `--port` directly.

---

## Quick test once connected

Ask Claude:

- "Use the FMP connector to pull the DCF for NVDA."
- "Pull ratios-ttm-bulk for these tickers: AAPL, MSFT, NVDA, KO, JPM."
- "Use fmp_request on `treasury-rates` and show me the latest 10-year."

---

## Notes

- Bulk files are market-wide and large. **Always pass `symbols`** to `fmp_bulk`
  unless you know the dataset is small; otherwise the result is capped at
  `max_rows` (default 2000). For full-universe dumps that feed the scorecard
  pipeline, the Drive-file workflow is still the better path.
- Errors come back with a plain-English reason (bad key, plan doesn't cover the
  endpoint, rate limit, wrong path) so you can fix them fast.
