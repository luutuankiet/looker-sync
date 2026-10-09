# The tool writes files and never touches git state

`looker-sync` pulls, pushes, validates and checks out an existing branch by name. It
never commits, deploys, resets or creates a branch, and it never sends `ref` to the branch
endpoint, because `ref` triggers a hard reset that Looker force-pushes. Those actions stay
clicks in the Looker IDE.

The trade-off is a manual commit step after every batch of pushes. It was chosen because a
wrong commit or reset in Looker is hard to undo, an agent running unattended is the least
careful actor, and the dev workspace keeps pushed files as uncommitted changes, so nothing is
lost. `switch` copies the IDE: it refuses while Looker holds uncommitted files, because the
API's own checkout silently carries them onto the target branch.
