#!/usr/bin/env python3
"""Weekly from-scratch refresh of the learn.liferay.com/w/dxp docs, on Firecrawl.

Builds raw/{capability}/*.md under filter_urls.resolve_docs_dir(): the
$LIFERAY_DOCS_DIR directory if that env var is set, otherwise ~/.liferay-docs
(same on every OS). Deliberately NOT the current working directory -- the
liferay-expert skill looks in that same shared location regardless of
which project you're in when you ask a question, so you don't end up with
a separate copy of the docs per project.

A single Firecrawl /v2/crawl job handles both URL discovery and content
extraction:

  - The job starts at SEED_URL and, scoped by includePaths to
    URL_SCOPE_REGEX, discovers and scrapes every page under /w/dxp/* in one
    pass -- each page's Markdown is already scoped to CONTENT_SELECTOR via
    scrapeOptions.includeTags, so this single job gets us both (a) the
    complete current set of URLs on the site and (b) each page's content, in
    one visit per page. That selector (see below) is precise enough that no
    further chrome-stripping is needed -- what Firecrawl returns is already
    the final page content.
  - Each page is classified with filter_urls.py's classify_url (capability
    prefixes + self-hosted prune rules) and, if in scope, written to
    raw/{capability}/{slug}.md -- unless classify_pages.py's heuristic
    (reused here, not duplicated) says it's a pure navigation/TOC page with
    no substantial content of its own, in which case it goes to
    raw/_navigation/{capability}/{slug}.md instead. This keeps
    raw/{capability}/ as signal for the liferay-expert skill, while still
    preserving the navigation pages (not deleting them) in case they're
    useful later.
  - Because every run starts from zero, a page that existed last run but
    isn't found this run (removed from the site, or now out of scope/pruned)
    is a *candidate* for quarantine -- but crawl discovery can miss a page
    that's still live (no longer linked from anywhere our crawl reached,
    while still resolving directly), so before quarantining anything we do a
    direct HTTP check on each candidate's own URL. Only a confirmed non-200
    gets quarantined (moved to raw/_removed/{capability}/{slug}.md, logged to
    reports/filtered/removed_log.jsonl); anything that still responds, or
    that we simply couldn't reach to check, is left in place and flagged for
    manual review instead. If a capability's discovered count drops
    implausibly (crawl likely failed partway), quarantine for that capability
    is skipped entirely and flagged for manual review instead of trusting a
    possibly-broken run.
  - reports/filtered/{capability}_urls.txt, self-hosted_pruned.txt and
    summary.json are regenerated from this run's live results, so they always
    reflect the current docs (same format filter_urls.py produces).

This tool's only job is fetching and saving -- it does not validate fetched
content quality (see docs/adr/0002-drop-content-validation.md for why, and
the accepted trade-off).

Setup and run (see README.md for the full explanation):
    # one-time: start a self-hosted Firecrawl, and point FIRECRAWL_API_URL at it
    uv run liferay-context-builder             # writes to resolve_docs_dir(), see above
    uv run liferay-context-builder --max-pages 200   # smaller test run
"""

import argparse
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import fetcher
from .classify_pages import analyze_body
from .classify_pages import classify as classify_navigation
from .filter_urls import (
    CAPABILITIES,
    SELF_HOSTED_PRUNE_RULES,
    atomic_write_text,
    build_frontmatter,
    classify_url,
    normalize,
    resolve_docs_dir,
    slugify,
)
from .index import (
    ANOMALIES_NAME,
    OFFICIAL_SOURCE_TYPE,
    append_anomalies,
    build_search_index,
    detect_anomalies,
    read_body_snapshot,
    reset_anomalies_report,
)

ROOT = resolve_docs_dir()
RAW_DIR = ROOT / "raw"
REMOVED_DIR = RAW_DIR / "_removed"
NAVIGATION_DIR = RAW_DIR / "_navigation"
FILTERED_DIR = ROOT / "reports" / "filtered"
REMOVED_LOG = FILTERED_DIR / "removed_log.jsonl"

SEED_URL = "https://learn.liferay.com/w/dxp/index"
# learn.liferay.com's article template puts the breadcrumb, sidebar TOC, and
# the actual article body all inside #main-content, with the maintenance
# banner and global footer outside it. .learn-article-content is scoped
# tighter still: just the title, body, and resource-type tags -- no
# breadcrumb/TOC/"Submit Feedback" chrome to strip afterward.
CONTENT_SELECTOR = ".learn-article-content"

