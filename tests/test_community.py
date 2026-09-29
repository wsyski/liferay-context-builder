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


def test_discover_article_urls_raises_when_listing_fetch_fails_twice(monkeypatch):
    monkeypatch.setattr(community.time, "sleep", lambda s: None)
    calls = []

    class FakeClient:
        def scrape(self, url, options):
            calls.append(url)
            return SimpleNamespace(success=False)

    with pytest.raises(RuntimeError, match="search listing page .* failed"):
        community.discover_article_urls(FakeClient(), "33317328")
    assert len(calls) == 2


def test_discover_article_urls_retries_a_transient_listing_miss(monkeypatch):
    monkeypatch.setattr(community.time, "sleep", lambda s: None)
    html = '<a href="https://learn.liferay.com/kb-article/a"></a>'
    results = iter([SimpleNamespace(success=False), SimpleNamespace(success=True, html=html)])

    class FakeClient:
        def scrape(self, url, options):
            return next(results, SimpleNamespace(success=True, html=html))

    assert community.discover_article_urls(FakeClient(), "33317328") == ["https://learn.liferay.com/kb-article/a"]


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
    def fake_run_all(resource_type_filter, limit, refresh):
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


def test_map_capability_matches_known_names_that_contain_commas():
    long_name = "DXP Self-Hosted Installation, Maintenance, and Administration"

    assert community.map_capability(long_name) == "self-hosted"
    assert community.map_capability(f"Platform, {long_name}") == "self-hosted"
    assert community.map_capability("Search, Sites") == "search"  # first value in the text wins
    assert community.map_capability("Sites, Search") == "sites"
    assert community.map_capability("Platform") is None
    assert community.map_capability("Searching") is None  # whole values only, not substrings
    assert community.map_capability(None) is None


def article_file(tmp_path, bucket, slug, tag_raw=None, capability="uncategorized"):
    path = tmp_path / "raw" / "community-howto" / bucket / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = f'capability_tag_raw: "{tag_raw}"\n' if tag_raw else ""
    path.write_text(f'---\nurl: "u"\nsource_type: community-howto\ncapability: {capability}\n{raw}---\n# T\n\nbody\n',
                    encoding="utf-8")
    return path


def test_run_resource_type_skips_articles_on_disk_and_refetches_only_missing(monkeypatch, tmp_path):
    configure_community_dirs(monkeypatch, tmp_path)
    have = "https://learn.liferay.com/kb-article/have"
    need = "https://learn.liferay.com/kb-article/need"
    article_file(tmp_path, "_uncategorized", "have")
    monkeypatch.setattr(community, "discover_article_urls", lambda client, rt, limit=None: [have, need])
    monkeypatch.setattr(community, "extract_article", lambda html, u: {"title": "N", "body": "text", "tags": {}})
    requested = []

    class FakeClient:
        def batch_scrape(self, urls, options):
            requested.append(list(urls))
            for url in urls:
                yield SimpleNamespace(url=url, success=True, html="x")

    stats = community.run_resource_type(FakeClient(), "howto", "1", "community-howto")

    assert requested == [[need]]
    assert stats.skipped_existing == 1 and len(stats.outcomes) == 1


def test_run_resource_type_refresh_refetches_articles_on_disk(monkeypatch, tmp_path):
    configure_community_dirs(monkeypatch, tmp_path)
    have = "https://learn.liferay.com/kb-article/have"
    article_file(tmp_path, "_uncategorized", "have")
    monkeypatch.setattr(community, "discover_article_urls", lambda client, rt, limit=None: [have])
    monkeypatch.setattr(community, "extract_article", lambda html, u: {"title": "N", "body": "text", "tags": {}})
    requested = []

    class FakeClient:
        def batch_scrape(self, urls, options):
            requested.append(list(urls))
            for url in urls:
                yield SimpleNamespace(url=url, success=True, html="x")

    stats = community.run_resource_type(FakeClient(), "howto", "1", "community-howto", refresh=True)

    assert requested == [[have]] and stats.skipped_existing == 0


def test_skipped_article_is_moved_to_the_folder_its_stored_tag_now_maps_to(monkeypatch, tmp_path):
    configure_community_dirs(monkeypatch, tmp_path)
    url = "https://learn.liferay.com/kb-article/moved"
    old = article_file(tmp_path, "_uncategorized", "moved",
                       tag_raw="DXP Self-Hosted Installation, Maintenance, and Administration")
    monkeypatch.setattr(community, "discover_article_urls", lambda client, rt, limit=None: [url])

    class FakeClient:
        def batch_scrape(self, urls, options):
            raise AssertionError("nothing should be fetched")

    community.run_resource_type(FakeClient(), "howto", "1", "community-howto")

    new = tmp_path / "raw" / "community-howto" / "self-hosted" / "moved.md"
    assert not old.exists() and new.exists()
    assert "capability: self-hosted\n" in new.read_text(encoding="utf-8")


def test_run_resource_type_retries_in_rounds_until_recovered(monkeypatch, tmp_path):
    configure_community_dirs(monkeypatch, tmp_path)
    urls = [f"https://learn.liferay.com/kb-article/a{i}" for i in range(3)]
    monkeypatch.setattr(community, "discover_article_urls", lambda client, rt, limit=None: urls)
    monkeypatch.setattr(community, "extract_article", lambda html, u: {"title": "N", "body": "text", "tags": {}})
    ok_after = {"a0": 0, "a1": 1, "a2": 2}  # succeeds once it has failed this many times
    seen = {}
    rounds = []

    class FakeClient:
        def batch_scrape(self, batch, options):
            rounds.append(len(batch))
            for url in batch:
                slug = url.rsplit("/", 1)[1]
                seen[slug] = seen.get(slug, 0) + 1
                yield SimpleNamespace(url=url, success=seen[slug] > ok_after[slug], html="x")

    stats = community.run_resource_type(FakeClient(), "howto", "1", "community-howto")

    assert rounds == [3, 2, 1] and stats.fetch_failed == [] and len(stats.outcomes) == 3


def test_run_resource_type_stops_retrying_when_a_round_recovers_nothing(monkeypatch, tmp_path):
    configure_community_dirs(monkeypatch, tmp_path)
    urls = ["https://learn.liferay.com/kb-article/x", "https://learn.liferay.com/kb-article/y"]
    monkeypatch.setattr(community, "discover_article_urls", lambda client, rt, limit=None: urls)
    rounds = []

    class FakeClient:
        def batch_scrape(self, batch, options):
            rounds.append(len(batch))
            for url in batch:
                yield SimpleNamespace(url=url, success=False, html=None)

    stats = community.run_resource_type(FakeClient(), "howto", "1", "community-howto")

    assert rounds == [2, 2]  # first pass, one retry that recovered nothing, then stop
    assert stats.fetch_failed == urls
