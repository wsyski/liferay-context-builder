from types import SimpleNamespace

import pytest

from liferay_docs_scraper import community


def configure_community_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(community, "ROOT", tmp_path)
    monkeypatch.setattr(community, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(community, "FILTERED_DIR", tmp_path / "reports" / "filtered")


def test_safe_slug_decodes_and_caps_long_percent_encoded_url():
    url = "https://learn.liferay.com/kb-article/" + ("%C3%A1" * 200)

    slug = community.safe_slug(url)

    assert len(slug.encode("utf-8")) <= 150
    assert "/" not in slug


def test_build_frontmatter_quotes_tag_values():
    frontmatter = community.build_frontmatter(
        'https://learn.liferay.com/kb-article/page?x="quoted"',
        "community-howto",
        "search",
        {"Capability": 'Search "Advanced"', "Feature": "Indexing: Blueprints"},
        "body",
    )

    assert 'url: "https://learn.liferay.com/kb-article/page?x=\\"quoted\\""' in frontmatter
    assert 'capability_tag_raw: "Search \\"Advanced\\""' in frontmatter
    assert 'feature: "Indexing: Blueprints"' in frontmatter


def test_extract_article_links_keeps_absolute_kb_article_urls_only():
    html = """
    <a href="https://learn.liferay.com/kb-article/how-to-fix-search"></a>
    <a href="/kb-article/relative"></a>
    <a href="https://example.com/kb-article/nope"></a>
    """

    assert community.extract_article_links(html) == {
        "https://learn.liferay.com/kb-article/how-to-fix-search"
    }


def test_discover_article_urls_raises_when_listing_fetch_fails():
    class FakeClient:
        def scrape(self, url, options):
            return SimpleNamespace(success=False)

    with pytest.raises(RuntimeError, match="search listing page .* failed"):
        community.discover_article_urls(FakeClient(), "33317328")


def test_discover_article_urls_stops_when_no_new_links():
    html = """
    <html><body>
      <a href="https://learn.liferay.com/kb-article/how-to-fix-search"></a>
      <a href="https://learn.liferay.com/kb-article/how-to-fix-search"></a>
    </body></html>
    """

    class FakeClient:
        def scrape(self, url, options):
            return SimpleNamespace(success=True, html=html)

    assert community.discover_article_urls(FakeClient(), "33317328") == [
        "https://learn.liferay.com/kb-article/how-to-fix-search"
    ]


def test_discover_article_urls_honors_limit_without_paging_further():
    html = """
    <html><body>
      <a href="https://learn.liferay.com/kb-article/a"></a>
      <a href="https://learn.liferay.com/kb-article/b"></a>
    </body></html>
    """

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def scrape(self, url, options):
            self.calls += 1
            return SimpleNamespace(success=True, html=html)

    client = FakeClient()
    assert community.discover_article_urls(client, "33317328", limit=1) == [
        "https://learn.liferay.com/kb-article/a"
    ]
    assert client.calls == 1


def test_run_resource_type_records_stream_crash(monkeypatch, tmp_path):
    configure_community_dirs(monkeypatch, tmp_path)

    class FakeClient:
        def batch_scrape(self, urls, options):
            raise RuntimeError("batch job b1 ended failed: boom")

    monkeypatch.setattr(community, "discover_article_urls",
                        lambda client, resource_type_id, limit=None: ["https://learn.liferay.com/kb-article/a"])

    stats = community.run_resource_type(FakeClient(), "howto", "33317328", "community-howto")

    assert stats.crawl_errors == ["batch job b1 ended failed: boom"]
    assert stats.discovered_total == 1


def test_run_resource_type_retries_transient_miss_once(monkeypatch, tmp_path):
    configure_community_dirs(monkeypatch, tmp_path)
    url = "https://learn.liferay.com/kb-article/a"
    monkeypatch.setattr(community, "discover_article_urls", lambda client, rt, limit=None: [url])
    monkeypatch.setattr(community, "extract_article",
                        lambda html, u: None if html == "bad" else {"title": "A", "body": "text", "tags": {}})

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def batch_scrape(self, urls, options):
            self.calls += 1
            yield SimpleNamespace(url=url, success=True, html="bad" if self.calls == 1 else "good")

    client = FakeClient()
    stats = community.run_resource_type(client, "howto", "33317328", "community-howto")

    assert client.calls == 2
    assert stats.fetch_failed == []
    assert len(stats.outcomes) == 1


def test_extract_article_converts_body_to_markdown():
    html = """<html><body><h1 class="knowledge-article-title">T</h1>
    <div class="knowledge-article-content"><p>Hello <strong>world</strong></p></div></body></html>"""
    parsed = community.extract_article(html, "https://learn.liferay.com/kb-article/t")
    assert parsed is not None
    assert "Hello **world**" in parsed["body"]


def test_main_exits_nonzero_when_run_all_reports_failure(monkeypatch):
    def fake_run_all(resource_type_filter, limit):
        return True

    monkeypatch.setattr(community, "run_all", fake_run_all)
    monkeypatch.setattr("sys.argv", ["liferay-context-builder-community"])

    with pytest.raises(SystemExit) as exc_info:
        community.main()

    assert exc_info.value.code == 1


def test_main_rejects_non_positive_limit(monkeypatch):
    monkeypatch.setattr("sys.argv", ["liferay-context-builder-community", "--limit", "0"])

    with pytest.raises(SystemExit) as exc_info:
        community.main()

    assert exc_info.value.code == 2
