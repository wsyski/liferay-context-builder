# TODO

State as of 2026-09-30. Library on disk: 4,842 pages (official 1,661, how-to 1,312,
troubleshooting 1,519, blogs 350), full-text index built, 139 tests passing.

## To finish

- [ ] **Commit and push** the uncommitted work (`git status`): chunked batch scrapes
      (`fetcher.py`), community resume / retry rounds / tag-mapping fix (`community.py`),
      tuned search weights (`skills/liferay-expert/scripts/docs.py`), `evals/search_eval.py`,
      README, tests. Stage `evals/` and this file too.
- [ ] **Refetch the missing troubleshooting articles** (2,084 failed in the last run):
      `uv run liferay-context-builder-community` — resumes, only fetches what is missing,
      about 40 min. It also moves the ~111 articles the old tag mapping left in
      `_uncategorized/`.
- [ ] **Rebuild the indexes** afterwards: `uv run liferay-context-builder --reindex-only`
      (the community stage rebuilds them too, so this is only a check).
- [ ] **Verify** after the refetch:
  - `reports/filtered/community-troubleshooting_summary.json`: `fetch_failed_count` small
    (a few percent at most — pages Firecrawl rendered without the article container are retried).
  - `uv run python evals/search_eval.py` — expect MRR around 0.95 or better.
  - `uv run liferay-context-builder-doctor` — full-text index present.
- [ ] Run `/skill-sync` as the last step (hub `SKILL.md` and `scripts/docs.py` are already
      in step with the repo).

## Follow-ups (not blocking)

- **Official crawl still loses pages.** The last run found 1,727 pages; the crawl missed 650,
  the direct refresh recovered 596, and 56 still failed. A single `/v2/crawl` job cannot be
  split into 100-URL jobs, so try lowering crawl concurrency or raising Firecrawl's
  `MAX_CONCURRENT_JOBS` (currently 5) / worker count, and compare `coverage_gap_count` in
  `reports/filtered/summary.json`. Firecrawl's queue table (`nuq.queue_scrape`, jobs with
  `stalls = 10`) shows the failures.
- **Batch chunk size (100) is from one live test** (291 of 300 articles). Re-check it if
  failures come back; it is `BATCH_CHUNK_SIZE` in `fetcher.py`.
- **`Platform` KB tag** (110 articles) is unmapped and stays in `_uncategorized/`. Map it
  only if you decide which capability it means (`CAPABILITY_TAG_MAP` in `community.py`).
- **Blog capability guesses:** `Frameworks` -> `development` and `Customer Data` ->
  `personalization` are assumptions (`CATEGORY_CAPABILITY` in `blogs.py`).
- **Two eval questions rank low:** `service.xml` (rank 3) and `osgi configuration file`
  (rank 5). Add more real questions to `evals/search_eval.py` as they come up.
- **Blog `tags` are a comma-separated string**, so Obsidian does not treat them as tags;
  only matters if you open `~/.liferay-docs` as a vault (skipped for now: no agent benefit).
- **Version:** `pyproject.toml` is still 0.8.0 — bump if you cut a release.
- **`.idea/`** is untracked; add it to `.gitignore` or leave it out of commits.
