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

    result = doctor.inspect_installation(docs_dir, project_dir)

    assert result.ok is True
    assert result.official_docs_count == 1
    assert result.discovered_total == 1900


def test_main_exits_nonzero_when_docs_or_skill_are_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "resolve_docs_dir", lambda: tmp_path / "missing-docs")
    monkeypatch.setattr("sys.argv", ["liferay-context-builder-doctor", "--project-dir", str(tmp_path / "project")])

    with pytest.raises(SystemExit) as exc_info:
        doctor.main()

    assert exc_info.value.code == 1


def test_next_steps_point_at_firecrawl(capsys, tmp_path):
    result = doctor.inspect_installation(tmp_path / "docs", tmp_path / "project")
    doctor.print_result(result)
    out = capsys.readouterr().out
    assert "crawl4ai" not in out
    assert "FIRECRAWL_API_URL" in out
