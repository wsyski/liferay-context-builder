import pytest

from liferay_docs_scraper import fetcher


def doc(url, status=200, markdown="body", raw_html=None):
    d = {"metadata": {"sourceURL": url, "statusCode": status}, "markdown": markdown}
    if raw_html is not None:
        d["rawHtml"] = raw_html
    return d


class FakeApi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: None)


def test_to_page_marks_non_2xx_as_failed():
    page = fetcher.to_page(doc("https://x/a", status=404))
    assert page.success is False
    assert page.url == "https://x/a"


def test_to_page_exposes_legacy_shaped_markdown():
    page = fetcher.to_page(doc("https://x/a", markdown="# T"))
    assert page.success is True
    assert page.markdown.raw_markdown == "# T"


def test_scrape_returns_failed_page_when_api_reports_failure(monkeypatch):
    monkeypatch.setattr(fetcher, "_request", FakeApi([{"success": False, "error": "timeout"}]))
    page = fetcher.scrape("https://x/a", {"formats": ["markdown"]})
    assert page.success is False and page.url == "https://x/a"


def test_crawl_polls_until_completed_and_follows_next(monkeypatch):
    api = FakeApi(
        [
            {"success": True, "id": "j1"},
            {"status": "scraping", "completed": 1, "total": 3, "data": []},
            {
                "status": "completed",
                "completed": 3,
                "total": 3,
                "data": [doc("https://x/a")],
                "next": "http://localhost:3002/v2/crawl/j1?skip=1",
            },
            {"status": "completed", "data": [doc("https://x/b"), doc("https://x/c")], "next": None},
            {"errors": [], "robotsBlocked": []},
        ]
    )
    monkeypatch.setattr(fetcher, "_request", api)

    urls = [p.url for p in fetcher.crawl("https://x/", {"limit": 3})]

    assert urls == ["https://x/a", "https://x/b", "https://x/c"]
    assert api.calls[0] == ("POST", "/v2/crawl", {"url": "https://x/", "limit": 3})
    assert api.calls[3][1] == "http://localhost:3002/v2/crawl/j1?skip=1"
    assert api.calls[4][1] == "/v2/crawl/j1/errors"


def test_crawl_raises_on_failed_job(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "_request",
        FakeApi(
            [
                {"success": True, "id": "j1"},
                {"status": "failed", "error": "boom"},
            ]
        ),
    )
    with pytest.raises(RuntimeError, match="crawl job j1 ended failed"):
        list(fetcher.crawl("https://x/", {}))


def test_crawl_yields_partial_data_before_raising_on_failed_job(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "_request",
        FakeApi(
            [
                {"success": True, "id": "j1"},
                {"status": "failed", "error": "boom", "data": [doc("https://x/a")], "next": None},
            ]
        ),
    )
    seen = []

    with pytest.raises(RuntimeError, match="crawl job j1 ended failed"):
        for page in fetcher.crawl("https://x/", {}):
            seen.append(page.url)

    assert seen == ["https://x/a"]


def test_crawl_yields_pages_listed_in_errors_endpoint_as_failed(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "_request",
        FakeApi(
            [
                {"success": True, "id": "j1"},
                {"status": "completed", "data": [doc("https://x/a")], "next": None},
                {
                    "errors": [
                        {"url": "https://x/timeout", "error": "timed out"},
                        {"url": "https://x/a", "error": "earlier attempt failed"},
                        {"url": "https://other.example/ext", "code": "EXTERNAL_LINK"},
                        {"url": "https://x/ext", "code": "EXTERNAL_LINK"},
                        {"url": "https://x/timeout", "error": "duplicate"},
                    ],
                    "robotsBlocked": ["https://x/robots"],
                },
            ]
        ),
    )

    pages = {p.url: p.success for p in fetcher.crawl("https://x/", {})}

    assert pages == {"https://x/a": True, "https://x/timeout": False}


