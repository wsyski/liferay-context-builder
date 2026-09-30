import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "liferay-expert" / "scripts" / "docs.py"
spec = importlib.util.spec_from_file_location("liferay_expert_docs", SCRIPT)
docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docs)


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry(title, source="official", capability="search", path="raw/search/a.md", **extra):
    base = {"title": title, "url": "https://x/" + title, "source_type": source, "capability": capability,
            "path": path, "headings": [], "fetched_at": iso(1), "summary": ""}
    return {**base, **extra}


@pytest.fixture
def library(tmp_path):
    def build(entries, pages=None):
        index = tmp_path / "reports" / "filtered" / "search_index.jsonl"
        index.parent.mkdir(parents=True)
        index.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        for rel, text in (pages or {}).items():
            page = tmp_path / rel
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(text, encoding="utf-8")
        return tmp_path
    return build


def run(capsys, root, *argv):
    docs.main(["--docs-dir", str(root), *argv])
    return capsys.readouterr().out


def test_search_requires_every_term_and_prefers_title_matches(capsys, library):
    root = library([
        entry("Synonym Sets", path="raw/search/synonyms.md", summary="Configure synonyms."),
        entry("Search Tuning", path="raw/search/tuning.md", headings=["Synonym sets"], summary="Tune search."),
        entry("Other", path="raw/search/other.md", summary="synonym only"),
    ])

    out = run(capsys, root, "search", "synonym", "sets")

    assert out.index("raw/search/synonyms.md") < out.index("raw/search/tuning.md")
    assert "raw/search/other.md" not in out


def test_search_ranks_by_score_then_authority_then_newest(capsys, library):
    root = library([
        entry("Osgi Tips", source="community-blog", capability="uncategorized", path="raw/b/old.md",
              published_at="2023-01-01"),
        entry("Osgi Tips", source="community-blog", capability="uncategorized", path="raw/b/new.md",
              published_at="2025-01-01"),
        entry("Osgi Tips", path="raw/o/official.md"),
    ])

    out = run(capsys, root, "search", "osgi")

    assert out.index("raw/o/official.md") < out.index("raw/b/new.md") < out.index("raw/b/old.md")


def test_search_filters_source_capability_and_since(capsys, library):
    root = library([
        entry("Osgi", source="community-blog", capability="uncategorized", path="raw/b/old.md",
              published_at="2022-03-01"),
        entry("Osgi", source="community-blog", capability="uncategorized", path="raw/b/new.md",
              published_at="2025-03-01"),
        entry("Osgi", capability="development", path="raw/d/official.md"),
    ])

    assert "raw/b/old.md" not in run(capsys, root, "search", "osgi", "--source", "blog", "--since", "2024")
    assert "raw/d/official.md" in run(capsys, root, "search", "osgi", "--since", "2024")  # undated is kept
    only_dev = run(capsys, root, "search", "osgi", "--capability", "development")
    assert "raw/d/official.md" in only_dev and "raw/b/new.md" not in only_dev


def test_search_caps_output_and_says_how_many_more(capsys, library):
    root = library([entry(f"Page {i}", path=f"raw/p/{i}.md") for i in range(5)])

    out = run(capsys, root, "search", "page", "--limit", "2")

    assert out.count("raw/p/") == 2
    assert "2 of 5 matches shown" in out


def test_search_reports_no_hits_without_failing(capsys, library):
    assert "No index hits for: nothing" in run(capsys, library([entry("A")]), "search", "nothing")


