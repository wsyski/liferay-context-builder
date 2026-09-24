---
name: liferay-expert
description: Answer Liferay DXP/Portal 7.x and quarterly-release questions grounded in local sources — the context library built from learn.liferay.com/w/dxp, and references/liferay-platform.md for platform internals and upgrades. Use when the user asks how something works in Liferay DXP, wants configuration/troubleshooting/upgrade steps, asks about a capability (search, commerce, sites, security, self-hosted, development, cloud, low-code, content management, digital asset management, personalization, integration, AI, getting started), or when the repo has portal-kernel/, service.xml, rest-config.yaml, client-extensions/, or liferay.workspace.product.
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

- **No official Markdown under `$DOCS_DIR/raw/{capability}/`?** Tell the
  user to run the builder once (takes ~20-25 min, hits learn.liferay.com
  directly, needs a running Firecrawl at `$FIRECRAWL_API_URL`):
  ```
  docker compose up -d   # in your Firecrawl checkout, one-time per machine session
  uv run liferay-context-builder
  ```
  Don't launch this yourself mid-conversation — it's long-running and the
  user should choose when to wait for it.
- **Official Markdown exists?** Check a few files' `fetched_at` frontmatter.
  If it's more than ~7 days old, mention the docs may be stale and that
  `uv run liferay-context-builder` refreshes them (still answer with what's
  there — don't block on refreshing).
- If the user wants a one-command local check, tell them:
  ```
  uv run liferay-context-builder-doctor
  ```

## Step 3: search and answer

Use a discover -> read -> answer flow:

1. Pick the likely capability folder(s) from the map below.
2. If `$DOCS_DIR/reports/filtered/search_index.jsonl` exists, search it first:
   ```
   grep -i "<keyword>" $DOCS_DIR/reports/filtered/search_index.jsonl
   ```
   Try 2-3 keyword variants. Prefer entries with `"source_type":"official"`.
   The `path` field points to the Markdown file to read.
3. If the index is missing or thin, fall back to direct Markdown search:
   `grep -ril "<keyword>" $DOCS_DIR/raw/<capability>/*.md`. Ignore any hits
   under `raw/_navigation/` (TOC-only, no unique content).
4. Read the best matching file(s) in full with the Read tool.
5. Answer grounded in what you read. Always cite the source: the file's
   frontmatter `url:` field.
6. **Official docs came up empty or thin, especially for a "how do I..." or
   "getting this error..." question?** Try the community content next:
   `grep -ril "<keyword>" $DOCS_DIR/raw/community-howto/<capability>/*.md
   $DOCS_DIR/raw/community-troubleshooting/<capability>/*.md` (also check
   `.../community-{howto,troubleshooting}/_uncategorized/*.md` — many of
   these articles aren't tagged with a capability at all). If a capability
   isn't obvious, community-troubleshooting articles are usually titled
   after the exact error message, so grepping the error text directly
   works well.
7. Nothing matches anywhere? Say so — try another capability or keyword
   before giving up, don't guess at an answer.

**Citing community content:** always say explicitly that it's community-
contributed, not official Liferay documentation (e.g. "According to a
community How-To article: ..."), still citing the source URL. Prefer an
official-docs answer over a community one when both cover the same
question — the frontmatter's `source_type:` field tells you which is
which (`source_type: community-howto` / `community-troubleshooting` vs.
no `source_type` field at all for official docs).

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

## Reference files

Topic detail beyond the docs lookup lives in files next to this skill. Read the
one that matches the question.

| Topic | File |
|---|---|
| Platform internals: OSGi/DS, Service Builder, REST Builder, Client Extensions | `references/liferay-platform.md` |
| Portal source grounding, API contracts, default behaviour | `references/liferay-platform.md` |
| Version upgrades and breaking changes | `references/liferay-platform.md` |
| Choosing an extension point (7.4+) | `references/liferay-platform.md` |

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
- `raw/community-howto/` and `raw/community-troubleshooting/` are a separate,
  much larger (~4,800 pages) set of community-contributed recipes and
  troubleshooting articles from learn.liferay.com/kb-article/*, fetched by
  `uv run liferay-context-builder-community` (a
  separate, multi-hour command -- not part of the weekly official-docs
  refresh, and not required for this skill to work at all). Same
  capability folder names as the official docs, plus a `_uncategorized/`
  bucket for articles the site itself didn't tag with a capability. Lower
  authority than official docs -- see Step 3.
