# looker-sync

A standard-library Python CLI that moves LookML between a local folder and your own Looker
dev workspace: `pull`, `status`, `push` (byte-for-byte read-back, then validation) and
`switch`. It never commits, deploys, resets or creates branches. Usage is in
[README.md](README.md) and the `looker-sync` skill.

## Hard constraints

- **Standard library only.** No dependencies, no install step.
- **Never write in prod mode, never delete without `--delete`, never send `ref` to the
  branch endpoint** (it hard-resets and force-pushes).
- **Credentials come only from the env file** (`LOOKER_URL`, `LOOKER_CLIENT_ID`,
  `LOOKER_CLIENT_SECRET`). Never print them and never commit one.
- **Test against a sandbox Looker project**, never a project someone depends on.

## Layout

```
scripts/looker_sync.py        the CLI
scripts/test_looker_sync.py   tests against a fake Looker (subprocess, no network)
scripts/gen-docs-index.sh     regenerates the docs index
docs/                         architecture, traps, reference, adr; indexed in docs/README.md
tmp/YYYYMMDD/<topic>/         scratch and evidence, gitignored
```

## Checks

`python3 -m unittest discover -s scripts -p 'test_looker_sync.py'`. If you change what the
fake Looker answers, confirm it against a live instance first: a fake that is kinder than
the real API makes tests pass for behaviour that does not exist.

<!-- The appendable seam. Everything ABOVE this comment belongs to the project and
     is never touched on a re-run; everything below is the standard block. Append
     it to whatever AGENTS.md already says. Never replace. The literal string
     "Standard block." on the first line is what check 2 of audit.sh looks for. -->

<!-- Standard block. Everything above belongs to this project; everything below is
     the pointer every repo laid out this way carries. -->

## Documentation

Indexed in [docs/README.md](docs/README.md). Every page is self-contained.

| where | what | read it |
|---|---|---|
| [architecture/](docs/architecture/) | where behaviour lives | before going looking for something |
| [traps/](docs/traps/) | failure modes with no error, by symptom | before debugging something wrong but not crashing |
| [reference/](docs/reference/) | simply true, expensive to re-derive | when you need the detail |
| [adr/](docs/adr/) | why it is the way it is | before changing something that looks odd |

## Before you wrap up

1. Sort what you learned: next action is an issue, a durable fact is a page, a hard-to-reverse
   choice is a decision record. Status, dates and plans are none of those.
2. A doc is the last resort: type error, test, comment at the site, then doc.
3. Verify against running code and date the page `verified:`.
4. Append, never rewrite. Then run `scripts/gen-docs-index.sh`.