def test_missing_index_exits_2_with_build_hint(capsys, tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        docs.main(["--docs-dir", str(tmp_path), "status"])

    assert exc_info.value.code == 2
    assert "liferay-context-builder" in capsys.readouterr().err


def test_status_counts_sources_and_flags_stale_official_docs(capsys, library):
    root = library([
        entry("Old", fetched_at=iso(30)),
        entry("Post", source="community-blog", published_at="2024-05-01"),
    ])

    out = run(capsys, root, "status")

    assert "2 indexed pages" in out
    assert "official: 1" in out and "STALE" in out
    assert "community-blog: 1" in out and "published 2024-05-01 .. 2024-05-01" in out


def test_status_does_not_flag_fresh_docs(capsys, library):
    assert "STALE" not in run(capsys, library([entry("New")]), "status")


PAGE = """---
url: "https://x/a"
content_hash: "sha256:abc"
---
[Title](https://x/a)

===================================================

Intro text.

## First

one
two

```
# not a heading
```

### Nested

deep

## Second

last
"""


def test_outline_lists_headings_with_lines_and_skips_fences_and_frontmatter(capsys, library):
    root = library([], {"raw/a.md": PAGE})

    out = run(capsys, root, "outline", "raw/a.md")

    assert "L5 # Title" in out
    assert "## First" in out and "### Nested" in out and "## Second" in out
    assert "not a heading" not in out
    assert "content_hash" not in out


def test_section_stops_at_next_heading_of_same_or_higher_level(capsys, library):
    root = library([], {"raw/a.md": PAGE})

    out = run(capsys, root, "section", "raw/a.md", "first")

    assert "one" in out and "deep" in out
    assert "last" not in out


def test_section_truncates_and_tells_where_to_continue(capsys, library):
    root = library([], {"raw/a.md": PAGE})

    out = run(capsys, root, "section", "raw/a.md", "First", "--max-lines", "3")

    assert "[truncated at 3 lines" in out and "offset=" in out


def test_section_with_unknown_heading_exits_2(capsys, library):
    root = library([], {"raw/a.md": PAGE})

    with pytest.raises(SystemExit) as exc_info:
        docs.main(["--docs-dir", str(root), "section", "raw/a.md", "missing"])

    assert exc_info.value.code == 2
    assert "docs.py outline" in capsys.readouterr().err


# --- full-text search (search.db built by the package's index builder) ---------------------------

def fts_available():
    import sqlite3
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        return True
    except sqlite3.Error:
        return False


needs_fts = pytest.mark.skipif(not fts_available(), reason="this Python's SQLite has no FTS5")


def page_text(title, capability, body, source="official", extra=""):
    return (f'---\nurl: "https://x/{title.replace(" ", "-")}"\nsource_type: {source}\n'
            f'capability: {capability}\n{extra}fetched_at: "2026-09-01T00:00:00Z"\n---\n# {title}\n\n{body}\n')


@pytest.fixture
def fts_library(tmp_path):
    from liferay_docs_scraper import index

    def build(pages):
        for rel, text in pages.items():
            page = tmp_path / "raw" / rel
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(text, encoding="utf-8")
        index.build_search_index(tmp_path / "raw", tmp_path / "reports" / "filtered")
        return tmp_path
    return build


@needs_fts
def test_full_text_search_finds_a_term_that_only_appears_in_the_body(capsys, fts_library):
    root = fts_library({
        "self-hosted/props.md": page_text("Portal Properties", "self-hosted",
                                          "Set company.security.auth.type to emailAddress for login."),
        "search/other.md": page_text("Other", "search", "Nothing relevant here at all."),
    })

    out = run(capsys, root, "search", "company.security.auth.type")

    assert "raw/self-hosted/props.md" in out
    assert "«company.security.auth.type»" in out
    assert "raw/search/other.md" not in out


@needs_fts
def test_full_text_search_stems_and_ranks_title_matches_above_body_matches(capsys, fts_library):
    root = fts_library({
        "development/a.md": page_text("Client Extension Basics", "development", "How to build one."),
        "development/b.md": page_text("Deployment Notes", "development",
                                      "A long note that mentions extensions once, in passing."),
    })

    out = run(capsys, root, "search", "extensions")

    assert out.index("development/a.md") < out.index("development/b.md")


@needs_fts
def test_full_text_search_prefers_official_over_blog_on_equal_relevance(capsys, fts_library):
    root = fts_library({
        "development/doc.md": page_text("Osgi Modules", "development", "Osgi modules explained."),
        "community-blog/_uncategorized/post.md": page_text(
            "Osgi Modules", "uncategorized", "Osgi modules explained.", source="community-blog",
            extra='published_at: "2025-01-01"\n'),
    })

    out = run(capsys, root, "search", "osgi")

    assert out.index("[official]") < out.index("[community-blog]")


@needs_fts
def test_full_text_search_filters_by_source_capability_and_since(capsys, fts_library):
    root = fts_library({
        "development/doc.md": page_text("Osgi Guide", "development", "About osgi."),
        "community-blog/_uncategorized/old.md": page_text(
            "Osgi Old", "uncategorized", "About osgi.", source="community-blog", extra='published_at: "2022-05-01"\n'),
        "community-blog/_uncategorized/new.md": page_text(
            "Osgi New", "uncategorized", "About osgi.", source="community-blog", extra='published_at: "2025-05-01"\n'),
    })

    assert "old.md" not in run(capsys, root, "search", "osgi", "--source", "blog", "--since", "2024")
    assert "new.md" in run(capsys, root, "search", "osgi", "--source", "blog", "--since", "2024")
    undated = run(capsys, root, "search", "osgi", "--since", "2024")
    assert "development/doc.md" in undated
    only_dev = run(capsys, root, "search", "osgi", "--capability", "development")
    assert "development/doc.md" in only_dev and "new.md" not in only_dev


@needs_fts
def test_full_text_search_falls_back_to_partial_matches_and_says_so(capsys, fts_library):
    root = fts_library({"integration/oauth.md": page_text("Using OAuth", "integration", "Request an access token.")})

    out = run(capsys, root, "search", "oauth", "token", "nonexistentword")

    assert "No page matches all the terms" in out
    assert "integration/oauth.md" in out


@needs_fts
def test_full_text_search_reports_no_hits_and_survives_operator_like_terms(capsys, fts_library):
    root = fts_library({"search/a.md": page_text("Alpha", "search", "Some text.")})

    assert "No hits for" in run(capsys, root, "search", "zzzz")
    run(capsys, root, "search", 'AND OR NEAR "quoted" (paren')  # must not raise an FTS syntax error


@needs_fts
def test_full_text_search_caps_output_and_says_how_many_more(capsys, fts_library):
    root = fts_library({f"search/p{i}.md": page_text(f"Page {i}", "search", "shared keyword text")
                        for i in range(5)})

    out = run(capsys, root, "search", "keyword", "--limit", "2")

    assert out.count("raw/search/") == 2
    assert "2 of 5 matches shown" in out


@needs_fts
def test_status_reports_full_text_search_ready(capsys, fts_library):
    root = fts_library({"search/a.md": page_text("Alpha", "search", "Some text.")})

    assert "full-text search: ready (1 pages)" in run(capsys, root, "status")


@needs_fts
def test_stale_database_falls_back_to_the_index_with_a_note(capsys, fts_library):
    import os
    import time
    root = fts_library({"search/a.md": page_text("Alpha Tuning", "search", "Some text.")})
    later = time.time() + 100
    os.utime(root / "raw" / "search", (later, later))  # a page changed after the database was built

    docs.main(["--docs-dir", str(root), "search", "alpha"])
    captured = capsys.readouterr()

    assert "raw/search/a.md" in captured.out
    assert "full-text search unavailable" in captured.err and "older than the Markdown" in captured.err


def test_missing_database_falls_back_to_the_index_with_a_note(capsys, library):
    root = library([entry("Alpha", path="raw/search/a.md")])

    docs.main(["--docs-dir", str(root), "search", "alpha"])
    captured = capsys.readouterr()

    assert "raw/search/a.md" in captured.out
    assert "no search.db" in captured.err


def test_url_finds_local_page_ignoring_scheme_fragment_and_trailing_slash(capsys, library):
    root = library([entry("Client Extensions", path="raw/development/client-extensions.md",
                          url="https://learn.liferay.com/w/dxp/development/client-extensions")])

    out = run(capsys, root, "url", "http://www.learn.liferay.com/w/dxp/development/Client-Extensions/?x=1#types")

    assert "raw/development/client-extensions.md" in out


def test_url_not_in_library_exits_1_and_suggests_a_search(capsys, library):
    root = library([entry("Other", url="https://learn.liferay.com/w/dxp/other")])

    with pytest.raises(SystemExit) as exit_info:
        run(capsys, root, "url", "https://learn.liferay.com/w/dxp/search/synonym-sets")

    assert exit_info.value.code == 1
    assert "docs.py search synonym sets" in capsys.readouterr().err
