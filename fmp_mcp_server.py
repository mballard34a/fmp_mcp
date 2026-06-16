#!/usr/bin/env python3
"""
BCNA FMP MCP Server
===================

A Model Context Protocol server that gives Claude direct access to the entire
Financial Modeling Prep (FMP) `stable` API surface, using your own API key.

Design
------
* `fmp_request`  - universal passthrough. Reaches ANY FMP stable endpoint,
                   present or future. This alone guarantees 100% coverage.
* `fmp_bulk`     - pulls FMP *bulk* CSV datasets and FILTERS them to a symbol
                   list so a 69 MB file never lands in the chat context.
* convenience    - a handful of named tools for the endpoints the BCNA
                   scorecard leans on (quote, ratios-ttm, scores, dcf, etc.).

Security
--------
The API key is read from the FMP_API_KEY environment variable. It is NEVER
hardcoded here and NEVER passed back to the model. Set it as a local env var
(Claude Desktop) or a hosting secret (claude.ai web/mobile).

Transports
----------
Same file, either transport:
    FMP_MCP_TRANSPORT=stdio   python fmp_mcp_server.py     # Claude Desktop (local)
    FMP_MCP_TRANSPORT=http    python fmp_mcp_server.py     # hosted / remote URL
or pass --stdio / --http on the command line.
"""

from __future__ import annotations

import csv
import io
import os
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

FMP_BASE = "https://financialmodelingprep.com/stable"
API_KEY = os.environ.get("FMP_API_KEY", "").strip()

# Largest text payload (e.g. CSV) the passthrough will return before truncating,
# to protect the model's context window. Bulk pulls should use fmp_bulk instead.
MAX_TEXT_CHARS = 100_000

