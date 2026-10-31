# Deployment checks

Routine deployment authorization is defined in the repository's
[AGENTS.md](../AGENTS.md). Follow the server-specific stop, pull, start
procedure referenced there, checking the complete pending diff to identify
affected bots.

Tmux's displayed `pane_start_command` includes shell escaping. Replaying it
as one raw shell command can preserve an extra quoting layer and fail with
exit status 127. Build the restart command from the existing launcher's
verified interpreter, working directory, environment assignments and script
arguments. Quote each argument for the shell exactly once.

Before retrying a failed launch, confirm the pane is dead and no bot process
for that session remains. After launch, check plugin-loading success, absence
of tracebacks and exactly one Python bot process per session. A successful
tmux command alone does not establish that the bot started.
