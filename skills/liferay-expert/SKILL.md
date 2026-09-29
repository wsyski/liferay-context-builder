---
name: liferay-expert
description: Ground Liferay DXP/Portal 7.x answers in local docs, portal source and samples, with citations. Use for any Liferay question or code: client extensions, OSGi, Service Builder, REST Builder, upgrades, config.
---

# Liferay Expert

Ground every Liferay DXP answer in the local docs — don't answer from
memory alone when this skill applies. Find the actual doc, read it, cite it.

## Step 1: find the docs

The docs live in one shared location, not in whatever project you're
currently in (so it isn't duplicated per-project). Resolve it once per
conversation:

- If `$LIFERAY_DOCS_DIR` is set, use that.
- Otherwise use the default: `~/.liferay-docs` (same name on macOS, Linux, and
  Windows -- the leading dot only actually hides it from a plain listing on
  macOS/Linux; on Windows it's just part of the folder name).

Call this `$DOCS_DIR` below. It should contain Markdown files under
`raw/{capability}/`.

## Step 2: is it there, and is it fresh?

`docs.py` below means `python3 <directory of this SKILL.md>/scripts/docs.py`
(standard library only). Run:

```
docs.py status
```

It prints the docs dir, the page count per source, fetch dates, and flags
official docs older than ~7 days as STALE.

- **"No search index" or no official pages?** Tell the user to run the builder
  once (takes ~20-25 min, hits learn.liferay.com directly, needs a running
  Firecrawl at `$FIRECRAWL_API_URL`):
  ```
  docker compose up -d   # in your Firecrawl checkout, one-time per machine session
  uv run liferay-context-builder
  ```
  Don't launch this yourself mid-conversation — it's long-running and the
  user should choose when to wait for it.
- **STALE?** Mention the docs may be out of date and that
  `uv run liferay-context-builder` refreshes them (still answer with what's
  there — don't block on refreshing).
- No `python3`? Check that `$DOCS_DIR/raw/{capability}/` has Markdown and look
  at a few files' `fetched_at` frontmatter instead.
- For a one-command local check of the whole setup, tell the user:
  `uv run liferay-context-builder-doctor`

## Step 3: search and answer

Use a discover -> read -> answer flow:

1. Pick the likely capability folder(s) from the map below.
2. Search the index. Every term must match; hits are ranked (title and heading
   matches first, then official > how-to > troubleshooting > blog, newest
   first) and capped at 15:
   ```
   docs.py search "<term>" "<term2>" [--source official|howto|troubleshooting|blog] [--capability <folder>] [--since 2024]
   ```
   Each hit shows source, capability, date, `path` (the file to read) and a
   one-line summary — judge relevance and recency from that before opening
   anything. Try 2-3 keyword variants; the docs use product terms ("client
   extension", "object"), not everyday synonyms. The source is
   `$DOCS_DIR/reports/filtered/search_index.jsonl`.
   - **No python3?** `grep -i "<keyword>" $DOCS_DIR/reports/filtered/search_index.jsonl | head -30`
     (cap it: a common term matches hundreds of ~1 KB lines), then
     `| grep community-blog` etc. to narrow by source.
   - **Index missing or thin?** `grep -ril "<keyword>" $DOCS_DIR/raw/<capability>/*.md`.
     Ignore hits under `raw/_navigation/` (TOC-only, no unique content).
3. Read the best match with the Read tool. Most pages are small; ~1 in 15
   exceeds 20 KB and a few exceed 100 KB (the upgrade breaking-changes pages
   reach 260 KB). Check first with `docs.py outline <path>` (size, headings
   with line numbers), then take just the part you need:
   `docs.py section <path> "<heading>"`. A page with no headings (the
   breaking-changes lists are per-class entries): `grep -n -i "<term>" <file>`,
   then Read with `offset`/`limit`.
4. Answer grounded in what you read. Always cite the source: the file's
   frontmatter `url:` field.
5. **Official docs came up empty or thin, especially for a "how do I..." or
   "getting this error..." question?** Search the community content:
   `docs.py search "<error text or task>" --source troubleshooting` (those
   articles are usually titled after the exact error message) or
   `--source howto`. Many of these aren't tagged with a capability, so don't
   filter by `--capability` first.
6. **Still nothing, or asking about a recent feature, a design rationale or a
   worked walkthrough?** Search the blog posts: `--source blog`, add
   `--since <year>` to skip old ones. Prefer the newest post; one older than
   the user's version may be outdated.
7. Nothing matches anywhere? Say so — try another capability or keyword
   before giving up, don't guess at an answer.

**Citing community content:** always say explicitly that it's community-
contributed, not official Liferay documentation (e.g. "According to a
community How-To article: ..."), still citing the source URL. Prefer an
official-docs answer over a community one when both cover the same
question — the frontmatter's `source_type:` field tells you which is
which (`source_type: community-howto` / `community-troubleshooting` vs.
no `source_type` field at all for official docs).

**Citing blog posts** (`source_type: community-blog`): lowest authority. Say
it's a community blog post, give the author and `published_at` (e.g. "A
community blog post by X from March 2024 says: ..."), and cite the URL. When a
blog post disagrees with the official docs or a KB article, go with the docs
and mention the conflict.

## Capability map

| Folder | Covers |
|---|---|
| `search` | Elasticsearch/OpenSearch/Solr, indexing, reindexing modes, search blueprints, facets, semantic search |
| `commerce` | Storefronts, pricing, orders, inventory, product catalogs, payments |
| `development` | Client extensions, service builder, APIs, theming, traditional Java dev, tooling |
| `sites` | Pages, site settings, page fragments, navigation, content pages |
| `low-code` | Objects, forms, workflow |
| `security` | Administration, permissions, users, SSO, virtual instances |
| `self-hosted` | Installation, upgrades, cloud-native experience (CNE), JVM tuning |
| `content-management-system` | Web content, blogs, documents, translations, tags/categories |
| `integration` | Headless/REST APIs, OAuth2, webhooks |
| `cloud` | Liferay Cloud/PaaS config, networking, scaling, migration |
| `digital-asset-management` | Documents and media, DAM DevOps, AI image generation |
| `personalization` | Segmentation, experiences (A/B testing), Analytics Cloud |
| `ai` | AI integrations (only 2 pages) |
| `getting-started` | Onboarding, Docker quick start, basic navigation |

When the right capability isn't obvious, grep 2-3 likely candidates rather
than guessing one and stopping.

## Community content

Not official documentation — always label it as community content (see
citing rules above). Same folder layout as `raw/`, one source per folder:

| Folder | Covers | Use when |
|---|---|---|
| `raw/community-howto/{capability}/` (`_uncategorized/` for untagged) | KB how-to recipes (learn.liferay.com/kb-article), ~1,400 | a "how do I..." the official docs don't answer |
| `raw/community-troubleshooting/{capability}/` | KB troubleshooting entries, ~3,700, titled after the error | an error message or symptom |
| `raw/community-blog/{capability}/` (`_uncategorized/` when no site category maps; most are) | liferay.dev blog posts from 2022 on, no announcements, with `published_at`, `author`, `tags` | recent features, design rationale, walkthroughs |

They come from optional, separate builder commands
(`uv run liferay-context-builder-community`, `uv run liferay-context-builder-blogs`);
this skill works without them. If `docs.py status` shows none, don't ask for
them unless official docs can't answer.

## Reference files

Topic detail beyond the docs lookup lives in files next to this skill. Read the
one that matches the question.

| Topic | File |
|---|---|
| Platform internals: OSGi/DS, Service Builder, REST Builder, Client Extensions | `references/liferay-platform.md` |
| Portal source grounding, API contracts, default behaviour | `references/liferay-platform.md` |
| Version upgrades and breaking changes | `references/liferay-platform.md` |
| Choosing an extension point (7.4+) | `references/liferay-platform.md` |
| Host-local portal checkouts, sample projects, vault, corpus path (only if this file exists) | `references/liferay-platform-local.md` |

## Notes

- All paths above (`raw/...`, `reports/...`) are relative to `$DOCS_DIR` from
  Step 1, not the current project directory.
- `liferay-context-builder-doctor` checks whether official Markdown exists in
  the shared docs directory and whether this project has
  `.claude/skills/liferay-expert/SKILL.md`; it never builds docs or installs
  anything.
- `raw/_navigation/{capability}/` = TOC-only pages, excluded on purpose —
  skip them, their linked subpages exist as real files elsewhere.
- `raw/_removed/{capability}/` = pages no longer on the live site. Only use
  as a last resort, and say explicitly that the source may be outdated.
- The docs refresh on demand via `uv run liferay-context-builder` (rerun weekly if
  you want to stay current); each file's `fetched_at` frontmatter tells you how
  current it is.
- `reports/filtered/{capability}_urls.txt` lists every in-scope URL per
  capability if you need to browse available topics without grepping content.
- `reports/filtered/search_index.jsonl` is a generated local retrieval index
  for titles, headings, source type, capability, file path, source URL, and
  freshness. Use it as the first shortlist when it exists.
- `reports/filtered/anomalies.jsonl` is an informational build-quality report
  (short bodies, missing titles, suspicious size changes, known error markers).
  If you cite a page listed there, mention the local copy may need checking.
