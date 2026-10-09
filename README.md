# looker-sync

Move LookML between a local folder and your own Looker dev workspace, with verification.
Python 3 standard library only.

```
uvx looker-sync init --project <name> [--branch <b>]   # works in a blank folder
uvx looker-sync status
uvx looker-sync push --dry-run
uvx looker-sync push
uvx looker-sync skill                                  # the full guide, written for agents
```

`init` in a folder with no credentials writes an `.env` template and a `.gitignore` entry,
then asks you to fill it in; run it again to pull.

The env file holds `LOOKER_URL`, `LOOKER_CLIENT_ID` and `LOOKER_CLIENT_SECRET`, for an API
key that belongs to **your own** Looker user.

## What it guarantees

- `push` writes your changes, reads every file back and compares bytes, then runs LookML
  validation and prints `file:line  message`.
- It refuses to overwrite a file edited in Looker since your last sync (`--force` to override).
- `pull` never deletes local files and refuses to overwrite unpushed local work.
- It never commits, deploys, resets or creates branches. Looker's API has no commit or push
  endpoint, so committing stays with git and the Looker IDE.
- A push that stops partway records what it wrote; the next push retries only the rest.

## One key, one branch at a time

The checked-out dev branch belongs to the Looker user, not to an API token. A second client on
another branch is refused by a branch-mismatch guard; only `switch` moves the branch, and the
browser IDE follows.

Exit codes: 0 ok, 1 Looker or network error, 2 refused by a guard, 3 validation errors.

Tests: `python3 -m unittest discover -s scripts -p 'test_looker_sync.py'`

## Claude plugin

The repo is also a Claude plugin: `skills/looker-sync/SKILL.md` is the one authoritative guide,
shipped in the plugin and inside the package (`looker-sync skill` prints it).
