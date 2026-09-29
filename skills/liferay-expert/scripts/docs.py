#!/usr/bin/env python3
"""Bounded lookups over the local Liferay docs library (standard library only).

  docs.py status                          is the library there, and how fresh?
  docs.py search TERM [TERM...]           ranked full-text hits (page bodies included), with the matching passage
  docs.py outline PATH                    headings of one page, with line numbers
  docs.py section PATH HEADING            just that section of a large page

The library is $LIFERAY_DOCS_DIR, else ~/.liferay-docs. PATH is a `path` value
from a search hit (relative to the library) or an absolute path. Output is
capped on purpose so it never floods an agent's context.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

INDEX_RELATIVE = Path("reports") / "filtered" / "search_index.jsonl"
DB_RELATIVE = Path("reports") / "filtered" / "search.db"
SOURCES = {
    "official": "official",
    "howto": "community-howto",
    "troubleshooting": "community-troubleshooting",
    "blog": "community-blog",
}
RANK = {"official": 0, "community-howto": 1, "community-troubleshooting": 2, "community-blog": 3}
STALE_AFTER_DAYS = 7
SUMMARY_CHARS = 160
SNIPPET_CHARS = 220
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
UNDERLINE_RE = re.compile(r"=+|-{3,}")
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def docs_dir(override: str | None) -> Path:
    return Path(override or os.environ.get("LIFERAY_DOCS_DIR") or "~/.liferay-docs").expanduser()


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(2)


def load_index(docs: Path) -> list[dict]:
    path = docs / INDEX_RELATIVE
    if not path.exists():
        fail(f"No search index at {path}. Build the library: uv run liferay-context-builder "
             f"(in the liferay-context-builder checkout), or grep {docs}/raw/ directly.")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def parse_day(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def cmd_status(args) -> None:
    docs = docs_dir(args.docs_dir)
    entries = load_index(docs)
    print(f"Docs dir: {docs}  ({len(entries)} indexed pages)")
    now = datetime.now(timezone.utc)
    for source in RANK:
        group = [e for e in entries if e.get("source_type") == source]
        if not group:
            continue
        fetched = [d for d in (parse_day(e.get("fetched_at", "")) for e in group) if d]
        line = f"  {source}: {len(group)}"
        if fetched:
            newest = max(fetched)
            age = (now - newest).days
            stale = "  STALE, refresh with uv run liferay-context-builder" if source == "official" and age >= STALE_AFTER_DAYS else ""
            line += f"  fetched {min(fetched).date()} .. {newest.date()}{stale}"
        dated = [e["published_at"] for e in group if e.get("published_at")]
        if dated:
            line += f"  published {min(dated)} .. {max(dated)}"
        print(line)
    connection, reason = open_db(docs)
    if connection is None:
        print(f"  full-text search: unavailable ({reason})")
    else:
        pages = connection.execute("SELECT count(*) FROM docs").fetchone()[0]
        connection.close()
        print(f"  full-text search: ready ({pages} pages)")
    if not any(e.get("source_type") == "official" for e in entries):
        print("  No official docs indexed: run uv run liferay-context-builder")


def relevance(entry: dict, terms: list[str]) -> int | None:
    """None unless every term appears somewhere in the entry; else a score
    that favours matches in the title, then headings."""
    whole = json.dumps(entry, ensure_ascii=False).lower()
    if any(t not in whole for t in terms):
        return None
    title = entry.get("title", "").lower()
    headings = " ".join(entry.get("headings", [])).lower()
    return sum(3 * (t in title) + 2 * (t in headings) + 1 for t in terms)


def db_stale_reason(docs: Path, db: Path) -> str | None:
    """Why the full-text database can't be trusted, or None. Builder runs replace
    files atomically, so a directory's mtime moves whenever a page changes."""
    if not db.exists():
        return "no search.db (built by the liferay-context-builder commands)"
    newest = max((os.stat(root).st_mtime for root, _dirs, _files in os.walk(docs / "raw")), default=0)
    if newest > db.stat().st_mtime:
        return "search.db is older than the Markdown files (a build is running or unfinished)"
    return None


def open_db(docs: Path) -> tuple[sqlite3.Connection | None, str | None]:
    """(connection, None) when full-text search is usable, else (None, reason)."""
    db = docs / DB_RELATIVE
    reason = db_stale_reason(docs, db)
    if reason:
        return None, reason
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        connection.execute("SELECT 1 FROM docs LIMIT 1")
        return connection, None
    except sqlite3.Error as exc:
        return None, f"search.db unusable ({exc})"


