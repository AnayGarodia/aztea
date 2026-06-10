"""Unit tests for core/web: /map (sitemap discovery) and /crawl (bounded BFS).

Fetch is mocked everywhere, so these are deterministic and hit no network. They
assert the same-domain filtering, dedup, sitemap-index recursion, robots fallback,
and the hard crawl bounds (limit/depth/path-globs).
"""

from __future__ import annotations

from agents import _site_fetch
from core.web import crawl, sitemap


def _raw(text, ctype="application/xml", final_url="https://example.com/sitemap.xml"):
    return _site_fetch.RawFetch(final_url=final_url, status=200, content_type=ctype, text=text)


def _html(html, final_url="https://example.com/"):
    return _site_fetch.FetchResult(final_url=final_url, status=200, content_type="text/html", html=html)


# --------------------------------------------------------------------------- /map
def test_map_site_collects_sitemap_and_page_links(monkeypatch):
    sitemap_xml = (
        "<urlset><url><loc>https://example.com/a</loc></url>"
        "<url><loc>https://example.com/b</loc></url>"
        "<url><loc>https://other.com/x</loc></url></urlset>"  # cross-domain -> dropped
    )

    def fake_raw(url):
        if url.endswith("robots.txt"):
            return None  # no robots -> falls back to /sitemap.xml
        if url.endswith("sitemap.xml"):
            return _raw(sitemap_xml)
        return None

    monkeypatch.setattr(sitemap._site_fetch, "fetch_raw", fake_raw)
    monkeypatch.setattr(
        sitemap._site_fetch, "fetch_static_html",
        lambda url: _html('<a href="/c">C</a><a href="https://example.com/a">dup</a>'),
    )
    out = sitemap.map_site("https://example.com/")
    assert {"https://example.com/a", "https://example.com/b", "https://example.com/c"} <= set(out["urls"])
    assert all("other.com" not in u for u in out["urls"])      # cross-domain filtered
    assert len(out["urls"]) == len(set(out["urls"])) == out["count"]  # deduped


def test_map_site_follows_sitemap_index(monkeypatch):
    index = "<sitemapindex><sitemap><loc>https://example.com/sm1.xml</loc></sitemap></sitemapindex>"
    child = "<urlset><url><loc>https://example.com/p1</loc></url></urlset>"

    def fake_raw(url):
        if url.endswith("sitemap.xml"):
            return _raw(index)
        if url.endswith("sm1.xml"):
            return _raw(child)
        return None

    monkeypatch.setattr(sitemap._site_fetch, "fetch_raw", fake_raw)
    monkeypatch.setattr(sitemap._site_fetch, "fetch_static_html", lambda url: None)
    out = sitemap.map_site("https://example.com/")
    assert out["urls"] == ["https://example.com/p1"]


def test_map_site_uses_robots_declared_sitemaps(monkeypatch):
    robots = "User-agent: *\nSitemap: https://example.com/custom.xml\n"
    child = "<urlset><url><loc>https://example.com/r1</loc></url></urlset>"

    def fake_raw(url):
        if url.endswith("robots.txt"):
            return _raw(robots, ctype="text/plain", final_url=url)
        if url.endswith("custom.xml"):
            return _raw(child)
        return None

    monkeypatch.setattr(sitemap._site_fetch, "fetch_raw", fake_raw)
    monkeypatch.setattr(sitemap._site_fetch, "fetch_static_html", lambda url: None)
    out = sitemap.map_site("https://example.com/")
    assert "https://example.com/r1" in out["urls"]


# --------------------------------------------------------------------------- /crawl
def _page(url, html):
    return _site_fetch.FetchResult(final_url=url, status=200, content_type="text/html", html=html)


def test_crawl_site_bfs_same_domain(monkeypatch):
    pages = {
        "https://example.com/": '<html><title>Home</title><body><a href="/a">A</a>'
                                '<a href="https://other.com/x">ext</a></body></html>',
        "https://example.com/a": '<html><title>A</title><body><a href="/b">B</a></body></html>',
        "https://example.com/b": "<html><title>B</title><body>leaf content</body></html>",
    }
    monkeypatch.setattr(
        crawl._site_fetch, "fetch_static_html",
        lambda url: _page(url.split("#")[0], pages[url.split("#")[0]]) if url.split("#")[0] in pages else None,
    )
    out = crawl.crawl_site("https://example.com/", limit=10, max_depth=2)
    urls = [p["url"] for p in out["pages"]]
    assert urls[0] == "https://example.com/"
    assert {"https://example.com/a", "https://example.com/b"} <= set(urls)
    assert all("other.com" not in u for u in urls)  # cross-domain never crawled
    assert out["count"] == 3


def test_crawl_site_respects_limit(monkeypatch):
    def fake_html(url):
        norm = url.split("#")[0]
        tag = norm.rstrip("/").split("/")[-1] or "root"
        html = f'<html><title>{tag}</title><body><a href="/{tag}1">x</a><a href="/{tag}2">y</a></body></html>'
        return _page(norm, html)

    monkeypatch.setattr(crawl._site_fetch, "fetch_static_html", fake_html)
    out = crawl.crawl_site("https://example.com/", limit=5, max_depth=9)
    assert out["count"] == 5 and out["limit_reached"] is True


def test_crawl_site_path_exclude_glob(monkeypatch):
    pages = {
        "https://example.com/": '<html><body><a href="/blog/1">b</a><a href="/admin/x">a</a></body></html>',
        "https://example.com/blog/1": "<html><title>blog</title><body>post</body></html>",
        "https://example.com/admin/x": "<html><title>admin</title><body>secret</body></html>",
    }
    monkeypatch.setattr(
        crawl._site_fetch, "fetch_static_html",
        lambda url: _page(url.split("#")[0], pages[url.split("#")[0]]) if url.split("#")[0] in pages else None,
    )
    out = crawl.crawl_site("https://example.com/", exclude=["/admin/*"])
    urls = [p["url"] for p in out["pages"]]
    assert "https://example.com/blog/1" in urls
    assert all("/admin/" not in u for u in urls)  # excluded path never fetched


def test_crawl_site_dedups_trailing_slash_and_converging_redirects(monkeypatch):
    # "https://example.com" and "https://example.com/" are one resource; a page that
    # links both must not burn two fetch slots on identical markdown. Same for two
    # URLs whose fetches redirect to one final page.
    pages = {
        "https://example.com": '<html><title>Home</title><body>'
                               '<a href="https://example.com/">self slash</a>'
                               '<a href="/a">A</a><a href="/a/">A slash</a>'
                               '<a href="/moved">M</a></body></html>',
        "https://example.com/a": "<html><title>A</title><body>leaf</body></html>",
        # /moved redirects (final_url) onto /a, which was already crawled.
        "https://example.com/moved": "<html><title>A</title><body>leaf</body></html>",
    }

    def fetch(url):
        norm = url.split("#")[0].rstrip("/") or url
        if norm not in pages:
            return None
        final = "https://example.com/a" if norm.endswith("/moved") else norm
        return _page(final, pages[norm])

    monkeypatch.setattr(crawl._site_fetch, "fetch_static_html", fetch)
    out = crawl.crawl_site("https://example.com", limit=10, max_depth=2)
    urls = [p["url"] for p in out["pages"]]
    assert len(urls) == len(set(u.rstrip("/") for u in urls))  # no slash-variant dups
    assert sorted(set(urls)) == ["https://example.com", "https://example.com/a"]
    assert out["count"] == 2
