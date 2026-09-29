#!/usr/bin/env python3
"""Scrape recent Liferay community blog posts from liferay.dev/blogs.

Separate from pipeline.py and community.py: different site, and the lowest
authority of the three sources -- posts are dated, version-specific opinion
and walkthroughs, and can contradict the current docs. Written to
raw/community-blog/_uncategorized/ with source_type, published_at, author
and categories in the frontmatter, so the liferay-expert skill can cite them
with a date and a caveat.

  - Listing pages are fetched one at a time through Firecrawl's /v2/scrape,
    2 s apart: each page decides whether the next is needed, and the site
    answers deep listing pages with 403, so pacing and failure handling sit
    in this module. The posts themselves are one flat URL list, so they go
    through a single /v2/batch/scrape job, with one retry pass for misses.
    Listing and posts are server-rendered.
  - Discovery: /blogs?delta=20&start=<page>, newest first. Paging stops at
    the first page whose entries are all older than --since (default
    2022-01-01), so old pages are never requested. A 403 or empty page ends
    discovery early and is reported as a crawl error (exit code 1) with
    whatever was found kept.
  - Filtering: posts categorised "News" (release announcements, webinars,
    event recaps) are skipped unless --include-news is given.
  - Re-runs: posts already on disk are skipped unless --refresh is given,
    since a full run is a few hundred posts.

Usage:
    uv run liferay-context-builder-blogs
    uv run liferay-context-builder-blogs --since 2024-01-01 --limit 20
"""

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import markdownify

from . import fetcher
from .filter_urls import atomic_write_text, quote_frontmatter_value, resolve_docs_dir, safe_filename_stem
from .index import (
    ANOMALIES_NAME,
    append_anomalies,
    build_search_index,
    detect_anomalies,
    ensure_anomalies_report,
    read_body_snapshot,
)

LISTING_URL = "https://liferay.dev/blogs"
POST_URL_PATTERN = re.compile(r"https://liferay\.dev/b/[^/?#]+")
PAGE_SIZE = 20
MAX_LISTING_PAGES = 200
SOURCE_TYPE = "community-blog"
BUCKET = "_uncategorized"
NEWS_CATEGORY = "News"
DEFAULT_SINCE = date(2022, 1, 1)

REQUEST_DELAY_SECONDS = 2  # serial polite pacing; robots.txt's Crawl-delay: 10 names other bots, not "*"
RETRY_DELAY_SECONDS = 60
# both the listing and the posts are server-rendered -- no JS wait needed
SCRAPE_OPTIONS = {"formats": ["rawHtml"], "onlyMainContent": False, "waitFor": 0, "maxAge": 0, "timeout": 60000}

LISTING_DATE_RE = re.compile(r"[A-Z][a-z]{2} \d{1,2}, \d{4}")

ROOT = resolve_docs_dir()
RAW_DIR = ROOT / "raw"
FILTERED_DIR = ROOT / "reports" / "filtered"


class FetchError(RuntimeError):
    pass


@dataclass
class ListingEntry:
    url: str
    title: str
    author: str
    published: date
    categories: list[str]
    tags: list[str]


@dataclass
class RunStats:
    discovered_total: int = 0
    skipped_news: int = 0
    skipped_existing: int = 0
    fetch_failed: list[str] = field(default_factory=list)
    crawl_errors: list[str] = field(default_factory=list)
    outcomes: list[tuple[str, str]] = field(default_factory=list)  # (url, "new" | "updated" | "unchanged")


def fetch_page(url: str) -> str:
    """Scrape url through Firecrawl with one delayed retry, then sleep
    REQUEST_DELAY_SECONDS so requests stay paced.
    FirecrawlUnavailable propagates: no point retrying a dead stack."""
    for attempt in (1, 2):
        page = fetcher.scrape(url, SCRAPE_OPTIONS)
        if page.success:
            time.sleep(REQUEST_DELAY_SECONDS)
            return page.html
        if attempt == 1:
            time.sleep(RETRY_DELAY_SECONDS)
    raise FetchError(f"{url}: scrape failed twice (non-2xx or empty)")


