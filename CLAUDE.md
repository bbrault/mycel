# CLAUDE.md

This file guides Claude Code when working with code in this repository.

## Project

**Mycel** is a Discord-driven multi-agent orchestrator for AI-Driven Development. Users invoke **workflows** on **forges** (YAML `forges:`) via Discord commands, which run **steps** (plan, elaborate, arch-review, implement, tech-review, etc.) by calling **agents** (Claude Code, Gemini, Cursor Agent) as subprocesses.

The **Mycel** network connects **Forges**; each Forge runs a **Workflow** (a sequence of steps); each step is executed by an **Agent**.

## Commands

```bash
python discord_bot.py  # Run
python3 -m pytest tests/ -v  # Tests
python3 -m py_compile mycel.py forge.py runner.py discord_bot.py  # Syntax check
```

## Architecture

```
discord_bot.py -> mycel.py -> forge.py -> runner.py
  |  |
  +----------------+--> message_bus.py
```

- **Mycel** (`mycel.py`): central orchestrator — loads `mycel_config.yaml` + `spells.yaml`, builds Forges, per-forge queues, auto-resume, hot-reload, on-complete chains
- **Forge** (`forge.py`): state machine — workflows, parallel steps (`asyncio.gather`), pre/post_run hooks, git prepare/finalize, output validation, caching, metrics, reviewer echo
- **Runners** (`runner.py`): ClaudeRunner, GeminiRunner, CursorRunner with fallbacks. Token tracking. `cwd` is the workspace repo.
- **MessageBus** (`message_bus.py`): async pub/sub, JSONL persistence
- **Discord Bot** (`discord_bot.py`): dynamic commands (`!dev`, `!mycel`), slash commands, threads, interactive buttons, pinned dashboard, role permissions

## Terminology

| Concept | Name | Config key |
|---------|------|------------|
| Project / network | **Mycel** | — |
| Execution environment | **Forge** | `forges:` (legacy: `circles:`) |
| Step / capability | **Step** | `spells:` in `spells.yaml` (legacy: `skills:`) |
| Sequence of steps | **Workflow** | `ritual:` |
| AI runner | **Agent** | `familiar:` |
| Global command | `!mycel` (alias: `!dispatch`) | — |
| Reviewer feedback | **Echo** | `{reviewer_feedback}` |

Internal note: state-persistence keys in `bus/<forge>/state.json` keep the legacy `current_skill`, `step_outputs`, `skill_metrics` names for backwards compatibility. Config keys `ritual:`, `familiar:`, `spells:` are kept internally — only the user-facing surface uses *workflow*, *step*, *agent*.

## Discord commands

```
!dev / !bugfix / !sentry / !hotfix / !devsecops / !discovery / !review <task>
!<forge> step <name> [instructions]   # run a single step
!<forge> from <step> [instructions]   # resume from a step, keep prior outputs
!<forge> resume / retry / abort / reset / status / log [N]
!<forge> reset metrics                 # reset state + zero skill_metrics + run_number
!mycel status / forges / steps / metrics / mcp / reload / reset
!mycel reset metrics                   # reset all forges + zero counters
!mycel sentry check | start | stop | status
!mycel aikido check | start | stop | status
```

`!dispatch` is kept as a backwards-compat alias for `!mycel`. Subcommands accept legacy synonyms: `forges`/`workflow`/`circles`, `steps`/`spells`/`skill`/`skills`. Inside a forge command, `step`/`spell`/`skill` are interchangeable.

Slash: `/forge`, `/spell` (run a step on a forge), `/mycel` group (`status`, `forges`, `steps`, `metrics`, `mcp`, `reload`, `reset-metrics`).

`!mycel mcp` runs `claude mcp list` and groups servers by health (connected / needs auth / failed). `reset metrics` zeroes `skill_metrics` and `run_number` (the persistent counters); plain `reset` only clears state to idle.

## Conventions

