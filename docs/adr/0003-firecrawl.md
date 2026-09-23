# 0003: Replace crawl4ai with self-hosted Firecrawl

Status: accepted (2026-09-23, fork wsyski/liferay-context-builder)

crawl4ai pulled Playwright + Chromium into every install. This fork runs against a
self-hosted Firecrawl v2 (`FIRECRAWL_API_URL`), which already provides JS rendering.
Only markdown/rawHtml formats are used, so no LLM is required. Output layout is unchanged.
