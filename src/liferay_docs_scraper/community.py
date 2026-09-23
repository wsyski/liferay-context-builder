#!/usr/bin/env python3
"""One-off scrape of Liferay's community-contributed Knowledge Base
articles: How-To recipes and Troubleshooting entries at
learn.liferay.com/kb-article/*.

Separate from pipeline.py (the weekly /w/dxp/* scrape) because the
discovery mechanism is completely different and this content is much
larger and lower-authority:

  - Discovery: /w/dxp/* is reached by a link-following crawl from one seed
    page. kb-article/* pages aren't linked from /w/dxp/* in any systematic
    way; the only way to enumerate them is learn.liferay.com's own faceted
    search UI (/learn-search?resource-type=X), a JS-rendered, paginated
    (start=<page number>&delta=<page size>) results list. This module
    pages through that listing purely to collect kb-article URLs, then
    fetches each URL directly with Firecrawl's batch scrape -- a flat list
    fetch, not a crawl.
  - Volume: ~1,400 How-To + ~3,700 Troubleshooting articles, roughly 3x
    the size of the official /w/dxp docs. Expect this to take
    considerably longer than pipeline.py's ~20-25 minutes -- run it
    separately, not as part of the weekly refresh.
  - Authority: every article carries a standard disclaimer ("How To/
    Troubleshooting articles are not official guidelines or officially
    supported documentation... community-contributed... may not always
    reflect the latest updates"). Written to
    raw/community-{howto,troubleshooting}/, separate from raw/{capability}/
    (the official docs), with source_type in the frontmatter, so the
    liferay-expert skill can cite these with a caveat instead of treating
    them the same as official docs.
  - Content template: different from /w/dxp/* pages (.learn-article-content
    doesn't exist here). The body lives in .knowledge-article-content, the
    title in .disclaimer-title (the only element with that class on the
    page, despite the name), and structured metadata in .category-tags
    elements (each preceded by an <h6> label: Capability, Feature,
    Deployment Approach, Applicable Versions, Resource Type). None of
    those live inside .knowledge-article-content, so each article is
    fetched as rawHtml for the full page and cleaned locally: BeautifulSoup
    pulls out the relevant elements, then markdownify converts just
    .knowledge-article-content's inner HTML to Markdown -- one fetch per
    article, not two.
  - Capability mapping: each article's "Capability:" tag text is matched
    against filter_urls.CAPABILITIES' 14 names (CAPABILITY_TAG_MAP below).
    Real-world tag text varies (older articles use different label
    strings, some articles have no Capability tag at all) -- unmatched
    articles go to raw/community-{howto,troubleshooting}/_uncategorized/
    rather than being dropped or guessed at, and the original tag text
    (if any) is preserved in that file's frontmatter either way, so
    nothing found is lost, just not auto-sorted. Unmapped tag values seen
    are also printed at the end and written to the run's summary report,
    so CAPABILITY_TAG_MAP can be extended later without re-scraping.

Usage:
    uv run liferay-context-builder-community
    uv run liferay-context-builder-community --resource-type howto
"""

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

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

SEARCH_URL = "https://learn.liferay.com/learn-search"
# key -> (resource-type facet id, source_type written to frontmatter)
RESOURCE_TYPES = {
    "howto": ("33317328", "community-howto"),
    "troubleshooting": ("33315839", "community-troubleshooting"),
}
PAGE_SIZE = 60
KB_ARTICLE_URL_PATTERN = re.compile(r"https://learn\.liferay\.com/kb-article/[a-zA-Z0-9\-]+")

# listing is server-rendered (spike) -- no JS wait needed
LISTING_OPTIONS = {"formats": ["rawHtml"], "waitFor": 0, "maxAge": 0, "timeout": 30000}
ARTICLE_OPTIONS = {"formats": ["rawHtml"], "onlyMainContent": False, "maxAge": 0, "timeout": 30000}

ROOT = resolve_docs_dir()
RAW_DIR = ROOT / "raw"
FILTERED_DIR = ROOT / "reports" / "filtered"

