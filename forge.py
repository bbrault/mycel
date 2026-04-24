from __future__ import annotations

import asyncio
import glob as globmod
import hashlib
import json
import logging
import operator
import os
import re
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional

from message_bus import Message, MessageBus
from runner import StreamCallback, get_runner

logger = logging.getLogger("arcane.circle")


# ------------------------------------------------------------------
# Issue context resolver
# ------------------------------------------------------------------

def resolve_issue_context(jira_id: str, docs_path: str, workspace: Dict[str, str]) -> str:
    """Search for local issue docs matching a JIRA ID and return their content."""
    if not jira_id:
        return "(aucun ID JIRA fourni)"

    md_files: List[str] = []

    # Search patterns (docs_path and workspace repos)
    search_roots = [docs_path] + list(workspace.values())

    for root in search_roots:
        if not root or not os.path.isdir(root):
            continue
        # Glob for directories starting with the JIRA ID
        for pattern in [
            os.path.join(root, "**", f"{jira_id}*"),
        ]:
            for match in globmod.glob(pattern, recursive=True):
                if os.path.isdir(match):
                    for f in sorted(os.listdir(match)):
                        if f.endswith(".md"):
                            md_files.append(os.path.join(match, f))

    if not md_files:
        logger.info("Aucun doc local trouve pour %s", jira_id)
        return f"(aucune documentation locale trouvee pour {jira_id} — decrivez la tache dans la commande)"

    # Deduplicate by filename
    seen: Dict[str, str] = {}
    for path in md_files:
        name = os.path.basename(path)
        if name not in seen:
            seen[name] = path

    # Sort in logical workflow order
    PRIORITY = [
        "functional_specs", "technical_plan", "architectural_review",
        "code_review", "privacy_review", "qa_scenarios", "functional_review",
    ]

    def _sort_key(name: str) -> int:
        for i, keyword in enumerate(PRIORITY):
            if keyword in name:
                return i
        return len(PRIORITY)

    sorted_names = sorted(seen.keys(), key=_sort_key)

    parts = [f"--- Documentation locale pour {jira_id} ---\n"]
    total_chars = 0
    MAX_TOTAL = 30000
    MAX_PER_FILE = 10000

    for name in sorted_names:
        path = seen[name]
        try:
            content = open(path, "r", encoding="utf-8").read()
        except OSError:
            continue

        if total_chars >= MAX_TOTAL:
            parts.append(f"\n[... fichiers restants ignores — limite de contexte atteinte ...]")
            break

        if len(content) > MAX_PER_FILE:
            content = content[:MAX_PER_FILE] + f"\n\n[... {name} tronque a {MAX_PER_FILE} chars ...]"

        remaining = MAX_TOTAL - total_chars
        if len(content) > remaining:
            content = content[:remaining] + "\n\n[... tronque ...]"

        parts.append(f"\n### {name}\n\n{content}")
        total_chars += len(content)

    logger.info("Issue %s: %d fichier(s) charge(s) (%d chars)", jira_id, len(sorted_names), total_chars)
    return "\n".join(parts)

# ------------------------------------------------------------------
# Safe condition evaluator (replaces eval())
# ------------------------------------------------------------------

_OPERATORS: Dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}


def _safe_evaluate_single(condition: str, data: Dict[str, Any]) -> bool:
    """Evaluate a single comparison like ``verdict == 'approved'``."""
    for op_str in sorted(_OPERATORS, key=len, reverse=True):
        if op_str not in condition:
            continue
        parts = condition.split(op_str, 1)
        if len(parts) != 2:
            continue
        left_key = parts[0].strip()
        right_raw = parts[1].strip().strip("'\"")

        left_val = data.get(left_key)
        if left_val is None:
            return False

        # Numeric comparison
        try:
            left_num = float(left_val) if not isinstance(left_val, (int, float)) else left_val
            right_num = float(right_raw)
            return _OPERATORS[op_str](left_num, right_num)
        except (ValueError, TypeError):
            pass

        # String comparison
        return _OPERATORS[op_str](str(left_val), right_raw)

    return False


def safe_evaluate_condition(condition: str, data: Dict[str, Any]) -> bool:
    """Evaluate a condition with optional ``and`` / ``or`` connectors."""
    if " and " in condition:
        return all(
            _safe_evaluate_single(part.strip(), data)
            for part in condition.split(" and ")
        )
    if " or " in condition:
        return any(
            _safe_evaluate_single(part.strip(), data)
            for part in condition.split(" or ")
        )
    return _safe_evaluate_single(condition, data)


# ------------------------------------------------------------------
# Robust JSON extraction
# ------------------------------------------------------------------


def extract_json(output: str) -> Optional[Dict[str, Any]]:
    """Try multiple strategies to extract a JSON object from runner output."""
    # Strategy 1: direct parse
    try:
        result = json.loads(output)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError):
        pass

    # Strategy 2: ```json code fence
    match = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", output, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(1))
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, TypeError):
            pass

    # Strategy 3: find outermost { … } in the text
    first_brace = output.find("{")
    last_brace = output.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = output[first_brace : last_brace + 1]
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def validate_output(output: str, skill: Dict[str, Any]) -> Optional[str]:
    """Validate that extracted JSON contains required fields.

    Returns None if valid, or an error message describing missing fields.
    """
    required = skill.get("required_fields")
    if not required:
        return None

    data = extract_json(output)
    if data is None:
        return "Output is not valid JSON"

    missing = [f for f in required if f not in data]
    if missing:
        return f"Missing required fields: {', '.join(missing)}"

    return None


# ------------------------------------------------------------------
# Forge
# ------------------------------------------------------------------


