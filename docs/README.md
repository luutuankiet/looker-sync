# Documentation

Every page here is written for a maintainer six months from now who opened
exactly this file from a search result and has nothing else loaded.

This index is generated. Run `scripts/gen-docs-index.sh` after adding or
renaming a page; `--check` fails if it is stale.

<!-- BEGIN GENERATED INDEX -- edit the pages, not this block -->

## Where things live

One page per area of the system. Read before going looking for where
something is implemented.

| page | covers | verified |
|---|---|---|
| [Sync model and guards](architecture/sync-model-and-guards.md) | how pull, push, status and switch decide what to write, which guard refuses what, and where in looker_sync.py each lives | 2026-10-09 |

## Traps

Failure modes that produce no error message, indexed by the symptom you
would observe. Read before debugging behaviour that is wrong but not
crashing.

| symptom | page | area | verified |
|---|---|---|---|
| a changed file shows git_status action add, so it looks like a new file | [MODIFIED_FILE_SHOWS_ACTION_ADD](traps/MODIFIED_FILE_SHOWS_ACTION_ADD.md) | file list / git_status | 2026-10-09 |
| unit tests pass but push fails against real Looker with HTTP 400 No such file or directory | [TESTS_PASS_BUT_PUSH_FAILS_ON_THE_REAL_LOOKER](traps/TESTS_PASS_BUT_PUSH_FAILS_ON_THE_REAL_LOOKER.md) | tests / fake Looker | 2026-10-09 |

## Reference

Simply true, and expensive to re-derive.

| page | summary | verified |
|---|---|---|
| [Looker file and branch API behaviour (live-verified)](reference/looker-file-api-behaviour.md) | How the Looker API answers for new folders, directories and the shared dev checkout, as seen on a live instance | 2026-10-09 |

## Decisions

Why the repo is the way it is. A merged decision is immutable -- supersede
it with a new one rather than editing it.

- [The tool writes files and never touches git state](adr/0001-read-and-write-files-only.md)
- [No commit command; ship on PyPI with the skill inside](adr/0002-no-commit-command-and-uvx-distribution.md)

<!-- END GENERATED INDEX -->