# Text seen in an article's "Capability:" tag -> one of filter_urls.CAPABILITIES'
# 14 keys. Deliberately conservative (lowercased, exact match); anything not
# listed here -- including no tag at all -- goes to _uncategorized/ instead
# of being guessed at.
CAPABILITY_TAG_MAP = {
    "cloud": "cloud",
    "search": "search",
    "self-hosted": "self-hosted",
    "self hosted": "self-hosted",
    "dxp self-hosted installation, maintenance, and administration": "self-hosted",
    "sites": "sites",
    "security": "security",
    "security and administration": "security",
    "development": "development",
    "development and tooling": "development",
    "commerce": "commerce",
    "personalization": "personalization",
    "low-code": "low-code",
    "low code": "low-code",
    "content management system": "content-management-system",
    "digital asset management": "digital-asset-management",
    "integration": "integration",
    "ai": "ai",
    "getting started": "getting-started",
}


def map_capability(tag_text: str | None) -> str | None:
    """tag_text may hold more than one value, comma-separated (see
    extract_article) -- return the first one that maps to a known
    capability, or None if none of them do."""
    if not tag_text:
        return None
    for part in tag_text.split(","):
        mapped = CAPABILITY_TAG_MAP.get(part.strip().lower())
        if mapped:
            return mapped
    return None


@dataclass
class ArticleOutcome:
    url: str
    capability: str | None
    slug: str
    status: str  # "new" | "updated" | "unchanged"


@dataclass
class RunStats:
    discovered_total: int = 0
    fetch_failed: list[str] = field(default_factory=list)
    crawl_errors: list[str] = field(default_factory=list)
    unmapped_capability_tags: dict[str, int] = field(default_factory=dict)
    outcomes: list[ArticleOutcome] = field(default_factory=list)


def discover_article_urls(
    client, resource_type_id: str, limit: int | None = None,
) -> list[str]:
    """Page through /learn-search?resource-type=X, PAGE_SIZE entries at a
    time, until a page returns no new kb-article links.

    Extracts hrefs from the parsed HTML rather than regexing the Markdown:
    a handful of slugs contain characters just outside
    KB_ARTICLE_URL_PATTERN's class (e.g. an apostrophe), and regexing
    Markdown link text truncated those into bogus short URLs.

    Spike: the listing is server-rendered -- 60 links per page without any
    JS wait needed."""
    urls: set[str] = set()
    page = 1
    while True:
        url = f"{SEARCH_URL}?q=&resource-type={resource_type_id}&delta={PAGE_SIZE}&start={page}"
        result = client.scrape(url, LISTING_OPTIONS)
        if not result.success:
            raise RuntimeError(f"search listing page {page} failed: {url}")
        page_urls = extract_article_links(result.html)
        if not page_urls or page_urls <= urls:
            break
        urls |= page_urls
        if limit is not None and len(urls) >= limit:
            break
        page += 1
    discovered = sorted(urls)
    return discovered[:limit] if limit is not None else discovered