mcp = FastMCP(
    "fmp",
    host=os.environ.get("FMP_MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("FMP_MCP_PORT", "8787")),
)

_client = httpx.Client(timeout=90.0, follow_redirects=True)


class FMPError(Exception):
    """Raised with an actionable message when an FMP call cannot be completed."""


# ---------------------------------------------------------------------------
# Core HTTP helpers
# ---------------------------------------------------------------------------
def _require_key() -> str:
    if not API_KEY:
        raise FMPError(
            "FMP_API_KEY is not set. Set it as an environment variable before "
            "starting the server (local) or as a hosting secret (remote). "
            "Do not hardcode the key."
        )
    return API_KEY


def _normalize_endpoint(endpoint: str) -> str:
    """Accept 'profile', '/profile', 'stable/profile', or a full URL tail."""
    e = endpoint.strip()
    for marker in ("financialmodelingprep.com/stable/", "financialmodelingprep.com/api/v3/"):
        if marker in e:
            e = e.split(marker, 1)[1]
    e = e.lstrip("/")
    for pref in ("stable/", "api/v3/", "v3/"):
        if e.startswith(pref):
            e = e[len(pref):]
    return e


def _get(endpoint: str, params: dict[str, Any] | None = None) -> httpx.Response:
    key = _require_key()
    ep = _normalize_endpoint(endpoint)
    q: dict[str, Any] = {}
    for k, v in (params or {}).items():
        if v is None:
            continue
        q[k] = ",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else v
    q["apikey"] = key

    url = f"{FMP_BASE}/{ep}"
    try:
        r = _client.get(url, params=q)
    except httpx.HTTPError as exc:
        raise FMPError(f"Network error calling FMP endpoint '{ep}': {exc}") from exc

    if r.status_code in (401, 403):
        raise FMPError(
            f"FMP rejected the request to '{ep}' (HTTP {r.status_code}). "
            "Verify FMP_API_KEY is valid and your plan covers this endpoint."
        )
    if r.status_code == 404:
        raise FMPError(
            f"FMP endpoint not found: '{ep}'. Check the path against the FMP docs "
            "(the universal tool expects the part after /stable/)."
        )
    if r.status_code == 429:
        raise FMPError(
            f"FMP rate limit hit on '{ep}' (HTTP 429). Wait and retry, or reduce "
            "request frequency."
        )
    if r.status_code >= 400:
        raise FMPError(f"FMP returned HTTP {r.status_code} for '{ep}': {r.text[:300]}")
    return r


def _payload(r: httpx.Response) -> Any:
    """Return parsed JSON when possible, else (size-guarded) text."""
    try:
        return r.json()
    except Exception:
        text = r.text
        if len(text) > MAX_TEXT_CHARS:
            return (
                text[:MAX_TEXT_CHARS]
                + f"\n\n[TRUNCATED: response was {len(text):,} chars. For bulk "
                "datasets call fmp_bulk(dataset, symbols=[...]) to filter instead.]"
            )
        return text


# ---------------------------------------------------------------------------
# Universal passthrough — covers every FMP endpoint
# ---------------------------------------------------------------------------
@mcp.tool()
def fmp_request(endpoint: str, params: dict[str, Any] | None = None) -> Any:
    """Call ANY Financial Modeling Prep `stable` endpoint and return its data.

    This is the universal passthrough; it reaches every FMP endpoint, including
    ones without a dedicated tool here. Use it whenever no convenience tool fits.

    Args:
        endpoint: The path AFTER /stable/, e.g. "profile", "ratios-ttm",
            "key-metrics", "grades-consensus", "insider-trading/search",
            "economic-indicators", "treasury-rates". A leading "/" or a
            "stable/" prefix is stripped automatically.
        params: Query parameters as a dict, e.g. {"symbol": "AAPL"},
            {"symbols": ["AAPL", "MSFT"]} (lists are comma-joined), or
            {"name": "GDP"}. Do NOT include apikey; it is added automatically.

    Returns:
        Parsed JSON (list/dict) for JSON endpoints, or raw text (size-guarded)
        for endpoints that return CSV.
    """
    return _payload(_get(endpoint, params))


# ---------------------------------------------------------------------------
# Filtered bulk — the scorecard workhorse
# ---------------------------------------------------------------------------
BULK_DATASETS: dict[str, str] = {
    "profile": "profile-bulk",                                   # needs part=N
    "rating": "rating-bulk",
    "dcf": "dcf-bulk",
    "scores": "scores-bulk",
    "price_target_summary": "price-target-summary-bulk",
    "key_metrics_ttm": "key-metrics-ttm-bulk",
    "ratios_ttm": "ratios-ttm-bulk",
    "peers": "peers-bulk",
    "upgrades_downgrades_consensus": "upgrades-downgrades-consensus-bulk",
    "etf_holder": "etf-holder-bulk",                             # needs part=N
}


@mcp.tool()
def fmp_bulk(
    dataset: str,
    symbols: list[str] | None = None,
    params: dict[str, Any] | None = None,
    max_rows: int = 2000,
) -> str:
    """Pull an FMP *bulk* CSV dataset, optionally filtered to specific symbols.

    Bulk files cover the whole market and can be tens of MB, so always pass a
    `symbols` list (e.g. your scorecard universe) to get back only those rows.

    Args:
        dataset: One of: profile, rating, dcf, scores, price_target_summary,
            key_metrics_ttm, ratios_ttm, peers, upgrades_downgrades_consensus,
            etf_holder.
        symbols: Tickers to keep (matched case-insensitively on the symbol
            column). Omit only for small datasets; otherwise the result is
            truncated to max_rows.
        params: Extra query params required by some datasets, e.g.
            {"part": 0} for profile/etf_holder.
        max_rows: Hard cap on returned rows (default 2000) to protect context.

    Returns:
        CSV text (header + matched rows) plus a one-line row-count footer.
    """
    if dataset not in BULK_DATASETS:
        raise FMPError(
            f"Unknown bulk dataset '{dataset}'. Options: "
            f"{', '.join(sorted(BULK_DATASETS))}."
        )
    p = dict(params or {})
    if dataset in ("profile", "etf_holder") and "part" not in p:
        p["part"] = 0 if dataset == "profile" else 1

    r = _get(BULK_DATASETS[dataset], p)
    reader = csv.reader(io.StringIO(r.text))
    rows = list(reader)
    if not rows:
        return "(empty response)"

    header, body = rows[0], rows[1:]
    if symbols:
        wanted = {s.strip().upper() for s in symbols}
        lower = [h.lower() for h in header]
        sym_idx = lower.index("symbol") if "symbol" in lower else 0
        body = [r_ for r_ in body if len(r_) > sym_idx and r_[sym_idx].upper() in wanted]

    matched = len(body)
    truncated = matched > max_rows
    body = body[:max_rows]

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    w.writerows(body)

    footer = f"\n# rows returned: {len(body)} (matched {matched})"
    if truncated:
        footer += f"; TRUNCATED to max_rows={max_rows} — narrow symbols or raise max_rows."
    if not symbols and matched > max_rows:
        footer += " | TIP: pass symbols=[...] to filter this bulk dataset."
    return out.getvalue() + footer


# ---------------------------------------------------------------------------
# Convenience tools (per-symbol) — the scorecard's common pulls
# ---------------------------------------------------------------------------
@mcp.tool()
def search_symbol(query: str, limit: int = 10) -> Any:
    """Search FMP for a ticker by symbol or company name."""
    return _payload(_get("search-symbol", {"query": query, "limit": limit}))


@mcp.tool()
def quote(symbol: str) -> Any:
    """Full quote for a symbol (Nasdaq delayed 15 min)."""
    return _payload(_get("quote", {"symbol": symbol}))


@mcp.tool()
def batch_quote(symbols: list[str]) -> Any:
    """Full quotes for several symbols in one call."""
    return _payload(_get("batch-quote", {"symbols": symbols}))


@mcp.tool()
def company_profile(symbol: str) -> Any:
    """Company profile: sector, industry, market cap, description, CEO, etc."""
    return _payload(_get("profile", {"symbol": symbol}))


@mcp.tool()
def key_metrics_ttm(symbol: str) -> Any:
    """Trailing-twelve-month key metrics for a symbol."""
    return _payload(_get("key-metrics-ttm", {"symbol": symbol}))


@mcp.tool()
def ratios_ttm(symbol: str) -> Any:
    """Trailing-twelve-month financial ratios for a symbol."""
    return _payload(_get("ratios-ttm", {"symbol": symbol}))


@mcp.tool()
def financial_scores(symbol: str) -> Any:
    """Altman Z-Score, Piotroski score and related health metrics."""
    return _payload(_get("financial-scores", {"symbol": symbol}))


@mcp.tool()
def dcf(symbol: str, levered: bool = False) -> Any:
    """Discounted cash flow valuation (set levered=True for the levered DCF)."""
    ep = "levered-discounted-cash-flow" if levered else "discounted-cash-flow"
    return _payload(_get(ep, {"symbol": symbol}))


@mcp.tool()
def price_target_summary(symbol: str) -> Any:
    """Analyst price-target summary across timeframes for a symbol."""
    return _payload(_get("price-target-summary", {"symbol": symbol}))


@mcp.tool()
def analyst_estimates(symbol: str, period: str = "annual", limit: int = 10) -> Any:
    """Analyst financial estimates (revenue, EPS) for a symbol."""
    return _payload(
        _get("analyst-estimates", {"symbol": symbol, "period": period, "limit": limit})
    )


@mcp.tool()
def grades_consensus(symbol: str) -> Any:
    """Analyst grade consensus (strong buy / buy / hold / sell counts)."""
    return _payload(_get("grades-consensus", {"symbol": symbol}))


@mcp.tool()
def insider_trading_search(
    symbol: str | None = None,
    reporting_cik: str | None = None,
    transaction_type: str | None = None,
    page: int = 0,
    limit: int = 100,
) -> Any:
    """Search insider (Form 4) trades by symbol, reporting CIK, or type."""
    return _payload(
        _get(
            "insider-trading/search",
            {
                "symbol": symbol,
                "reportingCik": reporting_cik,
                "transactionType": transaction_type,
                "page": page,
                "limit": limit,
            },
        )
    )


@mcp.tool()
def stock_screener(params: dict[str, Any]) -> Any:
    """Run the FMP company screener.

    Pass FMP screener params, e.g.
    {"marketCapMoreThan": 2000000000, "sector": "Technology",
     "isActivelyTrading": True, "limit": 100}.
    """
    return _payload(_get("company-screener", params))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _resolve_transport() -> str:
    if "--http" in sys.argv:
        return "streamable-http"
    if "--stdio" in sys.argv:
        return "stdio"
    t = os.environ.get("FMP_MCP_TRANSPORT", "stdio").strip().lower()
    if t in ("http", "streamable-http", "streamable_http", "remote"):
        return "streamable-http"
    return "stdio"


def main() -> None:
    mcp.run(transport=_resolve_transport())


if __name__ == "__main__":
    main()
