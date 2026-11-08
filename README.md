# looker-sync

Move LookML between a local folder and your own Looker dev workspace, with verification.
Python 3.9+, standard library only, nothing to install beyond [uv](https://docs.astral.sh/uv/).

```
uvx looker-sync init --project <name>
```

## Quick start

1. In an empty folder, run `uvx looker-sync init --project <your Looker project id>`.
2. It writes a `.env` template and adds `.env` and `.looker-sync/` to `.gitignore`, then stops
   with exit code 2. Fill in the three values below.
3. Run the same `init` command again. It writes `.looker-sync/config.json` and pulls the
   project files into the folder.
4. Edit the LookML, then `uvx looker-sync push --dry-run` and `uvx looker-sync push`.

`.env` holds an API key that belongs to **your own** Looker user:

```
LOOKER_URL=https://<your-instance>.looker.com
LOOKER_CLIENT_ID=...
LOOKER_CLIENT_SECRET=...
```

Create the key in Looker under Admin, Users, your user, Edit Keys. The tool looks for the
env file in this order: `--env-file`, the `LOOKER_SYNC_ENV` variable, the `env_file` in
`.looker-sync/config.json`, then `.env` in the project folder. Credentials are never printed
and never written anywhere else.

## Commands

| command | what it does |
|---|---|
| `init [--project P] [--mode dev\|prod] [--branch B] [--git-remote NAME \| --no-git]` | set up a folder (it may be empty), then a first pull. `--no-pull` writes metadata only; `--force` takes Looker's copy over files already there. In a git clone it links the folder to a remote (asks on a terminal, else tries `origin`); `--no-git` skips |
| `link [--remote NAME]` | link an existing folder to a git remote of its clone (default `origin`), so git and Looker stay on the same branch |
| `pull [--force] [--dry-run]` | copy Looker files to disk; never deletes local files |
| `status` | show what differs on each side, and how far the dev branch is ahead of and behind its remote; writes nothing |
| `push [--force] [--delete] [--dry-run] [--no-validate] [--verbose]` | write local changes, read each written file back, then validate. It reads from Looker only the files it will write or delete. `--no-validate` skips validation (the read-back still runs). `--verbose` times each step on stderr and shows info-severity validation lines, which are otherwise hidden and counted |
| `watch [--validate] [--verbose] [--poll MS] [--force]` | push every save as it happens, until Ctrl-C. See below |
| `switch (--branch B \| --mode dev\|prod) [--force]` | move Looker and the folder to another branch or mode. `--force` = back up local changes and confirm. In a linked folder it also stashes a dirty tree, checks out B in git and fast-forwards it to Looker's commit; it never pops the stash |
| `skill [--path]` | print the full usage guide written for agents |

Global options: `-C <project folder>` (default: the current folder) and `--env-file <path>`.
`--help` is the short reference; `looker-sync skill` is the long one.

Exit codes: 0 ok, 1 Looker or network error, 2 refused by a guard, 3 validation errors.

## Watch

`looker-sync watch` watches the folder and writes each save to your dev workspace, usually in
under a second. It runs until Ctrl-C.

| flag | what it does |
|---|---|
| `--validate` | validate in Looker after writes go quiet (2 s). Off by default |
| `--verbose` | show info-severity validation lines |
| `--poll MS` | scan interval in ms, default 50, at least 10 |
| `--force` | overwrite files changed in Looker since the last sync |

Each event is one line: time, kind, then the file or the reason.

| line | meaning |
|---|---|
| `ack` | Looker accepted the write |
| `verified` | the read-back matched the bytes sent, and the file is recorded as synced |
| `MISMATCH` | Looker holds different bytes than were sent; check it in the Looker IDE |
| `held` | not written, with the reason; the watch carries on |
| `released` | an earlier hold no longer applies |
| `paused` / `resumed` | nothing is written until the reason clears (another branch in Looker, session refused, project folder gone) |
| `notice` / `error` | information, or a failure that is being retried |

- **A syntax error holds everything.** One `.lkml` file that does not parse holds every pending
  file until it is fixed; the line names the file and the reason. `.dashboard.lookml` files are
  not checked. One-shot `push` runs no syntax check, so it is the way past a hold you disagree
  with.
- **A file changed in Looker is held,** never overwritten, unless you pass `--force`. Run `pull`
  to take Looker's copy. A file deleted in Looker is held the same way.
- **It never deletes.** A local delete or a moved file is reported and left for
  `push --delete`. A new file that looks like a copy of another is held too.
- **Other holds:** a git operation in progress, a file still being written after 3 s, and more
  than 200 changed files at once (run `push` for those).
- **Branch checks run every 5 s, in the background.** A branch switched in the Looker IDE between
  two checks can take a write; the next check prints an `error` line and pauses.

`watch` exits 0 on Ctrl-C, 2 if refused at start (prod mode, read-only branch, wrong branch) and
1 if Looker cannot be reached at start. Errors after that pause or retry; they do not exit.

## What it guarantees

- `push` writes your changes, reads every file back and compares bytes, then runs LookML
  validation and prints `file:line  message`.
- It refuses to overwrite a file edited in Looker since your last sync (`--force` to override).
- `pull` never deletes local files and refuses to overwrite unpushed local work.
- `push` never deletes files in Looker without `--delete`, and never writes in prod mode.
- A push that stops partway records what it wrote; the next push retries only the rest.

## What it does not do

It never commits, pushes to git, deploys, resets or creates branches. In a linked folder it only reads, fetches, stashes, checks out and fast-forwards local git; `status` warns and `pull`/`push` refuse when git is on another branch, printing the `switch` command that fixes it. Looker's API has no
commit or push endpoint, and the official Looker VS Code extension commits through local git
too. Pushed files show up in the dev workspace as uncommitted changes; commit them from the
Looker IDE or your own git checkout of the remote.

## One key, one branch at a time

The checked-out dev branch belongs to the Looker user, not to an API token. A second client on
another branch is refused by a branch-mismatch guard; only `switch` moves the branch, and the
browser IDE follows.

## For agents and Claude Code

`uvx looker-sync skill` prints the complete guide: the loop, every guard and what a refusal
means. The repo is also a Claude plugin, and `skills/looker-sync/SKILL.md` is the one
authoritative copy, shipped both in the plugin and inside the package.

## Development

```
python3 -m unittest discover -s scripts -p 'test_looker_sync.py'
```

The tests run the CLI against a fake Looker with no network. A release is a `v*.*.*` tag that
matches the version in `pyproject.toml`; the workflow in `.github/workflows/release.yml` tests,
builds and publishes to PyPI with trusted publishing, using notes from `releases/`.

## License

MIT