def test_crawl_survives_unreadable_errors_endpoint(monkeypatch, capsys):
    monkeypatch.setattr(
        fetcher,
        "_request",
        FakeApi(
            [
                {"success": True, "id": "j1"},
                {"status": "completed", "data": [doc("https://x/a")], "next": None},
                {"success": False, "httpStatus": 404},
            ]
        ),
    )

    assert [p.url for p in fetcher.crawl("https://x/", {})] == ["https://x/a"]
    assert "could not read crawl j1 errors" in capsys.readouterr().err


def test_collect_raises_after_consecutive_status_unavailable(monkeypatch):
    api = FakeApi(
        [{"success": True, "id": "j1"}]
        + [{"success": False, "httpStatus": 500}] * fetcher.MAX_MISSING_STATUS_POLLS
    )
    monkeypatch.setattr(fetcher, "_request", api)
    with pytest.raises(RuntimeError, match=r"crawl job j1 status unavailable"):
        list(fetcher.crawl("https://x/", {}))


def test_collect_tolerates_a_transient_status_outage(monkeypatch):
    api = FakeApi(
        [
            {"success": True, "id": "j1"},
            {"success": False, "httpStatus": 502},
            {"status": "completed", "data": [doc("https://x/a")], "next": None},
            {"errors": []},
        ]
    )
    monkeypatch.setattr(fetcher, "_request", api)

    assert [p.url for p in fetcher.crawl("https://x/", {})] == ["https://x/a"]


def test_request_turns_html_error_body_and_timeout_into_failed_calls(monkeypatch):
    import io
    import urllib.error

    def raise_http(*args, **kwargs):
        raise urllib.error.HTTPError("http://x", 502, "Bad Gateway", {}, io.BytesIO(b"<html>bad gateway</html>"))

    monkeypatch.setattr(fetcher.urllib.request, "urlopen", raise_http)
    assert fetcher._request("GET", "/v2/crawl/j1") == {"success": False, "httpStatus": 502}

    def raise_timeout(*args, **kwargs):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(fetcher.urllib.request, "urlopen", raise_timeout)
    assert fetcher._request("GET", "/v2/crawl/j1") == {"success": False, "httpStatus": 0}


def test_collect_raises_when_stalled_past_timeout(monkeypatch):
    api = FakeApi(
        [
            {"success": True, "id": "j1"},
            {"status": "scraping", "completed": 1, "total": 3, "data": []},
            {"status": "scraping", "completed": 1, "total": 3, "data": []},
        ]
    )
    monkeypatch.setattr(fetcher, "_request", api)
    clock = iter([0.0, 0.0, fetcher.STALL_TIMEOUT_SECONDS + 1])
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match=r"crawl job j1 stalled at 1/3"):
        list(fetcher.crawl("https://x/", {}))


def test_batch_scrape_reports_missing_urls_as_failed(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "_request",
        FakeApi(
            [
                {"success": True, "id": "b1"},
                {"status": "completed", "data": [doc("https://x/a")], "next": None},
            ]
        ),
    )
    pages = {p.url: p.success for p in fetcher.batch_scrape(["https://x/a", "https://x/b"], {})}
    assert pages == {"https://x/a": True, "https://x/b": False}


def test_batch_scrape_matches_requested_url_with_trailing_slash(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "_request",
        FakeApi(
            [
                {"success": True, "id": "b1"},
                {"status": "completed", "data": [doc("https://x/a")], "next": None},
            ]
        ),
    )
    pages = {p.url: p.success for p in fetcher.batch_scrape(["https://x/a/"], {})}
    assert pages == {"https://x/a": True}


def test_batch_scrape_matches_requested_url_percent_encoded(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "_request",
        FakeApi(
            [
                {"success": True, "id": "b1"},
                {"status": "completed", "data": [doc("https://x/a b")], "next": None},
            ]
        ),
    )
    pages = {p.url: p.success for p in fetcher.batch_scrape(["https://x/a%20b"], {})}
    assert pages == {"https://x/a b": True}


def test_crawl_skips_documents_with_no_resolved_url(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "_request",
        FakeApi(
            [
                {"success": True, "id": "j1"},
                {"status": "completed", "data": [{"markdown": "body"}], "next": None},
                {"errors": []},
            ]
        ),
    )
    pages = list(fetcher.crawl("https://x/", {}))
    assert pages == []
