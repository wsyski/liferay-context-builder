from datetime import date
from types import SimpleNamespace

import pytest

from liferay_docs_scraper import blogs, fetcher


def configure_blog_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(blogs, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(blogs, "FILTERED_DIR", tmp_path / "reports" / "filtered")
    monkeypatch.setattr(blogs, "REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr(blogs, "RETRY_DELAY_SECONDS", 0)


def listing_item(slug, when, categories=(), tags=(), author="Jane Doe"):
    cats = "".join(f'<a class="asset-category">{c}</a>' for c in categories)
    tag_links = "".join(f"<a>{t}</a>" for t in tags)
    return f"""
    <li class="list-group-item list-group-item-flex">
      <h2 class="title"><a href="https://liferay.dev/b/{slug}">Title {slug}</a></h2>
      <div class="search-results-metadata">
        <p class="list-group-subtext"><span><small><strong>{author}</strong></small></span>
          <span><span> | </span> {when} 4:04 PM</span></p>
        <span class="taglib-asset-categories-summary">{cats}</span>
        <span class="taglib-asset-tags-summary">{tag_links}</span>
      </div>
    </li>"""


def listing(*items):
    return "<html><body><ul>" + "".join(items) + "</ul></body></html>"


def post_html(body="<p>" + "word " * 40 + "</p>"):
    return f"""<html><body>
    <div class="component-paragraph" data-lfr-editable-type="rich-text"><p>10 Minute Read</p></div>
    <div class="component-paragraph" data-lfr-editable-type="rich-text">{body}</div>
    </body></html>"""


def entry(slug="a", published=date(2024, 1, 5), categories=()):
    return blogs.ListingEntry(f"https://liferay.dev/b/{slug}", f"Title {slug}", "Jane Doe",
                              published, list(categories), [])


def test_parse_listing_extracts_entry_fields():
    html = listing(listing_item("fix-osgi", "Jan 13, 2023", ["Featured", "AI"], ["osgi", "gogo"]))

    [parsed] = blogs.parse_listing(html)

    assert parsed.url == "https://liferay.dev/b/fix-osgi"
    assert parsed.title == "Title fix-osgi"
    assert parsed.author == "Jane Doe"
    assert parsed.published == date(2023, 1, 13)
    assert parsed.categories == ["Featured", "AI"]
    assert parsed.tags == ["osgi", "gogo"]


def test_parse_listing_ignores_items_that_are_not_posts():
    html = listing(
        '<li class="list-group-item-flex"><h2 class="title"><a href="https://example.com/b/x">x</a></h2></li>',
        listing_item("real", "Jan 13, 2023"),
    )

    assert [e.url for e in blogs.parse_listing(html)] == ["https://liferay.dev/b/real"]


def test_extract_post_takes_longest_paragraph_as_body():
    markdown = blogs.extract_post(post_html("<p>Hello <strong>world</strong> " + "x " * 30 + "</p>"))

    assert "Hello **world**" in markdown
    assert "Minute Read" not in markdown


def test_extract_post_returns_none_without_body():
    assert blogs.extract_post("<html><body><p>nothing</p></body></html>") is None


def test_discover_entries_stops_at_first_page_older_than_cutoff():
    pages = {
        1: listing(listing_item("new", "Mar 1, 2024")),
        2: listing(listing_item("mixed", "Jan 5, 2022"), listing_item("old", "Dec 20, 2021")),
        3: listing(listing_item("older", "Nov 1, 2021")),
        4: listing(listing_item("oldest", "Oct 1, 2021")),
    }
    requested = []

    def fetch(url):
        page = int(url.rsplit("start=", 1)[1])
        requested.append(page)
        return pages[page]

    stats = blogs.RunStats()
    found = blogs.discover_entries(fetch, date(2022, 1, 1), stats)

    assert [e.url.rsplit("/", 1)[1] for e in found] == ["new", "mixed"]
    assert requested == [1, 2, 3]
    assert stats.crawl_errors == []


def test_discover_entries_keeps_partial_results_when_a_page_fails():
    def fetch(url):
        if url.endswith("start=2"):
            raise blogs.FetchError("403")
        return listing(listing_item("first", "Mar 1, 2024"))

    stats = blogs.RunStats()
    found = blogs.discover_entries(fetch, date(2022, 1, 1), stats)

    assert len(found) == 1
    assert stats.crawl_errors == ["listing page 2: 403"]


def test_discover_entries_reports_empty_page_as_error():
    stats = blogs.RunStats()

    found = blogs.discover_entries(lambda url: listing(), date(2022, 1, 1), stats)

    assert found == []
    assert "no entries" in stats.crawl_errors[0]


def test_discover_entries_honors_limit_without_paging_further():
    calls = []

    def fetch(url):
        calls.append(url)
        return listing(listing_item("a", "Mar 1, 2024"), listing_item("b", "Mar 2, 2024"))

    found = blogs.discover_entries(fetch, date(2022, 1, 1), blogs.RunStats(), limit=1)

    assert len(found) == 1
    assert len(calls) == 1


def test_fetch_page_retries_once_then_raises(monkeypatch):
    monkeypatch.setattr(blogs, "REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr(blogs, "RETRY_DELAY_SECONDS", 0)
    calls = []

    def fake_scrape(url, options):
        calls.append(url)
        return fetcher.Page(url, False, None, None)

    monkeypatch.setattr(blogs.fetcher, "scrape", fake_scrape)

    with pytest.raises(blogs.FetchError, match="scrape failed twice"):
        blogs.fetch_page("https://liferay.dev/b/a")
    assert len(calls) == 2


def test_fetch_page_returns_html_after_transient_miss(monkeypatch):
    monkeypatch.setattr(blogs, "REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr(blogs, "RETRY_DELAY_SECONDS", 0)
    results = iter([fetcher.Page("u", False, None, None), fetcher.Page("u", True, None, "<html>ok</html>")])
    monkeypatch.setattr(blogs.fetcher, "scrape", lambda url, options: next(results))

    assert blogs.fetch_page("https://liferay.dev/b/a") == "<html>ok</html>"


def fake_batch(html_by_slug=None, calls=None):
    """batch_scrape stand-in: success with post_html() unless html_by_slug maps the slug to None (miss)."""
    html_by_slug = html_by_slug or {}

    def batch(urls, options):
        if calls is not None:
            calls.append(list(urls))
        for url in urls:
            slug = url.rsplit("/", 1)[1]
            html = html_by_slug.get(slug, post_html())
            yield fetcher.Page(url, html is not None, None, html)

    return batch


def stub_discovery(monkeypatch, entries):
    monkeypatch.setattr(blogs, "discover_entries", lambda fetch, since, stats, limit=None: entries)


def test_run_skips_news_and_existing_and_writes_post(monkeypatch, tmp_path):
    configure_blog_dirs(monkeypatch, tmp_path)
    existing = entry("have")
    blogs.post_path(existing).parent.mkdir(parents=True)
    blogs.post_path(existing).write_text("---\n---\nold", encoding="utf-8")
    entries = [entry("news", categories=["News"]), existing, entry("fresh", categories=["Featured"])]
    stub_discovery(monkeypatch, entries)
    calls = []

    stats = blogs.run(None, fake_batch(calls=calls), date(2022, 1, 1), include_news=False, refresh=False)

    assert calls == [["https://liferay.dev/b/fresh"]]
    assert (stats.skipped_news, stats.skipped_existing) == (1, 1)
    assert [status for _, status in stats.outcomes] == ["new"]
    text = blogs.post_path(entries[2]).read_text(encoding="utf-8")
    assert "source_type: community-blog" in text
    assert 'published_at: "2024-01-05"' in text
    assert 'categories: "Featured"' in text
    assert "# Title fresh" in text


def test_run_makes_no_batch_call_when_nothing_to_fetch(monkeypatch, tmp_path):
    configure_blog_dirs(monkeypatch, tmp_path)
    stub_discovery(monkeypatch, [entry("news", categories=["News"])])
    calls = []

    stats = blogs.run(None, fake_batch(calls=calls), date(2022, 1, 1), include_news=False, refresh=False)

    assert calls == []
    assert stats.fetch_failed == []


def test_run_refresh_refetches_existing_post(monkeypatch, tmp_path):
    configure_blog_dirs(monkeypatch, tmp_path)
    stub_discovery(monkeypatch, [entry("have")])
    batch = fake_batch()

    blogs.run(None, batch, date(2022, 1, 1), include_news=False, refresh=False)
    stats = blogs.run(None, batch, date(2022, 1, 1), include_news=False, refresh=True)

    assert [status for _, status in stats.outcomes] == ["unchanged"]


def test_run_retries_a_missed_post_once(monkeypatch, tmp_path):
    configure_blog_dirs(monkeypatch, tmp_path)
    stub_discovery(monkeypatch, [entry("flaky"), entry("good")])
    calls = []
    results = {"flaky": iter([None, post_html()])}

    def batch(urls, options):
        calls.append(list(urls))
        for url in urls:
            slug = url.rsplit("/", 1)[1]
            html = next(results[slug]) if slug in results else post_html()
            yield fetcher.Page(url, html is not None, None, html)

    stats = blogs.run(None, batch, date(2022, 1, 1), include_news=False, refresh=False)

    assert calls == [["https://liferay.dev/b/flaky", "https://liferay.dev/b/good"], ["https://liferay.dev/b/flaky"]]
    assert stats.fetch_failed == []
    assert len(stats.outcomes) == 2


def test_run_reports_post_that_fails_twice_and_keeps_others(monkeypatch, tmp_path):
    configure_blog_dirs(monkeypatch, tmp_path)
    stub_discovery(monkeypatch, [entry("bad"), entry("good")])

    stats = blogs.run(None, fake_batch({"bad": None}), date(2022, 1, 1), include_news=False, refresh=False)

    assert stats.fetch_failed == ["https://liferay.dev/b/bad"]
    assert len(stats.outcomes) == 1


def test_run_treats_unparseable_post_as_failed(monkeypatch, tmp_path):
    configure_blog_dirs(monkeypatch, tmp_path)
    stub_discovery(monkeypatch, [entry("empty")])

    stats = blogs.run(None, fake_batch({"empty": "<html><body>no article</body></html>"}), date(2022, 1, 1),
                      include_news=False, refresh=False)

    assert stats.fetch_failed == ["https://liferay.dev/b/empty"]


def test_run_records_batch_crash_and_marks_unreturned_posts_failed(monkeypatch, tmp_path):
    configure_blog_dirs(monkeypatch, tmp_path)
    stub_discovery(monkeypatch, [entry("a"), entry("b")])

    def batch(urls, options):
        yield fetcher.Page(urls[0], True, None, post_html())
        raise RuntimeError("batch job b1 ended failed: boom")

    stats = blogs.run(None, batch, date(2022, 1, 1), include_news=False, refresh=False)

    assert stats.crawl_errors == ["post batch: batch job b1 ended failed: boom"]
    assert stats.fetch_failed == ["https://liferay.dev/b/b"]
    assert len(stats.outcomes) == 1


def test_run_propagates_firecrawl_unavailable(monkeypatch, tmp_path):
    configure_blog_dirs(monkeypatch, tmp_path)
    stub_discovery(monkeypatch, [entry("a")])

    def batch(urls, options):
        raise fetcher.FirecrawlUnavailable("down")
        yield  # pragma: no cover

    with pytest.raises(fetcher.FirecrawlUnavailable):
        blogs.run(None, batch, date(2022, 1, 1), include_news=False, refresh=False)


def test_main_exits_nonzero_when_run_reports_failure(monkeypatch, tmp_path):
    configure_blog_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(blogs, "run", lambda *a, **k: SimpleNamespace(
        discovered_total=0, skipped_news=0, skipped_existing=0, outcomes=[],
        fetch_failed=["u"], crawl_errors=[]))
    monkeypatch.setattr(blogs, "write_report", lambda stats: None)
    monkeypatch.setattr("sys.argv", ["liferay-context-builder-blogs"])

    with pytest.raises(SystemExit) as exc_info:
        blogs.main()

    assert exc_info.value.code == 1


def test_main_rejects_non_positive_limit_and_bad_date(monkeypatch):
    for argv in (["--limit", "0"], ["--since", "2022/01/01"]):
        monkeypatch.setattr("sys.argv", ["liferay-context-builder-blogs", *argv])
        with pytest.raises(SystemExit) as exc_info:
            blogs.main()
        assert exc_info.value.code == 2
