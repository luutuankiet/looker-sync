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

**`DELETE /projects/{id}/git_branch/{name}`** in dev mode, on a branch you are not on, answered
204 and removed the branch from the git remote as well, not only from the dev workspace:
`git ls-remote` listed it just before and not just after. looker-sync never calls it.

**Not verified live:** the exact spelling of the bare-model path, `git_service_name` for the
bare and no-git shapes, the other fields of a remote-only branch, and `readonly` on a
colleague's personal branch (needs two users).

## Remote-only branches, bare repositories and two users (verified 2026-10-24)

Closes most of the list above. Sandbox project, dev mode, two throwaway branches pushed with
plain git one commit ahead of a local tracking branch in the clone.

**A remote-only branch** (pushed with git, never checked out in Looker) is listed with
`is_local: false, is_remote: true, ref: "", remote_ref: <remote tip>, ahead_count: null,
behind_count: null, personal: false, readonly: false, is_production: false, error: "",
message: ""` and an int `commit_at`.

**Right after the first checkout** (`PUT /projects/{id}/git_branch {"name": ...}`, 200), the
singular `GET /projects/{id}/git_branch` still answers `is_local: false, ref: ""` with null
counts, while `git_branches` already lists the branch `is_local: true, ref: <remote tip>,
ahead_count: 0`. A few seconds later the singular answer catches up. Code that reads `ref` from
the singular resource must fall back to `remote_ref`; `switch` does since this fix, and before
it skipped the fast-forward silently.

**The fast-forward in `switch`** runs live: with the local branch one commit behind the commit
Looker is on, `switch --branch <it>` ran `merge-base --is-ancestor` both ways, then
`merge --ff-only --quiet <commit>` (exit 0) and printed `git: fast-forwarded ...`. Seen both
on a second checkout and, after the fix, on a first checkout of a remote-only branch.

**After one first checkout** Looker reported every file of the branch as uncommitted
(`git_status: {"action": "add", "text": "Modified", "revertable": false}`) although the content
matched the commit, so `switch` refused to leave it. `POST /projects/{id}/reset_to_remote` on
that throwaway branch cleared it. Seen once in two first checkouts; cause unknown.

**`git_remote_url` shapes**, read over every project of the instance: a Looker-hosted bare
repository is `../../bare_models/<project id>.git` with `uses_git: true` and
`git_service_name: null`; a project without git has `uses_git: false`, `git_remote_url: null`,
`git_service_name: null`. Real remotes name the service (`github`, `bitbucket`).

**Two users: not verified.** A non-admin user (permissions `access_data`, `develop`,
`see_lookml` on the all-access model set) gets 404 on `/projects/{id}` and its branches for a
project with no model deployed in production, even with a model configuration pointing at it;
every project such a user could see had a model. The sandbox project has none, and deploying
one means writing to production. So `readonly` on a colleague's personal branch, and `push`
refusing it, still rest on the field the branch resource reports. A new user on this instance
also could not get API3 credentials (`POST /users/{id}/credentials_api3` answered 404); admin
sudo (`POST /login/{user_id}`) worked.
