---
symptom: "a changed file shows git_status action add, so it looks like a new file"
area: file list / git_status
verified: 2026-10-09
---

# A modified file reports `action: add` in `git_status`

**Symptom.** Code that reads `git_status.action` to tell new files from changed ones sees
`add` for a file that already existed.

**Cause.** Looker's file list gives a changed file `{'action': 'add', 'text': 'Modified', ...}`.
`action` is not a reliable new-versus-changed signal.

**Do.** Read `text`. `looker-sync` prefers `text` and falls back to `action`
(`Session.uncommitted`). Verified live 2026-10-09 against a Looker 26.x sandbox project.
