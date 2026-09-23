#!/usr/bin/env python3
"""Check whether local docs and the Claude Code skill are ready to use."""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .filter_urls import CAPABILITIES, resolve_docs_dir
from .index import ANOMALIES_NAME, COMMUNITY_SOURCE_TYPES, SEARCH_INDEX_NAME, parse_frontmatter

STALE_AFTER_DAYS = 7
SECONDS_PER_DAY = 24 * 60 * 60


@dataclass
class DoctorResult:
    docs_dir: Path
    official_docs_count: int
    community_docs_count: int
    skill_path: Path
    skill_installed: bool
    discovered_total: int | None = None
    coverage_gap_count: int = 0
    direct_refreshed_count: int = 0
    search_index_count: int = 0
    anomaly_count: int = 0
    oldest_fetched_at: datetime | None = None
    newest_fetched_at: datetime | None = None

    @property
    def docs_ready(self) -> bool:
        return self.official_docs_count > 0

    @property
    def docs_stale(self) -> bool:
        if self.newest_fetched_at is None:
            return False
        age = datetime.now(timezone.utc) - self.newest_fetched_at
        return age.total_seconds() >= STALE_AFTER_DAYS * SECONDS_PER_DAY

    @property
    def ok(self) -> bool:
        return self.docs_ready and self.skill_installed


def count_official_docs(raw_dir: Path) -> int:
    return sum(
        1
        for capability in CAPABILITIES
        for _path in (raw_dir / capability).glob("*.md")
    )


def count_community_docs(raw_dir: Path) -> int:
    return sum(
        1
        for source_type in COMMUNITY_SOURCE_TYPES
        for _path in (raw_dir / source_type).glob("*/*.md")
    )


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_fetch_window(raw_dir: Path) -> tuple[datetime | None, datetime | None]:
    timestamps: list[datetime] = []
    for capability in CAPABILITIES:
        for path in (raw_dir / capability).glob("*.md"):
            try:
                frontmatter, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            fetched_at = parse_timestamp(frontmatter.get("fetched_at", ""))
            if fetched_at:
                timestamps.append(fetched_at)
    if not timestamps:
        return None, None
    return min(timestamps), max(timestamps)


def count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def read_summary(docs_dir: Path) -> dict:
    summary_path = docs_dir / "reports" / "filtered" / "summary.json"
    if not summary_path.exists():
        return {}
    try:
        value = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def summary_int(summary: dict, key: str, default: int = 0) -> int:
    value = summary.get(key, default)
    return value if isinstance(value, int) else 0


def optional_summary_int(summary: dict, key: str) -> int | None:
    value = summary.get(key)
    return value if isinstance(value, int) else None


def inspect_installation(docs_dir: Path, project_dir: Path) -> DoctorResult:
    skill_path = project_dir / ".claude" / "skills" / "liferay-expert" / "SKILL.md"
    raw_dir = docs_dir / "raw"
    reports_dir = docs_dir / "reports" / "filtered"
    summary = read_summary(docs_dir)
    oldest_fetched_at, newest_fetched_at = read_fetch_window(raw_dir)
    return DoctorResult(
        docs_dir=docs_dir,
        official_docs_count=count_official_docs(raw_dir),
        community_docs_count=count_community_docs(raw_dir),
        discovered_total=optional_summary_int(summary, "discovered_total"),
        coverage_gap_count=summary_int(summary, "coverage_gap_count"),
        direct_refreshed_count=summary_int(summary, "direct_refreshed_count"),
        search_index_count=count_jsonl(reports_dir / SEARCH_INDEX_NAME),
        anomaly_count=count_jsonl(reports_dir / ANOMALIES_NAME),
        oldest_fetched_at=oldest_fetched_at,
        newest_fetched_at=newest_fetched_at,
        skill_path=skill_path,
        skill_installed=skill_path.exists(),
    )


def print_result(result: DoctorResult) -> None:
    docs_status = "OK" if result.docs_ready else "MISSING"
    skill_status = "OK" if result.skill_installed else "MISSING"
    discovered = f", {result.discovered_total} discovered in last report" if result.discovered_total else ""

    print(f"Docs dir: {result.docs_dir}")
    print(f"Official docs: {docs_status} ({result.official_docs_count} markdown files{discovered})")
    print(f"Community docs: {result.community_docs_count} markdown files")
    if result.newest_fetched_at:
        print(f"Official freshness: {format_fetch_window(result)}")
    print(f"Search index: {result.search_index_count} entries")
    print(f"Anomalies report: {result.anomaly_count} entries")
    if result.coverage_gap_count:
        print(
            "Crawl coverage gaps refreshed directly: "
            f"{result.direct_refreshed_count}/{result.coverage_gap_count}"
        )
    print(f"Claude Code skill: {skill_status} ({result.skill_path})")

    if result.ok:
        if result.docs_stale:
            print(f"Warning: official docs are older than ~{STALE_AFTER_DAYS} days; refresh with uv run liferay-context-builder.")
        print("Ready: ask Claude Code a Liferay DXP question in this project.")
        return

    print("\nNext steps:")
    if not result.docs_ready:
        print("  Set FIRECRAWL_API_URL or start the stack: cd /path/to/firecrawl && docker compose up -d")
        print("  uv run liferay-context-builder")
    if not result.skill_installed:
        print("  npx skills add wsyski/liferay-context-builder --skill liferay-expert -a claude-code")


def format_fetch_window(result: DoctorResult) -> str:
    oldest = result.oldest_fetched_at.date().isoformat() if result.oldest_fetched_at else "unknown"
    newest = result.newest_fetched_at.date().isoformat() if result.newest_fetched_at else "unknown"
    stale = " STALE" if result.docs_stale else ""
    return f"{oldest} .. {newest}{stale}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="Project directory where .claude/skills/liferay-expert should exist (default: current directory).",
    )
    args = parser.parse_args()

    result = inspect_installation(resolve_docs_dir(), args.project_dir)
    print_result(result)
    if not result.ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