def parse_listing(html: str) -> list[ListingEntry]:
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for item in soup.select("li.list-group-item-flex"):
        link = item.select_one("h2.title a[href]")
        meta = item.select_one(".search-results-metadata")
        if link is None or meta is None or not POST_URL_PATTERN.fullmatch(link["href"]):
            continue
        match = LISTING_DATE_RE.search(meta.get_text(" "))
        if match is None:
            continue
        author = meta.select_one("small strong")
        entries.append(ListingEntry(
            url=link["href"],
            title=link.get_text(strip=True),
            author=author.get_text(strip=True) if author else "",
            published=datetime.strptime(match.group(), "%b %d, %Y").date(),
            categories=[a.get_text(strip=True) for a in item.select("a.asset-category")],
            tags=[a.get_text(strip=True) for a in item.select(".taglib-asset-tags-summary a")],
        ))
    return entries


def discover_entries(fetch, since: date, stats: RunStats, limit: int | None = None) -> list[ListingEntry]:
    """Page through the listing newest-first until a page has nothing at or
    after `since`. Partial results are kept when a page fails or comes back
    empty -- the reason goes to stats.crawl_errors."""
    kept: dict[str, ListingEntry] = {}
    for page in range(1, MAX_LISTING_PAGES + 1):
        try:
            entries = parse_listing(fetch(f"{LISTING_URL}?delta={PAGE_SIZE}&start={page}"))
        except FetchError as exc:
            stats.crawl_errors.append(f"listing page {page}: {exc}")
            break
        if not entries:
            stats.crawl_errors.append(f"listing page {page}: no entries (blocked or layout changed)")
            break
        for entry in entries:
            if entry.published >= since:
                kept.setdefault(entry.url, entry)
        if all(entry.published < since for entry in entries):
            break
        if limit is not None and len(kept) >= limit:
            break
    found = list(kept.values())
    return found[:limit] if limit is not None else found


def extract_post(html: str) -> str | None:
    """Article body as Markdown. The body is the longest rich-text paragraph;
    the shorter ones are the read-time line and page chrome."""
    soup = BeautifulSoup(html, "html.parser")
    paragraphs = soup.select("div.component-paragraph[data-lfr-editable-type=rich-text]")
    body_el = max(paragraphs, key=lambda p: len(p.get_text()), default=None)
    if body_el is None or not body_el.get_text(strip=True):
        return None
    return markdownify(str(body_el), heading_style="ATX")


def post_path(entry: ListingEntry) -> Path:
    return RAW_DIR / SOURCE_TYPE / BUCKET / f"{safe_filename_stem(entry.url.rsplit('/', 1)[-1])}.md"


def build_frontmatter(entry: ListingEntry, full_content: str) -> str:
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content_hash = hashlib.sha256(full_content.encode("utf-8")).hexdigest()
    lines = [
        "---",
        f"url: {quote_frontmatter_value(entry.url)}",
        f"source_type: {SOURCE_TYPE}",
        "capability: uncategorized",
        f"author: {quote_frontmatter_value(entry.author)}",
        f"published_at: {quote_frontmatter_value(entry.published.isoformat())}",
        f"categories: {quote_frontmatter_value(', '.join(entry.categories))}",
        f"tags: {quote_frontmatter_value(', '.join(entry.tags))}",
        f"fetched_at: {quote_frontmatter_value(fetched_at)}",
        f"content_hash: {quote_frontmatter_value(f'sha256:{content_hash}')}",
        "---",
        "",
    ]
    return "\n".join(lines)


def read_existing_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("content_hash:"):
                return line.strip()
    return None


def write_post(entry: ListingEntry, html: str, stats: RunStats) -> bool:
    # One bad post must not kill a run that fetches hundreds -- record it and move on.
    try:
        markdown = extract_post(html)
        if markdown is None:
            return False
        out_path = post_path(entry)
        body = f"# {entry.title}\n\n{markdown}"
        previous_snapshot = read_body_snapshot(out_path)
        old_hash = read_existing_hash(out_path)
        existed_before = out_path.exists()
        atomic_write_text(out_path, build_frontmatter(entry, body) + body)
        append_anomalies(
            FILTERED_DIR / ANOMALIES_NAME,
            detect_anomalies(
                path=out_path, url=entry.url, source_type=SOURCE_TYPE, capability=BUCKET,
                body=body, previous=previous_snapshot,
            ),
        )
        new_hash = read_existing_hash(out_path)
        status = "new" if not existed_before else ("unchanged" if old_hash == new_hash else "updated")
        stats.outcomes.append((entry.url, status))
        return True
    except Exception as exc:  # noqa: BLE001 - any per-post failure is non-fatal here
        print(f"  ERROR processing {entry.url}: {exc}", file=sys.stderr)
        return False


