"""Minimal client for a self-hosted Firecrawl v2 API (stdlib only).

Only markdown/rawHtml formats are used -- never json/extract -- so no LLM is
involved on the Firecrawl side.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass

API_URL = os.environ.get("FIRECRAWL_API_URL", "http://localhost:3002").rstrip("/")
API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
POLL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 180
STALL_TIMEOUT_SECONDS = 600
TERMINAL_STATES = {"completed", "failed", "cancelled"}


class FirecrawlUnavailable(RuntimeError):
    pass


@dataclass
class Markdown:
    raw_markdown: str


@dataclass
class Page:
    # Same attribute names as the previous crawler's result type, so callers written against it keep working.
    url: str
    success: bool
    markdown: Markdown | None
    html: str | None


def _request(method: str, path_or_url: str, payload: dict | None = None) -> dict:
    url = path_or_url if path_or_url.startswith("http") else f"{API_URL}{path_or_url}"
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read() or b"{}") | {"success": False, "httpStatus": exc.code}
    except urllib.error.URLError as exc:
        raise FirecrawlUnavailable(f"Firecrawl not reachable at {API_URL}: {exc.reason}") from exc


def to_page(doc: dict, fallback_url: str = "") -> Page:
    meta = doc.get("metadata") or {}
    url = meta.get("sourceURL") or meta.get("url") or fallback_url
    status = meta.get("statusCode") or 0
    markdown = doc.get("markdown")
    html = doc.get("rawHtml") or doc.get("html")
    success = 200 <= status < 300 and bool(markdown or html)
    return Page(url, success, Markdown(markdown) if markdown else None, html)


def scrape(url: str, options: dict) -> Page:
    body = _request("POST", "/v2/scrape", {"url": url, **options})
    if not body.get("success"):
        return Page(url, False, None, None)
    return to_page(body["data"], url)


def _collect(kind: str, job_id: str) -> Iterator[dict]:
    last_reported = -1
    missing_status_streak = 0
    best_completed = -1
    stall_since = time.monotonic()
    while True:
        status = _request("GET", f"/v2/{kind}/{job_id}")
        state = status.get("status")
        if state is None:
            missing_status_streak += 1
            if missing_status_streak >= 3:
                raise RuntimeError(f"{kind.split('/')[0]} job {job_id} status unavailable: {status}")
            time.sleep(POLL_SECONDS)
            continue
        missing_status_streak = 0
        if state in TERMINAL_STATES:
            break
        completed = status.get("completed", 0)
        if completed > best_completed:
            best_completed = completed
            stall_since = time.monotonic()
        elif time.monotonic() - stall_since > STALL_TIMEOUT_SECONDS:
            raise RuntimeError(f"{kind.split('/')[0]} job {job_id} stalled at {completed}/{status.get('total', '?')}")
        if completed // 50 != last_reported // 50:
            print(f"  ...{completed}/{status.get('total', '?')} pages", flush=True, file=sys.stderr)
            last_reported = completed
        time.sleep(POLL_SECONDS)
    if state != "completed":
        raise RuntimeError(f"{kind.split('/')[0]} job {job_id} ended {state}: {status.get('error', '')}")
    yield from status.get("data", [])
    next_url = status.get("next")
    while next_url:
        chunk = _request("GET", next_url)
        yield from chunk.get("data", [])
        next_url = chunk.get("next")


def _start(path: str, payload: dict) -> str:
    body = _request("POST", path, payload)
    if not body.get("success") or "id" not in body:
        raise RuntimeError(f"Firecrawl rejected {path}: {body.get('error', body)}")
    return body["id"]


def _match_key(url: str) -> str:
    return urllib.parse.unquote(url).rstrip("/")


def crawl(seed_url: str, request: dict) -> Iterator[Page]:
    job_id = _start("/v2/crawl", {"url": seed_url, **request})
    for doc in _collect("crawl", job_id):
        page = to_page(doc)
        if not page.url:
            continue
        yield page


def batch_scrape(urls: list[str], options: dict) -> Iterator[Page]:
    job_id = _start("/v2/batch/scrape", {"urls": urls, **options})
    seen = set()
    for doc in _collect("batch/scrape", job_id):
        page = to_page(doc)
        if not page.url:
            continue
        seen.add(_match_key(page.url))
        yield page
    for url in urls:
        if _match_key(url) not in seen:
            yield Page(url, False, None, None)