def extract_article_links(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    return {
        a["href"] for a in soup.find_all("a", href=True)
        if KB_ARTICLE_URL_PATTERN.match(a["href"])
    }


def safe_slug(url: str) -> str:
    """Filesystem-safe filename stem for a kb-article URL. A handful of
    slugs are percent-encoded non-ASCII titles (e.g. Japanese) that, still
    encoded, run well past the OS filename length limit and crash the
    write -- decode first (shrinks percent-encoded bytes ~3x), then cap
    the byte length with a hash suffix as a hard safety net regardless of
    language or length."""
    decoded_path = unquote(urlparse(url).path)
    raw = decoded_path.removeprefix("/kb-article").strip("/").replace("/", "-") or "index"
    return safe_filename_stem(raw)


def read_existing_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("content_hash:"):
                return line.strip()
    return None


def extract_article(html: str, url: str) -> dict | None:
    """Parse one kb-article page's full HTML into title, clean body
    Markdown, and metadata tags. Returns None if the expected content
    container isn't present (page didn't render as a normal kb-article)."""
    soup = BeautifulSoup(html, "html.parser")
    content_el = soup.find(class_="knowledge-article-content")
    if content_el is None:
        return None

    title_el = soup.find(class_="disclaimer-title")
    title = title_el.get_text(strip=True) if title_el else url.rsplit("/", 1)[-1].replace("-", " ").title()

    tags: dict[str, str] = {}
    for tag_el in soup.find_all(class_="category-tags"):
        label_el = tag_el.find_previous_sibling()
        label = label_el.get_text(strip=True) if label_el else None
        if not label:
            continue
        # A label can have more than one value (e.g. an article tagged both
        # "Content Management System" and "Self-Hosted"), each its own <a>
        # inside this div -- join with ", " so map_capability() (and a
        # human reading the frontmatter) can tell them apart. Blindly
        # taking .get_text() on the whole div mashes multi-value tags into
        # one unmatchable blob with no separator at all.
        values = [a.get_text(strip=True) for a in tag_el.find_all("a")]
        tags[label] = ", ".join(values) if values else tag_el.get_text(" ", strip=True)

    body_markdown = markdownify(str(content_el), heading_style="ATX")

    return {"title": title, "body": body_markdown, "tags": tags}


def build_frontmatter(url: str, source_type: str, capability: str | None, tags: dict, full_content: str) -> str:
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content_hash = hashlib.sha256(full_content.encode("utf-8")).hexdigest()
    lines = [
        "---",
        f"url: {quote_frontmatter_value(url)}",
        f"source_type: {source_type}",
        f"capability: {capability or 'uncategorized'}",
    ]
    for label in ("Feature", "Deployment Approach", "Applicable Versions", "Resource Type"):
        if label in tags:
            key = label.lower().replace(" ", "_")
            lines.append(f"{key}: {quote_frontmatter_value(tags[label])}")
    if "Capability" in tags:
        lines.append(f"capability_tag_raw: {quote_frontmatter_value(tags['Capability'])}")
    lines += [
        f"fetched_at: {quote_frontmatter_value(fetched_at)}",
        f"content_hash: {quote_frontmatter_value(f'sha256:{content_hash}')}",
        "---",
        "",
    ]
    return "\n".join(lines)


def handle_article(result, source_type: str, stats: RunStats) -> bool:
    """Parse and write one fetched article. Returns False (without touching
    stats.fetch_failed) on any failure -- a failed/unparseable fetch or
    a per-article processing error -- so the caller can retry it once
    before giving up for good."""
    if not result.success:
        return False

    # One bad article (unexpected HTML shape, filesystem edge case, ...)
    # must not kill a run that's fetching thousands of pages -- log and
    # move on rather than letting an exception propagate out of the loop.
    try:
        parsed = extract_article(result.html, result.url)
        if parsed is None:
            return False

        tag_text = parsed["tags"].get("Capability")
        capability = map_capability(tag_text)
        if tag_text and capability is None:
            stats.unmapped_capability_tags[tag_text] = stats.unmapped_capability_tags.get(tag_text, 0) + 1

        slug = safe_slug(result.url)
        bucket = capability or "_uncategorized"
        out_path = RAW_DIR / source_type / bucket / f"{slug}.md"

        body = f"# {parsed['title']}\n\n{parsed['body']}"
        new_content = build_frontmatter(result.url, source_type, capability, parsed["tags"], body) + body

        previous_snapshot = read_body_snapshot(out_path)
        old_hash = read_existing_hash(out_path)
        existed_before = out_path.exists()
        atomic_write_text(out_path, new_content)
        append_anomalies(
            FILTERED_DIR / ANOMALIES_NAME,
            detect_anomalies(
                path=out_path,
                url=result.url,
                source_type=source_type,
                capability=capability or "_uncategorized",
                body=body,
                previous=previous_snapshot,
            ),
        )
        new_hash = read_existing_hash(out_path)
        status = "new" if not existed_before else ("unchanged" if old_hash == new_hash else "updated")
        stats.outcomes.append(ArticleOutcome(result.url, capability, slug, status))
        return True
    except Exception as exc:  # noqa: BLE001 - any per-article failure is non-fatal here
        print(f"  ERROR processing {result.url}: {exc}")
        return False


def run_resource_type(
    client, key: str, resource_type_id: str, source_type: str, limit: int | None = None,
) -> RunStats:
    stats = RunStats()
    print(f"\n=== {key} ({source_type}) ===")
    print("Discovering articles...")
    urls = discover_article_urls(client, resource_type_id, limit=limit)
    stats.discovered_total = len(urls)
    print(f"  {len(urls)} URLs found")

    try:
        retry = []
        for done, result in enumerate(client.batch_scrape(urls, ARTICLE_OPTIONS), start=1):
            if done % 200 == 0:
                print(f"  ...{done}/{len(urls)}")
            if not handle_article(result, source_type, stats):
                retry.append(result.url)

        # Spike: ~5% transient misses, all fine on retry.
        if retry:
            print(f"  retrying {len(retry)} articles once...", flush=True)
            for result in client.batch_scrape(retry, ARTICLE_OPTIONS):
                if not handle_article(result, source_type, stats):
                    stats.fetch_failed.append(result.url)
    except fetcher.FirecrawlUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - keep partial report data for long runs
        stats.crawl_errors.append(str(exc))
        print(f"\nERROR: fetch interrupted before finishing {key}: {exc}", file=sys.stderr)

    return stats


def counts_by_capability(stats: RunStats) -> dict[str, int]:
    counts: dict[str, int] = {}
    for outcome in stats.outcomes:
        bucket = outcome.capability or "_uncategorized"
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def write_report(key: str, stats: RunStats) -> None:
    FILTERED_DIR.mkdir(parents=True, exist_ok=True)
    report_path = FILTERED_DIR / f"{key}_summary.json"
    atomic_write_text(report_path, json.dumps({
        "discovered_total": stats.discovered_total,
        "written_total": len(stats.outcomes),
        "fetch_failed_count": len(stats.fetch_failed),
        "crawl_error_count": len(stats.crawl_errors),
        "crawl_errors": stats.crawl_errors,
        "by_capability": counts_by_capability(stats),
        "unmapped_capability_tags": stats.unmapped_capability_tags,
        "search_index_entries": build_search_index(RAW_DIR, FILTERED_DIR),
    }, indent=2) + "\n")


def print_summary(key: str, stats: RunStats) -> None:
    print(f"\n--- {key}: summary ---")
    print(f"Discovered: {stats.discovered_total}")
    print(f"Written: {len(stats.outcomes)}")
    if stats.crawl_errors:
        print(f"Fatal crawl errors: {len(stats.crawl_errors)}")
        for error in stats.crawl_errors:
            print(f"  - {error}")
    print(f"Fetch failures: {len(stats.fetch_failed)}")
    print("By capability:")
    for capability, count in sorted(counts_by_capability(stats).items(), key=lambda x: -x[1]):
        print(f"  {capability}: {count}")
    if stats.unmapped_capability_tags:
        print(f"\nUnmapped Capability tags ({len(stats.unmapped_capability_tags)} distinct values, routed to _uncategorized):")
        for tag, count in sorted(stats.unmapped_capability_tags.items(), key=lambda x: -x[1]):
            print(f'  "{tag}": {count} articles')


def run_all(resource_type_filter: str | None, limit: int | None) -> bool:
    any_failures = False
    ensure_anomalies_report(FILTERED_DIR)
    for key, (resource_type_id, source_type) in RESOURCE_TYPES.items():
        if resource_type_filter and key != resource_type_filter:
            continue
        # A crash partway through one resource type shouldn't cost the
        # other resource type its multi-hour run too -- report what
        # happened and keep going.
        try:
            stats = run_resource_type(fetcher, key, resource_type_id, source_type, limit=limit)
        except fetcher.FirecrawlUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"\n{key} FAILED COMPLETELY: {exc}")
            any_failures = True
            continue
        write_report(source_type, stats)
        print_summary(source_type, stats)
        any_failures = any_failures or bool(stats.fetch_failed) or bool(stats.crawl_errors)
    return any_failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--resource-type", choices=sorted(RESOURCE_TYPES), default=None,
                         help="Only scrape one resource type (default: both howto and troubleshooting).")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only fetch the first N discovered articles per resource type (smaller test run).")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be greater than zero")
    try:
        failed = run_all(args.resource_type, args.limit)
    except fetcher.FirecrawlUnavailable as exc:
        print(f"ERROR: {exc}\n  Set FIRECRAWL_API_URL or start the stack: "
              "cd /path/to/firecrawl && docker compose up -d", file=sys.stderr)
        sys.exit(1)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
