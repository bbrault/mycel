# Arcane — Documentation technique et fonctionnelle

> **Derniere mise a jour** : 2026-04-24
> **Version** : 3.0.0

---

## Table des matieres

1. [Vue d'ensemble](#vue-densemble)
2. [Terminologie](#terminologie)
3. [Architecture](#architecture)
4. [Composants](#composants)
5. [Circles](#circles)
6. [Spells](#spells)
7. [Workflow engine](#workflow-engine)
8. [Familiars (runners)](#familiars-runners)
9. [MCP et outils cloud](#mcp-et-outils-cloud)
10. [Resolution du contexte issue](#resolution-du-contexte-issue)
11. [Message bus](#message-bus)
12. [Bot Discord](#bot-discord)
13. [Sentry monitor](#sentry-monitor)
14. [Persistence et output AIDD](#persistence-et-output-aidd)
15. [Configuration](#configuration)
16. [Tests](#tests)
17. [Logging](#logging)
18. [Limites et evolutions](#limites-et-evolutions)

---

## Vue d'ensemble

Arcane est un orchestrateur multi-agents qui automatise le workflow AIDD (AI-Driven Development) de Kanta. Il transforme une tache JIRA ou une erreur Sentry en un pipeline d'etapes (spells) executees par des LLMs via CLI, avec des outputs sauvegardes au format AIDD.

**Flux principal** :

```
Utilisateur (Discord)
    !dev LAB-1890 Gestion des zones geographiques
    -> Arcane (file d'attente par circle + routage + auto-resume)
        -> Circle (state machine)
            -> resolve_issue_context(LAB-1890) -> charge docs locaux
            -> /plan (claude) -> LAB-1890-technical_plan.md
            -> /elaborate (claude + MCP JIRA) -> LAB-1890-functional_specs.md
            -> /arch-review (gemini) -> LAB-1890-architectural_review.md
            -> /implement (claude + git_prepare + post_run tests) -> LAB-1890-implementation.md
            -> /tech-review (cursor + pre_run diff + git_finalize) -> LAB-1890-code_review.md
            -> /qa-scenario (claude) -> LAB-1890-qa_scenarios.md
        <- Fichiers dans {issues_dir}/LAB-1890-*/
    <- Messages + fichiers temps reel (Discord)
```

---

## Terminologie

| Concept | Nom | Cle de config | Fichier |
|---|---|---|---|
| Projet | **Arcane** | — | — |
| Environnement d'execution | **Circle** | `circles:` | `arcane_config.yaml` |
| Etape/capacite | **Spell** | `spells:` | `skills.yaml` |
| Sequence de spells | **Ritual** | `ritual:` | `arcane_config.yaml` |
| Agent LLM (runner) | **Familiar** | `familiar:` / `runner:` | les deux |
| Commande globale | `!arcane` | — | `discord_bot.py` |
| Feedback inter-agents | **Echo** | `{reviewer_feedback}` | template spell |

Le fichier `skills.yaml` et certaines cles legacy (`runner:` au niveau spell, mots-cles `skill` / `forges` dans Discord) conservent le vocabulaire historique. L'interface utilisateur et les noms internes (`enqueue_circle_spell`, `list_circles`, `list_spells`) utilisent le vocabulaire Arcane.

---

## Architecture

```
+---------------------------------------------------------+
|                     Discord Bot                          |
|                  (discord_bot.py)                        |
|  Commandes dynamiques, threads, dashboard pinne,         |
|  boutons interactifs, feedback temps reel                |
+----------------------------+----------------------------+
                             |
+----------------------------v----------------------------+
|                       Arcane                             |
|                     (arcane.py)                          |
|  Config, circles, per-circle task queues, auto-resume,   |
|  hot-reload, circle chains (on_complete), Sentry monitor |
+--+------+------+------+------+------+------+------+-----+
   |      |      |      |      |      |      |      |
+--v-+ +--v-+ +--v-+ +--v-+ +--v-+ +--v-+ +--v-+ +--v-+
|dev | |bugf| |sent| |s-c | |hotf| |dvsc| |disc| |rev |  <- Circles
+--+-+ +--+-+ +--+-+ +--+-+ +--+-+ +--+-+ +--+-+ +--+-+
   |      |      |      |      |      |      |      |
+--v------v------v------v------v------v------v------v----+
|  Familiars (runner.py) : Claude / Gemini / Cursor CLI    |
|  + MessageBus (message_bus.py) : async queue + JSONL     |
|  + SentryMonitor (sentry_monitor.py) : polling background|
+---------------------------------------------------------+
```

### Dependances entre modules

| Module | Depend de |
|---|---|
| `discord_bot.py` | `arcane.py`, `message_bus.py` |
| `arcane.py` | `forge.py`, `message_bus.py`, `runner.py`, `sentry_monitor.py` |
| `forge.py` | `runner.py`, `message_bus.py` |
| `sentry_monitor.py` | `arcane.py`, MCP Sentry |
| `runner.py` | aucun (stdlib + logging) |
| `message_bus.py` | aucun (stdlib + logging) |

---

## Composants

### Arcane (`arcane.py`)

Le grimoire central.

| Methode | Description |
|---|---|
| `enqueue_circle(name, task, instructions)` | Enfile le ritual complet |
| `enqueue_circle_spell(name, spell, task, instructions)` | Enfile un spell isole |
| `enqueue_from_spell(name, from_spell, instructions)` | Reprise depuis une etape (garde les outputs precedents) |
| `resume_circle(name, instructions)` | Reprend un ritual en pause |
| `retry_circle(name, instructions)` | Relance le spell courant |
| `abort_circle(name)` | Arrete le spell en cours |
| `reset_circle(name)` / `reset_all()` | Reinitialise |
| `inject_feedback(name, feedback)` | Injecte un feedback utilisateur dans le spell courant |
| `reload_config()` | Hot-reload de `arcane_config.yaml` + `skills.yaml` sans redemarrage |
| `get_circle_status(name)` | Status visuel avec progression du ritual |
| `get_global_status()` | Dashboard global (tous les circles) |
| `get_circle_log(name, limit)` | Derniers N messages du JSONL |
| `list_circles()` / `list_spells()` | Listes formatees |
| `get_metrics()` | Metriques agregees (duree, tokens, cout $) |
| `start_sentry_monitor()` / `stop_sentry_monitor()` / `run_sentry_check()` | Controle du Sentry monitor |

**Fonctionnalites cles** :
- **Per-circle task queues** : chaque circle a sa propre queue, les circles tournent en parallele.
- **Auto-resume** : au demarrage, les rituels en pause sont restaures depuis le state sur disque.
- **Hot-reload** : `!arcane reload` recharge la config sans tuer le bot.
- **Circle chains** : `on_complete: <other_circle>` chaine un circle sur la fin d'un autre (ex: `sentry` -> `review`).

### Circle (`forge.py`)

State machine avec ritual, retries, pauses, parallelisme, resolution du contexte issue, hooks git, et output AIDD.

**Methodes cles** :

| Methode | Description |
|---|---|
| `run_workflow(task, instructions)` | Ritual complet depuis le debut |
| `run_from_skill(spell, instructions)` | Reprise depuis un spell (conserve les outputs precedents) |
| `run_single_skill(spell, task, instructions)` | Execution isolee d'un spell |
| `resolve_issue_context(jira_id, docs_path, workspace)` | Charge les docs locaux de l'issue |
| `_save_issue_doc(spell, output)` | Sauvegarde au format AIDD |
| `_prepare_git(spell)` / `_finalize_git(spell)` | Hooks git branche feature / push+MR |
| `_run_pre_run(spell)` / `_run_post_run(spell)` | Scripts bash autour d'un spell |
| `_run_parallel_group(group)` | Execution `asyncio.gather` de spells en parallele |

**Fonctions utilitaires** :

| Fonction | Description |
|---|---|
| `extract_json(output)` | 3 strategies : parse direct, code fence, accolades englobantes |
| `safe_evaluate_condition(condition, data)` | `==`, `!=`, `<`, `>`, `<=`, `>=` + `and`/`or` |

### Familiars (`runner.py`)

3 runners CLI avec fallback automatique sur Claude.

| Familiar | Commande | Fallback | Options |
|---|---|---|---|
| `ClaudeRunner` | `claude -p [--allowedTools ...] [--permission-mode ...]` | aucun | `allowed_tools`, `mcp_config`, `permission_mode`, `extra_args` |
| `GeminiRunner` | `gemini` (stdin) | Claude | propage `GEMINI_API_KEY` / `GOOGLE_API_KEY` |
| `CursorRunner` | `cursor agent -p --trust --force --approve-mcps` | Claude | detecte dans `/Applications/Cursor.app/` |

Factory : `get_runner(name, timeout, **runner_kwargs)`.
Startup check : `check_runners()` -> `{claude: bool, gemini: bool, cursor: bool}`.

**Tokens et cout** : les runners parsent la sortie CLI pour extraire `input_tokens`, `output_tokens`, `cost_usd` et les remontent dans `RunnerResult`. Agreges par circle dans `!arcane metrics`.

**`cwd`** : le runner est execute dans le repo workspace primaire (`workspace_first`) pour que `git`, `composer`, `npx` fonctionnent naturellement.

### Message bus (`message_bus.py`)

Pub/sub async. Queue creee dans `start()` (pas dans `__init__`, critique pour Python 3.9). Persiste en JSONL. `read_log()` pour relecture.

### Sentry monitor (`sentry_monitor.py`)

Tache de fond qui poll Sentry via MCP toutes les `interval` secondes. Detecte les nouvelles erreurs, les deduplique, et auto-enqueue le circle `sentry` (ou `sentry-comments`). Auto-fix optionnel filtrable par niveau (`fatal`, `error`, ...).

---

## Circles

| Circle | Ritual | Familiar par defaut | Canal | on_complete |
|---|---|---|---|---|
| **dev** | plan -> elaborate -> arch-review -> implement -> tech-review -> qa-scenario | claude | #dev | — |
| **bugfix** | diagnose -> plan -> implement -> tech-review | claude | #bugfix | — |
| **sentry** | diagnose -> plan -> implement -> tech-review | claude | #sentry | review |
| **sentry-comments** | diagnose -> elaborate -> implement | claude | #sentry | — |
| **hotfix** | diagnose -> implement -> tech-review | claude | #bugfix | — |
| **devsecops** | audit (composer/npm audit + secrets + OWASP) -> plan -> implement -> tech-review | cursor | #devsecops | — |
| **discovery** | research (Notion + JIRA) -> elaborate -> plan | claude | #discovery | — |
| **review** | mr-fetch -> **parallel**[mr-review, mr-review-arch, mr-review-quality] -> mr-summary | claude | #reviews | — |

Le `familiar` par defaut est ecrase par le `runner:` defini au niveau spell quand il y en a un (ex: arch-review -> gemini, tech-review -> cursor).

### Permissions

Roles Discord filtres dans `arcane_config.yaml` :

```yaml
permissions:
  default: all
  admin: all
  dev: [dev, bugfix, hotfix, review]
  ops: [sentry, sentry-comments, devsecops]
  product: [discovery]
```

### Boucles de retry

Les pass conditions re-bouclent sur un spell precedent :

```
plan      <------ arch-review  (si verdict != 'approved', max 3)
implement <------ tech-review  (si verdict != 'approved', max 3)
```

Le feedback du reviewer est injecte dans le spell relance via `{reviewer_feedback}` (mecanisme **Echo**).

### Points de pause (`auto_advance: false`)

arch-review, implement, tech-review, qa-scenario, mr-summary -> attendent `!<circle> resume`. Un bouton Discord "Resume" est ajoute au message de completion.

### Parallelisme

Le ritual `review` illustre le parallelisme : les 3 spells `mr-review*` tournent en `asyncio.gather` apres `mr-fetch`, puis `mr-summary` agrege leurs outputs.

---

## Spells

### Variables de template

| Variable | Description |
|---|---|
| `{task}` | Tache fournie par l'utilisateur |
| `{instructions}` | Instructions additionnelles + feedback injecte |
| `{previous_output}` | Output du spell precedent |
| `{step_output_<spell>}` | Output d'un spell specifique du meme run |
| `{step_summary_<spell>}` | Resume court d'un spell precedent |
| `{reviewer_feedback}` | Feedback du reviewer (Echo) pour les boucles de retry |
| `{pre_run_output}` | Sortie du script `pre_run:` |
| `{post_run_output}` | Sortie du script `post_run:` |
| `{workspace}` | Repos du workspace resolu |
| `{workspace_first}` | Chemin du repo principal (cwd des hooks bash) |
| `{forge_name}` | Nom du circle |
| `{docs_path}` | Chemin vers `kanta-ai-docs` |
| `{jira_id}` | ID JIRA extrait automatiquement (regex `[A-Z]+-\d+`) |
| `{issue_context}` | Contenu des docs locaux de l'issue (max 30K chars) |
| `{mr_project}` / `{mr_iid}` | Identifiants de la merge request GitLab |

### Tableau des spells

| Spell | Familiar | Timeout | MCP | Auto-advance | Pass condition | Hooks | Doc AIDD |
|---|---|---|---|---|---|---|---|
| **elaborate** | claude | 600s | oui | oui | — | — | functional_specs |
| **plan** | claude | 900s | non | oui | — | — | technical_plan |
| **arch-review** | gemini | 900s | non | non (pause) | `verdict == 'approved'` | — | architectural_review |
| **implement** | claude | 1800s | oui | non (pause) | — | git_prepare, post_run (tests) | implementation |
| **tech-review** | cursor | 900s | non | non (pause) | `verdict == 'approved'` | pre_run (git diff), git_finalize | code_review |
| **qa-scenario** | claude | 900s | non | non (pause) | — | — | qa_scenarios |
| **diagnose** | claude | 600s | oui | oui | — | — | diagnosis |
| **audit** | gemini | 600s | non | non (pause) | — | pre_run (composer/npm audit + secrets + outdated) | security_audit |
| **research** | claude | 900s | oui (Notion/JIRA) | non (pause) | — | — | research |
| **mr-fetch** | claude | 300s | non | oui | — | pre_run (glab view+diff) | mr_fetch |
| **mr-review** | claude | 600s | non | oui | — | — | mr_code_review |
| **mr-review-arch** | gemini | 600s | non | oui | — | — | mr_architectural_review |
| **mr-review-quality** | cursor | 600s | non | oui | — | — | mr_quality_review |
| **mr-summary** | claude | 600s | non | non (pause) | — | — | mr_review_summary |

### Schema d'un spell (`skills.yaml`)

```yaml
spells:
  plan:
    command: /plan
    runner: claude                    # familiar
    description: "..."
    timeout: 900
    auto_advance: true
    next_on_pass: arch-review
    next_on_fail: null
    pass_condition: null              # ex: "verdict == 'approved'"
    required_fields: [verdict, score, summary]   # optionnel, valide le JSON de sortie
    cache_outputs: true               # skip si inputs identiques
    git_prepare: false                # cree la branche feature avant le spell
    git_finalize: false               # push + ouvre la MR a la fin du spell
    pre_run: "cd {workspace_first} && git diff develop...HEAD"
    post_run: "cd {workspace_first} && composer test --no-interaction"
    runner_kwargs:
      allowed_tools: null             # desactive les MCP pour aller plus vite
    prompt: |
      Template avec {task}, {issue_context}, {step_output_elaborate},
      {reviewer_feedback}, {pre_run_output}, {post_run_output}, etc.
```

### MCP par spell

Les spells qui ont besoin d'acceder a JIRA, Figma, Notion, Sentry heritent de la config globale `claude.allowed_tools`. Les autres desactivent les MCP via `runner_kwargs.allowed_tools: null` pour gagner en latence.

- **Avec MCP** : elaborate, implement, diagnose.
- **Sans MCP** : plan, arch-review, tech-review, qa-scenario, audit, research, mr-*.

---

## Workflow engine

### Algorithme principal

```
POUR chaque entree du ritual:
    SI entree est une liste "parallel":
        lancer tous les spells en asyncio.gather
        SI l'un echoue: FAIL du ritual
        CONTINUER apres le groupe

    POUR le spell courant:
        1. Construire le prompt (variables + issue_context + feedback + pre_run_output)
        2. SI git_prepare: creer / checkout la branche feature
        3. SI pre_run: executer le script bash et capturer la sortie
        4. Lancer heartbeat (message Discord toutes les 30s)
        5. Executer via le familiar avec streaming (~1000 chars)
        6. SI post_run: executer le script bash et capturer la sortie
        7. SI git_finalize: push + ouvre MR GitLab
        8. Sauvegarder (state + bus JSON + doc AIDD + Discord attachment)

        SI required_fields: valider le JSON de sortie
        SI pass_condition existe:
            Extraire le JSON (3 strategies)
            Evaluer via safe_evaluate_condition()
            SI echoue et retries < max: injecter reviewer_feedback et goto next_on_fail
            SI echoue et retries >= max: FAIL

        SI auto_advance == false:
            PAUSE (attendre !<circle> resume)

        passer a next_on_pass
```

### Reprise partielle (`run_from_skill`)

`!<circle> from <spell>` :
1. Conserve les `step_outputs` des spells precedents.
2. Supprime les outputs des spells a partir du point de reprise.
3. Incremente le `run_number`.
4. Relance `_execute_from_current()` depuis l'index du spell.

### Cache des outputs

Un hash de `(prompt, instructions, step_outputs)` sert de cle. Si `cache_outputs: true` et le hash matche le run precedent, le spell est skippe et l'output precedent est reutilise. Utile pour les retries partiels.

### Echo (feedback inter-agents)

Quand une pass condition echoue, le resume du reviewer est injecte dans `{reviewer_feedback}` lors du retry du spell precedent. Cela permet a chaque run de tenir compte des objections du reviewer sans intervention humaine.

---

## Familiars (runners)

### Claude Code CLI

```
claude -p --allowedTools "mcp__*" --permission-mode auto
```

- Prompt passe via stdin.
- `ANTHROPIC_API_KEY` retire de l'env (force auth abonnement Claude).
- Options configurables : `allowed_tools`, `mcp_config`, `permission_mode`, `extra_args`.
- Override par spell via `runner_kwargs` dans `skills.yaml`.
- `cwd` = repo workspace principal.

### Cursor Agent CLI

```
/Applications/Cursor.app/Contents/Resources/app/bin/cursor agent -p --trust --force --approve-mcps
```

- Detection : `shutil.which("cursor")` + chemin macOS hardcode.
- Flags : `--trust` (workspace), `--force` (commandes), `--approve-mcps` (serveurs MCP).
- Fallback automatique sur Claude si absent ou en erreur.

### Gemini CLI

```
gemini < prompt
```

- `GEMINI_API_KEY` et `GOOGLE_API_KEY` propages dans l'env.
- Fallback automatique sur Claude si absent ou en erreur.

---

## MCP et outils cloud

Les MCP cloud de Claude (Atlassian, Notion, Figma, Sentry, Slack) sont actives via `--allowedTools` sur le CLI.

**Config globale** (`arcane_config.yaml`) :
```yaml
claude:
  allowed_tools: "mcp__claude_ai_Atlassian__*,mcp__claude_ai_Notion__*,mcp__claude_ai_Figma__*,mcp__claude_ai_Sentry__*,mcp__claude_ai_Slack__*"
  permission_mode: "auto"
```

**Desactivation par spell** (`skills.yaml`) :
```yaml
plan:
  runner_kwargs:
    allowed_tools: null   # Pas de MCP -> plus rapide
```

---

## Resolution du contexte issue

Quand un JIRA ID est detecte dans la tache (ex: `LAB-1890`), `resolve_issue_context()` :

1. Scanne `docs_path` et les repos du workspace a la recherche de dossiers `{JIRA_ID}-*`.
2. Charge les fichiers `.md` trouves, tries dans l'ordre logique (functional_specs -> technical_plan -> architectural_review -> ...).
3. Tronque a 10K chars par fichier, 30K chars au total.
4. Injecte le contenu dans `{issue_context}` du prompt.
5. Le resultat est cache dans `step_outputs` pour eviter les relectures.

Si aucun doc n'est trouve, le prompt invite a decrire la tache dans la commande.

---

## Message bus

Pub/sub asynchrone + persistance JSONL.

- `publish(message)` : envoie au channel, a tous les subscribers, et append au JSONL (`bus/<circle>/messages.jsonl`).
- `subscribe(callback)` : appelle le callback pour chaque message (un try/except isole les callbacks, une erreur n'arrete pas le bus).
- `read_log(circle, limit)` : relit les derniers N messages depuis le JSONL (utile pour `!<circle> log`).

Types de messages : `status`, `progress`, `heartbeat`, `streaming`, `result`, `error`, `feedback_injected`.

---

## Bot Discord

### Commandes dynamiques

Un circle du fichier `arcane_config.yaml` expose automatiquement les commandes `!<circle> ...`.

| Commande | Description |
|---|---|
| `!<circle> <description>` | Ritual complet |
| `!<circle> from <spell> [instructions]` | Reprise depuis un spell |
| `!<circle> skill <spell> [instructions]` | Spell isole (mot-cle legacy `skill`) |
| `!<circle> resume [instructions]` | Reprendre apres pause |
| `!<circle> retry [instructions]` | Relancer le spell courant |
| `!<circle> abort` | Avorter le spell en cours |
| `!<circle> status` | Progression visuelle du ritual |
| `!<circle> log [N]` | Derniers N messages |
| `!<circle> reset` | Reset du circle |

### Commandes globales `!arcane`

| Commande | Description |
|---|---|
| `!arcane status` | Etat global + queue + familiars disponibles |
| `!arcane forges` | Liste des circles |
| `!arcane skills` | Liste des spells |
| `!arcane metrics` | Duree, tokens, cout $ par circle |
| `!arcane reload` | Hot-reload de la config |
| `!arcane reset` | Reset de tous les circles |
| `!arcane sentry check` | Verification Sentry manuelle |
| `!arcane sentry start` / `stop` / `status` | Controle du Sentry monitor |

### Slash commands

- `/forge <circle> <task>` avec autocompletion sur les circles.
- `/skill <circle> <spell>` avec autocompletion sur les spells.
- `/arcane status | forges | skills | metrics | reload`.

### Status visuel

```
🔴 Circle dev — error (run #7)
📋 Tache : LAB-1890 Gestion des zones geographiques

Ritual :
  ✅ /plan
  ❌ /elaborate <- echoue
  ⬜ /arch-review
  ⬜ /implement
  ⬜ /tech-review
  ⬜ /qa-scenario

⚠️ Erreur : Runner error: timeout after 600s

💡 !dev from <spell> pour reprendre depuis une etape
```

### Feedback temps reel

- **Heartbeat** : message toutes les 30s pendant l'execution d'un spell.
- **Streaming** : preview toutes les ~1000 chars de l'output.
- **Resultat** : preview des 8 premieres lignes + upload du fichier `.md` en piece jointe.
- **Feedback libre** : tout message non-commande dans le canal du circle est injecte dans le spell en cours via `{instructions}` et apparait dans la boucle Echo.

### Threads, dashboard, boutons

- Un thread est cree par run. Reuse si un thread existe deja pour le circle+run.
- Un message pinne (dashboard) est auto-mis a jour avec l'etat de tous les circles.
- Boutons : **Resume** / **Retry** / **Reset** sur les pauses ; **Push + MR** sur `implement` quand `git_prepare` est actif.

### Erreurs

`on_command_error` capture et affiche les erreurs de commandes dans Discord (pas de silence).

---

## Sentry monitor

Tache de fond dans `sentry_monitor.py`.

**Config** (`arcane_config.yaml`) :

```yaml
sentry_monitor:
  enabled: true
  interval: 3600          # secondes entre deux polls
  auto_fix: false         # auto-enqueue le circle sentry
  auto_fix_levels:
    - fatal               # ne touche que aux erreurs fatales si auto_fix=true
```

**Boucle** :
1. Poll Sentry via MCP (`mcp__claude_ai_Sentry__*`).
2. Deduplique contre les erreurs deja traitees (state sur disque).
3. Notifie `#sentry` avec un resume.
4. Si `auto_fix: true` et le niveau matche `auto_fix_levels`, enqueue automatiquement le circle `sentry` avec l'ID Sentry en tache.

**Controle manuel** : `!arcane sentry {check|start|stop|status}`.

---

## Persistence et output AIDD

### Output AIDD (dossier issues)

Les spells sauvegardent leurs outputs dans `issues_dir` avec le nommage AIDD :

```
{issues_dir}/{JIRA_ID}-{titre-slugifie}/
├── {JIRA_ID}-functional_specs.md       <- elaborate
├── {JIRA_ID}-technical_plan.md         <- plan
├── {JIRA_ID}-architectural_review.md   <- arch-review
├── {JIRA_ID}-implementation.md         <- implement
├── {JIRA_ID}-code_review.md            <- tech-review
└── {JIRA_ID}-qa_scenarios.md           <- qa-scenario
```

Pour les reviews GitLab : `mr_fetch.md`, `mr_code_review.md`, `mr_architectural_review.md`, `mr_quality_review.md`, `mr_review_summary.md`.

Le mapping spell -> doc est dans `Circle._SKILL_TO_DOC`.

### Donnees internes (bus)

| Chemin | Contenu |
|---|---|
| `bus/<circle>/state.json` | Etat courant du circle |
| `bus/<circle>/skill_<name>.json` | Output JSON brut |
| `bus/<circle>/skill_<name>_output.md` | Output lisible |
| `bus/<circle>/messages.jsonl` | Log cumule |
| `bus/<circle>/runs/<N>/` | Archives par run |

---

## Configuration

### `arcane_config.yaml`

```yaml
# Noms de dossier (identiques dans chaque workspace)
repos:
  api: kanta-api-v2
  front: kanta-front-v2
  ui: kanta-ui
  connector: kanta-connector-system
  database: kanta-database
  basique-api: basique-api
  # ...

# Groupes de workspace : un dossier de base + une liste de repos
workspace_groups:
  feature:
    base_env: WORKSPACE_FEATURE          # resolu depuis .env
    repos: [api, front, ui, connector, database, basique-api]
  bug:
    base_env: WORKSPACE_BUG
    repos: [api, front, ui, connector, database, basique-api]
  sentry:
    base_env: WORKSPACE_SENTRY
    repos: [api, basique-api]
  review:
    base_env: WORKSPACE_REVIEW
    repos: [api, front, ui, connector, database, basique-api]
  infra:
    base_env: WORKSPACE_FEATURE
    repos: [api, front, infra]

defaults:
  familiar: claude
  max_retries: 3
  timeout: 300

claude:
  allowed_tools: "mcp__claude_ai_Atlassian__*,mcp__claude_ai_Notion__*,..."
  permission_mode: "auto"

sentry_monitor:
  enabled: true
  interval: 3600
  auto_fix: false
  auto_fix_levels: [fatal]

permissions:
  default: all
  admin: all
  dev: [dev, bugfix, hotfix, review]
  ops: [sentry, sentry-comments, devsecops]
  product: [discovery]

circles:
  dev:
    description: "Developpement de nouvelles fonctionnalites"
    familiar: claude
    channel: dev
    workspace_group: feature
    ritual:
      - plan
      - elaborate
      - arch-review
      - implement
      - tech-review
      - qa-scenario

  sentry:
    description: "Resolution d'erreurs Sentry"
    familiar: claude
    channel: sentry
    workspace_group: sentry
    on_complete: review            # chain -> review circle apres completion
    ritual:
      - diagnose
      - plan
      - implement
      - tech-review

  review:
    description: "Review multi-agents d'une MR GitLab"
    familiar: claude
    channel: reviews
    workspace_group: review
    ritual:
      - mr-fetch
      - parallel:                  # lance les 3 spells en asyncio.gather
        - mr-review
        - mr-review-arch
        - mr-review-quality
      - mr-summary
```

### `skills.yaml`

Voir [Schema d'un spell](#spells) plus haut.

### `.env`

```
DISCORD_BOT_TOKEN=...
GEMINI_API_KEY=...

WORKSPACE_FEATURE=~/Documents/workspace_feature/app
WORKSPACE_BUG=~/Documents/workspace_bug/app
WORKSPACE_SENTRY=~/Documents/workspace_sentry/app
WORKSPACE_REVIEW=~/Documents/workspace_review/app

DOCS_PATH=~/.workspace-manager/kanta-ai-docs
ISSUES_DIR=~/Documents/workspace_feature/app/kanta-app/docs/issues
```

---

## Tests

93 tests couvrant les fonctions pures et le workflow engine (mocks pour subprocess et Discord).

```bash
python3 -m pytest tests/ -v
```

| Module | Tests | Couvre |
|---|---|---|
| `tests/test_message_bus.py` | 8 | pub/sub, JSONL, read_log, resilience callbacks |
| `tests/test_runner.py` | 13 | RunnerResult, factory, Gemini, Cursor fallback, token parsing |
| `tests/test_forge.py` | 51 | extract_json, safe_evaluate_condition, build_prompt, jira_id, docs_path, pass_condition, required_fields |
| `tests/test_forge_workflow.py` | 15 | workflow complet, parallel, pause/resume, retry loops, reviewer_feedback, restart keeps outputs, state persistence |
| `tests/test_discord_bot.py` | 6 | chunk_message |
| **Total** | **93** | |

Syntax check rapide :

```bash
python3 -m py_compile arcane.py forge.py runner.py discord_bot.py message_bus.py sentry_monitor.py
```

---

## Logging

| Logger | Module |
|---|---|
| `arcane.core` | `arcane.py` |
| `arcane.circle` | `forge.py` |
| `arcane.familiar` | `runner.py` |
| `arcane.bus` | `message_bus.py` |
| `arcane.discord` | `discord_bot.py` |
| `arcane.sentry` | `sentry_monitor.py` |

---

## Limites et evolutions

### Limites actuelles

- **Timeout global par spell** : couvre runner + MCP + reflexion, pas differenciable.
- **Cursor macOS only** : chemin hardcode `/Applications/Cursor.app/`.
- **Sentry monitor via MCP uniquement** : pas d'API directe, depend de la session Claude MCP.
- **Terminologie legacy** : `skills.yaml`, mot-cle `skill`, slash `/forge`, sous-commande `!arcane forges` — cohabitent encore avec le vocabulaire Arcane.

### Evolutions envisageables

- [ ] Migration complete du vocabulaire legacy (`skills.yaml` -> `spells.yaml`, `/forge` -> `/circle`, `!arcane forges` -> `!arcane circles`).
- [ ] Dashboard web (etat des circles + historique).
- [ ] Diff automatique entre runs successifs.
- [ ] Webhooks pour notifications externes.
- [ ] Support Linux / Windows pour Cursor.
- [ ] Retention / archivage automatique du repertoire `bus/`.