def match_expression(terms: list[str], joiner: str) -> str:
    """Each term as an FTS5 phrase, so identifiers like company.security.auth.type
    and operators like AND/NEAR in user text can't be misread as query syntax."""
    return joiner.join('"' + t.replace('"', '""') + '"' for t in terms if re.search(r"\w", t))


HIT_SQL = """
SELECT path, title, source_type, capability, published_at, snippet(docs, -1, '«', '»', ' … ', 28)
FROM docs WHERE docs MATCH :query {filters}
ORDER BY bm25(docs, 10.0, 5.0, 3.0, 1.0)
         + CASE source_type WHEN 'official' THEN 0 WHEN 'community-blog' THEN 2 ELSE 1 END,
         published_at DESC
LIMIT :limit
"""


def full_text_hits(connection, args, joiner: str) -> tuple[list[tuple], int]:
    filters, params = [], {"query": match_expression(args.terms, joiner), "limit": args.limit}
    if not params["query"]:
        return [], 0
    if args.source:
        filters.append("AND source_type = :source")
        params["source"] = SOURCES[args.source]
    if args.capability:
        filters.append("AND capability = :capability")
        params["capability"] = args.capability
    if args.since:  # undated pages (official docs) are never dropped by --since
        filters.append("AND (published_at = '' OR published_at >= :since)")
        params["since"] = args.since
    where = " ".join(filters)
    total = connection.execute(f"SELECT count(*) FROM docs WHERE docs MATCH :query {where}", params).fetchone()[0]
    return connection.execute(HIT_SQL.format(filters=where), params).fetchall(), total


def print_hit(number: int, source: str, title: str, capability: str, published: str, path: str, detail: str) -> None:
    meta = " | ".join(x for x in (capability, published) if x)
    print(f"{number}. [{source}] {title} | {meta}")
    print(f"   {path}")
    if detail:
        print(f"   {detail}")


def search_full_text(connection, args) -> bool:
    """Print full-text hits; False only if the database could not answer."""
    try:
        rows, total = full_text_hits(connection, args, " ")
        partial = False
        if not rows and len(args.terms) > 1:
            rows, total = full_text_hits(connection, args, " OR ")
            partial = bool(rows)
    except sqlite3.Error:
        return False
    if not rows:
        print(f"No hits for: {' '.join(args.terms)}. Try other keywords or drop a filter.")
        return True
    if partial:
        print("No page matches all the terms; closest partial matches:")
    for number, (path, title, source, capability, published, passage) in enumerate(rows, start=1):
        passage = re.sub(r"[=-]{4,}", " ", passage)  # title underlines from the Markdown
        print_hit(number, source, title, capability, published, path, " ".join(passage.split())[:SNIPPET_CHARS])
    if total > len(rows):
        print(f"\n{len(rows)} of {total} matches shown; narrow with more terms, --source, --capability or --since.")
    return True


def search_titles(docs: Path, args) -> None:
    """Fallback without full-text: every term must appear in a page's index
    entry (title, headings, tags, summary, path)."""
    terms = [t.lower() for t in args.terms]
    source = SOURCES[args.source] if args.source else None
    hits = []
    for entry in load_index(docs):
        if source and entry.get("source_type") != source:
            continue
        if args.capability and entry.get("capability") != args.capability:
            continue
        # entries without a date (official docs) are never dropped by --since
        if args.since and entry.get("published_at") and entry["published_at"] < args.since:
            continue
        score = relevance(entry, terms)
        if score is not None:
            hits.append((score, entry))

    if not hits:
        print(f"No index hits for: {' '.join(args.terms)}. Try other keywords, drop a filter, "
              f"or grep -ril the raw Markdown under {docs}/raw/.")
        return
    hits.sort(key=lambda h: h[1].get("published_at", ""), reverse=True)  # newest first, then a stable sort:
    hits.sort(key=lambda h: (-h[0], RANK.get(h[1].get("source_type"), len(RANK))))
    for number, (_, entry) in enumerate(hits[:args.limit], start=1):
        print_hit(number, entry.get("source_type"), entry.get("title"), entry.get("capability"),
                  entry.get("published_at", ""), entry.get("path"), entry.get("summary", "")[:SUMMARY_CHARS])
    if len(hits) > args.limit:
        print(f"\n{args.limit} of {len(hits)} matches shown; narrow with more terms, "
              "--source, --capability or --since.")


