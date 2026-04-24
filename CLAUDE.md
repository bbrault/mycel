# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project

Arcane is a Discord-driven multi-agent orchestrator for AI-Driven Development. Users invoke **Circles** (execution environments) via Discord commands, which run **Spells** (plan, elaborate, arch-review, implement, tech-review, etc.) by calling **Familiars** (Claude Code, Gemini, Cursor Agent) as subprocesses.

## Commands

```bash
python discord_bot.py                      # Run
python3 -m pytest tests/ -v               # Tests (93)
python3 -m py_compile arcane.py forge.py runner.py discord_bot.py  # Syntax check
```

## Architecture

```
discord_bot.py  ->  arcane.py  ->  forge.py  ->  runner.py
                       |                |
                       +----------------+-->  message_bus.py
```

- **Arcane** (`arcane.py`): grimoire central — config, circles, per-circle task queues, auto-resume, hot-reload, forge chains
- **Circle** (`forge.py`): state machine — rituals (workflows), parallel spells (`asyncio.gather`), pre/post_run hooks, git prepare/finalize, output validation, caching, metrics, reviewer echo (inter-agent feedback)
- **Familiars** (`runner.py`): ClaudeRunner, GeminiRunner, CursorRunner with fallbacks. Token tracking. `cwd` set to workspace repo.
- **MessageBus** (`message_bus.py`): async pub/sub, JSONL persistence
- **Discord Bot** (`discord_bot.py`): dynamic commands (`!dev`, `!arcane`), slash commands, threads, interactive buttons, pinned dashboard, role permissions

## Terminology

| Concept | Name | Config key |
|---|---|---|
| Project | **Arcane** | — |
| Execution environment | **Circle** | `circles:` |
| Step/capability | **Spell** | `spells:` (skills.yaml) |
| Sequence of spells | **Ritual** | `ritual:` |
| AI runner | **Familiar** | `familiar:` |
| Global command | `!arcane` | — |
| Reviewer feedback | **Echo** | `{reviewer_feedback}` |

## Discord Commands

```
!dev / !bugfix / !sentry / !hotfix / !devsecops / !discovery / !review <task>
!<circle> skill <name> [instructions]   -> cast isolated spell (keyword is `skill`)
!<circle> from <spell> [instructions]   -> restart from a spell, keep prior outputs
!<circle> resume / retry / abort / reset / status / log [N]
!arcane status / forges / skills / metrics / reload / reset
!arcane sentry check | start | stop | status
```

Slash commands: `/forge <circle> <task>`, `/skill <circle> <spell>`, `/arcane status|forges|skills|metrics|reload`.

Note: config terms are **circles**/**spells**/**familiars**/**rituals**, but legacy command keywords (`skill`, `forges`, `/forge`) still use the old names.

## Conventions

- Python 3.9+ : `from __future__ import annotations`, `Optional[str]`
- French prompts and messages
- Loggers: `arcane.<module>` (arcane.core, arcane.circle, arcane.familiar, arcane.bus, arcane.discord)
- No ANTHROPIC_API_KEY in runner env
- Prompt via stdin, never CLI args
- Template variables: `{task}`, `{instructions}`, `{previous_output}`, `{step_output_*}`, `{step_summary_*}`, `{reviewer_feedback}`, `{pre_run_output}`, `{post_run_output}`, `{mr_project}`, `{mr_iid}`

## Configuration

- `arcane_config.yaml`: circles (with `channel`, `ritual`, `familiar`, `workspace_group`, `on_complete`), permissions, workspace_groups, repos
- `skills.yaml`: spells with prompt, familiar, timeout, `required_fields`, `pre_run`, `post_run`, `git_prepare`, `git_finalize`
- `.env`: DISCORD_BOT_TOKEN, GEMINI_API_KEY, WORKSPACE_FEATURE, WORKSPACE_BUG, WORKSPACE_SENTRY, WORKSPACE_REVIEW, DOCS_PATH, ISSUES_DIR

## Circles

| Circle | Ritual | Channel | Familiar |
|---|---|---|---|
| dev | plan -> elaborate -> arch-review -> implement -> tech-review -> qa-scenario | #dev | claude |
| bugfix | diagnose -> plan -> implement -> tech-review | #bugfix | claude |
| sentry | diagnose -> plan -> implement -> tech-review (on_complete -> review) | #sentry | claude |
| sentry-comments | diagnose -> elaborate -> implement | #sentry | claude |
| hotfix | diagnose -> implement -> tech-review | #bugfix | claude |
| devsecops | audit -> plan -> implement -> tech-review | #devsecops | cursor |
| discovery | research -> elaborate -> plan | #discovery | claude |
| review | mr-fetch -> parallel[mr-review, mr-review-arch, mr-review-quality] -> mr-summary | #reviews | claude |

Per-spell familiar overrides in `skills.yaml` (`runner:` key): arch-review and mr-review-arch -> gemini; tech-review and mr-review-quality -> cursor.

## Sentry Monitor

Background task that polls Sentry every `sentry_monitor.interval` seconds and auto-enqueues the `sentry` circle on new errors. Controlled via `!arcane sentry {check|start|stop|status}`. Config lives under `sentry_monitor:` in `arcane_config.yaml` (`enabled`, `interval`, `auto_fix`, `auto_fix_levels`). Code in `sentry_monitor.py`.