class Circle:
    """An execution environment that runs a state-machine of skills."""

    # Regex for JIRA-like IDs (e.g. LAB-123, KANTA-456)
    _JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

    # Regex for GitLab MR URLs (e.g. https://gitlab.com/group/project/-/merge_requests/42)
    _MR_URL_RE = re.compile(r"https?://[^/]+/(.+?)/-/merge_requests/(\d+)")

    # Map Cortex skill names → AIDD document names
    _SKILL_TO_DOC: Dict[str, str] = {
        "elaborate": "functional_specs",
        "plan": "technical_plan",
        "arch-review": "architectural_review",
        "implement": "implementation",
        "tech-review": "code_review",
        "qa-scenario": "qa_scenarios",
        "diagnose": "diagnosis",
        "audit": "security_audit",
        "research": "research",
        "mr-fetch": "mr_fetch",
        "mr-review": "mr_code_review",
        "mr-review-arch": "mr_architectural_review",
        "mr-review-quality": "mr_quality_review",
        "mr-summary": "mr_review_summary",
    }

    def __init__(
        self,
        name: str,
        description: str,
        workflow: List[str],
        skills: Dict[str, Dict[str, Any]],
        workspace: Dict[str, str],
        bus: MessageBus,
        default_runner: str = "claude",
        max_retries: int = 3,
        timeout: int = 180,
        bus_dir: str = "bus",
        docs_path: str = "",
        issues_dir: str = "",
        runner_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.name = name
        self.description = description
        self.workflow = workflow
        self.skills = skills
        self.workspace = workspace
        self.docs_path = docs_path
        self.issues_dir = issues_dir
        self.runner_kwargs = runner_kwargs or {}
        self.bus = bus
        self.default_runner = default_runner
        self.max_retries = max_retries
        self.timeout = timeout
        self.bus_dir = bus_dir

        self.state: Dict[str, Any] = {
            "status": "idle",
            "current_skill": None,
            "current_index": 0,
            "task": None,
            "instructions": None,
            "retries": {},
            "step_outputs": {},
            "previous_output": None,
            "error": None,
            "run_number": 0,
        }

        # Created lazily in _ensure_resume_event() — Python 3.9 event loop compat
        self._resume_event: Optional[asyncio.Event] = None
        self._extra_instructions: Optional[str] = None
        self._feedback_buffer: List[str] = []
        self._running_task: Optional[asyncio.Task[None]] = None

        self._load_state()

    def _ensure_resume_event(self) -> asyncio.Event:
        """Lazy-create the resume event inside the running event loop."""
        if self._resume_event is None:
            self._resume_event = asyncio.Event()
        return self._resume_event

    def _parse_mr_url(self) -> tuple:
        """Extract (project_path, mr_iid) from the task string, or ("", "")."""
        task_str = self.state.get("task") or ""
        match = self._MR_URL_RE.search(task_str)
        if match:
            return match.group(1), match.group(2)
        return "", ""

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    @property
    def _state_dir(self) -> str:
        return os.path.join(self.bus_dir, self.name)

    @property
    def _state_path(self) -> str:
        return os.path.join(self._state_dir, "state.json")

    @property
    def _run_dir(self) -> str:
        run_num = self.state.get("run_number", 0)
        return os.path.join(self.bus_dir, self.name, "runs", str(run_num))

    def _save_state(self) -> None:
        os.makedirs(self._state_dir, exist_ok=True)
        with open(self._state_path, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False, indent=2)

    def _load_state(self) -> None:
        if os.path.exists(self._state_path):
            with open(self._state_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            # Merge with defaults for keys added in newer versions
            for key, default in [("run_number", 0)]:
                loaded.setdefault(key, default)
            self.state = loaded

    def _save_skill_output(self, skill_name: str, output: str) -> None:
        payload = {"skill": skill_name, "output": output}

        # Save to current state dir (overwritten each run)
        os.makedirs(self._state_dir, exist_ok=True)
        path = os.path.join(self._state_dir, f"skill_{skill_name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

        # Save to versioned run dir (history)
        os.makedirs(self._run_dir, exist_ok=True)
        run_path = os.path.join(self._run_dir, f"skill_{skill_name}.json")
        with open(run_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def _archive_run(self) -> None:
        """Archive current state into the versioned run directory."""
        os.makedirs(self._run_dir, exist_ok=True)
        archive_path = os.path.join(self._run_dir, "state.json")
        with open(archive_path, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False, indent=2)

    def _get_issue_dir(self) -> Optional[str]:
        """Return the issue directory path for the current JIRA ID or MR, or None."""
        if not self.issues_dir:
            return None
        task_str = self.state.get("task") or ""
        jira_match = self._JIRA_RE.search(task_str)

        if jira_match:
            jira_id = jira_match.group(1)
            # Build slug from task title (text after JIRA ID, or full task if ID is embedded)
            title_part = task_str[jira_match.end():].strip()
            if not title_part:
                # JIRA ID alone — use text before it or the full task
                title_part = task_str[:jira_match.start()].strip() or task_str
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", title_part).strip("-").lower()
            slug = slug[:60]
            folder_name = f"{jira_id}-{slug}" if slug else jira_id
            return os.path.join(self.issues_dir, folder_name)

        # Fallback: MR URL → "MR-42-group-project"
        mr_project, mr_iid = self._parse_mr_url()
        if mr_project and mr_iid:
            project_slug = re.sub(r"[^a-zA-Z0-9]+", "-", mr_project).strip("-").lower()
            folder_name = f"MR-{mr_iid}-{project_slug}"
            return os.path.join(self.issues_dir, folder_name)

        return None

    def _save_issue_doc(self, skill_name: str, output: str) -> Optional[str]:
        """Save skill output as an AIDD-formatted doc in the issues directory.

        Returns the file path if saved, None otherwise.
        """
        issue_dir = self._get_issue_dir()
        if not issue_dir:
            return None

        task_str = self.state.get("task") or ""
        jira_match = self._JIRA_RE.search(task_str)
        if jira_match:
            prefix = jira_match.group(1)
        else:
            mr_project, mr_iid = self._parse_mr_url()
            prefix = f"MR-{mr_iid}" if mr_iid else "UNKNOWN"

        doc_name = self._SKILL_TO_DOC.get(skill_name, skill_name)
        filename = f"{prefix}-{doc_name}.md"

        os.makedirs(issue_dir, exist_ok=True)
        filepath = os.path.join(issue_dir, filename)

        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(output)

        logger.info("Issue doc sauvegarde: %s", filepath)
        return filepath

    def _next_run_number(self) -> int:
        return self.state.get("run_number", 0) + 1

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_prompt(self, skill: Dict[str, Any]) -> str:
        workspace_str = "\n".join(
            f"- {k}: {v}" for k, v in self.workspace.items()
        ) if self.workspace else "(aucun workspace)"

        feedback = ""
        if self._feedback_buffer:
            feedback = "\n\n**Feedback utilisateur** :\n" + "\n".join(self._feedback_buffer)
            self._feedback_buffer.clear()

        extra = self._extra_instructions or ""
        base_instructions = self.state.get("instructions") or ""
        all_instructions = f"{base_instructions}\n{extra}".strip()
        if feedback:
            all_instructions = f"{all_instructions}{feedback}"

        template: str = skill["prompt"]

        # Extract JIRA ID from task
        task_str = self.state.get("task") or ""
        jira_match = self._JIRA_RE.search(task_str)
        jira_id = jira_match.group(1) if jira_match else ""

        # Load local issue docs (only on first call per workflow, then cached)
        issue_ctx_key = f"_issue_context_{jira_id}"
        if jira_id and issue_ctx_key not in self.state.get("step_outputs", {}):
            issue_context = resolve_issue_context(jira_id, self.docs_path, self.workspace)
            self.state.setdefault("step_outputs", {})[issue_ctx_key] = issue_context
        else:
            issue_context = self.state.get("step_outputs", {}).get(issue_ctx_key, "(aucun contexte issue)")

        mr_project, mr_iid = self._parse_mr_url()
        pre_run_output = self.state.get("step_outputs", {}).get("_pre_run", "")
        post_run_output = self.state.get("step_outputs", {}).get("_post_run", "")

        # Reviewer feedback (inter-agent communication on rejection)
        reviewer_feedback = self.state.get("reviewer_feedback", "")
        if reviewer_feedback:
            # Consume after injection — only used once
            self.state["reviewer_feedback"] = ""

        variables: Dict[str, str] = {
            "task": task_str,
            "instructions": all_instructions,
            "previous_output": self.state.get("previous_output") or "(aucun)",
            "workspace": workspace_str,
            "forge_name": self.name,
            "docs_path": self.docs_path,
            "jira_id": jira_id,
            "issue_context": issue_context,
            "mr_project": mr_project,
            "mr_iid": mr_iid,
            "pre_run_output": pre_run_output,
            "post_run_output": post_run_output,
            "reviewer_feedback": reviewer_feedback or "(aucun — premier passage)",
        }

        for step_name, step_output in self.state.get("step_outputs", {}).items():
            variables[f"step_output_{step_name}"] = step_output

        # Step summaries (compressed context for downstream skills)
        for step_name, summary in self.state.get("step_summaries", {}).items():
            variables[f"step_summary_{step_name}"] = summary

        prompt = template
        for key, value in variables.items():
            prompt = prompt.replace("{" + key + "}", value)

        # Replace any remaining {step_output_XXX} or {step_summary_XXX} with placeholder
        prompt = re.sub(r"\{step_(?:output|summary)_[\w-]+\}", "(non disponible)", prompt)

        return prompt

    # ------------------------------------------------------------------
    # Pass condition evaluation
    # ------------------------------------------------------------------

    def _evaluate_pass_condition(self, condition: str, output: str) -> bool:
        data = extract_json(output)
        if data is None:
            logger.warning("Circle %s: impossible d'extraire du JSON pour la condition", self.name)
            return False
        try:
            return safe_evaluate_condition(condition, data)
        except Exception as exc:
            logger.error("Circle %s: erreur évaluation condition: %s", self.name, exc)
            return False

    # ------------------------------------------------------------------
    # Reviewer feedback extraction (inter-agent communication)
    # ------------------------------------------------------------------

    def _store_step_summary(self, skill_name: str, output: str) -> None:
        """Extract the summary field from skill output for context compression.

        Stores a short summary that downstream skills can use via {step_summary_*}
        instead of the full raw output, reducing prompt size significantly.
        """
        summaries = self.state.setdefault("step_summaries", {})
        data = extract_json(output)
        if data and "summary" in data:
            summary = str(data["summary"])[:1000]
            # Include verdict and score if present (useful for review skills)
            verdict = data.get("verdict", "")
            score = data.get("score", "")
            if verdict:
                summary = f"[verdict={verdict}, score={score}] {summary}"
            summaries[skill_name] = summary
        else:
            # Fallback: first 500 chars of output
            summaries[skill_name] = output.strip()[:500]

    def _extract_reviewer_feedback(self, reviewer_skill: str, output: str) -> str:
        """Extract structured feedback from a reviewer's rejection for the retry target.

        Builds a human-readable summary of what the reviewer found wrong,
        so the next agent can address the specific issues.
        """
        data = extract_json(output)
        if data is None:
            return f"/{reviewer_skill} a rejete mais le JSON n'a pas pu etre parse. Output brut (500 chars) :\n{output[:500]}"

        lines = [f"--- Feedback de /{reviewer_skill} (verdict: {data.get('verdict', '?')}, score: {data.get('score', '?')}) ---\n"]

        # Extract issues/findings from common reviewer output fields
        for field in ("issues", "findings", "blocking_issues", "pattern_violations",
                       "security_issues", "breaking_changes", "scalability_issues",
                       "coupling_issues", "code_smells", "complexity_issues"):
            items = data.get(field)
            if not items or not isinstance(items, list):
                continue
            lines.append(f"**{field}** :")
            for item in items[:10]:  # Cap at 10 items
                if isinstance(item, dict):
                    severity = item.get("severity", item.get("status", ""))
                    desc = item.get("description", item.get("details", ""))
                    file_ref = item.get("file", "")
                    suggestion = item.get("suggestion", item.get("remediation", ""))
                    line_parts = []
                    if severity:
                        line_parts.append(f"[{severity}]")
                    if file_ref:
                        line_parts.append(f"{file_ref}")
                    line_parts.append(str(desc))
                    if suggestion:
                        line_parts.append(f"→ {suggestion}")
                    lines.append(f"  - {' '.join(line_parts)}")
                else:
                    lines.append(f"  - {item}")

        # Include summary if present
        summary = data.get("summary", "")
        if summary:
            lines.append(f"\n**Resume** : {summary}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Streaming callback
    # ------------------------------------------------------------------

    def _make_stream_callback(self, skill_name: str) -> StreamCallback:
        """Create a callback that posts a single editable streaming preview to the bus."""
        state: Dict[str, Any] = {"total_chars": 0, "last_preview_at": 0}
        stream_id = f"{self.name}_{skill_name}"
        PREVIEW_EVERY = 3000  # chars between updates (less frequent = less Discord API calls)

        async def _callback(chunk: str) -> None:
            state["total_chars"] += len(chunk)
            if state["total_chars"] - state["last_preview_at"] < PREVIEW_EVERY:
                return
            state["last_preview_at"] = state["total_chars"]

            lines = chunk.strip().split("\n")
            preview = "\n".join(lines[-5:])[:300]

            await self.bus.publish(Message(
                source="forge",
                content=f"📡 **Circle {self.name}** → /{skill_name} ({state['total_chars']} chars)...\n```\n{preview}\n```",
                level="debug",
                forge_name=self.name,
                skill_name=skill_name,
                data={"streaming_update": True, "streaming_id": stream_id},
            ))

        return _callback

    async def _heartbeat(self, skill_name: str, runner_name: str) -> None:
        """Post a heartbeat message every 30s while a skill runs."""
        elapsed = 0
        INTERVAL = 30
        while True:
            await asyncio.sleep(INTERVAL)
            elapsed += INTERVAL
            await self.bus.publish(Message(
                source="forge",
                content=f"⏳ **Circle {self.name}** → /{skill_name} en cours ({elapsed}s, runner={runner_name})",
                level="debug",
                forge_name=self.name,
                skill_name=skill_name,
            ))

    # ------------------------------------------------------------------
    # Git workspace preparation
    # ------------------------------------------------------------------

    async def _git_exec(self, repo_path: str, *args: str, timeout: int = 30) -> str:
        """Run a git command in a repo and return stdout (with timeout)."""
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"git {' '.join(args)} timeout ({timeout}s) in {os.path.basename(repo_path)}")
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git {' '.join(args)} failed in {os.path.basename(repo_path)}: {err}")
        return stdout.decode("utf-8", errors="replace").strip()

    def _resolve_run_cwd(self) -> Optional[str]:
        """Determine the working directory for runner execution.

        Priority: first affected repo from plan > first workspace repo > None.
        This ensures claude/cursor run inside the project and have access to
        .claude/, .cursor/, CLAUDE.md, etc.
        """
        affected = self._get_affected_repos()
        if affected:
            for repo_name in affected:
                repo_path = self.workspace.get(repo_name)
                if repo_path and os.path.isdir(repo_path):
                    return repo_path

        # Fallback: first workspace repo
        for repo_path in self.workspace.values():
            if os.path.isdir(repo_path):
                return repo_path

        return None

    def _get_affected_repos(self) -> List[str]:
        """Detect which repos are affected from the plan output."""
        plan_output = self.state.get("step_outputs", {}).get("plan", "")
        if plan_output:
            plan_data = extract_json(plan_output)
            if plan_data and "affected_repos" in plan_data:
                return plan_data["affected_repos"]
        return []

    async def _prepare_git_repo(
        self, repo_name: str, repo_path: str, jira_id: str, skill_name: str,
    ) -> None:
        """Prepare a single repo: check clean, setup feature branch."""
        expected_branch = f"feature/{jira_id}"

        await self.bus.publish(Message(
            source="forge",
            content=f"🔀 **Circle {self.name}** → Verification git **{repo_name}**",
            level="info",
            forge_name=self.name,
            skill_name=skill_name,
        ))

        # 1. Check for uncommitted changes (ignore untracked files)
        status = await self._git_exec(repo_path, "status", "--porcelain", "--untracked-files=no")
        if status:
            dirty_files = len(status.strip().split("\n"))
            raise RuntimeError(
                f"Repo {repo_name} a {dirty_files} modification(s) non commitee(s). "
                f"Commitez ou stashez avant de lancer implement."
            )

        # 2. Check current branch
        current_branch = await self._git_exec(repo_path, "branch", "--show-current")

        if current_branch.startswith(f"feature/{jira_id}"):
            logger.info("Repo %s: deja sur %s", repo_name, current_branch)
            try:
                await self._git_exec(repo_path, "pull", "--ff-only", "origin", current_branch)
            except RuntimeError:
                logger.info("Repo %s: pas d'upstream pour %s, continue", repo_name, current_branch)
            await self.bus.publish(Message(
                source="forge",
                content=f"  ✅ **{repo_name}** — branche `{current_branch}` prete",
                level="info",
                forge_name=self.name,
            ))
        else:
            logger.info("Repo %s: sur %s, creation %s", repo_name, current_branch, expected_branch)

            await self._git_exec(repo_path, "checkout", "develop")
            try:
                await self._git_exec(repo_path, "pull", "origin", "develop")
            except RuntimeError:
                logger.warning("Repo %s: pull develop echoue, continue", repo_name)

            try:
                await self._git_exec(repo_path, "flow", "feature", "start", jira_id)
            except RuntimeError:
                try:
                    await self._git_exec(repo_path, "checkout", "-b", expected_branch)
                except RuntimeError:
                    await self._git_exec(repo_path, "checkout", expected_branch)

            new_branch = await self._git_exec(repo_path, "branch", "--show-current")
            await self.bus.publish(Message(
                source="forge",
                content=f"  ✅ **{repo_name}** — branche `{new_branch}` creee depuis develop",
                level="info",
                forge_name=self.name,
            ))

    async def _prepare_git(self, skill_name: str) -> None:
        """Prepare git workspace before implement (parallel across repos)."""
        skill = self.skills.get(skill_name, {})
        if not skill.get("git_prepare", False):
            return

        task_str = self.state.get("task") or ""
        jira_match = self._JIRA_RE.search(task_str)
        if not jira_match:
            logger.warning("Circle %s: pas de JIRA ID, skip git prepare", self.name)
            return
        jira_id = jira_match.group(1)

        affected = self._get_affected_repos()

        # Collect repos to prepare
        repos_to_prepare: List[tuple] = []
        for repo_name, repo_path in self.workspace.items():
            if not os.path.isdir(os.path.join(repo_path, ".git")):
                continue
            if affected and repo_name not in affected:
                logger.info("Repo %s: non concerne par le plan, skip", repo_name)
                continue
            repos_to_prepare.append((repo_name, repo_path))

        if not repos_to_prepare:
            return

        # Prepare all repos in parallel
        results = await asyncio.gather(
            *[self._prepare_git_repo(name, path, jira_id, skill_name) for name, path in repos_to_prepare],
            return_exceptions=True,
        )

        # Check for errors
        errors = [str(r) for r in results if isinstance(r, BaseException)]
        if errors:
            raise RuntimeError(errors[0])

        await self.bus.publish(Message(
            source="forge",
            content=f"🔀 **Circle {self.name}** → Workspace git pret pour /{skill_name} ({len(repos_to_prepare)} repos)",
            level="info",
            forge_name=self.name,
        ))

    # ------------------------------------------------------------------
    # JIRA notification
    # ------------------------------------------------------------------

    async def _notify_jira(self, skill_name: str, summary: str) -> None:
        """Post a comment on the JIRA issue to track workflow progress."""
        if not self.runner_kwargs.get("allowed_tools"):
            return  # No MCP tools configured

        task_str = self.state.get("task") or ""
        jira_match = self._JIRA_RE.search(task_str)
        if not jira_match:
            return
        jira_id = jira_match.group(1)

        comment = f"[Arcane] Forge {self.name} — /{skill_name} termine.\n{summary[:500]}"
        prompt = (
            f"Ajoute un commentaire sur le ticket JIRA {jira_id}. "
            f"Contenu du commentaire :\n\n{comment}\n\n"
            f"Utilise l'outil MCP Atlassian pour poster ce commentaire. "
            f"Reponds juste 'done' quand c'est fait."
        )

        try:
            import shutil
            claude_path = shutil.which("claude")
            if not claude_path:
                return

            allowed = self.runner_kwargs.get("allowed_tools", "")
            args = [claude_path, "-p", "--allowedTools", allowed, "--permission-mode", "auto"]

            env = os.environ.copy()
            env.pop("ANTHROPIC_API_KEY", None)

            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=60,
            )
            logger.info("Circle %s: JIRA %s notifie pour /%s", self.name, jira_id, skill_name)
        except Exception as exc:
            logger.warning("Circle %s: notification JIRA echouee: %s", self.name, exc)

    # ------------------------------------------------------------------
    # Git finalize (push + MR creation)
    # ------------------------------------------------------------------

    async def _finalize_git(self, skill_name: str) -> None:
        """Push branch and create MR after a successful skill (when git_finalize is set)."""
        skill = self.skills.get(skill_name, {})
        if not skill.get("git_finalize", False):
            return

        task_str = self.state.get("task") or ""
        jira_match = self._JIRA_RE.search(task_str)
        if not jira_match:
            logger.warning("Circle %s: pas de JIRA ID, skip git finalize", self.name)
            return
        jira_id = jira_match.group(1)

        # Get plan summary for MR description (if available)
        plan_output = self.state.get("step_outputs", {}).get("plan", "")
        mr_description = f"Ritual Arcane pour {jira_id}"
        if plan_output:
            from forge import extract_json
            plan_data = extract_json(plan_output)
            if plan_data and "summary" in plan_data:
                mr_description = plan_data["summary"]

        for repo_name, repo_path in self.workspace.items():
            if not os.path.isdir(os.path.join(repo_path, ".git")):
                continue

            # Check current branch
            current_branch = await self._git_exec(repo_path, "branch", "--show-current")
            if not current_branch.startswith("feature/"):
                continue

            # Check if there are commits to push
            try:
                ahead = await self._git_exec(repo_path, "rev-list", "--count", f"origin/develop..{current_branch}")
                if ahead.strip() == "0":
                    continue
            except RuntimeError:
                continue

            await self.bus.publish(Message(
                source="forge",
                content=f"🚀 **Circle {self.name}** → Push **{repo_name}** branche `{current_branch}`",
                level="info",
                forge_name=self.name,
                skill_name=skill_name,
            ))

            # Push
            try:
                await self._git_exec(repo_path, "push", "-u", "origin", current_branch)
            except RuntimeError as exc:
                await self.bus.publish(Message(
                    source="forge",
                    content=f"⚠️ **Circle {self.name}** → Push echoue pour **{repo_name}** : {exc}",
                    level="warning",
                    forge_name=self.name,
                ))
                continue

            # Create MR via glab (if glab is available)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "glab", "mr", "create",
                    "--title", f"[{jira_id}] {task_str[jira_match.end():].strip()[:80]}",
                    "--description", mr_description[:2000],
                    "--source-branch", current_branch,
                    "--target-branch", "develop",
                    "--no-editor",
                    cwd=repo_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                mr_output = stdout.decode("utf-8", errors="replace").strip()

                if proc.returncode == 0:
                    await self.bus.publish(Message(
                        source="forge",
                        content=f"✅ **Circle {self.name}** → MR creee pour **{repo_name}** : {mr_output}",
                        level="info",
                        forge_name=self.name,
                        skill_name=skill_name,
                    ))
                else:
                    err = stderr.decode("utf-8", errors="replace").strip()
                    await self.bus.publish(Message(
                        source="forge",
                        content=f"⚠️ **Circle {self.name}** → Creation MR echouee pour **{repo_name}** : {err[:300]}",
                        level="warning",
                        forge_name=self.name,
                    ))
            except (asyncio.TimeoutError, FileNotFoundError) as exc:
                await self.bus.publish(Message(
                    source="forge",
                    content=f"⚠️ **Circle {self.name}** → glab non disponible pour la creation MR : {exc}",
                    level="warning",
                    forge_name=self.name,
                ))

    # ------------------------------------------------------------------
    # Skill execution
    # ------------------------------------------------------------------

    async def _exec_hook(self, skill_name: str, hook_name: str, template: str, timeout_s: int = 120) -> str:
        """Execute a bash hook (pre_run or post_run) and return its stdout."""
        task_str = self.state.get("task") or ""
        mr_project, mr_iid = self._parse_mr_url()
        workspace_first = self._resolve_run_cwd() or "."
        cmd = (
            template
            .replace("{mr_project}", mr_project)
            .replace("{mr_iid}", mr_iid)
            .replace("{task}", task_str)
            .replace("{workspace_first}", workspace_first)
        )

        logger.info("Circle %s: %s pour /%s: %s", self.name, hook_name, skill_name, cmd[:100])
        await self.bus.publish(Message(
            source="forge",
            content=f"⚡ **Circle {self.name}** → /{skill_name} {hook_name} en cours...",
            level="info",
            forge_name=self.name,
            skill_name=skill_name,
        ))

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            output = stdout.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                logger.warning("Circle %s: %s failed (rc=%d): %s", self.name, hook_name, proc.returncode, err[:200])
                # For post_run, include both stdout and stderr (test results may be in either)
                return f"{output}\n(exit code {proc.returncode})\n{err[:500]}" if hook_name == "post_run" else f"({hook_name} error: {err[:500]})"

            logger.info("Circle %s: %s OK (%d chars)", self.name, hook_name, len(output))
            return output
        except asyncio.TimeoutError:
            logger.warning("Circle %s: %s timeout (%ds)", self.name, hook_name, timeout_s)
            return f"({hook_name} timeout)"
        except Exception as exc:
            logger.warning("Circle %s: %s exception: %s", self.name, hook_name, exc)
            return f"({hook_name} error: {exc})"

    async def _run_skill(self, skill_name: str) -> str:
        skill = self.skills[skill_name]

        # Git preparation (if configured for this skill)
        await self._prepare_git(skill_name)

        # Pre-run bash hook (e.g. fetch MR diff before calling the runner)
        pre_run_template = skill.get("pre_run")
        if pre_run_template:
            pre_run_output = await self._exec_hook(skill_name, "pre_run", pre_run_template)
            self.state.setdefault("step_outputs", {})["_pre_run"] = pre_run_output

        runner_name = skill.get("runner") or self.default_runner
        skill_timeout = skill.get("timeout") or self.timeout

        # Merge runner kwargs: global config + per-skill overrides
        kwargs = dict(self.runner_kwargs)
        skill_runner_kwargs = skill.get("runner_kwargs")
        if skill_runner_kwargs:
            kwargs.update(skill_runner_kwargs)
        runner = get_runner(runner_name, timeout=skill_timeout, **kwargs)

        prompt = self._build_prompt(skill)

        # Output caching: skip runner if same prompt was already executed
        # Skip cache when: git side effects, reviewer feedback active, or cache disabled
        has_side_effects = skill.get("git_prepare") or skill.get("git_finalize")
        has_feedback = bool(self.state.get("reviewer_feedback"))
        skip_cache = self.state.get("_skip_cache", False)
        cache_hit = None
        prompt_hash = ""
        if not has_side_effects and not has_feedback and not skip_cache:
            prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
            cache_path = os.path.join(self.bus_dir, self.name, f"cache_{skill_name}_{prompt_hash}.txt")
            if os.path.isfile(cache_path):
                try:
                    with open(cache_path, "r", encoding="utf-8") as fh:
                        cache_hit = fh.read()
                    logger.info("Circle %s: cache hit pour /%s (hash=%s)", self.name, skill_name, prompt_hash)
                except OSError:
                    cache_hit = None

        if cache_hit is not None:
            await self.bus.publish(Message(
                source="forge",
                content=f"⚡ **Circle {self.name}** → /{skill_name} — cache hit (resultat identique au run precedent)",
                level="info",
                forge_name=self.name,
                skill_name=skill_name,
            ))
            return cache_hit

        stream_cb = self._make_stream_callback(skill_name)

        await self.bus.publish(Message(
            source="forge",
            content=f"🔧 **Circle {self.name}** → /{skill_name} — {skill.get('description', '')} (runner={runner_name}, timeout={skill_timeout}s)",
            level="info",
            forge_name=self.name,
            skill_name=skill_name,
        ))

        # Determine cwd: use first affected repo, or first workspace repo
        run_cwd = self._resolve_run_cwd()

        logger.info("Circle %s: lancement skill /%s (runner=%s, timeout=%ds, cwd=%s)",
                     self.name, skill_name, runner_name, skill_timeout,
                     os.path.basename(run_cwd) if run_cwd else ".")

        # Track execution metrics
        t_start = time.monotonic()

        # Run skill with retry on transient failures (max 2 retries, backoff 5s/15s)
        max_runner_retries = 2
        last_error = ""
        result = None
        for attempt in range(1 + max_runner_retries):
            heartbeat_task = asyncio.create_task(self._heartbeat(skill_name, runner_name))
            try:
                result = await runner.run(prompt, timeout=skill_timeout, on_output=stream_cb, cwd=run_cwd)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

            if result.success:
                break

            last_error = result.stderr
            if attempt < max_runner_retries:
                backoff = 5 * (attempt + 1)  # 5s, 10s
                logger.warning("Circle %s: /%s runner echoue (tentative %d/%d), retry dans %ds: %s",
                               self.name, skill_name, attempt + 1, 1 + max_runner_retries, backoff, last_error[:200])
                await self.bus.publish(Message(
                    source="forge",
                    content=f"⚠️ **Circle {self.name}** → /{skill_name} erreur transitoire (tentative {attempt + 1}), retry dans {backoff}s...",
                    level="warning",
                    forge_name=self.name,
                    skill_name=skill_name,
                ))
                await asyncio.sleep(backoff)

        if result is None or not result.success:
            await self.bus.publish(Message(
                source="forge",
                content=f"❌ **Circle {self.name}** → /{skill_name} erreur apres {1 + max_runner_retries} tentatives ({result.runner_used if result else '?'}): {last_error[:500]}",
                level="error",
                forge_name=self.name,
                skill_name=skill_name,
                data={"stderr": last_error},
            ))
            raise RuntimeError(f"Runner error: {last_error}")

        # Validate output structure if required_fields is defined
        output_text = result.output
        validation_error = validate_output(output_text, skill)
        if validation_error:
            await self.bus.publish(Message(
                source="forge",
                content=f"⚠️ **Circle {self.name}** → /{skill_name} output invalide : {validation_error}",
                level="warning",
                forge_name=self.name,
                skill_name=skill_name,
            ))
            raise RuntimeError(f"Output validation failed for /{skill_name}: {validation_error}")

        # Build a short preview of the output
        output_len = len(output_text)
        preview_lines = output_text.strip().split("\n")
        preview = "\n".join(preview_lines[:8])[:500]
        if len(preview_lines) > 8 or output_len > 500:
            preview += "\n..."

        # Save to issues directory (AIDD format)
        issue_file = self._save_issue_doc(skill_name, output_text)

        # Also save a copy in the bus state dir
        bus_output_file = os.path.join(self._state_dir, f"skill_{skill_name}_output.md")
        with open(bus_output_file, "w", encoding="utf-8") as fh:
            fh.write(output_text)

        # Use the issue file for Discord attachment (preferred), fallback to bus file
        output_file = issue_file or bus_output_file

        await self.bus.publish(Message(
            source="forge",
            content=f"✅ **Circle {self.name}** → /{skill_name} terminé ({output_len} chars, runner={result.runner_used})\n📄 `{os.path.basename(output_file)}`\n```\n{preview}\n```",
            level="info",
            forge_name=self.name,
            skill_name=skill_name,
            data={"runner_used": result.runner_used, "output_file": output_file},
        ))

        logger.info("Circle %s: skill /%s terminé (runner=%s, %d chars)", self.name, skill_name, result.runner_used, output_len)

        # Record execution metrics
        duration_s = round(time.monotonic() - t_start, 1)
        metrics = self.state.setdefault("skill_metrics", {})
        skill_metrics: Dict[str, Any] = {
            "duration_s": duration_s,
            "runner_used": result.runner_used,
            "output_chars": output_len,
        }
        if result.token_usage:
            skill_metrics["tokens"] = result.token_usage
        metrics[skill_name] = skill_metrics

        # Save to cache (for skills without side effects)
        if not skill.get("git_prepare") and not skill.get("git_finalize"):
            try:
                prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
                cache_path = os.path.join(self.bus_dir, self.name, f"cache_{skill_name}_{prompt_hash}.txt")
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as fh:
                    fh.write(output_text)
            except OSError:
                pass

        # Post-run bash hook (e.g. run tests after implement)
        post_run_template = skill.get("post_run")
        if post_run_template:
            post_run_output = await self._exec_hook(skill_name, "post_run", post_run_template, timeout_s=300)
            self.state.setdefault("step_outputs", {})["_post_run"] = post_run_output
            # Truncate for storage but keep full output available
            post_summary = post_run_output.strip()[-2000:] if len(post_run_output) > 2000 else post_run_output.strip()
            await self.bus.publish(Message(
                source="forge",
                content=f"🧪 **Circle {self.name}** → /{skill_name} post_run terminé\n```\n{post_summary[:500]}\n```",
                level="info",
                forge_name=self.name,
                skill_name=skill_name,
            ))

        # Git finalize (push + MR creation if configured)
        await self._finalize_git(skill_name)

        # JIRA notification (fire-and-forget — don't block on failure)
        json_data = extract_json(output_text)
        jira_summary = ""
        if json_data:
            jira_summary = json_data.get("summary", "")
        asyncio.create_task(self._notify_jira(skill_name, jira_summary))

        return output_text

    # ------------------------------------------------------------------
    # Parallel skill helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_parallel_step(step: Any) -> bool:
        """Check if a workflow step is a parallel group."""
        return isinstance(step, dict) and "parallel" in step

    @staticmethod
    def _get_parallel_skills(step: Any) -> List[str]:
        """Return the list of skill names in a parallel step."""
        if isinstance(step, dict) and "parallel" in step:
            return step["parallel"]
        return []

    def _get_step_skill_names(self, index: int) -> List[str]:
        """Return skill name(s) at a workflow index (handles parallel)."""
        step = self.workflow[index]
        if self._is_parallel_step(step):
            return self._get_parallel_skills(step)
        return [step]

    def _find_skill_workflow_index(self, skill_name: str) -> int:
        """Find the workflow index containing a skill (including parallel groups)."""
        for i, step in enumerate(self.workflow):
            if isinstance(step, str) and step == skill_name:
                return i
            if self._is_parallel_step(step) and skill_name in self._get_parallel_skills(step):
                return i
        raise ValueError(f"Skill /{skill_name} not found in workflow")

    def _all_skill_names(self) -> List[str]:
        """Flatten workflow into a list of all skill names."""
        names: List[str] = []
        for step in self.workflow:
            if isinstance(step, str):
                names.append(step)
            elif self._is_parallel_step(step):
                names.extend(self._get_parallel_skills(step))
        return names

    async def _run_parallel_skills(self, skill_names: List[str]) -> None:
        """Run multiple skills in parallel using asyncio.gather."""
        await self.bus.publish(Message(
            source="forge",
            content=f"⚡ **Circle {self.name}** → Lancement parallele : {', '.join(f'/{s}' for s in skill_names)}",
            level="info",
            forge_name=self.name,
        ))

        async def _run_one(name: str) -> tuple:
            output = await self._run_skill(name)
            return name, output

        results = await asyncio.gather(
            *[_run_one(s) for s in skill_names],
            return_exceptions=True,
        )

        errors: List[str] = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                errors.append(f"/{skill_names[i]}: {result}")
            else:
                name, output = result
                self._save_skill_output(name, output)
                self.state["step_outputs"][name] = output

        if errors:
            raise RuntimeError(f"Parallel skills failed: {'; '.join(errors)}")

        # Set previous_output to combined outputs
        all_outputs = [self.state["step_outputs"][s] for s in skill_names if s in self.state["step_outputs"]]
        self.state["previous_output"] = "\n\n---\n\n".join(all_outputs)

        await self.bus.publish(Message(
            source="forge",
            content=f"✅ **Circle {self.name}** → Parallel terminé : {', '.join(f'/{s}' for s in skill_names)}",
            level="info",
            forge_name=self.name,
        ))

    # ------------------------------------------------------------------
    # Workflow engine
    # ------------------------------------------------------------------

    async def run_workflow(self, task: str, instructions: Optional[str] = None) -> None:
        run_number = self._next_run_number()
        logger.info("Circle %s: démarrage workflow run #%d", self.name, run_number)

        self.state["status"] = "running"
        self.state["task"] = task
        self.state["instructions"] = instructions
        self.state["current_index"] = 0
        self.state["retries"] = {}
        self.state["step_outputs"] = {}
        self.state["previous_output"] = None
        self.state["error"] = None
        self.state["run_number"] = run_number
        self._save_state()

        try:
            await self._execute_from_current()
        except Exception as exc:
            self.state["status"] = "error"
            self.state["error"] = str(exc)
            self._save_state()
            self._archive_run()
            logger.error("Circle %s: erreur fatale run #%d: %s", self.name, run_number, exc)
            await self.bus.publish(Message(
                source="forge",
                content=f"💥 **Circle {self.name}** — erreur fatale : {exc}",
                level="error",
                forge_name=self.name,
            ))

    async def run_single_skill(
        self,
        skill_name: str,
        task: str,
        instructions: Optional[str] = None,
    ) -> str:
        if skill_name not in self.skills:
            raise ValueError(f"Skill inconnu : {skill_name}")

        run_number = self._next_run_number()
        self.state["status"] = "running"
        self.state["task"] = task
        self.state["instructions"] = instructions
        self.state["current_skill"] = skill_name
        self.state["run_number"] = run_number
        self._save_state()

        try:
            output = await self._run_skill(skill_name)
            self._save_skill_output(skill_name, output)
            self.state["step_outputs"][skill_name] = output
            self.state["previous_output"] = output

            # Extract summary for context compression (inter-agent communication)
            self._store_step_summary(skill_name, output)
            self.state["status"] = "idle"
            self._save_state()
            self._archive_run()
            return output
        except Exception as exc:
            self.state["status"] = "error"
            self.state["error"] = str(exc)
            self._save_state()
            self._archive_run()
            raise

    async def _execute_from_current(self) -> None:
        while self.state["current_index"] < len(self.workflow):
            step = self.workflow[self.state["current_index"]]

            # --- Parallel step ---
            if self._is_parallel_step(step):
                parallel_skills = self._get_parallel_skills(step)
                self.state["current_skill"] = f"parallel:{','.join(parallel_skills)}"
                self._save_state()

                await self._run_parallel_skills(parallel_skills)

                self.state["current_index"] += 1
                self._save_state()
                continue

            # --- Sequential step ---
            skill_name = step
            skill = self.skills[skill_name]
            self.state["current_skill"] = skill_name
            self._save_state()

            output = await self._run_skill(skill_name)

            self._save_skill_output(skill_name, output)
            self.state["step_outputs"][skill_name] = output
            self.state["previous_output"] = output

            # Extract summary for context compression (inter-agent communication)
            self._store_step_summary(skill_name, output)

            pass_condition = skill.get("pass_condition")
            if pass_condition:
                passed = self._evaluate_pass_condition(pass_condition, output)

                if not passed:
                    retry_key = skill_name
                    retries = self.state["retries"].get(retry_key, 0) + 1
                    self.state["retries"][retry_key] = retries

                    if retries >= self.max_retries:
                        self.state["status"] = "failed"
                        self.state["error"] = f"{skill_name} échoué après {retries} essais"
                        self._save_state()
                        self._archive_run()
                        await self.bus.publish(Message(
                            source="forge",
                            content=f"💥 **Circle {self.name}** → {skill_name} échoué après {retries} essais. Workflow arrêté.",
                            level="error",
                            forge_name=self.name,
                            skill_name=skill_name,
                        ))
                        return

                    # Extract structured feedback from reviewer for the retry target
                    reviewer_feedback = self._extract_reviewer_feedback(skill_name, output)
                    self.state["reviewer_feedback"] = reviewer_feedback
                    self.state["_skip_cache"] = True  # Force fresh execution on retry loop

                    next_on_fail = skill.get("next_on_fail")
                    fail_index = None
                    if next_on_fail:
                        try:
                            fail_index = self._find_skill_workflow_index(next_on_fail)
                        except ValueError:
                            pass

                    if fail_index is not None:
                        self.state["current_index"] = fail_index

                        await self.bus.publish(Message(
                            source="forge",
                            content=f"🔄 **Circle {self.name}** → {skill_name} rejeté (essai {retries}/{self.max_retries}). Retour à /{next_on_fail}\n📋 Feedback transmis au prochain agent",
                            level="warning",
                            forge_name=self.name,
                            skill_name=skill_name,
                        ))

                        self._save_state()
                        continue
                    else:
                        self.state["status"] = "failed"
                        self.state["error"] = f"{skill_name} rejected, no fallback"
                        self._save_state()
                        self._archive_run()
                        return

            auto_advance = skill.get("auto_advance", True)
            if not auto_advance:
                self.state["status"] = "paused"
                self._save_state()

                await self.bus.publish(Message(
                    source="forge",
                    content=f"⏸ **Circle {self.name}** en pause après /{skill_name}",
                    level="info",
                    forge_name=self.name,
                    skill_name=skill_name,
                    data={"paused": True, "skill_name": skill_name},
                ))

                evt = self._ensure_resume_event()
                evt.clear()
                await evt.wait()

                self.state["status"] = "running"

            # Advance to next skill
            next_on_pass = skill.get("next_on_pass")
            if next_on_pass:
                try:
                    self.state["current_index"] = self._find_skill_workflow_index(next_on_pass)
                except ValueError:
                    self.state["current_index"] += 1
            else:
                # No explicit next → advance to next index (loop ends naturally)
                self.state["current_index"] += 1

            self.state["retries"].pop(skill_name, None)
            self.state.pop("_skip_cache", None)
            self._save_state()

        self.state["status"] = "completed"
        self.state["current_skill"] = None
        self._save_state()
        self._archive_run()

        await self.bus.publish(Message(
            source="forge",
            content=f"🏁 **Circle {self.name}** — workflow terminé avec succès (run #{self.state['run_number']})",
            level="info",
            forge_name=self.name,
        ))

    # ------------------------------------------------------------------
    # External control
    # ------------------------------------------------------------------

    def resume(self, instructions: Optional[str] = None) -> None:
        if instructions:
            self._extra_instructions = instructions
        self._ensure_resume_event().set()

    def inject_feedback(self, feedback: str) -> None:
        self._feedback_buffer.append(feedback)

    def retry(self, instructions: Optional[str] = None) -> None:
        if instructions:
            self._extra_instructions = instructions
        self._ensure_resume_event().set()

    def abort(self) -> bool:
        """Abort the currently running skill. Returns True if aborted, False if nothing to abort."""
        if self.state.get("status") != "running":
            return False
        if self._running_task and not self._running_task.done():
            self._running_task.cancel()
        self.state["status"] = "paused"
        self.state["error"] = f"Avorte par l'utilisateur (skill /{self.state.get('current_skill', '?')})"
        self._save_state()
        logger.info("Circle %s: abort demande", self.name)
        return True

    def reset(self) -> None:
        if self._running_task and not self._running_task.done():
            self._running_task.cancel()
        self.state = {
            "status": "idle",
            "current_skill": None,
            "current_index": 0,
            "task": None,
            "instructions": None,
            "retries": {},
            "step_outputs": {},
            "previous_output": None,
            "error": None,
            "run_number": self.state.get("run_number", 0),
        }
        self._feedback_buffer.clear()
        self._extra_instructions = None
        self._save_state()

    # ------------------------------------------------------------------
    # Run from a specific skill (keep previous outputs)
    # ------------------------------------------------------------------

    async def run_from_skill(
        self,
        skill_name: str,
        instructions: Optional[str] = None,
    ) -> None:
        """Restart the workflow from a specific skill, keeping prior outputs."""
        try:
            start_index = self._find_skill_workflow_index(skill_name)
        except ValueError:
            raise ValueError(f"Skill /{skill_name} n'est pas dans le workflow de cette forge")

        # Keep the task and step_outputs from the previous run
        task = self.state.get("task")
        if not task:
            raise ValueError("Pas de tâche en cours — lancez d'abord un workflow complet")

        previous_outputs = dict(self.state.get("step_outputs", {}))
        # Remove outputs from the restart index and all subsequent steps
        for i in range(start_index, len(self.workflow)):
            for name in self._get_step_skill_names(i):
                previous_outputs.pop(name, None)

        run_number = self._next_run_number()
        logger.info("Circle %s: reprise depuis /%s (run #%d)", self.name, skill_name, run_number)

        self.state["status"] = "running"
        self.state["instructions"] = instructions or self.state.get("instructions")
        self.state["current_index"] = start_index
        self.state["current_skill"] = skill_name
        self.state["retries"] = {}
        self.state["step_outputs"] = previous_outputs
        self.state["_skip_cache"] = True  # Force re-execution on restart
        # Get previous_output from the last skill before the restart point
        prev_output = None
        if start_index > 0:
            prev_names = self._get_step_skill_names(start_index - 1)
            for name in reversed(prev_names):
                if name in previous_outputs:
                    prev_output = previous_outputs[name]
                    break
        self.state["previous_output"] = prev_output
        self.state["error"] = None
        self.state["run_number"] = run_number
        self._save_state()

        try:
            await self._execute_from_current()
        except Exception as exc:
            self.state["status"] = "error"
            self.state["error"] = str(exc)
            self._save_state()
            self._archive_run()
            logger.error("Circle %s: erreur run #%d: %s", self.name, run_number, exc)
            await self.bus.publish(Message(
                source="forge",
                content=f"💥 **Circle {self.name}** — erreur fatale : {exc}",
                level="error",
                forge_name=self.name,
            ))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def progress_bar(self) -> str:
        """Return a compact one-line progress bar for the dashboard."""
        s = self.state
        status = s.get("status", "idle")
        if status == "idle":
            return f"\u26aa **{self.name}** \u2014 idle"

        completed = set(k for k in s.get("step_outputs", {}) if not k.startswith("_"))
        total_skills = len(self._all_skill_names())
        done_count = len(completed & set(self._all_skill_names()))

        # Build bar: 10 blocks
        if total_skills > 0:
            filled = round(done_count / total_skills * 10)
        else:
            filled = 0
        bar = "\u2588" * filled + "\u2591" * (10 - filled)
        pct = round(done_count / total_skills * 100) if total_skills > 0 else 0

        icon = {
            "running": "\U0001f7e2", "paused": "\U0001f7e1",
            "completed": "\U0001f535", "failed": "\U0001f534", "error": "\U0001f534",
        }.get(status, "\u26aa")

        current = s.get("current_skill", "")
        skill_info = f" `/{current}`" if current and status == "running" else ""
        task_short = (s.get("task") or "")[:50]

        return f"{icon} **{self.name}** {bar} {pct}%{skill_info} \u2014 {task_short}"

    @property
    def status_summary(self) -> str:
        s = self.state
        completed_skills = set(
            k for k in s.get("step_outputs", {}) if not k.startswith("_")
        )
        current = s.get("current_skill")
        status = s.get("status", "idle")

        # Build visual workflow progress
        steps: List[str] = []
        for step in self.workflow:
            if self._is_parallel_step(step):
                p_skills = self._get_parallel_skills(step)
                all_done = all(s in completed_skills for s in p_skills)
                is_current = current and current.startswith("parallel:") and status == "running"
                icons = []
                for s in p_skills:
                    if s in completed_skills:
                        icons.append(f"✅`/{s}`")
                    elif is_current:
                        icons.append(f"🔄`/{s}`")
                    else:
                        icons.append(f"⬜`/{s}`")
                label = " | ".join(icons)
                if is_current:
                    steps.append(f"  ⚡ [{label}] ← parallele")
                elif all_done:
                    steps.append(f"  ⚡ [{label}]")
                else:
                    steps.append(f"  ⬜ [{label}]")
            else:
                skill = step
                if skill in completed_skills:
                    steps.append(f"  ✅ `/{skill}`")
                elif skill == current and status == "running":
                    steps.append(f"  🔄 `/{skill}` ← en cours")
                elif skill == current and status == "paused":
                    steps.append(f"  ⏸ `/{skill}` ← en pause")
                elif skill == current and status in ("error", "failed"):
                    steps.append(f"  ❌ `/{skill}` ← échoué")
                else:
                    steps.append(f"  ⬜ `/{skill}`")

        status_icon = {
            "idle": "⚪", "running": "🟢", "paused": "🟡",
            "completed": "🔵", "failed": "🔴", "error": "🔴",
        }.get(status, "⚪")

        lines = [
            self.progress_bar,
            f"\U0001f4cb Tache : {(s.get('task') or chr(8212))[:120]}",
            "",
            "**Workflow :**",
        ]
        lines.extend(steps)

        if s.get("error"):
            lines.append(f"\n⚠️ Erreur : {s['error']}")
        if s.get("retries"):
            retries_str = ", ".join(f"/{k}: {v}/3" for k, v in s["retries"].items())
            lines.append(f"🔄 Retries : {retries_str}")

        # Hint for available actions
        if status in ("error", "failed"):
            lines.append(f"\n💡 `!{self.name} from <skill>` pour reprendre depuis une étape")
        elif status == "paused":
            lines.append(f"\n💡 `!{self.name} resume` pour continuer")

        return "\n".join(lines)
