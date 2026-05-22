# Mycel — guide utilisateur

Ce guide s’adresse aux personnes qui lancent et suivent des travaux via Discord. Pour l’architecture et la configuration serveur, voir `DOCUMENTATION.md` et `README.md`.

## 1. À quoi sert Mycel ?

Mycel enchaîne automatiquement des étapes d’IA (analyse, plan, implémentation, revue, etc.) sur vos dépôts, à partir d’un message dans le bon canal Discord. Chaque **forge** (ex. `dev`, `bugfix`, `sentry`) correspond à un **rituel** adapté au type de travail.

## 2. Prérequis côté Discord

- Le bot doit être présent sur le serveur et avoir l’**intent « Message Content »** activée.
- Vous devez disposer des **rôles** autorisés pour la forge que vous utilisez (voir `permissions` dans `mycel_config.yaml`).
- Utiliser le **canal** configuré pour la forge (ex. `#dev` pour la forge `dev`).

## 3. Démarrer un rituel

Dans le canal de la forge, envoyez une commande **`!<nom_forge>`** : le nom est **exactement** la clé définie sous `forges:` dans `mycel_config.yaml` (liste : `!mycel forges`). Exemples fréquents :

- `!dev …` — développement fonctionnel
- `!bugfix …` — correction de bug
- `!hotfix …` — correctif urgent
- `!sentry …` — traitement d’incident Sentry (selon config)
- `!review …` — revue de merge request (URL GitLab)
- `!discovery …` — découverte produit
- `!devsecops …` — audit sécurité
- `!aikido …` — remédiation Aikido

Tout le texte après le préfixe est la **tâche** transmise au premier sort (souvent un identifiant Jira du type `PROJ-1234`, une description, ou une URL de MR).

Le bot crée en général un **fil de discussion (thread)** : continuez la conversation **dans ce fil** pour la même session.

### Contenu utile dans la tâche

- Un **ID Jira** (`ABC-123`) pour lier dossiers locaux, contexte et éventuels worktrees.
- Une **URL de merge request** pour la forge `review`.
- Pour Sentry / Aikido, les identifiants ou le texte attendus par votre organisation (voir rituels configurés).

## 4. Suivre et influencer le travail

### Pendant l’exécution

- Les réponses du modèle peuvent **streamer** dans le fil.
- Un **tableau de bord** ou messages épinglés peuvent résumer l’état (selon déploiement).

### Ajouter du contexte sans commande

Dans le fil actif, un message **sans** préfixe `!` peut être pris comme **feedback** : le bot confirme souvent avec une réaction (ex. 📝). Ce texte est ajouté aux **instructions** du **prochain** sort — pas au sort déjà en cours.

### Commandes de contrôle (dans le fil ou le canal, selon habillage du bot)

Remplacez `<forge>` par le nom logique de la forge (`dev`, `bugfix`, etc.) :

| Commande | Effet |
|----------|--------|
| `!<forge> status` | Affiche l’avancement du rituel. |
| `!<forge> log` ou `!<forge> log 50` | Dernières lignes du journal du bus pour cette forge. |
| `!<forge> resume [texte]` | Reprend après une pause ; texte optionnel ajouté aux instructions. |
| `!<forge> retry [texte]` | Relance l’étape en cours (échec / pause) ; texte optionnel. |
| `!<forge> abort` | Annule l’exécution en cours (état « en pause »). |
| `!<forge> reset` | Remet la forge à l’état idle (conserve les **métriques** et le compteur de run). |
| `!<forge> reset metrics` | Réinitialise l’état **et** remet à zéro les compteurs de métriques / numéro de run. |

### Sort isolé ou reprise plus loin dans le rituel

| Commande | Effet |
|----------|--------|
| `!<forge> spell <nom> [instructions]` | Exécute **un seul** sort (`spell`, `skill` et `step` sont équivalents). |
| `!<forge> from <nom> [instructions]` | Reprend le rituel **à partir** de ce sort en **conservant** les sorties des étapes précédentes. |

## 5. Commandes globales (`!mycel`)

Ces commandes ne ciblent pas une forge en particulier (sauf indication) :

| Commande | Effet |
|----------|--------|
| `!mycel status` | Vue d’ensemble : forges, files, familiers, extraits des monitors. |
| `!mycel forges` | Liste des forges configurées (`workflow` / `circles` = synonymes). |
| `!mycel spells` | Liste des sorts définis dans `spells.yaml`. |
| `!mycel metrics` | Temps, tokens, estimation de coût des derniers enregistrements. |
| `!mycel mcp` | Santé des serveurs MCP vus par `claude mcp list`. |
| `!mycel reload` | Recharge `mycel_config.yaml` et `spells.yaml` sans redémarrer le bot. |
| `!mycel reset` | Réinitialise l’état de **toutes** les forges. |
| `!mycel reset metrics` | Idem + remise à zéro des métriques partout. |
| `!mycel sentry check \| start \| stop \| status` | Contrôle du monitor Sentry. |
| `!mycel aikido check \| start \| stop \| status` | Contrôle du monitor Aikido. |

**Alias :** `!dispatch` = `!mycel`.

## 6. Commandes slash

Disponibilité selon enregistrement du bot sur le serveur :

- `/forge` — lancer un rituel avec nom de forge + tâche.
- `/spell` — lancer un sort précis sur une forge.
- Groupe `/mycel` — `status`, `forges`, `spells`, `metrics`, `mcp`, `reload`, `reset-metrics`, etc.

## 7. Permissions

Les rôles Discord sont associés à des listes de forges dans `mycel_config.yaml` (`permissions:`). Si votre rôle n’a pas accès à une forge, le bot refusera la commande. Demandez à un administrateur d’ajuster les rôles ou la config.

## 8. Cas d’usage rapides

- **Nouvelle fonctionnalité :** dans `#dev`, `!dev PROJ-456 Courte description` puis suivi dans le fil ; ajoutez des précisions en message simple si besoin.
- **Bug :** `!bugfix PROJ-789 Reproduire : …`
- **Revue de MR :** `!review https://gitlab.com/…/merge_requests/…` dans le canal review configuré.
- **Relance après revue négative :** le système peut injecter un `{reviewer_feedback}` ; utilisez `retry` ou des instructions pour orienter la correction.

## 9. Dépannage

| Problème | Piste |
|----------|--------|
| « Rien ne se passe » | Vérifier le bon canal, les rôles, que le bot est en ligne. |
| Erreur CLI / timeout | `!mycel status`, `!<forge> log` ; un timeout peut être augmenté dans `spells.yaml` ou `defaults`. |
| Mauvaise étape | `status` puis `retry` ou `from <sort>` si vous voulez repartir d’un point précis. |
| Config modifiée | `!mycel reload` côté opérateur ; sinon redémarrage du process `python discord_bot.py`. |
| Feedback non pris en compte | Le message sans `!` s’applique au **sort suivant** ; évitez d’envoyer pendant que le même sort tourne si vous attendez un changement immédiat — utilisez plutôt `abort` puis `resume` avec instructions. |

## 10. Où en savoir plus ?

- `README.md` — installation et démarrage rapide.
- `DOCUMENTATION.md` — détails techniques (bus, état, YAML, variables de prompt).