DEFAULT_MAX_DEPTH = 12
DEFAULT_MAX_PAGES = 3000
DEFAULT_PAGE_TIMEOUT_MS = 60_000
# Politeness: Firecrawl serialises a crawl when delay is set.
DEFAULT_CRAWL_DELAY_SECONDS = 1
URL_SCOPE_REGEX = "^/w/dxp(/|$)"
SCRAPE_OPTIONS = {
    "formats": ["markdown"],
    "includeTags": [CONTENT_SELECTOR],
    "onlyMainContent": False,
    "waitFor": 0,
    "maxAge": 0,
    "timeout": DEFAULT_PAGE_TIMEOUT_MS,
}
# If a capability's freshly discovered URL count falls below this fraction of
# its previous count, treat the run as suspect and skip quarantining orphans
# for that capability rather than mass-deleting good content on a bad crawl.
QUARANTINE_SAFETY_RATIO = 0.5

# Print a one-line progress update this often during the crawl. Firecrawl
# job progress is printed by fetcher while polling; this counts processed
# pages.
PROGRESS_EVERY = 50
# No hardcoded fallback here: the crawl discovers the real total as it goes, so
# the only honest "how much is left" estimate is this same docs dir's own
# last run (reports/filtered/summary.json's discovered_total) -- see
# estimate_total_pages(). Only a brand-new docs dir with no prior run has
# no such estimate at all, in which case the progress line just omits the
# percentage rather than guess at a number.


@dataclass
class PageOutcome:
    url: str
    capability: str
    slug: str
    status: str  # "new" | "updated" | "unchanged"
    is_navigation: bool = False


@dataclass
class RunStats:
    discovered_urls: set[str] = field(default_factory=set)
    fetch_failed: list[str] = field(default_factory=list)
    crawl_errors: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    pruned: list[tuple] = field(default_factory=list)
    outcomes: dict = field(default_factory=lambda: {name: [] for name in CAPABILITIES})
    direct_refreshed: list[str] = field(default_factory=list)
    coverage_gap_count: int = 0

    @property
    def discovered_total(self) -> int:
        return len(self.discovered_urls)


@dataclass
class QuarantineResult:
    quarantined: dict[str, list[str]] = field(default_factory=lambda: {name: [] for name in CAPABILITIES})
    still_alive: dict[str, list[str]] = field(default_factory=lambda: {name: [] for name in CAPABILITIES})
    direct_refresh_candidates: dict[str, dict[str, str]] = field(default_factory=lambda: {name: {} for name in CAPABILITIES})
    skipped_capabilities: list[str] = field(default_factory=list)

    def direct_refresh_urls(self) -> list[str]:
        return sorted(url for capability_urls in self.direct_refresh_candidates.values() for url in capability_urls.values())


def read_existing_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("content_hash:"):
                return line.strip()
    return None


def build_crawl_request(max_depth: int, max_pages: int) -> dict:
    return {
        "includePaths": [URL_SCOPE_REGEX],
        "maxDiscoveryDepth": max_depth,
        "limit": max_pages,
        "sitemap": "skip",  # learn.liferay.com child sitemaps return empty bodies (spike 2026-09-23)
        "crawlEntireDomain": True,  # default only follows children of the seed path
        "allowExternalLinks": False,
        "delay": DEFAULT_CRAWL_DELAY_SECONDS,
        "scrapeOptions": SCRAPE_OPTIONS,
    }


def estimate_total_pages() -> int | None:
    """discovered_total from this docs dir's own last run, if any -- the
    only honest basis for a "% done" estimate, since the crawl doesn't know the
    real total until the crawl finishes. None on a docs dir's first-ever
    run (no summary.json yet)."""
    summary_path = FILTERED_DIR / "summary.json"
    if not summary_path.exists():
        return None
    try:
        return json.loads(summary_path.read_text(encoding="utf-8")).get("discovered_total")
    except (json.JSONDecodeError, OSError):
        return None


