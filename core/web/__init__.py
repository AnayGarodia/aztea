"""Firecrawl-parity web primitives built on the Phase-A SSRF-safe fetch/extract.

  fetch_backend — direct vs proxy vs remote-browser selection (env-driven, Phase C)
  sitemap       — /map: robots.txt + sitemap.xml + page-link URL discovery (no scrape)
  crawl         — /crawl: bounded BFS that fetches each page to markdown, same-domain only

Only ``fetch_backend`` is re-exported here. ``sitemap`` / ``crawl`` are imported
directly (``from core.web import sitemap``) on purpose: they depend on
``agents._site_fetch``, which depends back on ``core.web.fetch_backend``, so eagerly
importing them from this package __init__ would form an import cycle. fetch_backend
has no such dependency, so re-exporting it is safe.
"""

from __future__ import annotations

from core.web import fetch_backend

__all__ = ["fetch_backend"]
