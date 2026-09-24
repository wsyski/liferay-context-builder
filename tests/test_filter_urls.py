from pathlib import Path

from liferay_docs_scraper import filter_urls


def test_resolve_docs_dir_uses_env_override(monkeypatch, tmp_path):
    docs_dir = tmp_path / "custom docs"
    monkeypatch.setenv("LIFERAY_DOCS_DIR", str(docs_dir))

    assert filter_urls.resolve_docs_dir() == docs_dir


def test_resolve_docs_dir_expands_user_in_env(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/tester")
    monkeypatch.setenv("LIFERAY_DOCS_DIR", "~/liferay docs")

    assert filter_urls.resolve_docs_dir() == Path("/Users/tester/liferay docs")


def test_resolve_docs_dir_defaults_to_home_cache(monkeypatch):
    monkeypatch.setattr(filter_urls.Path, "home", staticmethod(lambda: Path("/Users/tester")))
    monkeypatch.delenv("LIFERAY_DOCS_DIR", raising=False)

    assert filter_urls.resolve_docs_dir() == Path("/Users/tester/.liferay-docs")


def test_safe_filename_stem_handles_windows_invalid_chars_reserved_names_and_length():
    assert filter_urls.safe_filename_stem("CON") == "_CON"
    assert filter_urls.safe_filename_stem('<>:"\\|?*') == "--------"

    stem = filter_urls.safe_filename_stem("界" * 200)

    assert len(stem.encode("utf-8")) <= filter_urls.MAX_FILENAME_STEM_BYTES
    assert stem == filter_urls.safe_filename_stem("界" * 200)


def test_slugify_sanitizes_and_caps_filename_stem():
    url = "https://learn.liferay.com/w/dxp/search/" + ("界" * 200)

    slug = filter_urls.slugify(url, "/w/dxp/search")

    assert "/" not in slug
    assert len(slug.encode("utf-8")) <= filter_urls.MAX_FILENAME_STEM_BYTES


def test_frontmatter_quotes_special_characters():
    frontmatter = filter_urls.build_frontmatter(
        'https://learn.liferay.com/w/dxp/search/page?x="quoted"',
        "search",
        "body",
    )

    assert 'url: "https://learn.liferay.com/w/dxp/search/page?x=\\"quoted\\""' in frontmatter


def test_atomic_write_text_replaces_existing_file_and_cleans_temp(tmp_path):
    path = tmp_path / "nested" / "file.md"
    filter_urls.atomic_write_text(path, "old")
    filter_urls.atomic_write_text(path, "new")

    assert path.read_text(encoding="utf-8") == "new"
    assert list(path.parent.glob("*.tmp")) == []


def test_dxp_root_index_is_known_not_unexpected():
    result = filter_urls.classify_url("https://learn.liferay.com/w/dxp/index")

    assert result["capability"] is None
    assert result["known_out_of_scope"] is True


def test_unrecognized_non_capability_url_is_still_flagged():
    result = filter_urls.classify_url("https://learn.liferay.com/w/dxp/some-new-page")

    assert result["capability"] is None
    assert result["known_out_of_scope"] is False


def test_breaking_changes_reference_subpages_are_kept():
    url = ("https://learn.liferay.com/w/dxp/self-hosted-installation-and-upgrades/upgrading-liferay/"
           "deprecations-and-breaking-changes-reference/2025-q1-breaking-changes")
    result = filter_urls.classify_url(url)
    assert result["capability"] == "self-hosted"
    assert result["prune_reason"] is None
