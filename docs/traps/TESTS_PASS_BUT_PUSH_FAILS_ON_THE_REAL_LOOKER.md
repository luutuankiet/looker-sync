---
symptom: "unit tests pass but push fails against real Looker with HTTP 400 No such file or directory"
area: tests / fake Looker
verified: 2026-10-09
---

# Tests pass because the fake Looker is kinder than the real one

**Symptom.** `python3 -m unittest discover -s scripts -p 'test_looker_sync.py'` is green, yet a
real `push` that creates a file in a new folder stops with
`HTTP 400: No such file or directory - <server path>`.

**Mechanism.** The fake answered a file create in a missing folder with 404, and the code
only created the folder on 404. Real Looker answers 400. The test exercised a branch the
real API never takes. A push that then stopped partway also left Looker written but
`.looker-sync/state.json` not updated, so the next push refused with "changed on both sides".

**Fix.** The fake now answers 400, and a test makes a write fail partway and checks that the
next push needs no `--force`. When you add or change a fake answer, check it against a
sandbox project first and record it in
[looker-file-api-behaviour](../reference/looker-file-api-behaviour.md).

**Verify.** `python3 -m unittest discover -s scripts -p 'test_looker_sync.py'` must fail if the
folder-creation branch is removed.

**General rule.** A fake that is kinder than the real service proves nothing about the
case it imitates.
