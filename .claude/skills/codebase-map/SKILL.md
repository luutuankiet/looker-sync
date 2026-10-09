---
name: codebase-map
description: Where behaviour lives in looker-sync: which file owns the sync rules, the guards, the Looker API calls and the tests. Use before hunting for where something is implemented or before changing a guard.
---

# Where things live

Open the one page for your area, then go straight to the file. Line numbers are a
starting point, not an address; confirm by what the code says.

<!-- BEGIN GENERATED INDEX -- edit the pages, not this block -->

## Where things live

One page per area of the system. Read before going looking for where
something is implemented.

| page | covers | verified |
|---|---|---|
| [Sync model and guards](../../../docs/architecture/sync-model-and-guards.md) | how pull, push, status and switch decide what to write, which guard refuses what, and where in looker_sync.py each lives | 2026-10-09 |

<!-- END GENERATED INDEX -->

The same table, browsable, is [docs/README.md](../../../docs/README.md). To add a page,
follow the `repo-maintenance` skill, then run `scripts/gen-docs-index.sh`.
