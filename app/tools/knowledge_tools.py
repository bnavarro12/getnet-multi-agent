"""Tools used by the Knowledge Agent (and shared with the Support Agent)."""

from __future__ import annotations

import logging

from app.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)


def _search_knowledge_base(ctx: ToolContext, query: str) -> dict:
    hits = ctx.services.retriever.search(query)
    for h in hits:
        if h["url"] not in {s["url"] for s in ctx.sources}:
            ctx.sources.append({"title": h["title"], "url": h["url"], "type": "knowledge_base"})
    if not hits:
        return {"results": [], "note": "No relevant content found in the Getnet knowledge base."}
    return {"results": hits}


def _web_search(ctx: ToolContext, query: str) -> dict:
    try:
        results = ctx.services.web_search(query)
    except Exception as exc:  # network issues, rate limits
        logger.warning("web_search failed: %s", exc)
        return {"results": [], "error": "Web search is temporarily unavailable."}
    for r in results[:3]:
        ctx.sources.append({"title": r.get("title", ""), "url": r.get("url", ""), "type": "web"})
    return {"results": results}


SEARCH_KNOWLEDGE_BASE = Tool(
    name="search_knowledge_base",
    description=(
        "Semantic search over Getnet's official website content (card machines such as Get Clássica, "
        "Get Smart, Get Mini, Get Tap; Pix; receivables advance/antecipação; crediário; payment link; "
        "digital account; fees; help center). Use for ANY question about Getnet products, services, "
        "rules or how-to. Write the query in Portuguese for best results, since the site is in Portuguese."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query, e.g. 'diferença Get Clássica Get Smart'"}},
        "required": ["query"],
    },
    fn=_search_knowledge_base,
)

WEB_SEARCH = Tool(
    name="web_search",
    description=(
        "Search the public web for general, real-time information that is NOT about Getnet products "
        "(weather, exchange rates, news, general facts). Also use it as a fallback when the knowledge "
        "base has no answer about Getnet, adding 'Getnet' to the query."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Web search query"}},
        "required": ["query"],
    },
    fn=_web_search,
)


def ddg_web_search(query: str, max_results: int = 5) -> list[dict]:
    """Default web search backend: DuckDuckGo via the `ddgs` package (free, no API key)."""
    from ddgs import DDGS

    with DDGS() as ddgs:
        rows = ddgs.text(query, max_results=max_results, region="br-pt")
    return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in rows]
