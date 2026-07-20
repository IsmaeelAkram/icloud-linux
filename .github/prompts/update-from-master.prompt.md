---
name: Update from master
description: Synchronize the fork with upstream master and incorporate the result into a working branch.
agent: agent
---

# Update from master

This repository follows upstream's `master` branch. Do not substitute `main`.

1. Inspect `git status --short --branch` and stop if the target branch has
   uncommitted work.
2. Fetch both remotes: `git fetch --prune upstream` and
   `git fetch --prune origin`.
3. Update the fork's `master` from `upstream/master` with a fast-forward only
   operation. Push the result to `origin/master`.
4. Compare the target branch with the updated `master`. Prefer upstream's
   implementation when it supersedes local work; retain and port only behavior
   with no upstream equivalent.
5. Bring `master` into the target branch with a merge or rebase appropriate to
   the branch's published-history policy. Resolve conflicts in favor of the
   upstream design unless the local behavior is deliberately retained.
6. Run the smallest relevant validation and push the updated target branch.

Do not force-push `master`, reset published branches, or treat remote-tracking
references as a replacement for fetching first.
