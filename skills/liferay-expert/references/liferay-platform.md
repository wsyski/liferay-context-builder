# Liferay Platform Reference

Source-grounded guidance for Liferay DXP/Portal 7.x and quarterly releases:
extension points, portal internals, and version upgrades. Cite what you read
— a portal file as `file:line`, a doc as its frontmatter `url:`. Don't answer
from memory when a source can settle it.

The scraped docs corpus (how-to, configuration, admin UI, concepts) is covered
by the parent `SKILL.md`; this file is the platform/source side.

## Pick the source

| Question kind | Source |
|---|---|
| API contract, extension point, "which interface/service do I override", default behaviour | A portal source checkout for the target version |
| Upgrade / "what changed between X and Y" | `readme/BREAKING_CHANGES*` and `readme/BREAKING_CHANGES_AMENDMENTS*` in the **target** checkout; then the same class/file diffed across checkouts |
| How-to, configuration, admin UI, concepts | Docs corpus (parent `SKILL.md`) |
| Error message / symptom | Docs corpus community-troubleshooting (parent `SKILL.md`), then source |
| Working example of a module type | A local `liferay-blade-samples` checkout |
| Frontend tooling (liferay-npm-*, JS toolkit) | A local `liferay-frontend-projects` checkout |

## Portal source and version selection

- Keep one checkout per release line, keyed by its release tag (e.g.
  `7.4.3.129-ga129`, `2026.q2.0`). Never mix facts from two checkouts unless the
  task compares versions — they are separate release lines, not one branch advanced.
- Before treating something as platform behaviour, confirm the checkout's age:
  `git -C <checkout> log -1 --format=%cs`. A checkout may be older than the
  quarterly lines it is meant to represent.
- Search literal/known names with `grep -rn` / `git grep` scoped to a subtree
  (`portal-kernel/src`, `portal-impl/src`, `modules/apps/<app>`). Intent-level
  search: `semble search "<query>" <checkout>/<subtree>` — always scope to a
  subtree, since indexing a whole checkout is slow. Anything beyond a quick
  confirm returns large output: delegate it to a helper and keep only the
  distilled result.

## Upgrade workflow

1. Identify source and target versions (workspace `liferay.workspace.product`,
   bundle, or ask).
2. In the target checkout, grep `readme/BREAKING_CHANGES*` for every
   API/class/taglib/property the project uses
   (`grep -n "<ClassName>\|<property>" readme/BREAKING_CHANGES*`); each entry has
   What changed / Who is affected / How to update / Why.
3. No matching entry is a result, not a failure: say so explicitly, then compare
   the relevant module or tooling directly between the two checkouts (e.g.
   `diff -r <old>/modules/apps/<app> <new>/modules/apps/<app> | head`, or the
   bnd/`build.gradle` files involved) before concluding nothing changed.
4. Cross-check the docs corpus `self-hosted` breaking-changes reference pages.
5. For each hit, open the class in both checkouts
   (`diff <old>/<path> <new>/<path>`) before prescribing a fix.
6. Report per affected project file: entry cited, required change, confidence.

## Choosing the extension point (7.4+)

1. **Liferay Object** — new business entity without complex Java logic.
2. **Client Extension** — custom UI, remote apps, theme CSS/JS, workflow/object
   actions calling external services.
3. **REST Builder** — typed OpenAPI contract over existing domain data.
4. **OSGi module** (`@Component`) — portal internals override, custom service
   impl, listeners, indexers.
5. **Service Builder** — persistable entities needing custom SQL, joins, heavy
   transactions.

Pre-7.4 or portal-core work: skip Objects and Client Extensions.

## OSGi / DS

- `@Component` for services; `@Reference`, not `ServiceTracker`, unless binding
  must be dynamic.
- Interface in `-api`, implementation in `-impl`/`-service`; never implementation
  in `-api`.
- `@Activate`/`@Modified`/`@Deactivate` for configuration-aware components.
- Look in `portal-kernel` for an existing SPI before writing one.

## Service Builder

- `service.xml` is the source of truth; never hand-edit generated output.
- Regenerate with `buildService` from the `*-service` module.
- Editable: `*LocalServiceImpl`, `*ServiceImpl`, `*ModelImpl` (model hints only).
- Finders in `service.xml`; complex queries in `*FinderImpl` (custom SQL /
  dynamic query).
- `*ServiceImpl` is the permission-checked remote face; `*LocalServiceImpl` is
  the implementation.

## REST Builder / headless

- `rest-config.yaml` + OpenAPI YAML are the source; regenerate with `buildREST`;
  implement only in `*ResourceImpl`.
- `portal-vulcan` for `Page<T>`, pagination, `EntityModel` filters.
- Extend existing APIs via headless-delivery extension points or
  `EntityExtensionHandler` before raw JAX-RS.
- New integrations use headless REST `/o/...`, never `/api/jsonws`.

## Client Extensions

- Config: `client-extension.yaml` (workspace) /
  `*.client-extension-config.json` (built).
- Types include `customElement`, `globalCSS`, `globalJS`, `themeCSS`, `iframe`,
  `objectAction`, `workflowAction`, `oAuthApplicationUserAgent`,
  `oAuthApplicationHeadlessServer` — verify the exact set for the target version
  in the docs corpus `development` folder.
- Mutating calls from a CX need an OAuth2 scope or `p_auth`; prefer headless APIs
  over DB access.

## What NOT to do

- No implementation in `-api` modules; no edits to generated Service/REST Builder
  output.
- No `PortalUtil.getBean()`; no `*LocalServiceUtil` from remote-facing endpoints
  (bypasses permissions).
- No new Service Builder entity for data an Object can model.
- No `blade gw` in a portal fork.
