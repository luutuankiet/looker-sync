---
title: Looker file and branch API behaviour (live-verified)
summary: How the Looker API answers for new folders, directories and the shared dev checkout, as seen on a live instance
verified: 2026-10-09
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
