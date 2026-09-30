import json

import pytest

from liferay_docs_scraper import doctor


def test_inspect_installation_reports_ready_project(tmp_path):
    docs_dir = tmp_path / "docs"
    project_dir = tmp_path / "project"
    (docs_dir / "raw" / "search").mkdir(parents=True)
    (docs_dir / "raw" / "search" / "page.md").write_text("body", encoding="utf-8")
    (docs_dir / "reports" / "filtered").mkdir(parents=True)
    (docs_dir / "reports" / "filtered" / "summary.json").write_text(
        json.dumps({"discovered_total": 1900}),
        encoding="utf-8",
    )
    (project_dir / ".claude" / "skills" / "liferay-expert").mkdir(parents=True)
    (project_dir / ".claude" / "skills" / "liferay-expert" / "SKILL.md").write_text("skill", encoding="utf-8")

    result = doctor.inspect_installation(docs_dir, project_dir, home=tmp_path / "home")

    assert result.ok is True
    assert result.skill_installed is True
    assert result.official_docs_count == 1
    assert result.discovered_total == 1900


def test_main_exits_nonzero_when_docs_are_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "resolve_docs_dir", lambda: tmp_path / "missing-docs")
    monkeypatch.setattr("sys.argv", ["liferay-context-builder-doctor", "--project-dir", str(tmp_path / "project")])

    with pytest.raises(SystemExit) as exc_info:
        doctor.main()

    assert exc_info.value.code == 1


def test_next_steps_point_at_firecrawl(capsys, tmp_path):
    result = doctor.inspect_installation(tmp_path / "docs", tmp_path / "project", home=tmp_path / "home")
    doctor.print_result(result)
    out = capsys.readouterr().out
    assert "crawl4ai" not in out
    assert "FIRECRAWL_API_URL" in out


def test_full_text_pages_counts_the_fts_database_and_tolerates_absence(tmp_path):
    from liferay_docs_scraper import index

    assert doctor.count_full_text_pages(tmp_path / "nope.db") is None
    (tmp_path / "junk.db").write_text("not a database", encoding="utf-8")
    assert doctor.count_full_text_pages(tmp_path / "junk.db") is None

    page = tmp_path / "raw" / "search" / "a.md"
    page.parent.mkdir(parents=True)
    page.write_text('---\ncapability: search\n---\n# A\n\nBody text.\n', encoding="utf-8")
    index.build_search_index(tmp_path / "raw", tmp_path / "reports")
    assert doctor.count_full_text_pages(tmp_path / "reports" / "search.db") == 1


@pytest.mark.parametrize("base", [".claude", ".agents"])
def test_skill_found_in_user_level_locations(tmp_path, base):
    skill = tmp_path / "home" / base / "skills" / "liferay-expert" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill", encoding="utf-8")

    result = doctor.inspect_installation(tmp_path / "docs", tmp_path / "project", home=tmp_path / "home")

    assert result.skill_installed is True
    assert result.skill_path == skill


def test_missing_skill_does_not_fail_ready_docs(capsys, tmp_path):
    docs_dir = tmp_path / "docs"
    (docs_dir / "raw" / "search").mkdir(parents=True)
    (docs_dir / "raw" / "search" / "page.md").write_text("body", encoding="utf-8")

    result = doctor.inspect_installation(docs_dir, tmp_path / "project", home=tmp_path / "home")
    doctor.print_result(result)

    assert result.ok is True
    assert "not installed (optional)" in capsys.readouterr().out


def make_library(tmp_path):
    docs_dir = tmp_path / "docs"
    (docs_dir / "raw" / "search").mkdir(parents=True)
    (docs_dir / "raw" / "search" / "a.md").write_text('---\ncapability: search\n---\n# A\n\nBody.\n', encoding="utf-8")
    blog = docs_dir / "raw" / "community-blog" / "_uncategorized"
    blog.mkdir(parents=True)
    (blog / "post.md").write_text(
        '---\nsource_type: community-blog\npublished_at: 2026-04-17T10:00:00Z\n---\n# Post\n\nBody.\n',
        encoding="utf-8")
    return docs_dir


def test_community_breakdown_and_newest_blog_are_reported(capsys, tmp_path):
    docs_dir = make_library(tmp_path)

    doctor.print_result(doctor.inspect_installation(docs_dir, tmp_path / "p", home=tmp_path / "h"))

    assert "Community docs: 1 markdown files (howto 0, troubleshooting 0, blog 1; newest blog 2026-04-17)" in capsys.readouterr().out


def test_consistent_index_has_no_warnings_and_partial_index_is_flagged(capsys, tmp_path):
    from liferay_docs_scraper import index

    docs_dir = make_library(tmp_path)
    index.build_search_index(docs_dir / "raw", docs_dir / "reports" / "filtered")
    result = doctor.inspect_installation(docs_dir, tmp_path / "p", home=tmp_path / "h")
    assert result.index_warnings == []

    late = docs_dir / "raw" / "community-blog" / "_uncategorized" / "late.md"
    late.write_text('---\nsource_type: community-blog\n---\n# Late\n\nBody.\n', encoding="utf-8")
    result = doctor.inspect_installation(docs_dir, tmp_path / "p", home=tmp_path / "h")
    doctor.print_result(result)

    out = capsys.readouterr().out
    assert "has 2 pages but 3 Markdown files are on disk" in out
    assert "Full-text index is older than the Markdown files" in out
    assert "--reindex-only" in out
    assert result.ok is True
