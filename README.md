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
| `init [--project P] [--mode dev\|prod] [--branch B]` | set up a folder (it may be empty), then a first pull. `--no-pull` writes metadata only; `--force` takes Looker's copy over files already there |
| `pull [--force] [--dry-run]` | copy Looker files to disk; never deletes local files |
| `status` | show what differs on each side, and how far the dev branch is ahead of and behind its remote; writes nothing |
| `push [--force] [--delete] [--dry-run]` | write local changes, read every file back, then validate |
| `switch (--branch B \| --mode dev\|prod)` | move Looker and the folder to another branch or mode |
| `skill [--path]` | print the full usage guide written for agents |

Global options: `-C <project folder>` (default: the current folder) and `--env-file <path>`.
`--help` is the short reference; `looker-sync skill` is the long one.

Exit codes: 0 ok, 1 Looker or network error, 2 refused by a guard, 3 validation errors.

## What it guarantees

- `push` writes your changes, reads every file back and compares bytes, then runs LookML
  validation and prints `file:line  message`.
- It refuses to overwrite a file edited in Looker since your last sync (`--force` to override).
- `pull` never deletes local files and refuses to overwrite unpushed local work.
- `push` never deletes files in Looker without `--delete`, and never writes in prod mode.
- A push that stops partway records what it wrote; the next push retries only the rest.

## What it does not do

It never commits, pushes to git, deploys, resets or creates branches. Looker's API has no
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
