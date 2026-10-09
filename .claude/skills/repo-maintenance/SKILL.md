---
name: repo-maintenance
description: The traps, reference pages and decision records of looker-sync, indexed by symptom. Use before debugging behaviour that is wrong but not crashing, before changing the fake Looker in the tests, and whenever a debugging session cost more than an hour.
---

# Traps and reference

Scan the tables below by the symptom you see, then open exactly one page. Each page
is self-contained.

<!-- BEGIN GENERATED INDEX -- edit the pages, not this block -->

## Traps

Failure modes that produce no error message, indexed by the symptom you
would observe. Read before debugging behaviour that is wrong but not
crashing.

| symptom | page | area | verified |
|---|---|---|---|
| a changed file shows git_status action add, so it looks like a new file | [MODIFIED_FILE_SHOWS_ACTION_ADD](../../../docs/traps/MODIFIED_FILE_SHOWS_ACTION_ADD.md) | file list / git_status | 2026-10-09 |
| unit tests pass but push fails against real Looker with HTTP 400 No such file or directory | [TESTS_PASS_BUT_PUSH_FAILS_ON_THE_REAL_LOOKER](../../../docs/traps/TESTS_PASS_BUT_PUSH_FAILS_ON_THE_REAL_LOOKER.md) | tests / fake Looker | 2026-10-09 |

## Reference

Simply true, and expensive to re-derive.

| page | summary | verified |
|---|---|---|
| [Looker file and branch API behaviour (live-verified)](../../../docs/reference/looker-file-api-behaviour.md) | How the Looker API answers for new folders, directories and the shared dev checkout, as seen on a live instance | 2026-10-09 |

## Decisions

Why the repo is the way it is. A merged decision is immutable -- supersede
it with a new one rather than editing it.

- [The tool writes files and never touches git state](../../../docs/adr/0001-read-and-write-files-only.md)
- [No commit command; ship on PyPI with the skill inside](../../../docs/adr/0002-no-commit-command-and-uvx-distribution.md)

<!-- END GENERATED INDEX -->

## Where a lesson goes

Type error, then test, then a comment at the site, then a doc. A doc is the last resort.
A fact with a symptom is a trap in `docs/traps/`; a fact that is simply true is a page in
`docs/reference/`; a hard-to-reverse choice is a record in `docs/adr/`. Verify against
running code, date the page `verified:`, then run `scripts/gen-docs-index.sh`.
