---
title: Looker HTTP behaviour behind watch and fast push
summary: Latency, keep-alive, token life and retry rules for the Looker API, with each fact marked as seen live or only in code and tests
verified: 2026-11-08
---

# Looker HTTP behaviour behind watch and fast push

Why `watch` and `push` are built the way they are. Numbers are from a Linux host against a
sandbox project, in dev mode, on a warm connection. The code is `ConnectionPool`, `Looker` and
`WatchNet` in `src/looker_sync/cli.py`.

Each fact says how it is known: **live** means seen on a real Looker instance; **code** means
read from our code and exercised only against the fake Looker in
`scripts/test_looker_sync.py`.

## Latency per request (live, n=20, ms)

| request | median | note |
|---|---|---|
| connection setup (TLS) | 88 | |
| `POST /login` | 660 | |
| `PATCH /session` | 343 | |
| read a 1 KB file | 345 | p95 376 |
| write a 1 KB file | 441 | p95 474 |
| 100 KB more in the file | | adds about 90 to a read, about 130 to a write |
| `GET git_branch` | 584 | `?fields=name` is ignored and is no faster |
| `GET /session` | 311 | does not name the branch, so it cannot replace `git_branch` |
| `GET files` (the list) | 614 | |
| `validate` after a write | 926 | |

Whole cycles, median ms: read, write and read-back 1127; write and read-back 788; write only
449.

About 300 ms per request is a server-side floor. Parallel requests and fewer requests are the
only levers.

## What a write returns (live)

- The body of a write response is `{}`. There is no `ETag` and no `Last-Modified`. Only a
  read-back proves the bytes, and the API has no compare-and-set.
- A read right after a write returned the new bytes in 40 of 40 trials. No stale read was seen.

## Keep-alive

| fact | how known |
|---|---|
| Idle connections survived 30, 120 and 300 s | live |
| Reconnecting cost 67 to 104 ms | live |
| A connection that dies with no reset (a silent drop) | **not tested** anywhere |
| A reused connection that fails is retried once. Reads always; a write only if no response byte arrived and the error was not a timeout. A fresh connection that fails is a real error | code |
| Reads run on up to 8 parallel connections. The pool keeps up to 16 idle ones | code |
| The branch check of `watch` uses the same pool as everything else, so a save and a check never share a connection at the same moment | code |

Reading 30 files took 11.950 s over one keep-alive connection, 3.564 s over 4 and 2.477 s over
8 (live, on a Mac).

## Token life

| fact | how known |
|---|---|
| `POST /login` answers `expires_in` 3599 s and no refresh token, so a refresh is a new login | live |
| A refresh is due at 80% of `expires_in`, about 48 minutes after login | code |
| A refresh logs in, sets the workspace on the new token and confirms it, then swaps the token in one assignment. No request sees half of it | code |
| `watch` refreshes on a background thread, so a save never waits on a login. One-shot commands refresh only if a request comes after the due time | code |
| In `watch`, the new token must also be in dev mode on the right branch before the swap. If not, the old token is kept and the watch pauses | code |
| HTTP 401 mid-request: one refresh, one retry. A second 401 raises `AuthExpired`. In `watch` that pauses and tries a new login every 5 s; it does not exit | code |
| The token is never printed and never written to disk | code |

The longest live run was a 600 s idle `watch`, well inside the token life, so a real expiry, a
real refresh and a real 401 have **not** been seen live.

## Why the branch check is off the save path (live and code)

`GET git_branch` costs 584 ms, more than a whole write. A save does not wait for it. A
background loop checks `GET /session` and `GET git_branch` every 5 s, and every write is held
if the branch has not been confirmed for 30 s. The API has no compare-and-set, so a branch
changed between two checks can take a write; the next check says so and pauses. See the watch
section of [sync-model-and-guards](../architecture/sync-model-and-guards.md).
