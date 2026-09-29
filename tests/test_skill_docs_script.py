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