def process_crawl_result(result, stats: RunStats) -> None:
    url = normalize(result.url)

    if not result.success:
        stats.fetch_failed.append(url)
        return

    stats.discovered_urls.add(url)
    classification = classify_url(url)
    capability = classification["capability"]

    if capability is None:
        if not classification["known_out_of_scope"]:
            stats.unmatched.append(url)
        return

    if classification["prune_reason"] is not None:
        stats.pruned.append((url, classification["prune_reason"]))
        return

    markdown_obj = getattr(result, "markdown", None)
    markdown = getattr(markdown_obj, "raw_markdown", None)
    if not markdown:
        stats.fetch_failed.append(url)
        return

    prefix = CAPABILITIES[capability]
    slug = slugify(url, prefix)

    # Pure navigation/TOC pages (per classify_pages.py's heuristic) go to
    # raw/_navigation/ instead of raw/{capability}/, so the docs a future
    # consultation skill reads stays high-signal.
    total_words, link_ratio = analyze_body(markdown)
    is_navigation = classify_navigation(total_words, link_ratio) == "index"

    content_path = RAW_DIR / capability / f"{slug}.md"
    navigation_path = NAVIGATION_DIR / capability / f"{slug}.md"
    out_path = navigation_path if is_navigation else content_path
    other_path = content_path if is_navigation else navigation_path

    new_content = build_frontmatter(url, capability, markdown) + markdown
    previous_snapshot = read_body_snapshot(out_path) or read_body_snapshot(other_path)
    old_hash_line = read_existing_hash(out_path) or read_existing_hash(other_path)
    existed_before = out_path.exists() or other_path.exists()
    atomic_write_text(out_path, new_content)
    append_anomalies(
        FILTERED_DIR / ANOMALIES_NAME,
        detect_anomalies(
            path=out_path,
            url=url,
            source_type=OFFICIAL_SOURCE_TYPE,
            capability=capability,
            body=markdown,
            previous=previous_snapshot,
        ),
    )
    if other_path.exists():
        other_path.unlink()  # reclassified since last run -- drop the stale copy
    new_hash_line = read_existing_hash(out_path)

    if not existed_before:
        status = "new"
    elif old_hash_line == new_hash_line:
        status = "unchanged"
    else:
        status = "updated"

    stats.outcomes[capability].append(PageOutcome(url, capability, slug, status, is_navigation))


def run_crawl(max_depth: int, max_pages: int) -> RunStats:
    stats = RunStats()
    expected_total = estimate_total_pages()
    start_time = time.monotonic()
    seen = 0

    try:
        for result in fetcher.crawl(SEED_URL, build_crawl_request(max_depth, max_pages)):
            seen += 1
            if seen % PROGRESS_EVERY == 0:
                elapsed_min = (time.monotonic() - start_time) / 60
                rate = seen / elapsed_min if elapsed_min > 0 else 0
                if expected_total:
                    pct = min(100, round(100 * seen / expected_total))
                    progress = f"~{pct}% of the last run -- estimate, not exact"
                else:
                    progress = "first run in this docs dir, no previous estimate"
                print(
                    f"  ...{seen} pages seen ({progress}) -- "
                    f"{elapsed_min:.1f} min elapsed, ~{rate:.0f} pages/min",
                    flush=True,
                )

            try:
                process_crawl_result(result, stats)
            except Exception as exc:  # noqa: BLE001 - one malformed page must not kill the crawl
                url = normalize(getattr(result, "url", "unknown:"))
                print(f"  ERROR processing {url}: {exc}", file=sys.stderr)
                stats.fetch_failed.append(url)
    except fetcher.FirecrawlUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - report partial runs instead of losing them
        stats.crawl_errors.append(str(exc))
        print(f"\nERROR: crawl interrupted before finishing: {exc}", file=sys.stderr)

    return stats


def read_url_from_file(path: Path) -> str | None:
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("url:"):
                return line.strip().removeprefix("url:").strip().strip('"')
    return None


