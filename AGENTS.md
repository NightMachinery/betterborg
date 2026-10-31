If `${nightNotes}/public/cheatsheets/borg/AGENTS.md` exists, read it first.

---

## Deployment authorization

Requests to implement or fix Betterborg include testing, committing, pushing
to both maintained remotes, and deploying the completed changes to the
existing Betterborg server. Routine deployments and the bot restarts they
require are pre-authorized. Do not ask for separate deployment or restart
approval unless the user explicitly requests code-only work or an action
falls outside the existing deployment procedure.

Before deployment, compare the deployed revision with the intended published
revision. The deployment scope includes already-published commits pending on
the current branch from earlier tasks. Inspect that entire diff and stop and
restart every bot it affects, even if another bot also needs a restart. These
related restarts are covered by the same deployment authorization.

Follow the referenced project's stop, pull, start procedure: verify a clean
server worktree, stop affected bots before updating files, fast-forward only,
restart with their existing launchers, and verify clean startup and one bot
process per session. Preserve unrelated work and keep global agent
instructions unchanged when updating this repository's deployment policy.
