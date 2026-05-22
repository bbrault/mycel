# Plan d'Amélioration Arcane (AIDD Orchestrator)

Ce document détaille les axes d'amélioration techniques et fonctionnels pour le projet Arcane, classés par priorité.

## 📊 Synthèse de l'Analyse
Arcane est un orchestrateur multi-agents robuste utilisant le Model Context Protocol (MCP) pour l'automatisation du cycle de développement. Sa force réside dans son système de "rituels" et sa capacité à faire collaborer différents modèles (Claude, Gemini, Cursor).

---

## 🚀 Priorité P0 : Fiabilité & Expérience Utilisateur
*Ces éléments sont critiques pour une utilisation en production quotidienne.*

### 1. Persistance de l'État (Workflow Continuity)
- **Problème** : L'état des cercles est en mémoire. Un redémarrage du bot perd la file d'attente et l'avancement des rituels longs.
- **Action** : Implémenter une base SQLite pour stocker l'état de la `Dispatch` et des `Circle`. Permettre la reprise (`!resume`) après crash.
- **Fichier impacté** : `dispatch.py`, `forge.py`.

### 2. Human-in-the-Loop (Interactive Approval)
- **Problème** : Les agents s'enchaînent sans validation humaine, ce qui peut mener à des erreurs coûteuses en tokens.
- **Action** : Ajouter un champ `wait_for_approval` dans `skills.yaml`. Utiliser les composants Discord (boutons Approuver/Rejeter) entre des étapes critiques comme `plan` et `implement`.
- **Fichier impacté** : `forge.py`, `discord_bot.py`.

---

## 🛠 Priorité P1 : Excellence Technique & Scalabilité
*Améliorations structurelles pour faciliter la maintenance et l'extension.*

### 1. Validation de Configuration (Pydantic)
- **Problème** : Les fichiers YAML sont chargés sans validation de schéma stricte.
- **Action** : Créer des modèles Pydantic pour `dispatch_config.yaml` et `skills.yaml`. Valider au démarrage pour éviter les erreurs de runtime.
- **Fichier impacté** : `dispatch.py`.

### 2. I/O Asynchrone (Non-blocking performance)
- **Problème** : Les lectures de fichiers dans `resolve_issue_context` sont synchrones.
- **Action** : Migrer les lectures de docs et de logs vers `aiofiles`.
- **Fichier impacté** : `forge.py`, `message_bus.py`.

### 3. Système de Plugins pour Moniteurs
- **Problème** : `SentryMonitor` et `AikidoMonitor` sont codés en dur dans `Dispatch`.
- **Action** : Créer une classe de base `BaseMonitor` et charger les moniteurs dynamiquement via la config.
- **Fichier impacté** : `dispatch.py`, `sentry_monitor.py`, `aikido_monitor.py`.

---

## 💡 Priorité P2 : Nouvelles Capacités (Innovation)
*Fonctionnalités avancées pour transformer Arcane en plateforme complète.*

### 1. RAG Codebase Indexing (Sémantique)
- **Action** : Intégrer un outil d'indexation vectorielle local (type ChromaDB ou simple BM25) pour que le skill `research` puisse interroger la codebase de manière sémantique.

### 2. Exécution de Tests E2E automatisés
- **Action** : Ajouter un runner capable de lancer des tests Playwright/Cypress après l'étape `implement` et de remonter les screenshots en cas d'échec dans Discord.

### 3. Dashboard ROI & Metrics
- **Action** : Étendre `!dispatch metrics` pour calculer le coût estimé par ticket JIRA et générer un rapport hebdomadaire sur le temps humain économisé vs coût API.

---

## 📝 Roadmap de Mise en Œuvre
1. **Semaine 1 (P0)** : Mise en place de la persistance SQLite et des boutons d'approbation Discord.
2. **Semaine 2 (P1)** : Refactoring vers Pydantic et migration `aiofiles`.
3. **Semaine 3 (P1/P2)** : Formalisation de l'interface Monitor et prototypage du RAG.
