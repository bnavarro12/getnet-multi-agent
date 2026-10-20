"""RAG ingestion: crawl -> clean -> chunk -> embed -> store.

Run manually with:   python -m app.rag.ingest
It also runs automatically on startup when no index exists (AUTO_INGEST=true).

Design notes
* Seed URLs live in data/sources.txt (official Getnet pages: products, Pix, receivables,
  installments, payment link, help center). From each seed we follow same-domain links
  one level deep, capped by CRAWL_MAX_PAGES, so new product pages are picked up.
* Only public pages are fetched, sequentially, with a delay and a clear User-Agent.
* Boilerplate (nav, footer, scripts, cookie banners) is stripped before chunking.
* Chunks overlap so an answer split across a boundary is still retrievable.
* Each chunk keeps its source URL and page title -> answers can cite their sources.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import Settings, get_settings
from app.rag.store import Chunk, VectorStore

logger = logging.getLogger(__name__)

USER_AGENT = "GetnetSupportAssistant-RAG/1.0 (technical challenge; polite crawler)"
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".mp4", ".css", ".js")
SKIP_PATH_HINTS = ("/wp-admin", "/wp-json", "/feed", "/tag/", "/author/", "/login", "/cart", "?",
                   # legal boilerplate: huge, rarely answers customer questions, and wastes embedding quota
                   "/legal", "privacidade", "privacy", "cookie", "termos", "terms",
                   # news / marketing pages: no product or support facts
                   "/press-room", "/client-stories", "/blog", "/whats-new", "/redirect-")
MAX_PAGE_CHARS = 60000  # safety cap; large enough to keep the full FAQ page (/duvidas)


def is_relevant(url: str) -> bool:
    """getnet.net is a multi-country site: keep only the English pages (the challenge's suggested source).

    Country editions (/pt is Portugal, /es Spain, /ar Argentina, /mx Mexico...) describe other markets'
    products and fees, which would give Brazilian merchants wrong answers.
    """
    parsed = urlparse(url)
    if parsed.netloc.endswith("getnet.net"):
        return parsed.path == "/en" or parsed.path.startswith("/en/")
    return True


def load_seed_urls(settings: Settings) -> list[str]:
    lines = settings.sources_file.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def clean_html(html: str) -> tuple[str, str, list[str]]:
    """Return (title, main_text, links) for a page."""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    links = [a.get("href", "") for a in soup.find_all("a")]

    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form", "header", "footer", "nav"]):
        tag.decompose()
    for el in soup.select('[id*="cookie"], [class*="cookie-"], [role="navigation"], [class*="breadcrumb"]'):
        if el.name not in ("body", "html", "main"):
            el.decompose()

    main = soup.find("main") or soup.body or soup
    text = main.get_text("\n", strip=True)
    # Drop very short UI fragments ("Saiba mais", "Peça já") and duplicate lines.
    seen, lines = set(), []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) < 3 or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return title, "\n".join(lines), links


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Paragraph-aware chunking with character overlap."""
    paragraphs = [p for p in text.split("\n") if p.strip()]
    chunks, current = [], ""
    for p in paragraphs:
        if len(current) + len(p) + 1 <= size:
            current = f"{current}\n{p}" if current else p
            continue
        if current:
            chunks.append(current)
            current = current[-overlap:] + "\n" + p if overlap else p
        else:
            current = p
        while len(current) > size:  # a single giant paragraph
            chunks.append(current[:size])
            current = current[size - overlap:]
    if current.strip():
        chunks.append(current)
    return [c.strip() for c in chunks if len(c.strip()) > 80]


def _same_site(url: str, allowed_hosts: set[str]) -> bool:
    return urlparse(url).netloc in allowed_hosts


def crawl(settings: Settings) -> list[dict]:
    seeds = load_seed_urls(settings)
    allowed_hosts = {urlparse(u).netloc for u in seeds}
    queue: list[tuple[str, int]] = [(u, 0) for u in seeds]
    visited: set[str] = set()
    pages: list[dict] = []

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True) as http:
        while queue and len(pages) < settings.crawl_max_pages:
            url, depth = queue.pop(0)
            url = urldefrag(url)[0].rstrip("/")
            if url in visited:
                continue
            visited.add(url)
            try:
                r = http.get(url)
                if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                    logger.info(json.dumps({"event": "crawl_skip", "url": url, "status": r.status_code}))
                    continue
            except httpx.HTTPError as exc:
                logger.warning(json.dumps({"event": "crawl_error", "url": url, "error": str(exc)[:120]}))
                continue

            title, text, links = clean_html(r.text)
            if len(text) > 200:
                pages.append({"url": url, "title": title, "text": text[:MAX_PAGE_CHARS]})
                logger.info(json.dumps({"event": "crawl_ok", "url": url, "chars": len(text)}))

            if depth < 1:  # follow internal links one level deep
                for href in links:
                    nxt = urldefrag(urljoin(url, href))[0].rstrip("/")
                    if (nxt.startswith("http") and _same_site(nxt, allowed_hosts) and is_relevant(nxt)
                            and nxt not in visited
                            and not nxt.lower().endswith(SKIP_EXT)
                            and not any(h in nxt for h in SKIP_PATH_HINTS)):
                        queue.append((nxt, depth + 1))
            time.sleep(settings.crawl_delay_s)
    return pages


def build_index(settings: Settings, llm) -> VectorStore:
    t0 = time.perf_counter()
    pages = crawl(settings)
    chunks: list[Chunk] = []
    for page in pages:
        for i, piece in enumerate(chunk_text(page["text"], settings.chunk_size, settings.chunk_overlap)):
            cid = hashlib.sha1(f"{page['url']}#{i}".encode()).hexdigest()[:12]
            # Prefixing the title gives each chunk context ("which page is this from?").
            chunks.append(Chunk(id=cid, text=f"{page['title']}\n{piece}", url=page["url"], title=page["title"]))

    # De-duplicate chunks that appear on many pages (shared banners, legal text).
    unique: dict[str, Chunk] = {}
    for c in chunks:
        key = hashlib.sha1(c.text.split("\n", 1)[-1].encode()).hexdigest()
        unique.setdefault(key, c)
    chunks = list(unique.values())

    store = VectorStore(meta={
        "built_at": datetime.now(timezone.utc).isoformat(),
        "pages": len(pages),
        "chunks": len(chunks),
        "embedding_model": settings.embedding_model,
        "sources": sorted({p["url"] for p in pages}),
    })
    if not chunks:
        # Don't persist an empty index: the next start will retry the crawl.
        logger.error(json.dumps({"event": "ingest_empty", "hint": "check network access to the source URLs"}))
        return store
    vectors = llm.embed([c.text for c in chunks])
    store.add(chunks, vectors)
    store.save(settings.index_path)
    logger.info(json.dumps({"event": "ingest_done", "pages": len(pages), "chunks": len(chunks),
                            "seconds": round(time.perf_counter() - t0, 1)}))
    return store


if __name__ == "__main__":
    from app.llm import GeminiClient
    from app.observability import setup_logging

    s = get_settings()
    setup_logging(s.log_level)
    build_index(s, GeminiClient(s))
