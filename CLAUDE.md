# CLAUDE.md — read this first

This file is the entry point for every new Claude Code session in this
repository. It exists so a fresh session can recover context cheaply instead
of re-reading the whole tree.

## Startup order

1. `PROJECT_STATE.md` — what this product is and where it stands.
2. `CURRENT_TASK.md` — the one active task. If it's empty/stale, ask the user.
3. `DECISIONS.md` — settled calls that must not be re-litigated.
4. Then run `git status` and `git log --oneline -10` to see what's actually
   changed since these files were last updated.

Prefer these four files over rediscovering the repository from scratch. Only
read module source (`mrp_mcp_tools/`, `mrp_mcp_governance/`) when the active
task requires touching that code.

## Standing rules

- **Never assume memory from a previous chat session.** These files are the
  only carried-over state; the conversation history is not available to a new
  session.
- **Do not reopen closed tests or the release gate** unless shipped code
  relevant to them actually changed. Re-verifying settled work wastes effort
  and can contradict published claims that depend on it staying stable.
- **Require explicit human approval before:** `git push`, tagging/creating a
  release, uploading to apps.odoo.com, any destructive git operation, or any
  action visible outside this machine.
- **Keep token usage low.** Don't paste large file contents, test output, or
  audit reports into chat — reference the file path instead.
- This repo (`verimantle-odoo-apps`) is the **publication tree**: only the two
  shippable module directories plus this memory system. Additional internal
  project documentation is maintained outside this repository — don't
  duplicate it here.

## Maintenance rule for these four files

- After a meaningful milestone (published, fixed, gate cleared): update
  `PROJECT_STATE.md`.
- When the active task changes or completes: **replace** the contents of
  `CURRENT_TASK.md` (it holds exactly one task, not a backlog).
- When a durable decision is made that shouldn't be revisited: **append** a
  short dated entry to `DECISIONS.md`.
- Never turn any of the four files into a dumping ground for reports, logs,
  or exploratory notes.
