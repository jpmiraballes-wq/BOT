# Workflow

## GitHub-first development rule

This repository is the source of truth for code changes.

Do not ask the operator to paste large patches, heredocs, or multi-screen code blocks into the Mac terminal except in a true emergency.

Standard workflow:

1. Make code changes in GitHub / versioned files.
2. The operator should only need short terminal commands such as `git pull`, compile/test commands, start/stop commands, and log inspection commands.
3. Every operational change must be traceable and reversible through git.
4. Before launching a long run, validate with clear terminal output such as `COMPILE_OK`, `PATCH_OK`, `RUNNING_PID`, and `LOG:path/to/log`.
5. Avoid ad-hoc terminal patching because zsh quoting, heredocs, wrong Python executable, or partially pasted code can corrupt the working tree.
6. If a local hotfix is unavoidable, immediately move that hotfix into the repository as a tracked file or commit.

Operational preference:

- GitHub writes code.
- Mac terminal runs code.
- Terminal is not the editor unless there is no alternative.

Current preferred workflow:

1. Stop old processes.
2. Pull from the repository.
3. Run the versioned script or one short launch command.
4. Watch the log only; do not keep patching live unless the process is stopped first.