def is_confirmed_gone(url: str, timeout: float = 10.0) -> bool:
    """True only if the URL itself, fetched directly (no crawl involved),
    confirms it's actually gone (404/410). Any other outcome -- 200, a
    different error, a timeout, a network hiccup on our end -- is NOT treated
    as confirmation, since the crawl's link-following can miss pages that are still
    live but just unlinked from wherever our crawl reached this run."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return False  # any successful response means it's still there
    except urllib.error.HTTPError as exc:
        return exc.code in (404, 410)
    except Exception:  # noqa: BLE001 - network errors on our side aren't proof of anything
        return False


def quarantine_orphans(stats: RunStats) -> QuarantineResult:
    """Move raw/{capability}/*.md and raw/_navigation/{capability}/*.md files
    that this run didn't touch to raw/_removed/{capability}/ -- but only
    after directly confirming the URL is actually gone (see
    is_confirmed_gone). Orphans that turn out to still be live, or that we
    couldn't check, are left in place and reported separately so a human
    can look into the crawl's coverage gap."""
    result = QuarantineResult()

    if stats.crawl_errors:
        result.skipped_capabilities = list(CAPABILITIES)
        return result

    for capability in CAPABILITIES:
        content_dir = RAW_DIR / capability
        navigation_dir = NAVIGATION_DIR / capability
        on_disk_paths = {p.stem: p for p in content_dir.glob("*.md")}
        on_disk_paths.update({p.stem: p for p in navigation_dir.glob("*.md")})
        if not on_disk_paths:
            continue

        # Only files untouched by this run's outcomes are orphans.
        current_slugs = {o.slug for o in stats.outcomes[capability]}
        orphans = set(on_disk_paths) - current_slugs

        previous_count = len(on_disk_paths)
        new_count = len(current_slugs)
        if previous_count > 0 and new_count < QUARANTINE_SAFETY_RATIO * previous_count:
            result.skipped_capabilities.append(capability)
            continue

        if not orphans:
            continue

        removed_dir = REMOVED_DIR / capability
        removed_dir.mkdir(parents=True, exist_ok=True)
        removed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for slug in sorted(orphans):
            src = on_disk_paths[slug]
            url = read_url_from_file(src)
            if url is None or not is_confirmed_gone(url):
                result.still_alive[capability].append(slug)
                if url is not None:
                    result.direct_refresh_candidates[capability][slug] = url
                continue

            dst = removed_dir / f"{slug}.md"
            shutil.move(str(src), str(dst))
            result.quarantined[capability].append(slug)
            with REMOVED_LOG.open("a", encoding="utf-8") as log:
                log.write(json.dumps({"capability": capability, "slug": slug, "url": url, "removed_at": removed_at}) + "\n")

    return result


def refresh_still_alive_pages(quarantine_result: QuarantineResult, stats: RunStats) -> None:
    urls = quarantine_result.direct_refresh_urls()
    if not urls:
        return

    stats.coverage_gap_count = len(urls)
    print(f"\nDirectly refreshing live URLs not rediscovered by the crawl: {len(urls)}")
    try:
        for result in fetcher.batch_scrape(urls, SCRAPE_OPTIONS):
            url = normalize(result.url)
            before_failures = len(stats.fetch_failed)
            process_crawl_result(result, stats)
            if len(stats.fetch_failed) == before_failures:
                stats.direct_refreshed.append(url)
    except Exception as exc:  # noqa: BLE001 - preserve the main crawl results
        stats.crawl_errors.append(f"direct refresh failed: {exc}")
        print(f"\nERROR: direct refresh interrupted: {exc}", file=sys.stderr)


def retry_failed_pages(stats: RunStats) -> None:
    urls = sorted(set(stats.fetch_failed))
    if not urls:
        return
    print(f"\nRetrying {len(urls)} failed or empty pages once...", flush=True)
    stats.fetch_failed = []
    processed = set()
    try:
        for result in fetcher.batch_scrape(urls, SCRAPE_OPTIONS):
            processed.add(normalize(result.url))
            try:
                process_crawl_result(result, stats)
            except Exception as exc:  # noqa: BLE001 - one malformed page must not kill the retry pass
                url = normalize(getattr(result, "url", "unknown:"))
                print(f"  ERROR processing {url}: {exc}", file=sys.stderr)
                stats.fetch_failed.append(url)
    except Exception as exc:  # noqa: BLE001 - keep unprocessed originals as failures
        stats.fetch_failed.extend(url for url in urls if url not in processed)
        print(f"ERROR: retry pass failed: {exc}", file=sys.stderr)


def write_filtered_reports(stats: RunStats) -> None:
    FILTERED_DIR.mkdir(parents=True, exist_ok=True)

    for capability in CAPABILITIES:
        urls = sorted(o.url for o in stats.outcomes[capability])
        atomic_write_text(FILTERED_DIR / f"{capability}_urls.txt", "\n".join(urls) + "\n")

    pruned_lines = [f"{url}\t# {reason}" for url, reason in sorted(stats.pruned)]
    atomic_write_text(FILTERED_DIR / "self-hosted_pruned.txt", "\n".join(pruned_lines) + ("\n" if pruned_lines else ""))

    navigation_urls = sorted(
        o.url for outcomes in stats.outcomes.values() for o in outcomes if o.is_navigation
    )
    atomic_write_text(FILTERED_DIR / "navigation_urls.txt", "\n".join(navigation_urls) + ("\n" if navigation_urls else ""))

    prune_counts = {label: 0 for label, _ in SELF_HOSTED_PRUNE_RULES}
    for _, reason in stats.pruned:
        prune_counts[reason] += 1

    summary = {
        "capabilities": {
            name: {
                "unique_urls": len(stats.outcomes[name]),
                "navigation_pages": sum(1 for o in stats.outcomes[name] if o.is_navigation),
            } for name in CAPABILITIES
        },
        "self_hosted_pruned": {"total": len(stats.pruned), "by_rule": prune_counts},
        "total_in_scope": sum(len(stats.outcomes[name]) for name in CAPABILITIES),
        "total_navigation_pages": len(navigation_urls),
        "discovered_total": stats.discovered_total,
        "coverage_gap_count": stats.coverage_gap_count,
        "direct_refreshed_count": len(stats.direct_refreshed),
        "fetch_failed_count": len(stats.fetch_failed),
        "crawl_error_count": len(stats.crawl_errors),
        "crawl_errors": stats.crawl_errors,
        "unmatched_count": len(stats.unmatched),
        "search_index_entries": build_search_index(RAW_DIR, FILTERED_DIR),
    }
    atomic_write_text(FILTERED_DIR / "summary.json", json.dumps(summary, indent=2) + "\n")


def print_summary(stats: RunStats, quarantine_result: QuarantineResult) -> None:
    print(f"\nTotal discovered under /w/dxp: {stats.discovered_total}")
    if stats.crawl_errors:
        print(f"Fatal crawl errors: {len(stats.crawl_errors)}")
        for error in stats.crawl_errors:
            print(f"  - {error}")

    if stats.fetch_failed:
        print(f"Fetch failures: {len(stats.fetch_failed)}")
        for url in stats.fetch_failed:
            print(f"  - {url}")

    print("\nBy capability (new / updated / unchanged / navigation):")
    total_in_scope = 0
    total_navigation = 0
    for capability, outcomes in stats.outcomes.items():
        new = sum(1 for o in outcomes if o.status == "new")
        updated = sum(1 for o in outcomes if o.status == "updated")
        unchanged = sum(1 for o in outcomes if o.status == "unchanged")
        navigation = sum(1 for o in outcomes if o.is_navigation)
        total_in_scope += len(outcomes)
        total_navigation += navigation
        print(f"  {capability:12s}: {len(outcomes):4d} total  "
              f"({new} new, {updated} updated, {unchanged} unchanged, {navigation} navigation)")
    print(f"\nTotal in scope: {total_in_scope} ({total_navigation} in raw/_navigation/, "
          f"{total_in_scope - total_navigation} in raw/{{capability}}/)")

    print(f"\nSelf-hosted pruned: {len(stats.pruned)}")

    quarantined = quarantine_result.quarantined
    total_quarantined = sum(len(v) for v in quarantined.values())
    print(f"\nQuarantined (URL verified as gone, HTTP 404/410): {total_quarantined}")
    for capability, slugs in quarantined.items():
        if slugs:
            print(f"  {capability}: {len(slugs)}")
            for slug in slugs:
                print(f"    - {slug}")

    still_alive = quarantine_result.still_alive
    total_still_alive = sum(len(v) for v in still_alive.values())
    if total_still_alive:
        print(f"\nNot rediscovered by the crawl but STILL LIVE: {total_still_alive}")
        for capability, slugs in still_alive.items():
            if slugs:
                print(f"  {capability}: {len(slugs)}")
                for slug in slugs:
                    print(f"    - {slug}")

    if stats.direct_refreshed:
        print(f"\nDirectly refreshed after confirming they were still live: {len(stats.direct_refreshed)}")

    if quarantine_result.skipped_capabilities:
        print("\nWARNING: quarantine skipped due to a suspicious count drop "
              "(possible incomplete crawl), review manually:")
        for capability in quarantine_result.skipped_capabilities:
            print(f"  - {capability}")

    if stats.unmatched:
        print(f"\nUnexpected URLs (neither in scope nor known pruned URLs), {len(stats.unmatched)}:")
        for url in stats.unmatched:
            print(f"  {url}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    args = parser.parse_args()

    expected_total = estimate_total_pages()
    size_hint = f"~{expected_total} pages last time" if expected_total else "~20-25 min usually"
    print(f"Starting crawl ({size_hint}) -- progress every {PROGRESS_EVERY} pages...", flush=True)
    reset_anomalies_report(FILTERED_DIR)
    try:
        stats = run_crawl(args.max_depth, args.max_pages)
    except fetcher.FirecrawlUnavailable as exc:
        print(f"ERROR: {exc}\n  Set FIRECRAWL_API_URL or start the stack: "
              "cd /path/to/firecrawl && docker compose up -d", file=sys.stderr)
        sys.exit(1)
    if not stats.crawl_errors:
        retry_failed_pages(stats)
    quarantine_result = quarantine_orphans(stats)
    refresh_still_alive_pages(quarantine_result, stats)
    write_filtered_reports(stats)
    print_summary(stats, quarantine_result)

    if stats.fetch_failed or stats.crawl_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
