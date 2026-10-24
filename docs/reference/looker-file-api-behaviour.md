---
title: Looker file and branch API behaviour (live-verified)
summary: How the Looker API answers for new folders, directories and the shared dev checkout, as seen on a live instance
verified: 2026-10-24
---

# Looker file and branch API behaviour

Seen on a Looker 26.x instance, in a sandbox project, dev mode. The fake
Looker in `scripts/test_looker_sync.py` copies these answers; if one of them changes, fix
the fake first.

| call | answer |
|---|---|
| `POST /projects/{p}/files` into a folder that does not exist | **400** `No such file or directory - <server path>` (not 404) |
| `POST /projects/{p}/directories` `{"path": "a/b"}` | 200, and it creates `a` too |
| the same call for a folder that already exists | 400 `Directory already exists` (harmless) |
| `GET /projects/{p}/files` | lists files only; an empty folder never appears |
| `DELETE /projects/{p}/directories` | 400 with a Ruby `delete_prefix` error; an empty folder created by the API can only be removed in the IDE |
| `PUT /projects/{p}/git_branch` from a second API token of the same user | moves the branch for every token and the browser IDE |

The last row is the reason `looker_sync.py` aborts on a branch mismatch: the checked-out
branch belongs to the user's dev workspace, not to a token. A token only holds the mode
(dev or production). Two tokens of one user cannot work on different dev branches at once.
Different users have separate workspaces; that case was not tested.

## Unpublished file routes the Looker VS Code extension uses (verified 2026-10-09)

Static analysis of `Google.vscode-looker-official` 1.0.0, compared with the 26.18.4 public
spec. All sit under `/api/4.0/`, use the normal bearer token and need a dev-mode session.
Absent from the public spec: `POST/PUT/DELETE /projects/{id}/files`,
`GET /projects/{id}/file/content`, `POST/DELETE /projects/{id}/directories`,
`POST /projects/{id}/revert_uncommitted` (26.16+) and `GET /projects/{id}/download_zip`
(26.16+). `looker-sync` uses the first three kinds; the last two are not adopted yet.
The extension calls no commit or push route. Response bodies of the write routes are ignored
by the extension, so they are not known from this source.

`GET /projects/{id}/git_branch` returns `ahead_count`, `behind_count`, `ref` and `remote_ref`;
`status` prints the counts.

## Git fields of the project and branch resources (verified 2026-10-24)

Read in dev mode on the sandbox project; field names and shapes only. The fake Looker serves
the subset `looker-sync` reads.

**`GET /projects/{id}`.** `uses_git` (bool), `git_remote_url`, `git_production_branch_name`,
`pull_request_mode` (a short string such as `off`), `git_service_name` (`github` for a GitHub
remote). `git_remote_url` takes three shapes: a normal remote URL (SSH or HTTPS), a relative
path into Looker's own bare-model store for a Looker-hosted repository, or null for a project
without a remote. `looker-sync link` accepts only the first.

**`GET /projects/{id}/git_branch` and each entry of `GET /projects/{id}/git_branches`** carry
the same keys: `name`, `ref`, `remote_ref`, `ahead_count`, `behind_count`, `readonly`,
`personal`, `is_local`, `is_remote`, `is_production`, `remote`, `remote_name`, `error`,
`message` (empty strings when fine), `owner_name`, `commit_at` (an int, epoch seconds) and
`can` (an object). In dev mode the production branch reads `readonly: true, personal: false,
is_production: true`; your own dev branch `personal: true, readonly: false`; a shared
non-personal branch `personal: false, readonly: false`. A branch pushed with plain git appears
in the list as `is_local: false, is_remote: true` straight after the push, and checking it out
by name answers 200 (seen during the design probe).

**Not verified live:** the exact spelling of the bare-model path, `git_service_name` for the
bare and no-git shapes, the other fields of a remote-only branch, and `readonly` on a
colleague's personal branch (needs two users).
