# liferay-context-builder

Builds a local, cited copy of the Liferay DXP documentation from
`learn.liferay.com` so a coding agent can read the real docs before answering
Liferay questions.

- Official docs (`learn.liferay.com/w/dxp/*`, ~2,000 pages) and, optionally,
  community How-To and Troubleshooting articles (`/kb-article/*`, ~4,800) and
  recent community blog posts (`liferay.dev/blogs`, 2022 onward).
- Plain Markdown files with source URL and fetch time in the frontmatter, plus a
  JSONL search index. No vector database, no embeddings, no bundled Liferay
  content.
- Fetching goes through a self-hosted [Firecrawl](https://github.com/firecrawl/firecrawl)
  v2 instance, using only its markdown and rawHtml formats (no LLM needed).
- Refreshing is manual: run the builder when you want fresh docs.
- Ships the `liferay-expert` agent skill: it searches the library, cites it, and
  (via `references/liferay-platform.md`) covers platform internals and upgrades.

Python 3.10-3.13 · [MIT license](LICENSE) · fork of
[mordonez/liferay-context-builder](https://github.com/mordonez/liferay-context-builder)

## Quickstart

```bash
# 1. Start Firecrawl (in your Firecrawl checkout) and wait until it is live
docker compose up -d
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3002/v0/health/liveness   # -> 200

# 2. Build the whole library into ~/.liferay-docs (~1.5 h): official docs, then
#    community KB articles, then liferay.dev blog posts
cd /path/to/liferay-context-builder
uv run liferay-context-builder
#    official docs only (~20-25 min):
#    uv run liferay-context-builder --official-only

# 3. Check the result
uv run liferay-context-builder-doctor

# 4. Stop Firecrawl
cd /path/to/firecrawl && docker compose stop
```

The PyPI package of the same name is the upstream project and does not use
Firecrawl; run these commands from a checkout of this repository with `uv run`.

## Requirements

- Python 3.10-3.13 and [`uv`](https://docs.astral.sh/uv/)
- A running self-hosted Firecrawl v2 API:

| Variable | Default | Purpose |
|---|---|---|
| `FIRECRAWL_API_URL` | `http://localhost:3002` | Firecrawl base URL |
| `FIRECRAWL_API_KEY` | unset | Sent as `Authorization: Bearer <key>` when set |
| `LIFERAY_DOCS_DIR` | `~/.liferay-docs` | Where the library is written and read |

```bash
# Example: Firecrawl on another host, library inside the current repo
export FIRECRAWL_API_URL=http://build-box:3002
export LIFERAY_DOCS_DIR="$PWD/.liferay-docs"
uv run liferay-context-builder
```

## How It Works

```mermaid
flowchart LR
  A[learn.liferay.com] --> B[Firecrawl /v2/crawl and /v2/batch/scrape]
  B --> C[Markdown in ~/.liferay-docs/raw]
  C --> D[reports/filtered: search_index.jsonl, summary.json, anomalies.jsonl]
  C --> E[liferay-expert skill]
  D --> E
  E --> F[answers citing learn.liferay.com URLs]
```

**Official docs** (`liferay-context-builder`):

1. One Firecrawl crawl job starts at `https://learn.liferay.com/w/dxp/index` and
   follows links:
   `includePaths: ["^/w/dxp(/|$)"]`, `crawlEntireDomain: true`,
   `sitemap: "skip"`, `maxDiscoveryDepth: 12`, `limit: 3000`, `delay: 1`.
   The site's child sitemaps return empty bodies, so discovery relies on links.
2. Each page is scraped with `includeTags: [".learn-article-content"]`, which
   returns the article body without navigation, banners or cookie dialogs.
3. The URL prefix decides the capability folder; pure table-of-contents pages go
   to `raw/_navigation/`.
4. Pages that failed or came back empty are re-scraped once in a batch job.
5. Files from the previous run that were not rediscovered are checked directly:
   still live → refreshed; HTTP 404/410 → moved to `raw/_removed/`.
6. Reports and the search index are regenerated.

**Community articles** (`liferay-context-builder-community`): pages through the
server-rendered search listing (`/learn-search?resource-type=...`, 60 links per
page), batch-scrapes each article as raw HTML, extracts title, tags and
`.knowledge-article-content`, converts it with `markdownify`, and retries
missed articles once.

**Community blog posts** (`liferay-context-builder-blogs`): pages through
`liferay.dev/blogs?delta=20&start=<page>` (newest first) until a page has
nothing at or after `--since` (default 2022-01-01), then scrapes each post
as raw HTML and keeps the article body. Listing pages are scraped one at a
time, 2 s apart; the posts go through one `/v2/batch/scrape` job, and misses
are retried once. Posts categorised `News` (release announcements, webinars, events)
are skipped unless `--include-news` is given, and posts already on disk are
skipped unless `--refresh` is given or the site's Atom feed (latest 20 posts)
shows they were edited after they were fetched. Each post is filed under the
capability its first mappable site category names (`AI`, `Cloud`, `CMS`,
`Commerce`, `Customer Data`, `Frameworks`, `Integration`, `Low-Code`,
`Security`, `Sites`); the rest, about 80%, go to `_uncategorized/`, and a
re-run moves posts whose categories changed without refetching them. The site
returns 403 for deep listing
pages; that ends discovery early, keeps what was found, and exits 1.

**One command builds everything.** `liferay-context-builder` runs the official
crawl, then the community KB articles, then the blog posts, and exits 1 if any
stage reported a failure. A stage that fails does not skip the next one.
`--official-only` stops after the official docs. The community and blog stages
also have their own commands (see the Command Reference), and the flags `--max-depth`
and `--max-pages` apply to the official stage only.

**Failure behaviour**

- Firecrawl unreachable → one-line error naming `FIRECRAWL_API_URL`, exit 1.
- A Firecrawl job with no status for 3 polls, or no progress for 10 minutes →
  crawl error, no pages are quarantined, exit 1.
- Any page still failing after the retry is listed and the run exits 1; pages
  already written stay valid.

## Command Reference

```bash
uv run liferay-context-builder                      # everything: official docs, community KB articles, blogs
uv run liferay-context-builder --official-only      # official docs only (~20-25 min)
uv run liferay-context-builder --official-only --max-pages 30   # quick smoke run
uv run liferay-context-builder --official-only --max-depth 12 --max-pages 3000

# the two extra stages also run on their own:
uv run liferay-context-builder-community                              # How-To + Troubleshooting
uv run liferay-context-builder-community --resource-type howto        # one type
uv run liferay-context-builder-community --resource-type troubleshooting --limit 100

uv run liferay-context-builder-blogs                                  # posts since 2022-01-01, no News
uv run liferay-context-builder-blogs --since 2024-01-01 --limit 20    # smaller test run
uv run liferay-context-builder-blogs --include-news --refresh         # everything, re-fetch existing

uv run liferay-context-builder-doctor                                 # status of docs + skill
uv run liferay-context-builder-doctor --project-dir /path/to/project
```

Example output of a smoke run:

```text
$ uv run liferay-context-builder --max-pages 30
Starting crawl (~20-25 min usually) -- progress every 50 pages...

Total discovered under /w/dxp: 30

By capability (new / updated / unchanged / navigation):
  cloud       :    4 total  (4 new, 0 updated, 0 unchanged, 1 navigation)
  self-hosted :    5 total  (5 new, 0 updated, 0 unchanged, 0 navigation)
  sites       :    4 total  (4 new, 0 updated, 0 unchanged, 0 navigation)
  ...
Total in scope: 29 (4 in raw/_navigation/, 25 in raw/{capability}/)
Quarantined (URL verified as gone, HTTP 404/410): 0
```

```text
$ uv run liferay-context-builder-community --resource-type howto --limit 10
Discovered: 10
Written: 10
Fetch failures: 0
By capability:
  _uncategorized: 5
  content-management-system: 3
  digital-asset-management: 1
  sites: 1
```

Timings: official docs ~20-25 minutes on a warm Firecrawl stack (the first crawl
after a cold `docker compose up` is noticeably slower); community ~1 hour;
blogs a few minutes for a first run, seconds for a re-run that only
finds posts already on disk.
The skill flags docs older than about 7 days, so a weekly refresh is plenty.

## The Library

```text
~/.liferay-docs/
  raw/{capability}/*.md                      official docs (read first)
  raw/_navigation/{capability}/*.md          table-of-contents pages
  raw/_removed/{capability}/*.md             pages confirmed gone (404/410)
  raw/community-howto/{capability}/*.md
  raw/community-troubleshooting/{capability}/*.md
  raw/community-blog/{capability}/*.md       blog posts, with published_at
  reports/filtered/
    search_index.jsonl                       one JSON line per page
    summary.json                             counts of the last run
    anomalies.jsonl                          short/odd pages worth a check
    {capability}_urls.txt                    in-scope URLs per capability
    removed_log.jsonl                        quarantine log
```

Capabilities: `search`, `commerce`, `development`, `sites`, `low-code`,
`security`, `self-hosted`, `content-management-system`, `integration`, `cloud`,
`digital-asset-management`, `personalization`, `ai`, `getting-started`.
Community articles and blog posts without a usable capability go to `_uncategorized/`.

Official page, e.g. `raw/self-hosted/cloud-native-experience-cne-kubernetes-ready.md`:

```markdown
---
url: "https://learn.liferay.com/w/dxp/self-hosted-installation-and-upgrades/cloud-native-experience/cne-kubernetes-ready"
capability: self-hosted
fetched_at: "2026-09-23T15:58:04Z"
content_hash: "sha256:201204a2871c69a8e5fdc2ca71d3abc6f1734087bba4b9d60d681091110e7b4c"
---
[Cloud Native Experience Kubernetes Ready](https://learn.liferay.com/w/dxp/...)
=====

The Kubernetes Ready path of the Cloud Native Experience (CNE) deploys Liferay DXP
on any CNCF-conformant Kubernetes cluster using the official `liferay-default` Helm chart. ...
```

Community article, e.g. `raw/community-howto/_uncategorized/auditing-the-remote-client-ip-address-changed-after-upgrade.md`:

```markdown
---
url: "https://learn.liferay.com/kb-article/auditing-the-remote-client-ip-address-changed-after-upgrade"
source_type: community-howto
capability: uncategorized
deployment_approach: "Liferay Self-Hosted"
applicable_versions: "DXP 7.4, DXP 7.0"
resource_type: "How To"
fetched_at: "2026-09-23T16:00:48Z"
content_hash: "sha256:30a53478..."
---
# Auditing the remote client IP address changed after upgrade

## Issue

* After upgrading from Liferay 7.0 to a more recent Quarterly Release ...
```

Blog post, e.g. `raw/community-blog/_uncategorized/cookie-consent-management.md`:

```markdown
---
url: "https://liferay.dev/b/cookie-consent-management"
source_type: community-blog
capability: uncategorized
author: "David H Nebinger"
published_at: "2026-09-23"
categories: "Featured"
tags: "cookies, cookie management, cmp"
fetched_at: "2026-09-29T20:34:28Z"
content_hash: "sha256:529a66a0..."
---
# Cookie Consent Management
```

The search index has one JSON line per page, official docs first, then community
How-To, Troubleshooting and blogs. Besides `title`, `url`, `source_type`,
`capability`, `path`, `headings` and `fetched_at`, each line has a one-line
`summary`, and community pages add `published_at`, `author`, `tags`,
`categories` and `applicable_versions` when the page has them, so an agent can
judge relevance and recency from a grep hit without opening the file. Any run
regenerates it.

Search index line (`reports/filtered/search_index.jsonl`, shortened):

```json
{"title": "Cloud Native Experience Cne Kubernetes Ready", "url": "https://learn.liferay.com/w/dxp/self-hosted-installation-and-upgrades/cloud-native-experience/cne-kubernetes-ready", "source_type": "official", "capability": "self-hosted", "path": "raw/self-hosted/cloud-native-experience-cne-kubernetes-ready.md", "headings": [], "fetched_at": "2026-09-23T15:58:04Z"}
```

Searching it by hand:

```bash
cd ~/.liferay-docs
grep -i "synonym" reports/filtered/search_index.jsonl | head        # shortlist by title/headings/summary
grep -i "mcp" reports/filtered/search_index.jsonl | grep community-blog \
  | jq -r '[.published_at,.title,.path]|@tsv'                        # compact blog hits with dates
grep -ril "client extension" raw/development/ | head                # full-text in one capability
grep -ril "ClassNotFoundException" raw/community-troubleshooting/   # error text -> troubleshooting
jq '{discovered_total, fetch_failed_count}' reports/filtered/summary.json
```

## The Skill

`skills/liferay-expert/SKILL.md` teaches an agent to find the library
(`$LIFERAY_DOCS_DIR`, else `~/.liferay-docs`), shortlist via the search index,
read the matching Markdown, and cite the frontmatter `url`. Community articles
are labelled as community content and official docs win when both cover a
topic. Blog posts are indexed in `search_index.jsonl` (`source_type:
community-blog`) but the skill has no blog-specific routing or citation rule. A Reference-files table routes platform questions to
`references/liferay-platform.md` (OSGi/DS, Service Builder, REST Builder,
Client Extensions, extension-point choice, and the version-upgrade /
breaking-changes workflow), so the docs lookup stays focused. The skill also ships
`scripts/docs.py` (standard library only), which the agent runs for bounded
lookups: `status` (is the library there and fresh), `search` (AND of terms,
filter by source, capability or date, ranked compact hits), and `outline` /
`section` (read one part of a large page). Without `python3` the skill falls
back to plain `grep`. The skill never
starts a build itself; when docs are missing or older than ~7 days it tells you
which command to run.

Install it into a Claude Code project (copy the whole skill directory, so
`references/` comes with it):

```bash
npx skills add wsyski/liferay-context-builder --skill liferay-expert -a claude-code
# or copy it manually
mkdir -p .claude/skills/liferay-expert && cp -r /path/to/liferay-context-builder/skills/liferay-expert/. .claude/skills/liferay-expert/
```

Example questions it answers from the library:

> How do I configure synonym sets in Liferay Search?
>
> Which Helm chart does the Cloud Native Experience Kubernetes path use?
>
> After upgrading to a quarterly release the audit table stores a different client IP — why?

Platform questions it answers from `references/liferay-platform.md`:

> Which extension point should a custom workflow action use in 7.4+?
>
> What changed in an API between 7.4 GA3 and the 2026.Q2 release?

## Doctor

```text
$ uv run liferay-context-builder-doctor
Docs dir: ~/.liferay-docs
Official docs: OK (25 markdown files, 30 discovered in last report)
Community docs: 10 markdown files
Official freshness: 2026-09-23 .. 2026-09-23
Search index: 35 entries
Anomalies report: 23 entries
Claude Code skill: MISSING (/path/to/project/.claude/skills/liferay-expert/SKILL.md)

Next steps:
  npx skills add wsyski/liferay-context-builder --skill liferay-expert -a claude-code
```

It reports the active docs directory, official and community file counts, the
freshness window, index and anomaly counts, and whether the skill is installed
in the project. It never builds or installs anything.

## Troubleshooting

**`ERROR: Firecrawl not reachable at http://localhost:3002`** — start Firecrawl
(`docker compose up -d` in its checkout) or point `FIRECRAWL_API_URL` at the
running instance.

**`crawl job ... stalled` or `status unavailable`** — the Firecrawl job stopped
progressing (worker crash, stack restart). Check `docker compose logs api` in
the Firecrawl checkout, then rerun.

**Blogs run reports `listing page N: ...` under crawl errors** — liferay.dev
blocked or emptied a listing page (403 on deep pages, or a layout change).
Posts found before that page were still fetched; rerun later or lower the
range with `--since`.

**Run ends with fetch failures** — the listed pages failed twice. Rerun later;
everything already written stays valid and nothing is quarantined on a failed
crawl.

**Agent says docs are missing** — check `echo "$LIFERAY_DOCS_DIR"`; the builder
and the skill must use the same directory. Run the doctor.

**Docs are stale** — rerun `uv run liferay-context-builder`.

## Development

```bash
uv sync --group dev
uv run --group dev pytest -q
uv run --group dev ruff check src tests
uv build
```

Tests mock the Firecrawl API; no network access is needed. CI runs lint, tests
and a package build on Python 3.10-3.13. Design decisions are in
[`docs/adr/`](docs/adr/).

## License

[MIT](LICENSE) applies to this tool and skill only. Liferay documentation
content remains Liferay's and is fetched locally by each user.
