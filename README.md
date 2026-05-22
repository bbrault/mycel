# Mycel

Multi-agent orchestrator for Discord-driven AI-Driven Development (AIDD). Each configured **forge** (`forges:` in YAML) runs a **workflow** of **steps** by invoking **agents** (Claude Code, Gemini, Cursor) as subprocesses.

The Mycel network connects Forges; each Forge runs a Workflow executed by Agents.

## Requirements

- Python 3.9+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` in PATH)
- Discord bot with **Message Content Intent**
- (Optional) [Cursor](https://www.cursor.com/), [Gemini CLI](https://ai.google.dev/gemini-api/docs/gemini-cli), [glab](https://gitlab.com/gitlab-org/cli)

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

## Configuration

1. Create a Discord bot and put the token in `.env`
2. Create channels: `#agents`, `#dev`, `#bugfix`, `#sentry`, `#devsecops`, `#discovery`, `#reviews`, `#aikido`
3. Set workspaces in `.env`, e.g.:

```env
WORKSPACE_FEATURE=~/Documents/workspace_feature/app
WORKSPACE_BUG=~/Documents/workspace_bug/app
WORKSPACE_REMEDIATION=~/Documents/workspace_remediation/app
```

Edit `mycel_config.yaml` and `spells.yaml` for your org. The legacy filenames `dispatch_config.yaml` and `skills.yaml` are still loaded as fallbacks.

## Run

```bash
python discord_bot.py
```

## Commands

### Per-forge (dynamic)

```
!dev LAB-1890 Add geographic zones
!bugfix LAB-1234 Fix /api/orders crash
!review https://gitlab.com/org/api/-/merge_requests/456
!<forge> step <name> [instructions]
!<forge> from <step> [instructions]
!<forge> resume / retry / abort / reset / status / log [N]
!<forge> reset metrics                  # reset state + zero skill_metrics & run_number
```

### Global (`!mycel`)

```
!mycel status
!mycel forges
!mycel steps
!mycel metrics
!mycel mcp                              # check MCP server health
!mycel reload
!mycel reset
!mycel reset metrics                    # reset all forges + zero counters
!mycel sentry check | start | stop | status
!mycel aikido check | start | stop | status
```

`!dispatch` is kept as a backwards-compat alias for `!mycel`. Subcommands accept legacy synonyms (`workflow`/`circles` → `forges`, `spells`/`skill`/`skills` → `steps`). Inside a forge command, `step` / `spell` / `skill` are interchangeable.

Slash: `/forge`, `/spell` (run a step), `/mycel` group with `status`, `forges`, `steps`, `metrics`, `mcp`, `reload`, `reset-metrics`.

## Layout

| File | Role |
|------|------|
| `mycel.py` | Config, queues, workers, monitors |
| `forge.py` | `Forge` workflow engine |
| `runner.py` | CLI runners + streaming |
| `message_bus.py` | Bus + JSONL |
| `discord_bot.py` | Bot UI |
| `sentry_monitor.py` | Sentry polling |
| `aikido_monitor.py` | Aikido polling |

## Documentation

- **[GUIDE_UTILISATEUR.md](GUIDE_UTILISATEUR.md)** — utilisation Discord (commandes, fils, permissions, dépannage).
- **[DOCUMENTATION.md](DOCUMENTATION.md)** — architecture technique, YAML, bus, état, rétrocompatibilité.
- **CLAUDE.md** — repères pour les contributeurs et assistants de code.
