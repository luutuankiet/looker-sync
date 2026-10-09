# No commit command; ship on PyPI with the skill inside

**Commit.** `looker-sync` has no `commit`. Looker's API 4.0 has no commit or push endpoint:
`update_git_branch` with `ref` hard-resets and force-pushes, `reset_to_remote` discards
unpushed work, and `deploy_to_production` publishes. The official Looker VS Code extension
1.0.0 was decompiled (static analysis, no calls to any instance) to look for a hidden route.
It has none: it syncs files over `/files`, `/directories` and `/file/content`, and commits
with the user's local git, after which Looker pulls from the remote and the extension calls
`reset_to_remote`. So agents cannot commit LookML through this tool; `status` reports
`ahead_count` and `behind_count` and the commit stays with git or the Looker IDE.

**Distribution.** The package is published to PyPI so `uvx looker-sync` works, by a tag-driven
GitHub Actions release using trusted publishing (no token). `skills/looker-sync/SKILL.md`
is the single authoritative guide: the Claude plugin loads it from there, the wheel
force-includes it, and `looker-sync skill` prints it. Standard library only still holds.

Deferred: `revert_uncommitted` and `download_zip` (Looker 26.16+, unpublished) could replace
parts of `switch` and the first pull. See `docs/reference/looker-file-api-behaviour.md`.
