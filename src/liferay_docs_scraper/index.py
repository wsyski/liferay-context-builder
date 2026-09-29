"""Local retrieval index and lightweight scrape anomaly reports."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .filter_urls import CAPABILITIES, atomic_write_text

SEARCH_INDEX_NAME = "search_index.jsonl"
ANOMALIES_NAME = "anomalies.jsonl"

OFFICIAL_SOURCE_TYPE = "official"
COMMUNITY_SOURCE_TYPES = ("community-howto", "community-troubleshooting", "community-blog")
# Authority order: an agent reading the index top-down meets official docs first.
SOURCE_RANK = {OFFICIAL_SOURCE_TYPE: 0, **{t: i for i, t in enumerate(COMMUNITY_SOURCE_TYPES, start=1)}}
# Frontmatter fields worth carrying into the index so an agent can filter by
# date, tag or version straight from a grep hit, without opening the file.
INDEX_EXTRA_FIELDS = (
    "published_at", "author", "categories", "tags",
    "applicable_versions", "feature", "deployment_approach", "resource_type",
)
SUMMARY_CHARS = 200

MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
ANOMALY_ERROR_MARKERS = (
    "An unexpected error occurred",
    "There was an error processing the request",
)
MIN_BODY_CHARS = 80
MIN_BODY_WORDS = 15
LARGE_BODY_CHARS = 250_000
SHRINK_RATIO = 0.5
GROWTH_RATIO = 3.0


@dataclass
class ContentSnapshot:
    body_chars: int
    body_words: int


def parse_frontmatter(markdown: str) -> tuple[dict[str, str], str]:
    match = FRONTMATTER_RE.match(markdown)
    if not match:
        return {}, markdown

    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        value = raw_value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value.strip('"')
        values[key.strip()] = value
    return values, markdown[match.end():]


def extract_headings(body: str, limit: int = 12) -> list[str]:
    headings: list[str] = []
    for match in HEADING_RE.finditer(body):
        text = MARKDOWN_LINK_RE.sub(r"\1", match.group(2)).strip()
        if text:
            headings.append(text)
        if len(headings) >= limit:
            break
    return headings


def title_from_body(body: str, fallback: str) -> str:
    headings = extract_headings(body, limit=1)
    if headings:
        return headings[0]
    return fallback.replace("-", " ").strip().title() or fallback


def summarize(body: str, limit: int = SUMMARY_CHARS) -> str:
    """First plain prose line of the body, so a grep hit says what the page is about."""
    for line in body.splitlines():
        text = MARKDOWN_LINK_RE.sub(r"\1", line.strip())
        if len(text) < 40 or text[0] in "#[|>-*=`!" or text.startswith("---"):
            continue
        return text if len(text) <= limit else text[:limit].rstrip() + "…"
    return ""


def snapshot(body: str) -> ContentSnapshot:
    return ContentSnapshot(body_chars=len(body), body_words=len(body.split()))


def detect_anomalies(
    *,
    path: Path,
    url: str,
    source_type: str,
    capability: str,
    body: str,
    previous: ContentSnapshot | None = None,
) -> list[dict]:
    found: list[dict] = []
    current = snapshot(body)
    base = {
        "path": str(path),
        "url": url,
        "source_type": source_type,
        "capability": capability,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    def add(kind: str, detail: dict | None = None) -> None:
        item = dict(base)
        item["kind"] = kind
        if detail:
            item.update(detail)
        found.append(item)

    if current.body_chars < MIN_BODY_CHARS or current.body_words < MIN_BODY_WORDS:
        add("short_body", {"body_chars": current.body_chars, "body_words": current.body_words})
    if any(marker in body for marker in ANOMALY_ERROR_MARKERS):
        add("error_marker")
    if not extract_headings(body, limit=1):
        add("missing_title")
    if current.body_chars > LARGE_BODY_CHARS:
        add("large_body", {"body_chars": current.body_chars})

    if previous and previous.body_chars > 0:
        ratio = current.body_chars / previous.body_chars
        if ratio < SHRINK_RATIO:
            add("body_shrank", {"previous_body_chars": previous.body_chars, "body_chars": current.body_chars})
        elif ratio > GROWTH_RATIO:
            add("body_grew", {"previous_body_chars": previous.body_chars, "body_chars": current.body_chars})

    return found


def read_body_snapshot(path: Path) -> ContentSnapshot | None:
    if not path.exists():
        return None
    try:
        _frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return snapshot(body)


def append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_anomalies(path: Path, anomalies: list[dict]) -> None:
    if not anomalies:
        return
    append_jsonl(path, anomalies)


def source_dirs(raw_dir: Path) -> list[tuple[str, Path]]:
    dirs: list[tuple[str, Path]] = []
    for capability in CAPABILITIES:
        dirs.append((OFFICIAL_SOURCE_TYPE, raw_dir / capability))
    for source_type in COMMUNITY_SOURCE_TYPES:
        source_root = raw_dir / source_type
        if source_root.exists():
            dirs.extend((source_type, path) for path in source_root.iterdir() if path.is_dir())
    return dirs


def build_search_index(raw_dir: Path, reports_dir: Path) -> int:
    entries: list[dict] = []
    for source_type, directory in source_dirs(raw_dir):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue

            capability = frontmatter.get("capability") or directory.name
            headings = extract_headings(body)
            entry = {
                "title": title_from_body(body, path.stem),
                "url": frontmatter.get("url", ""),
                "source_type": frontmatter.get("source_type", source_type),
                "capability": capability,
                "path": str(path.relative_to(raw_dir.parent)),
                "headings": headings,
                "fetched_at": frontmatter.get("fetched_at", ""),
            }
            entry.update({key: frontmatter[key] for key in INDEX_EXTRA_FIELDS if frontmatter.get(key)})
            entry["summary"] = summarize(body)
            entries.append(entry)

    entries.sort(key=lambda item: (
        SOURCE_RANK.get(item["source_type"], len(SOURCE_RANK)), item["capability"], item["title"],
    ))
    write_jsonl(reports_dir / SEARCH_INDEX_NAME, entries)
    return len(entries)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def reset_anomalies_report(reports_dir: Path) -> None:
    atomic_write_text(reports_dir / ANOMALIES_NAME, "")


def ensure_anomalies_report(reports_dir: Path) -> None:
    path = reports_dir / ANOMALIES_NAME
    if not path.exists():
        atomic_write_text(path, "")