def cmd_search(args) -> None:
    docs = docs_dir(args.docs_dir)
    connection, reason = open_db(docs)
    if connection is not None:
        try:
            if search_full_text(connection, args):
                return
            reason = "search.db could not answer the query"
        finally:
            connection.close()
    print(f"note: full-text search unavailable ({reason}); matching titles, headings, tags and summaries only.",
          file=sys.stderr)
    search_titles(docs, args)


def read_page(docs: Path, page: str) -> tuple[Path, list[str]]:
    path = Path(page)
    if not path.is_absolute():
        path = docs / page
    if not path.is_file():
        fail(f"No such file: {path}")
    return path, path.read_text(encoding="utf-8").splitlines()


def underline_of(lines: list[str], index: int) -> str:
    """The `====`/`----` line underlining lines[index], or "". Official pages
    put one blank line between their title and its `====`, so that is allowed
    for `=` too."""
    following = [x.strip() for x in lines[index + 1:index + 3]]
    if following and UNDERLINE_RE.fullmatch(following[0]):
        return following[0]
    if len(following) == 2 and not following[0] and set(following[1]) == {"="}:
        return following[1]
    return ""


def headings_of(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line number, level, text) for each heading outside code fences: `#`
    headings, and underlined ones (the title line official pages start with).
    Frontmatter is skipped so its closing `---` doesn't underline a field."""
    start = 0
    if lines and lines[0] == "---":
        closing = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
        start = closing + 1 if closing else 0
    found, in_fence = [], False
    for index in range(start, len(lines)):
        line = lines[index]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match:
            found.append((index + 1, len(match.group(1)), LINK_RE.sub(r"\1", match.group(2))))
            continue
        underline = underline_of(lines, index)
        if line.strip() and line.lstrip()[0] not in "|-*>" and underline:
            found.append((index + 1, 1 if underline[0] == "=" else 2, LINK_RE.sub(r"\1", line.strip())))
    return found


def cmd_outline(args) -> None:
    path, lines = read_page(docs_dir(args.docs_dir), args.path)
    print(f"{path}: {len(lines)} lines, {path.stat().st_size} bytes")
    for number, level, text in headings_of(lines):
        print(f"L{number} {'#' * level} {text}")


def cmd_section(args) -> None:
    path, lines = read_page(docs_dir(args.docs_dir), args.path)
    headings = headings_of(lines)
    wanted = args.heading.lower()
    match = next((h for h in headings if h[2].lower() == wanted), None) or \
        next((h for h in headings if wanted in h[2].lower()), None)
    if match is None:
        fail(f"No heading matching {args.heading!r} in {path}. Run: docs.py outline {args.path}")
    start, level, _ = match
    end = next((n - 1 for n, lvl, _ in headings if n > start and lvl <= level), len(lines))
    shown_end = min(end, start + args.max_lines - 1)
    print(f"{path}:L{start}-L{shown_end}")
    print("\n".join(lines[start - 1:shown_end]))
    if shown_end < end:
        print(f"\n[truncated at {args.max_lines} lines; the section runs to L{end}. "
              f"Use Read with offset={shown_end + 1} to continue.]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docs-dir", help="Library location (default: $LIFERAY_DOCS_DIR, else ~/.liferay-docs).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Is the library there, and how fresh is it?").set_defaults(run=cmd_status)

    search = sub.add_parser("search", help="Ranked hits; every TERM must match (case-insensitive).")
    search.add_argument("terms", nargs="+", metavar="TERM")
    search.add_argument("--source", choices=sorted(SOURCES), help="Only this kind of page.")
    search.add_argument("--capability", help="Only this capability folder, e.g. search, commerce.")
    search.add_argument("--since", help="Drop dated pages older than this (YYYY or YYYY-MM-DD); official docs are undated.")
    search.add_argument("--limit", type=int, default=15, help="Maximum hits to print (default 15).")
    search.set_defaults(run=cmd_search)

    outline = sub.add_parser("outline", help="Headings of one page, with line numbers and size.")
    outline.add_argument("path")
    outline.set_defaults(run=cmd_outline)

    section = sub.add_parser("section", help="Print one section of a page, up to the next heading of its level.")
    section.add_argument("path")
    section.add_argument("heading", help="Heading text; exact match preferred, else substring.")
    section.add_argument("--max-lines", type=int, default=200)
    section.set_defaults(run=cmd_section)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