- Python 3.9+ : `from __future__ import annotations`, `Optional[str]`
- Prompts in `spells.yaml` may use French for product output; user-facing bot strings are English.
- Loggers: `mycel.<module>` (mycel.core, mycel.forge, mycel.agent, mycel.bus, mycel.discord)
- No `ANTHROPIC_API_KEY` in runner env
- Prompt via stdin, not CLI args
- Template variables: `{task}`, `{instructions}`, `{previous_output}`, `{step_output_*}`, `{step_summary_*}`, `{reviewer_feedback}`, `{pre_run_output}`, `{post_run_output}`, `{mr_project}`, `{mr_iid}`

## Configuration

Config is schema-validated by Pydantic models (`config_models.py`) at startup **and on every `!mycel reload`**. Structural errors (wrong types, a forge missing `channel`, a spell with neither `prompt` nor `prompt_file`) raise `ConfigValidationError` — startup aborts, and `reload` is rejected with the live config left unchanged. Cross-reference issues (forge → unknown `workspace_group`, group repo absent from `repos:`, ritual step with no matching spell, unknown `on_complete`) are logged as warnings, not fatal.

- `mycel_config.yaml` (legacy: `dispatch_config.yaml`): `forges:` (legacy `circles:`) with `channel`, `ritual` (workflow), `familiar` (agent), `workspace_group`, `on_complete`, `provisioner`; `permissions`, `workspace_groups`, `repos`, `kanta_stack`; `auto_resume_max_age_s` (top-level, default 1800 = 30 min) — paused-state recovery window; older state is *not* auto-resumed at startup. Set 0 to disable auto-resume entirely.
- **Provisioning** (`provisioner:` per forge): `kanta_stack` delegates to `bin/kanta-stack` (top-level `kanta_stack.bin`) to spin up a per-task isolated dev stack — git worktrees of the 4 repos in `dev-tasks/<slug>/`, a dedicated DB (migrated+seeded), a docker-compose project on its own port, and a symlinked AIDD workspace. The task `slug` is the embedded JIRA id lowercased (`KAN-341`→`kan-341`) or a slug of the task text. Per-forge `kanta_stack.teardown` (`down`|`destroy`|`keep`, default `down`) runs on a *completed* workflow; a non-completed run keeps the stack. Provisioning happens at the first step and populates `self.workspace` (worktree folder → repo key via `repos:`), so cwd resolution and git_prepare/finalize work unchanged. Code: `kanta_stack.py`. Legacy `dynamic_workspace: true` maps to `provisioner: clone` (the old /tmp git-clone path, kept for back-compat).
- `spells.yaml` (legacy: `skills.yaml`): `spells:` (legacy `skills:`) — each step: prompt, `runner`, timeout, `required_fields`, `pre_run`, `pre_run_timeout` (default 120s), `post_run`, `post_run_timeout` (default 300s), `git_prepare`, `git_finalize`
- `.env`: `DISCORD_BOT_TOKEN`, `GEMINI_API_KEY`, `WORKSPACE_*` (e.g. `WORKSPACE_REMEDIATION` for `sentry` / `aikido` forges), `DOCS_PATH`, `ISSUES_DIR`, `REMEDIATION_AUTO_FIX` (`true`/`false`, overrides `auto_fix` on sentry+aikido monitors — `false` posts a "🔧 /fix <id>" button per new issue instead of auto-enqueuing)

## Sentry monitor

Background task: polls Sentry on `sentry_monitor.interval` and can auto-enqueue the `sentry` forge. Control: `!mycel sentry {check|start|stop|status}`. Config: `sentry_monitor:` in `mycel_config.yaml`. Code: `sentry_monitor.py`.

## Aikido monitor

Background task: polls Aikido on `aikido_monitor.interval` and can auto-enqueue the `aikido` forge for criticals. Control: `!mycel aikido {check|start|stop|status}`. Config: `aikido_monitor:` in `mycel_config.yaml`. Code: `aikido_monitor.py`.
