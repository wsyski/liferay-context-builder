#!/usr/bin/env python3
"""Navigation-vs-content heuristic, shared with pipeline.py.

An "index"/navigation page is one whose body (frontmatter stripped) is
short and consists mostly of links to subpages -- no substantial technical
content of its own. Everything else is "content".

Heuristic (no API calls, pure text analysis):
  - Strip Markdown link syntax down to visible text (`[text](url)` -> `text`)
    and strip heading/emphasis markup (`#`, `*`, `_`, backticks).
  - total_words: word count of that visible text.
  - link_ratio: fraction of those words that come from inside a Markdown
    link's link-text span.
  - "index" iff total_words < INDEX_MAX_WORDS and link_ratio >= INDEX_MIN_LINK_RATIO.
"""

import re

INDEX_MAX_WORDS = 150
INDEX_MIN_LINK_RATIO = 0.5

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
MARKUP_RE = re.compile(r"[#*_`\\]")


def analyze_body(body: str) -> tuple[int, float]:
    links = LINK_RE.findall(body)
    link_word_count = sum(len(text.split()) for text, _url in links)

    visible = LINK_RE.sub(lambda m: m.group(1), body)
    visible = MARKUP_RE.sub("", visible)
    total_words = len(visible.split())

    link_ratio = (link_word_count / total_words) if total_words else 0.0
    return total_words, link_ratio


def classify(total_words: int, link_ratio: float) -> str:
    if total_words < INDEX_MAX_WORDS and link_ratio >= INDEX_MIN_LINK_RATIO:
        return "index"
    return "content"
