# Arcane

Orchestrateur multi-agents pilote via Discord pour le workflow AIDD (AI-Driven Development). Chaque **Circle** execute un **Ritual** de **Spells** en invoquant des **Familiars** (Claude Code, Gemini, Cursor Agent) en subprocess.

## Prerequis

- Python 3.9+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` dans le PATH)
- Bot Discord avec **Message Content Intent**
- (Optionnel) [Cursor](https://www.cursor.com/), [Gemini CLI](https://ai.google.dev/gemini-api/docs/gemini-cli), [glab](https://gitlab.com/gitlab-org/cli)

## Installation

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

## Configuration

1. Creez un bot Discord → copiez le token dans `.env`
2. Creez les canaux : `#agents`, `#dev`, `#bugfix`, `#sentry`, `#devsecops`, `#discovery`, `#reviews`
3. Configurez les workspaces dans `.env` :

```env
WORKSPACE_FEATURE=~/Documents/workspace_feature/app
WORKSPACE_BUG=~/Documents/workspace_bug/app
WORKSPACE_SENTRY=~/Documents/workspace_sentry/app
```

## Lancement

```bash
python discord_bot.py
```

## Commandes

### Circles (dynamiques)

```
!dev LAB-1890 Gestion des zones geographiques
!bugfix LAB-1234 Fix crash /api/orders
!sentry SENTRY-5678 TypeError
!review https://gitlab.com/kanta/api/-/merge_requests/456
!discovery reduction du time-to-value sur l'onboarding DPO
!devsecops audit mensuel API + front

!<circle> skill <name> [instructions]   # Cast un spell isole (mot-cle legacy : `skill`)
!<circle> from <spell>                  # Reprendre depuis un spell
!<circle> resume / retry / abort        # Controler le ritual
!<circle> status / log [N]              # Inspecter
!<circle> reset                         # Reinitialiser
```

### Arcane (global)

```
!arcane status    # Grimoire — etat de tous les circles
!arcane forges    # Liste des circles
!arcane skills    # Liste des spells
!arcane metrics   # Metriques (duree, tokens, cout $)
!arcane reload    # Recharger la config sans redemarrage
!arcane reset     # Reset de tous les circles
```

### Sentry Monitor (global)

```
!arcane sentry check     # Verification manuelle des nouvelles erreurs Sentry
!arcane sentry start     # Demarrer le monitoring periodique
!arcane sentry stop      # Arreter le monitoring
!arcane sentry status    # Etat du monitoring
```

### Slash commands

```
/forge circle: task:              # Avec autocompletion
/skill circle: spell_name:
/arcane status | forges | skills | metrics | reload
```

## Circles

| Circle | Ritual | Canal |
|---|---|---|
| **dev** | plan -> elaborate -> arch-review -> implement -> tech-review -> qa-scenario | #dev |
| **bugfix** | diagnose -> plan -> implement -> tech-review | #bugfix |
| **sentry** | diagnose -> plan -> implement -> tech-review (chain -> review) | #sentry |
| **sentry-comments** | diagnose -> elaborate -> implement | #sentry |
| **hotfix** | diagnose -> implement -> tech-review | #bugfix |
| **devsecops** | audit (composer/npm audit + secrets + OWASP) -> plan -> implement -> tech-review | #devsecops |
| **discovery** | research (Notion + JIRA) -> elaborate -> plan | #discovery |
| **review** | mr-fetch -> **parallel**[mr-review, mr-review-arch, mr-review-quality] -> mr-summary | #reviews |

## Features

- **Parallel spells** : `asyncio.gather` pour les reviews MR
- **Multi-worker** : circles independants en parallele
- **Threads Discord** : un thread par ritual, reuse si existant
- **Boutons interactifs** : Resume / Retry / Reset / Push+MR
- **Progress bar** : dashboard pinne auto-mis a jour
- **Inter-agent echo** : le feedback du reviewer est transmis au spell relance
- **Pre/post hooks** : scripts bash avant/apres un spell (tests, git diff)
- **Output caching** : skip si memes inputs
- **Runner retry** : 2 retries avec backoff sur erreur transitoire
- **Circle chains** : `on_complete` pour enchainer les circles (ex: sentry -> review)
- **Sentry monitor** : polling periodique des erreurs Sentry + auto-enqueue du circle `sentry`
- **Auto-resume** : rituels en pause restaures au redemarrage du bot
- **Git hooks** : `git_prepare` / `git_finalize` pour les spells qui touchent au code
- **Pre/post hooks** : scripts bash avant/apres un spell (tests, git diff)
- **Cost tracking** : tokens + cout $ dans `!arcane metrics`

## Workflows specifiques

### Discovery — proposer de nouvelles features

`!discovery <theme>` scanne la roadmap Notion Kanta + JIRA + les repos pour proposer 3-5 idees de features chiffrees (valeur / effort / priorite), ancrees dans du signal reel. Le ritual fait une pause apres `research` — relance avec `!discovery resume <choix>` pour elaborer l'idee retenue, puis planifier.

Sources consultees : [Notion roadmap](https://www.notion.so/kanta-app/343758d9c3c18024bf30d77bae1a6f9b), JIRA (3 derniers mois), CHANGELOG / README des repos.

### DevSecOps — audit securite recurrent

`!devsecops <perimetre>` lance un scan automatique (`composer audit`, `npm audit`, detection de secrets, deps obsoletes) puis demande a Gemini un rapport OWASP/CVE priorise, avec pause avant `plan`. Les phases suivantes planifient puis appliquent les correctifs sur une branche dediee et ouvrent la MR.

Recommande en recurrent via `/schedule` (audit hebdomadaire ou mensuel).

## Tests

```bash
python3 -m pytest tests/ -v    # 93 tests
```