def scrape_posts(entries: list[ListingEntry], batch_scrape, stats: RunStats) -> list[ListingEntry]:
    """Batch-scrape and write entries; return the ones that failed. A job
    that dies part-way is recorded in stats.crawl_errors and every entry it
    never returned counts as failed."""
    by_key = {fetcher.match_key(e.url): e for e in entries}
    handled: set[str] = set()
    failed: list[ListingEntry] = []
    try:
        for page in batch_scrape([e.url for e in entries], SCRAPE_OPTIONS):
            entry = by_key.get(fetcher.match_key(page.url))
            if entry is None or entry.url in handled:
                continue
            handled.add(entry.url)
            print(f"  [{len(handled)}/{len(entries)}] {entry.url}", flush=True)
            if not (page.success and write_post(entry, page.html, stats)):
                failed.append(entry)
    except fetcher.FirecrawlUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - keep what the job returned before it died
        stats.crawl_errors.append(f"post batch: {exc}")
        print(f"\nERROR: post batch interrupted: {exc}", file=sys.stderr)
        failed.extend(e for e in entries if e.url not in handled)
    return failed


def run(fetch, batch_scrape, since: date, include_news: bool, refresh: bool, limit: int | None = None) -> RunStats:
    stats = RunStats()
    print(f"Discovering posts since {since.isoformat()}...", flush=True)
    entries = discover_entries(fetch, since, stats, limit=limit)
    stats.discovered_total = len(entries)
    print(f"  {len(entries)} posts found", flush=True)

    todo = []
    for entry in entries:
        if not include_news and NEWS_CATEGORY in entry.categories:
            stats.skipped_news += 1
        elif not refresh and post_path(entry).exists():
            stats.skipped_existing += 1
        else:
            todo.append(entry)

    failed = scrape_posts(todo, batch_scrape, stats) if todo else []
    if failed and not stats.crawl_errors:
        print(f"  retrying {len(failed)} posts once...", flush=True)
        failed = scrape_posts(failed, batch_scrape, stats)
    stats.fetch_failed = [e.url for e in failed]
    return stats


def write_report(stats: RunStats) -> None:
    FILTERED_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(FILTERED_DIR / f"{SOURCE_TYPE}_summary.json", json.dumps({
        "discovered_total": stats.discovered_total,
        "skipped_news": stats.skipped_news,
        "skipped_existing": stats.skipped_existing,
        "written_total": len(stats.outcomes),
        "fetch_failed_count": len(stats.fetch_failed),
        "fetch_failed": stats.fetch_failed,
        "crawl_error_count": len(stats.crawl_errors),
        "crawl_errors": stats.crawl_errors,
        "search_index_entries": build_search_index(RAW_DIR, FILTERED_DIR),
    }, indent=2) + "\n")


def print_summary(stats: RunStats) -> None:
    print(f"\n--- {SOURCE_TYPE}: summary ---")
    print(f"Discovered: {stats.discovered_total} (skipped {stats.skipped_news} news, "
          f"{stats.skipped_existing} already on disk)")
    print(f"Written: {len(stats.outcomes)}")
    if stats.crawl_errors:
        print(f"Crawl errors (discovery may be incomplete): {len(stats.crawl_errors)}")
        for error in stats.crawl_errors:
            print(f"  - {error}")
    print(f"Fetch failures: {len(stats.fetch_failed)}")
    for url in stats.fetch_failed:
        print(f"  - {url}")


def parse_since(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", type=parse_since, default=DEFAULT_SINCE,
                        help=f"Only posts published on or after this date (default: {DEFAULT_SINCE.isoformat()}).")
    parser.add_argument("--include-news", action="store_true",
                        help="Also fetch posts categorised News (release announcements, webinars, events).")
    parser.add_argument("--refresh", action="store_true",
                        help="Re-fetch posts that are already on disk.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only fetch the first N discovered posts (smaller test run).")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be greater than zero")

    ensure_anomalies_report(FILTERED_DIR)
    try:
        stats = run(fetch_page, fetcher.batch_scrape, args.since, args.include_news, args.refresh, limit=args.limit)
    except fetcher.FirecrawlUnavailable as exc:
        print(f"ERROR: {exc}\n  Set FIRECRAWL_API_URL or start the stack: "
              "cd /path/to/firecrawl && docker compose up -d", file=sys.stderr)
        sys.exit(1)
    write_report(stats)
    print_summary(stats)
    if stats.fetch_failed or stats.crawl_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
