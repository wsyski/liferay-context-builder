#!/usr/bin/env python3
"""Shared URL/capability utilities for the docs pipeline.

Capability classification (matching learn.liferay.com/w/dxp URLs to one of
the 14 capabilities listed on /w/dxp/index, plus the self-hosted prune
rules), the URL->filename/frontmatter helpers used when writing pages to
raw/{capability}/*.md, and resolve_docs_dir() -- the one place that decides
where that raw/ docs folder actually lives on disk.
"""

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

CAPABILITIES = {
    "cloud": "/w/dxp/cloud",
    "search": "/w/dxp/search",
    "self-hosted": "/w/dxp/self-hosted-installation-and-upgrades",
    "sites": "/w/dxp/sites",
    "security": "/w/dxp/security-and-administration",
    "development": "/w/dxp/development",
    "commerce": "/w/dxp/commerce",
    "personalization": "/w/dxp/personalization",
    "low-code": "/w/dxp/low-code",
    "content-management-system": "/w/dxp/content-management-system",
    "digital-asset-management": "/w/dxp/digital-asset-management",
    "integration": "/w/dxp/integration",
    "ai": "/w/dxp/ai",
    "getting-started": "/w/dxp/getting-started",
}

# All 14 capabilities listed on https://learn.liferay.com/w/dxp/index are in
# scope now; nothing under /w/dxp is deliberately excluded anymore.
OUT_OF_SCOPE_PREFIXES: list[str] = []

# In-scope-but-not-a-capability paths: the /w/dxp root index is the capability
# listing itself, so it has no capability bucket. The crawl starts there (see
# pipeline.SEED_URL), so it's rediscovered every run and must be recognised as
# expected rather than reported as an unexpected URL.
NON_CAPABILITY_PATHS = frozenset({"/w/dxp/index"})

# (rule label, substring whose presence -- followed by more path -- excludes the URL)
SELF_HOSTED_PRUNE_RULES = [
    (
        "installing-earlier-liferay-versions-on-application-servers subpage",
        "/installing-earlier-liferay-versions-on-application-servers/",
    ),
    (
        "cne-aws-ready subpage",
        "/cloud-native-experience/cne-cloud-provider-ready/cne-aws-ready/",
    ),
    (
        "cne-gcp-ready subpage",
        "/cloud-native-experience/cne-cloud-provider-ready/cne-gcp-ready/",
    ),
]

MAX_FILENAME_STEM_BYTES = 150
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
INVALID_FILENAME_CHARS = set('<>:"\\|?*')


def normalize(url: str) -> str:
    """Strip a trailing slash from the path, keep everything else as-is."""
    parsed = urlparse(url)
    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return parsed._replace(path=path).geturl()


def matches_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def prune_reason(path: str) -> str | None:
    for label, substr in SELF_HOSTED_PRUNE_RULES:
        if substr in path:
            return label
    return None


def classify_url(url: str) -> dict:
    """Classify a single (already-normalized) URL for the capability pipeline.

    Returns a dict with:
      - capability: matched capability name, or None if out of scope
      - prune_reason: self-hosted prune rule label, or None
      - known_out_of_scope: True if it's a known-excluded capability or a
        known non-capability page (the /w/dxp root index) rather than an
        unrecognized/"odd" URL worth flagging for manual review
    """
    path = urlparse(url).path
    matched_capability = None
    for name, prefix in CAPABILITIES.items():
        if matches_prefix(path, prefix):
            matched_capability = name
            break

    if matched_capability is None:
        known_out_of_scope = path in NON_CAPABILITY_PATHS or any(
            matches_prefix(path, prefix) for prefix in OUT_OF_SCOPE_PREFIXES
        )
        return {"capability": None, "prune_reason": None, "known_out_of_scope": known_out_of_scope}

    reason = prune_reason(path) if matched_capability == "self-hosted" else None
    return {"capability": matched_capability, "prune_reason": reason, "known_out_of_scope": False}


def slugify(url: str, prefix: str) -> str:
    """URL path (with the capability prefix stripped) -> a flat filename stem."""
    path = urlparse(url).path
    remainder = path[len(prefix):].strip("/")
    if not remainder:
        return "index"
    return safe_filename_stem(remainder.replace("/", "-"))


def quote_frontmatter_value(value: str) -> str:
    """Double-quote a scalar safely for the simple YAML frontmatter we write."""
    return json.dumps(value, ensure_ascii=False)


def safe_filename_stem(value: str) -> str:
    """Return a cross-platform filename stem, capped below common byte limits."""
    cleaned = "".join(
        "-" if char in INVALID_FILENAME_CHARS or ord(char) < 32 else char
        for char in value
    ).strip(" .")
    if not cleaned:
        cleaned = "index"
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    encoded = cleaned.encode("utf-8")
    if len(encoded) <= MAX_FILENAME_STEM_BYTES:
        return cleaned

    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:10]
    prefix_bytes = MAX_FILENAME_STEM_BYTES - len(digest) - 1
    truncated = encoded[:prefix_bytes].decode("utf-8", errors="ignore").strip(" .")
    if not truncated:
        truncated = "index"
    return f"{truncated}-{digest}"


def atomic_write_text(path: Path, content: str) -> None:
    """Write UTF-8 text atomically in the target directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name:
            with suppress(FileNotFoundError):
                os.unlink(temp_name)


def build_frontmatter(url: str, capability: str, markdown: str) -> str:
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    lines = [
        "---",
        f"url: {quote_frontmatter_value(url)}",
        f"capability: {capability}",
        f"fetched_at: {quote_frontmatter_value(fetched_at)}",
        f"content_hash: {quote_frontmatter_value(f'sha256:{content_hash}')}",
        "---",
        "",
    ]
    return "\n".join(lines)


def resolve_docs_dir() -> Path:
    """Where the local docs (raw/, reports/filtered/) live: $LIFERAY_DOCS_DIR
    if set, otherwise ~/.liferay-docs. The same folder regardless of which
    OS you're on or which project you're running the scraper or the skill
    from -- deliberately not a platform-specific app-data convention (e.g.
    macOS's Application Support, Windows' LOCALAPPDATA), since that adds
    complexity a single-purpose tool cache doesn't need. The dot-prefix
    (not a bare ~/liferay-docs) matches the usual convention for
    tool-managed data dirs (~/.cache, ~/.npm, ~/.cargo, ...) so it doesn't
    clutter a plain `ls ~`, while staying one simple, uniform path."""
    override = os.environ.get("LIFERAY_DOCS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".liferay-docs"
