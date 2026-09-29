import json

from liferay_docs_scraper import index


def test_build_search_index_includes_official_and_community_docs(tmp_path):
    raw_dir = tmp_path / "raw"
    reports_dir = tmp_path / "reports" / "filtered"
    official = raw_dir / "search" / "synonym-sets.md"
    community = raw_dir / "community-howto" / "_uncategorized" / "fix.md"
    official.parent.mkdir(parents=True)
    community.parent.mkdir(parents=True)
    official.write_text(
        '---\nurl: "https://learn.liferay.com/w/dxp/search/synonym-sets"\n'
        'capability: search\nfetched_at: "2026-07-09T10:00:00Z"\n---\n'
        "# Synonym Sets\n\n## Configure synonyms\n",
        encoding="utf-8",
    )
    community.write_text(
        '---\nurl: "https://learn.liferay.com/kb-article/fix"\n'
        'source_type: community-howto\ncapability: uncategorized\n---\n'
        "# Fix Search\n\nBody\n",
        encoding="utf-8",
    )

    count = index.build_search_index(raw_dir, reports_dir)

    assert count == 2
    entries = [
        json.loads(line)
        for line in (reports_dir / "search_index.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert entries[0]["source_type"] == "official"
    assert entries[0]["title"] == "Synonym Sets"
    assert entries[0]["path"] == "raw/search/synonym-sets.md"
    assert entries[1]["source_type"] == "community-howto"


def test_build_search_index_includes_community_blog_posts(tmp_path):
    raw_dir = tmp_path / "raw"
    post = raw_dir / "community-blog" / "_uncategorized" / "osgi-tips.md"
    post.parent.mkdir(parents=True)
    post.write_text(
        '---\nurl: "https://liferay.dev/b/osgi-tips"\nsource_type: community-blog\n'
        'capability: uncategorized\npublished_at: "2024-03-12"\n---\n# OSGi Tips\n\nBody\n',
        encoding="utf-8",
    )

    assert index.build_search_index(raw_dir, tmp_path / "reports") == 1
    [line] = (tmp_path / "reports" / "search_index.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["source_type"] == "community-blog"


def test_build_search_index_carries_filter_fields_and_summary(tmp_path):
    raw_dir = tmp_path / "raw"
    post = raw_dir / "community-blog" / "_uncategorized" / "osgi-tips.md"
    post.parent.mkdir(parents=True)
    post.write_text(
        '---\nurl: "https://liferay.dev/b/osgi-tips"\nsource_type: community-blog\n'
        'capability: uncategorized\nauthor: "Jane"\npublished_at: "2024-03-12"\ntags: "osgi, gogo"\n'
        'categories: ""\n---\n# OSGi Tips\n\n'
        "Deploying modules in the right order is harder than it looks, so here is a checklist.\n\nMore.\n",
        encoding="utf-8",
    )

    index.build_search_index(raw_dir, tmp_path / "reports")

    [line] = (tmp_path / "reports" / "search_index.jsonl").read_text(encoding="utf-8").splitlines()
    entry = json.loads(line)
    assert entry["published_at"] == "2024-03-12"
    assert entry["tags"] == "osgi, gogo"
    assert entry["author"] == "Jane"
    assert "categories" not in entry
    assert entry["summary"].startswith("Deploying modules in the right order")


def test_build_search_index_orders_sources_by_authority(tmp_path):
    raw_dir = tmp_path / "raw"
    for source_type, name in (("community-blog", "b"), ("community-troubleshooting", "t"), ("community-howto", "h")):
        page = raw_dir / source_type / "_uncategorized" / f"{name}.md"
        page.parent.mkdir(parents=True)
        page.write_text(f"---\nsource_type: {source_type}\ncapability: uncategorized\n---\n# {name}\n", encoding="utf-8")
    official = raw_dir / "search" / "o.md"
    official.parent.mkdir(parents=True)
    official.write_text("---\ncapability: search\n---\n# o\n", encoding="utf-8")

    index.build_search_index(raw_dir, tmp_path / "reports")

    lines = (tmp_path / "reports" / "search_index.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["source_type"] for line in lines] == [
        "official", "community-howto", "community-troubleshooting", "community-blog",
    ]


def test_summarize_skips_headings_links_and_short_lines():
    body = "# T\n\n[T](https://x)\n=====\n\nshort\n\n- a bullet that is long enough to pass the length check here\n\n" \
           "The first real sentence of prose that describes the page in enough words.\n"

    assert index.summarize(body) == "The first real sentence of prose that describes the page in enough words."
    assert len(index.summarize("x" * 500)) == index.SUMMARY_CHARS + 1


def test_extract_headings_and_title_drop_markdown_link_syntax():
    body = "## [Discovering the Authorization Server](https://learn.liferay.com/w/dxp/x#y)\n\ntext\n"

    assert index.extract_headings(body) == ["Discovering the Authorization Server"]
    assert index.title_from_body(body, "slug") == "Discovering the Authorization Server"


def test_summarize_replaces_markdown_links_with_their_text():
    body = "If you have been enjoying the [last years of](https://liferay.dev/twentyfour/2021) talks, join us."

    assert index.summarize(body) == "If you have been enjoying the last years of talks, join us."


def test_detect_anomalies_reports_short_error_and_size_change(tmp_path):
    previous = index.ContentSnapshot(body_chars=1000, body_words=100)

    anomalies = index.detect_anomalies(
        path=tmp_path / "raw" / "search" / "page.md",
        url="https://learn.liferay.com/w/dxp/search/page",
        source_type="official",
        capability="search",
        body="An unexpected error occurred.",
        previous=previous,
    )

    kinds = {item["kind"] for item in anomalies}
    assert {"short_body", "error_marker", "missing_title", "body_shrank"} <= kinds
