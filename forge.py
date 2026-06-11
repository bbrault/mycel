from __future__ import annotations

import ast
import asyncio
import glob as globmod
import hashlib
import json
import logging
import operator
import os
import shutil
import shlex
import re
import signal
import socket
import subprocess
import tempfile
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from message_bus import Message, MessageBus, atomic_write_json
from runner import StreamCallback, get_runner

logger = logging.getLogger("mycel.forge")


# ------------------------------------------------------------------
# Issue context resolver
# ------------------------------------------------------------------

def resolve_issue_context(jira_id: str, docs_path: str, workspace: Dict[str, str]) -> str:
    """Search for local issue docs matching a JIRA ID and return their content."""
    if not jira_id:
        return "(no JIRA id provided)"

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
        logger.info("No local doc found for %s", jira_id)
        return f"(no local documentation for {jira_id} — describe the task in the command)"

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

    logger.info("Issue %s: %d file(s) loaded (%d chars)", jira_id, len(sorted_names), total_chars)
    return "\n".join(parts)

# ------------------------------------------------------------------
# Safe condition evaluator (replaces eval())
# ------------------------------------------------------------------

_OPERATORS: Dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.GtE: operator.ge,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.Lt: operator.lt,
}

_MISSING = object()


def _compare(op_fn: Callable[[Any, Any], bool], left: Any, right: Any) -> bool:
    """Compare two values, preferring numeric comparison, falling back to string."""
    try:
        left_num = left if isinstance(left, (int, float)) else float(left)
        right_num = right if isinstance(right, (int, float)) else float(right)
        return bool(op_fn(left_num, right_num))
    except (ValueError, TypeError):
        return bool(op_fn(str(left), str(right)))


def _resolve_operand(node: ast.AST, data: Dict[str, Any]) -> Any:
    """Resolve a leaf node: a field name (looked up in ``data``) or a literal."""
    if isinstance(node, ast.Name):
        return data.get(node.id, _MISSING)
    if isinstance(node, ast.Constant):
        return node.value
    raise ValueError(f"unsupported operand: {type(node).__name__}")


def _eval_node(node: ast.AST, data: Dict[str, Any]) -> bool:
    """Recursively evaluate a boolean-expression AST against ``data``.

    Handles parentheses and arbitrarily nested ``and`` / ``or`` / ``not`` —
    precedence comes for free from Python's own parser.
    """
    if isinstance(node, ast.BoolOp):
        results = [_eval_node(value, data) for value in node.values]
        return all(results) if isinstance(node.op, ast.And) else any(results)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_node(node.operand, data)
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise ValueError("chained comparisons are not supported")
        op_fn = _OPERATORS.get(type(node.ops[0]))
        if op_fn is None:
            raise ValueError(f"unsupported operator: {type(node.ops[0]).__name__}")
        left = _resolve_operand(node.left, data)
        right = _resolve_operand(node.comparators[0], data)
        # A field absent from the data makes its comparison False (legacy behavior).
        if left is _MISSING or right is _MISSING or left is None:
            return False
        return _compare(op_fn, left, right)
    raise ValueError(f"unsupported expression: {type(node).__name__}")


def safe_evaluate_condition(condition: str, data: Dict[str, Any]) -> bool:
    """Evaluate a boolean condition string against ``data`` without ``eval``.

    Supports field/literal comparisons (``==`` ``!=`` ``>=`` ``<=`` ``>`` ``<``)
    combined with ``and`` / ``or`` / ``not`` and arbitrary parentheses, e.g.
    ``(verdict == 'approved' or verdict == 'approved_with_reservations') and score >= 80``.
    Numeric comparison is attempted first, falling back to string comparison.
    Returns False on any unsupported or malformed expression.
    """
    try:
        tree = ast.parse(condition, mode="eval")
        return bool(_eval_node(tree.body, data))
    except (SyntaxError, ValueError) as exc:
        logger.warning("Invalid pass condition %r: %s", condition, exc)
        return False


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


class Forge:
    """An execution environment that runs a state-machine of spells (a ritual)."""

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
        spells: Dict[str, Dict[str, Any]],
        workspace: Dict[str, str],
        bus: MessageBus,
        default_runner: str = "claude",
        max_retries: int = 3,
        timeout: int = 180,
        bus_dir: str = "bus",
        docs_path: str = "",
        issues_dir: str = "",
        runner_kwargs: Optional[Dict[str, Any]] = None,
        use_git_worktree: bool = False,
        dynamic_workspace: bool = False,
        gitlab_config: Optional[Dict[str, Any]] = None,
        docker_env_mapping: Optional[Dict[str, str]] = None,
        repo_folders: Optional[Dict[str, str]] = None,
    ) -> None:
        self.name = name
        self.description = description
        self.workflow = workflow
        self.spells = spells
        self.workspace = workspace
        self._original_workspace: Dict[str, str] = dict(workspace)
        self.use_git_worktree: bool = use_git_worktree
        self._worktree_registry: List[Tuple[str, str, str]] = []
        self.dynamic_workspace: bool = dynamic_workspace
        self._gitlab_config: Dict[str, Any] = gitlab_config or {}
        self._docker_env_mapping: Dict[str, str] = docker_env_mapping or {}
        self._repo_folders: Dict[str, str] = repo_folders or {}
        self._dynamic_workspace_dir: Optional[str] = None
        self._dynamic_workspace_port: Optional[int] = None
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
            "current_skill_started_at": None,
            "current_index": 0,
            "task": None,
            "instructions": None,
            "retries": {},
            "step_outputs": {},
            "previous_output": None,
            "error": None,
            "run_number": 0,
            "last_review": None,
            "git_snapshot": {},
            "external_snapshot": {},
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
        atomic_write_json(self._state_path, self.state)

    def _load_state(self) -> None:
        if os.path.exists(self._state_path):
            with open(self._state_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            # Merge with defaults for keys added in newer versions
            for key, default in [
                ("run_number", 0),
                ("current_skill_started_at", None),
                ("last_review", None),
                ("git_snapshot", {}),
                ("external_snapshot", {}),
            ]:
                loaded.setdefault(key, default)
            self.state = loaded

            dw = self.state.get("dynamic_workspace")
            if dw and os.path.isdir(dw.get("dir", "")):
                self._dynamic_workspace_dir = dw["dir"]
                self._dynamic_workspace_port = dw.get("port")

    def _save_skill_output(self, skill_name: str, output: str) -> None:
        payload = {"skill": skill_name, "output": output}

        # Save to current state dir (overwritten each run)
        os.makedirs(self._state_dir, exist_ok=True)
        path = os.path.join(self._state_dir, f"skill_{skill_name}.json")
        atomic_write_json(path, payload)

        # Save to versioned run dir (history)
        os.makedirs(self._run_dir, exist_ok=True)
        run_path = os.path.join(self._run_dir, f"skill_{skill_name}.json")
        atomic_write_json(run_path, payload)

    def _archive_run(self) -> None:
        """Archive current state into the versioned run directory."""
        os.makedirs(self._run_dir, exist_ok=True)
        archive_path = os.path.join(self._run_dir, "state.json")
        atomic_write_json(archive_path, self.state)

    def _jira_folder_name(self) -> Optional[str]:
        """Return `<jira_id>-<title-slug>` for the current task, or just `<jira_id>` if no title.

        Reused for both the AIDD issue dir and the git worktree path so folder
        names stay consistent across the run. Prefers the actual checked-out
        branch name (e.g. `feature/LAB-1918-cleanup-param-pays` → `LAB-1918-cleanup-param-pays`)
        when one of the workspace repos is on a `<jira_id>-…` branch; falls back to
        slugging the user-supplied task text.
        """
        task_str = self.state.get("task") or ""
        jira_match = self._JIRA_RE.search(task_str)
        if not jira_match:
            return None
        jira_id = jira_match.group(1)

        branch_slug = self._branch_folder_from_git(jira_id)
        if branch_slug:
            return branch_slug

        # Title = text after the JIRA ID, then text before (do NOT fall back to the
        # full task: that produces a redundant `LAB-1918-lab-1918` slug).
        title_part = task_str[jira_match.end():].strip()
        if not title_part:
            title_part = task_str[:jira_match.start()].strip()
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title_part).strip("-").lower()[:60]
        if slug and slug != jira_id.lower():
            return f"{jira_id}-{slug}"
        return jira_id

    def _branch_folder_from_git(self, jira_id: str) -> Optional[str]:
        """Look across workspace repos for a checked-out branch named like
        `feature/<jira_id>-<slug>` (or `<jira_id>-<slug>`). Returns the bare
        `<jira_id>-<slug>` form, or None if nothing matches.
        """
        repos = self._original_workspace or self.workspace or {}
        jira_lower = jira_id.lower()
        for path in repos.values():
            if not self._is_git_repo(path):
                continue
            try:
                proc = subprocess.run(
                    ["git", "-C", path, "branch", "--show-current"],
                    capture_output=True, text=True, timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            branch = (proc.stdout or "").strip()
            if not branch:
                continue
            bare = branch[len("feature/"):] if branch.startswith("feature/") else branch
            if bare.lower() == jira_lower:
                continue  # bare jira_id is no better than the fallback
            if bare.lower().startswith(jira_lower + "-"):
                return bare
        return None

    def _get_issue_dir(self) -> Optional[str]:
        """Return the issue directory path for the current JIRA ID or MR, or None."""
        if not self.issues_dir:
            return None
        folder_name = self._jira_folder_name()
        if folder_name:
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

        logger.info("Issue doc saved: %s", filepath)
        return filepath

    def _next_run_number(self) -> int:
        return self.state.get("run_number", 0) + 1

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_prompt(self, skill: Dict[str, Any]) -> str:
        workspace_str = "\n".join(
            f"- {k}: {v}" for k, v in self.workspace.items()
        ) if self.workspace else "(no workspace)"

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
            issue_context = self.state.get("step_outputs", {}).get(issue_ctx_key, "(no issue context)")

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
            "previous_output": self.state.get("previous_output") or "(none)",
            "workspace": workspace_str,
            "forge_name": self.name,
            "docs_path": self.docs_path,
            "jira_id": jira_id,
            "issue_context": issue_context,
            "mr_project": mr_project,
            "mr_iid": mr_iid,
            "pre_run_output": pre_run_output,
            "post_run_output": post_run_output,
            "reviewer_feedback": reviewer_feedback or "(none — first pass)",
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
            logger.warning("Forge %s: could not extract JSON for condition", self.name)
            return False
        try:
            return safe_evaluate_condition(condition, data)
        except Exception as exc:
            logger.error("Forge %s: condition evaluation error: %s", self.name, exc)
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
                # Persist as last_review for status display
                self.state["last_review"] = {
                    "skill": skill_name,
                    "verdict": str(verdict),
                    "score": score if isinstance(score, (int, float)) else None,
                    "summary": str(data.get("summary", ""))[:300],
                    "blocking_issues": len(data.get("blocking_issues") or []),
                    "when": time.time(),
                }
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
                content=f"📡 **Forge {self.name}** → /{skill_name} ({state['total_chars']} chars)...\n```\n{preview}\n```",
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
                content=f"⏳ **Forge {self.name}** → /{skill_name} running ({elapsed}s, runner={runner_name})",
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

    async def _glab_json(self, repo_path: str, *args: str, timeout: int = 20) -> Optional[Any]:
        """Run a glab command with JSON output, return parsed result or None."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "glab", *args,
                cwd=repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            logger.debug("Forge %s: glab not available: %s", self.name, exc)
            return None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("Forge %s: glab %s timed out", self.name, " ".join(args))
            return None
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()[:200]
            logger.debug("Forge %s: glab %s rc=%d: %s", self.name, " ".join(args), proc.returncode, err)
            return None
        raw = stdout.decode("utf-8", errors="replace").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Forge %s: glab output not JSON: %s", self.name, raw[:100])
            return None

    async def refresh_external_snapshot(self) -> Dict[str, Any]:
        """Fetch MR + pipeline state from GitLab via glab (on-demand).

        Best-effort: missing glab, missing branch, network errors are silently
        skipped. Stored in state["external_snapshot"]. Returns the snapshot.
        """
        # Ensure we know branches first
        if not self.state.get("git_snapshot"):
            await self.refresh_git_snapshot()
        git_snap = self.state.get("git_snapshot") or {}
        if not git_snap:
            return {}

        snapshot: Dict[str, Any] = {}
        now = time.time()

        async def _fetch_repo(repo_name: str, repo_path: str, branch: str) -> Tuple[str, Dict[str, Any]]:
            info: Dict[str, Any] = {"refreshed_at": now, "branch": branch}
            # MR for this branch — include all states (opened/merged/closed)
            mrs = await self._glab_json(
                repo_path, "mr", "list", "--source-branch", branch, "--all", "-F", "json"
            )
            mr_pick: Optional[Dict[str, Any]] = None
            if isinstance(mrs, list) and mrs:
                # Prefer opened, then most recent
                opened = [m for m in mrs if (m.get("state") == "opened")]
                pool = opened or mrs
                mr_pick = max(pool, key=lambda m: m.get("updated_at") or m.get("created_at") or "")
            if mr_pick:
                info["mr"] = {
                    "iid": mr_pick.get("iid"),
                    "state": mr_pick.get("state"),
                    "title": (mr_pick.get("title") or "")[:120],
                    "web_url": mr_pick.get("web_url"),
                    "draft": bool(mr_pick.get("draft") or mr_pick.get("work_in_progress")),
                    "target_branch": mr_pick.get("target_branch"),
                }
            else:
                info["mr"] = None

            # Latest pipeline on this branch
            pipelines = await self._glab_json(
                repo_path, "ci", "list", "-r", branch, "-P", "1", "-F", "json"
            )
            pipe_pick: Optional[Dict[str, Any]] = None
            if isinstance(pipelines, list) and pipelines:
                pipe_pick = pipelines[0]
            elif isinstance(pipelines, dict):
                pipe_pick = pipelines
            if pipe_pick:
                info["pipeline"] = {
                    "id": pipe_pick.get("id"),
                    "status": pipe_pick.get("status"),
                    "web_url": pipe_pick.get("web_url"),
                    "sha": (pipe_pick.get("sha") or "")[:8],
                    "ref": pipe_pick.get("ref"),
                    "updated_at": pipe_pick.get("updated_at"),
                }
            else:
                info["pipeline"] = None

            return repo_name, info

        coros = []
        for repo_name, repo_info in git_snap.items():
            branch = repo_info.get("branch")
            repo_path = self.workspace.get(repo_name)
            if not branch or not repo_path or not os.path.isdir(repo_path):
                continue
            # Skip default branches — no MR is expected
            if branch in ("develop", "main", "master"):
                continue
            coros.append(_fetch_repo(repo_name, repo_path, branch))

        if coros:
            results = await asyncio.gather(*coros, return_exceptions=True)
            for r in results:
                if isinstance(r, tuple):
                    repo_name, info = r
                    snapshot[repo_name] = info
                elif isinstance(r, Exception):
                    logger.warning("Forge %s: external refresh error: %s", self.name, r)

        self.state["external_snapshot"] = snapshot
        self._save_state()
        return snapshot

    async def refresh_git_snapshot(self) -> None:
        """Refresh per-repo git snapshot in state for status display.

        Best-effort: per-repo failures are caught so one bad repo does not
        prevent others from being inspected. Stored in state["git_snapshot"].
        """
        if not self.workspace:
            return
        snapshot: Dict[str, Any] = {}
        now = time.time()
        for repo_name, repo_path in self.workspace.items():
            if not repo_path or not os.path.isdir(repo_path):
                continue
            info: Dict[str, Any] = {"refreshed_at": now}
            try:
                info["branch"] = await self._git_exec(repo_path, "branch", "--show-current", timeout=10) or None
            except RuntimeError:
                info["branch"] = None
            info["ahead"] = None
            info["behind"] = None
            info["base"] = None
            for base in ("origin/develop", "origin/main", "origin/master"):
                try:
                    counts = await self._git_exec(
                        repo_path, "rev-list", "--left-right", "--count", f"{base}...HEAD", timeout=10
                    )
                    parts = counts.split()
                    if len(parts) == 2:
                        info["behind"] = int(parts[0])
                        info["ahead"] = int(parts[1])
                        info["base"] = base
                        break
                except (RuntimeError, ValueError):
                    continue
            try:
                line = await self._git_exec(
                    repo_path, "log", "-1", "--pretty=format:%h%x00%s%x00%cr", timeout=10
                )
                parts = line.split("\0")
                info["last_sha"] = parts[0] if len(parts) > 0 else ""
                info["last_msg"] = (parts[1][:80] if len(parts) > 1 else "")
                info["last_msg_when"] = parts[2] if len(parts) > 2 else ""
            except RuntimeError:
                info["last_sha"] = ""
                info["last_msg"] = ""
                info["last_msg_when"] = ""
            try:
                porcelain = await self._git_exec(repo_path, "status", "--porcelain", timeout=10)
                lines = [ln for ln in porcelain.splitlines() if ln.strip()]
                info["dirty"] = bool(lines)
                info["dirty_count"] = len(lines)
            except RuntimeError:
                info["dirty"] = False
                info["dirty_count"] = 0
            snapshot[repo_name] = info
        self.state["git_snapshot"] = snapshot
        self._save_state()

    # ------------------------------------------------------------------
    # Dynamic workspace provisioning
    # ------------------------------------------------------------------

    @staticmethod
    def _find_free_port(start: int = 8080, end: int = 9000) -> int:
        for port in range(start, end):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("127.0.0.1", port))
                    return port
            except OSError:
                continue
        raise RuntimeError(f"No free port found in range {start}-{end}")

    async def _provision_dynamic_workspace(self) -> None:
        if not self.dynamic_workspace or self._dynamic_workspace_dir:
            return

        analyze_output = self.state.get("step_outputs", {}).get("analyze")
        if not analyze_output:
            raise RuntimeError("dynamic_workspace enabled but no 'analyze' output found")

        data = extract_json(analyze_output)
        if not data or "required_repos" not in data:
            raise RuntimeError("analyze output missing 'required_repos' field")

        required_repos: List[str] = data["required_repos"]
        if not required_repos:
            raise RuntimeError("analyze returned empty required_repos list")

        timestamp = int(time.time())
        base_dir = os.path.join(tempfile.gettempdir(), "mycel-runs")
        run_dir = os.path.join(base_dir, f"{self.name}-{timestamp}")
        os.makedirs(run_dir, exist_ok=True)
        self._dynamic_workspace_dir = run_dir

        ssh_base = self._gitlab_config.get("ssh_base", "git@gitlab.com:kanta-app")
        docker_repo = self._gitlab_config.get("docker_repo", "kanta-docker")

        clone_targets: List[Tuple[str, str, str]] = []
        for repo_key in required_repos:
            folder = self._repo_folders.get(repo_key, repo_key)
            url = f"{ssh_base}/{folder}.git"
            clone_targets.append((repo_key, folder, url))

        docker_url = f"{ssh_base}/{docker_repo}.git"
        clone_targets.append(("_docker", docker_repo, docker_url))

        await self.bus.publish(Message(
            source="forge",
            content=f"📦 **Forge {self.name}** — Cloning {len(clone_targets)} repos into `{run_dir}`",
            level="info",
            forge_name=self.name,
        ))

        async def _clone_one(repo_key: str, folder: str, url: str) -> Tuple[str, str]:
            dest = os.path.join(run_dir, folder)
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", url, dest,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"Clone failed for {folder}: {err}")
            logger.info("Forge %s: cloned %s → %s", self.name, folder, dest)
            return repo_key, dest

        results = await asyncio.gather(
            *[_clone_one(k, f, u) for k, f, u in clone_targets],
            return_exceptions=True,
        )

        errors = [str(r) for r in results if isinstance(r, BaseException)]
        if errors:
            await self._cleanup_dynamic_workspace()
            raise RuntimeError(f"Dynamic workspace clone failed: {'; '.join(errors)}")

        new_workspace: Dict[str, str] = {}
        docker_path = ""
        for r in results:
            if isinstance(r, tuple):
                repo_key, dest = r
                if repo_key == "_docker":
                    docker_path = dest
                else:
                    new_workspace[repo_key] = dest

        self.workspace = new_workspace

        port = self._find_free_port()
        self._dynamic_workspace_port = port

        env_lines = [
            f"KANTA_LAB_PORT={port}",
            "KANTA_LAB_API_PATH=",
            "KANTA_LAB_FRONT_PATH=",
        ]
        for repo_key, path in new_workspace.items():
            env_var = self._docker_env_mapping.get(repo_key)
            if env_var:
                env_lines.append(f"{env_var}={path}")

        for token in ("NPM_TOKEN", "FONTAWESOME_PACKAGE_TOKEN", "REVERB_APP_KEY"):
            val = os.environ.get(token, "")
            if not val:
                logger.warning("Forge %s: token %s not set in environment", self.name, token)
            env_lines.append(f"{token}={val}")

        if docker_path:
            env_path = os.path.join(docker_path, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(env_lines) + "\n")
            logger.info("Forge %s: wrote kanta-docker .env at %s (port=%d)", self.name, env_path, port)

        self.state["dynamic_workspace"] = {
            "dir": run_dir,
            "port": port,
            "repos": list(new_workspace.keys()),
            "docker_path": docker_path,
        }
        self._save_state()

        repos_str = ", ".join(new_workspace.keys())
        await self.bus.publish(Message(
            source="forge",
            content=(
                f"✅ **Forge {self.name}** — Dynamic workspace ready\n"
                f"  📂 `{run_dir}`\n"
                f"  🔌 Port: {port}\n"
                f"  📦 Repos: {repos_str}\n"
                f"  🐳 Docker: `{docker_path}`"
            ),
            level="info",
            forge_name=self.name,
        ))

    async def _cleanup_dynamic_workspace(self) -> None:
        if not self._dynamic_workspace_dir:
            return
        dir_to_remove = self._dynamic_workspace_dir
        self._dynamic_workspace_dir = None
        self._dynamic_workspace_port = None
        self.workspace = dict(self._original_workspace)
        self.state.pop("dynamic_workspace", None)
        self._save_state()
        try:
            shutil.rmtree(dir_to_remove, ignore_errors=True)
            logger.info("Forge %s: cleaned up dynamic workspace %s", self.name, dir_to_remove)
            await self.bus.publish(Message(
                source="forge",
                content=f"🧹 **Forge {self.name}** — Dynamic workspace cleaned: `{dir_to_remove}`",
                level="info",
                forge_name=self.name,
            ))
        except Exception as exc:
            logger.warning("Forge %s: dynamic workspace cleanup failed: %s", self.name, exc)

    def _cleanup_dynamic_workspace_sync(self) -> None:
        if not self._dynamic_workspace_dir:
            return
        dir_to_remove = self._dynamic_workspace_dir
        self._dynamic_workspace_dir = None
        self._dynamic_workspace_port = None
        self.workspace = dict(self._original_workspace)
        self.state.pop("dynamic_workspace", None)
        shutil.rmtree(dir_to_remove, ignore_errors=True)
        logger.info("Forge %s: sync cleaned up dynamic workspace %s", self.name, dir_to_remove)

    # ------------------------------------------------------------------
    # Git worktree management
    # ------------------------------------------------------------------

    @staticmethod
    def _is_git_repo(path: str) -> bool:
        """True if `path` is a git working tree.

        `.git` is a *directory* in a normal clone but a *file* (`gitdir: ...`)
        in a linked worktree. `os.path.isdir` misses worktrees — which silently
        broke git_prepare/git_finalize for worktree-based forges (no branch,
        no push, no warning). `os.path.exists` accepts both.
        """
        return bool(path) and os.path.exists(os.path.join(path, ".git"))

    def _worktree_paths_active(self) -> bool:
        if not self.use_git_worktree or not self._original_workspace or not self.workspace:
            return False
        sample = next(iter(self.workspace.values()), "")
        return bool(sample and ".dispatch-worktrees" in os.path.normpath(sample))

    async def _git_fetch_default_remote(self, main_path: str) -> None:
        try:
            await self._git_exec(main_path, "fetch", "origin", timeout=120)
        except RuntimeError as exc:
            logger.warning("Forge %s: git fetch in %s: %s", self.name, main_path, exc)

    async def _choose_worktree_start_ref(self, main_path: str) -> str:
        for ref in ("origin/develop", "origin/main", "develop", "main"):
            try:
                await self._git_exec(main_path, "rev-parse", "--verify", ref, timeout=15)
                return ref
            except RuntimeError:
                continue
        raise RuntimeError(
            f"Aucun ref develop/main utilisable pour worktree (repo {os.path.basename(main_path)})."
        )

    async def _remove_stale_worktree_path(self, main_path: str, wt_path: str) -> None:
        try:
            await self._git_exec(main_path, "worktree", "remove", "--force", wt_path, timeout=60)
        except RuntimeError:
            if os.path.isdir(wt_path):
                shutil.rmtree(wt_path, ignore_errors=True)
        try:
            await self._git_exec(main_path, "worktree", "prune", timeout=20)
        except RuntimeError:
            pass

    async def _ensure_git_worktrees(self) -> None:
        """One isolated worktree per main clone under .dispatch-worktrees/issues/<id>/."""
        if self._dynamic_workspace_dir:
            return
        if not self.use_git_worktree or self._worktree_paths_active():
            return

        task_str = self.state.get("task") or ""
        jira_m = self._JIRA_RE.search(task_str)
        if not jira_m:
            logger.warning(
                "Forge %s: worktree requis mais pas d'ID ticket (ex. KANTA-123) dans la tache",
                self.name,
            )
            return
        jira_id = jira_m.group(1)
        if not self._original_workspace:
            raise RuntimeError(
                f"Forge {self.name}: git_worktree actif mais aucun workspace resolu "
                f"(base_env du workspace_group absent ou vide ?)."
            )

        first_main = next(iter(self._original_workspace.values()), "")
        if not self._is_git_repo(first_main):
            base_hint = os.path.dirname(os.path.normpath(first_main)) if first_main else "?"
            raise RuntimeError(
                f"Forge {self.name}: git_worktree actif mais le clone source est introuvable "
                f"({first_main or '?'} n'est pas un depot git). Verifiez que `{base_hint}` "
                f"existe et contient les clones du workspace_group avant de lancer le workflow."
            )
        base_path = os.path.dirname(os.path.normpath(first_main))
        # Use `<jira_id>-<title-slug>` for the worktree folder (matches issue dir).
        folder_name = self._jira_folder_name() or jira_id
        issue_safe = re.sub(r"[^\w\-.]+", "-", folder_name)
        wt_base = os.path.join(base_path, ".dispatch-worktrees", "issues", issue_safe)
        self._worktree_registry.clear()

        for repo_name, main_path in self._original_workspace.items():
            if not self._is_git_repo(main_path):
                continue
            st = await self._git_exec(
                main_path, "status", "--porcelain", "--untracked-files=no", timeout=30,
            )
            if st:
                raise RuntimeError(
                    f"Depot {repo_name}: modifications non commitees sur le clone principal. "
                    f"Stashez/committez {main_path} avant d'utiliser le workflow."
                )
            await self._git_fetch_default_remote(main_path)
            start_ref = await self._choose_worktree_start_ref(main_path)
            folder = os.path.basename(os.path.normpath(main_path))
            wt_path = os.path.join(wt_base, folder)
            if os.path.exists(wt_path):
                await self._remove_stale_worktree_path(main_path, wt_path)
            await self._git_exec(
                main_path,
                "worktree", "add", "-B", f"feature/{jira_id}", wt_path, start_ref,
                timeout=120,
            )
            self._worktree_registry.append((main_path, wt_path, repo_name))
            self.workspace[repo_name] = wt_path
            await self.bus.publish(Message(
                source="forge",
                content=f"🌿 **{self.name}** → worktree **{repo_name}** : `{wt_path}` (branche `feature/{jira_id}`)",
                level="info",
                forge_name=self.name,
            ))

        if self._worktree_registry:
            await self.bus.publish(Message(
                source="forge",
                content=f"🌳 **{self.name}** → {len(self._worktree_registry)} worktree(s) prets (ID `{jira_id}`). Les clones "
                f"sous `{base_path}/.dispatch-worktrees` sont isoles du depot principal.",
                level="info",
                forge_name=self.name,
            ))

    async def _teardown_git_worktrees(self) -> None:
        if not self._worktree_registry:
            self.workspace = dict(self._original_workspace)
            return
        for main_path, wt_path, repo_name in self._worktree_registry:
            try:
                await self._remove_stale_worktree_path(main_path, wt_path)
            except Exception as exc:
                logger.warning("Worktree remove %s: %s", wt_path, exc)
                await self.bus.publish(Message(
                    source="forge",
                    content=f"⚠ **{self.name}** — impossible de supprimer proprement le worktree {repo_name}: {exc}",
                    level="warning",
                    forge_name=self.name,
                ))
        self._worktree_registry.clear()
        self.workspace = dict(self._original_workspace)
        await self.bus.publish(Message(
            source="forge",
            content=f"🧹 **{self.name}** → worktrees supprimes, workspace restaure sur les clones principaux.",
            level="info",
            forge_name=self.name,
        ))

    def _teardown_git_worktrees_sync(self) -> None:
        if not self._worktree_registry:
            self.workspace = dict(self._original_workspace)
            return
        for main_path, wt_path, _name in self._worktree_registry:
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", wt_path],
                    cwd=main_path,
                    capture_output=True,
                    timeout=60,
                )
            except OSError as exc:
                logger.warning("Worktree remove sync: %s", exc)
            if os.path.isdir(wt_path):
                shutil.rmtree(wt_path, ignore_errors=True)
            try:
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=main_path,
                    capture_output=True,
                    timeout=20,
                )
            except OSError:
                pass
        self._worktree_registry.clear()
        self.workspace = dict(self._original_workspace)

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
            content=f"🔀 **Forge {self.name}** → Git check **{repo_name}**",
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
            logger.info("Repo %s: already on %s", repo_name, current_branch)
            try:
                await self._git_exec(repo_path, "pull", "--ff-only", "origin", current_branch)
            except RuntimeError:
                logger.info("Repo %s: no upstream for %s, continuing", repo_name, current_branch)
            await self.bus.publish(Message(
                source="forge",
                content=f"  ✅ **{repo_name}** — branche `{current_branch}` prete",
                level="info",
                forge_name=self.name,
            ))
        else:
            logger.info("Repo %s: on %s, creating %s", repo_name, current_branch, expected_branch)

            await self._git_exec(repo_path, "checkout", "develop")
            try:
                await self._git_exec(repo_path, "pull", "origin", "develop")
            except RuntimeError:
                logger.warning("Repo %s: pull develop failed, continuing", repo_name)

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
        skill = self.spells.get(skill_name, {})
        if not skill.get("git_prepare", False):
            return

        task_str = self.state.get("task") or ""
        jira_match = self._JIRA_RE.search(task_str)
        if not jira_match:
            raise RuntimeError(
                f"Forge {self.name}: /{skill_name} demande git_prepare mais aucun ID ticket "
                f"(ex. KAN-123) n'a ete trouve dans la tache; impossible de creer la branche feature."
            )
        jira_id = jira_match.group(1)

        affected = self._get_affected_repos()

        # Collect repos to prepare
        repos_to_prepare: List[tuple] = []
        for repo_name, repo_path in self.workspace.items():
            if not self._is_git_repo(repo_path):
                continue
            if affected and repo_name not in affected:
                logger.info("Repo %s: not in plan, skipping", repo_name)
                continue
            repos_to_prepare.append((repo_name, repo_path))

        if not repos_to_prepare:
            raise RuntimeError(
                f"Forge {self.name}: /{skill_name} demande git_prepare mais aucun depot git "
                f"n'a ete trouve dans le workspace. Verifiez le base_env du workspace_group "
                f"(repos attendus: {', '.join(self.workspace.keys()) or 'aucun'})."
            )

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
            content=f"🔀 **Forge {self.name}** → Git workspace ready for /{skill_name} ({len(repos_to_prepare)} repos)",
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

        comment = f"[Dispatch] workflow {self.name} — /{skill_name} completed.\n{summary[:500]}"
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
            logger.info("Forge %s: JIRA %s notified for /%s", self.name, jira_id, skill_name)
        except Exception as exc:
            logger.warning("Forge %s: JIRA notification failed: %s", self.name, exc)

    # ------------------------------------------------------------------
    # Git finalize (push + MR creation)
    # ------------------------------------------------------------------

    async def _finalize_git(self, skill_name: str) -> None:
        """Push branch and create MR after a successful skill (when git_finalize is set)."""
        skill = self.spells.get(skill_name, {})
        if not skill.get("git_finalize", False):
            return

        task_str = self.state.get("task") or ""
        jira_match = self._JIRA_RE.search(task_str)
        if not jira_match:
            logger.warning("Forge %s: no JIRA ID, skipping git finalize", self.name)
            return
        jira_id = jira_match.group(1)

        # Get plan summary for MR description (if available)
        plan_output = self.state.get("step_outputs", {}).get("plan", "")
        mr_description = f"Dispatch workflow for {jira_id}"
        if plan_output:
            from forge import extract_json
            plan_data = extract_json(plan_output)
            if plan_data and "summary" in plan_data:
                mr_description = plan_data["summary"]

        for repo_name, repo_path in self.workspace.items():
            if not self._is_git_repo(repo_path):
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
                content=f"🚀 **Forge {self.name}** → Push **{repo_name}** branch `{current_branch}`",
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
                    content=f"⚠️ **Forge {self.name}** → Push failed for **{repo_name}** : {exc}",
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
                        content=f"✅ **Forge {self.name}** → MR created for **{repo_name}** : {mr_output}",
                        level="info",
                        forge_name=self.name,
                        skill_name=skill_name,
                    ))
                else:
                    err = stderr.decode("utf-8", errors="replace").strip()
                    await self.bus.publish(Message(
                        source="forge",
                        content=f"⚠️ **Forge {self.name}** → MR creation failed for **{repo_name}** : {err[:300]}",
                        level="warning",
                        forge_name=self.name,
                    ))
            except (asyncio.TimeoutError, FileNotFoundError) as exc:
                await self.bus.publish(Message(
                    source="forge",
                    content=f"⚠️ **Forge {self.name}** → glab not available for MR creation : {exc}",
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
        # Values come from Discord input (task, MR URL) — shell-quote them so they
        # can never break out of the hook command. shlex.quote() leaves already-safe
        # strings (paths, group/repo, digits) unchanged; empty values stay empty so
        # hooks that tolerate a missing MR URL keep their current behaviour.
        def _q(value: str) -> str:
            return shlex.quote(value) if value else ""

        cmd = (
            template
            .replace("{mr_project}", _q(mr_project))
            .replace("{mr_iid}", _q(mr_iid))
            .replace("{task}", _q(task_str))
            .replace("{workspace_first}", _q(workspace_first))
        )

        logger.info("Forge %s: %s for /%s (timeout=%ds): %s", self.name, hook_name, skill_name, timeout_s, cmd[:100])
        await self.bus.publish(Message(
            source="forge",
            content=f"⚡ **Forge {self.name}** → /{skill_name} {hook_name} running... (timeout {timeout_s}s)",
            level="info",
            forge_name=self.name,
            skill_name=skill_name,
        ))

        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group → kill children too
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            output = stdout.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                logger.warning("Forge %s: %s failed (rc=%d): %s", self.name, hook_name, proc.returncode, err[:200])
                # For post_run, include both stdout and stderr (test results may be in either)
                return f"{output}\n(exit code {proc.returncode})\n{err[:500]}" if hook_name == "post_run" else f"({hook_name} error: {err[:500]})"

            logger.info("Forge %s: %s OK (%d chars)", self.name, hook_name, len(output))
            return output
        except asyncio.TimeoutError:
            logger.warning("Forge %s: %s timeout (%ds) — killing process group", self.name, hook_name, timeout_s)
            await self._kill_proc_group(proc)
            partial = ""
            if proc is not None and proc.stdout is not None:
                try:
                    data = await asyncio.wait_for(proc.stdout.read(), timeout=2)
                    partial = data.decode("utf-8", errors="replace").strip()[-1500:]
                except (asyncio.TimeoutError, Exception):
                    pass
            suffix = f"\n--- last stdout ---\n{partial}" if partial else ""
            return f"({hook_name} timeout after {timeout_s}s){suffix}"
        except Exception as exc:
            logger.warning("Forge %s: %s exception: %s", self.name, hook_name, exc)
            await self._kill_proc_group(proc)
            return f"({hook_name} error: {exc})"

    @staticmethod
    async def _kill_proc_group(proc: Optional[asyncio.subprocess.Process]) -> None:
        """Best-effort kill of the subprocess and its children (for `start_new_session=True`)."""
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    async def _run_skill(self, skill_name: str) -> str:
        skill = self.spells[skill_name]

        await self._ensure_git_worktrees()

        # Git preparation (if configured for this skill)
        await self._prepare_git(skill_name)

        # Pre-run bash hook (e.g. fetch MR diff before calling the runner)
        pre_run_template = skill.get("pre_run")
        if pre_run_template:
            pre_run_timeout = int(skill.get("pre_run_timeout", 120))
            pre_run_output = await self._exec_hook(skill_name, "pre_run", pre_run_template, timeout_s=pre_run_timeout)
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
                    logger.info("Forge %s: cache hit for /%s (hash=%s)", self.name, skill_name, prompt_hash)
                except OSError:
                    cache_hit = None

        if cache_hit is not None:
            await self.bus.publish(Message(
                source="forge",
                content=f"⚡ **Forge {self.name}** → /{skill_name} — cache hit (same result as previous run)",
                level="info",
                forge_name=self.name,
                skill_name=skill_name,
            ))
            return cache_hit

        stream_cb = self._make_stream_callback(skill_name)

        await self.bus.publish(Message(
            source="forge",
            content=f"⚒️ **Forge {self.name}** → /{skill_name} — {skill.get('description', '')} (runner={runner_name}, timeout={skill_timeout}s)",
            level="info",
            forge_name=self.name,
            skill_name=skill_name,
        ))

        # Determine cwd: use first affected repo, or first workspace repo
        run_cwd = self._resolve_run_cwd()

        # Fail loud: a step with git side effects must run inside a real repo.
        # Without a valid cwd the agent runs rootless and may *claim* it committed
        # while touching nothing — silently producing no branch, commit, or push.
        if run_cwd is None and (skill.get("git_prepare") or skill.get("git_finalize")):
            raise RuntimeError(
                f"Forge {self.name}: /{skill_name} a des effets git mais aucun repo de travail "
                f"valide n'a ete resolu (cwd introuvable). L'agent ne sera pas lance a vide. "
                f"Verifiez le workspace de la forge (base_env du workspace_group)."
            )

        logger.info("Forge %s: starting skill /%s (runner=%s, timeout=%ds, cwd=%s)",
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
                logger.warning("Forge %s: /%s runner failed (attempt %d/%d), retry in %ds: %s",
                               self.name, skill_name, attempt + 1, 1 + max_runner_retries, backoff, last_error[:200])
                await self.bus.publish(Message(
                    source="forge",
                    content=f"⚠️ **Forge {self.name}** → /{skill_name} transient error (attempt {attempt + 1}), retry in {backoff}s...",
                    level="warning",
                    forge_name=self.name,
                    skill_name=skill_name,
                ))
                await asyncio.sleep(backoff)

        if result is None or not result.success:
            await self.bus.publish(Message(
                source="forge",
                content=f"❌ **Forge {self.name}** → /{skill_name} error after {1 + max_runner_retries} attempts ({result.runner_used if result else '?'}): {last_error[:500]}",
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
                content=f"⚠️ **Forge {self.name}** → /{skill_name} invalid output: {validation_error}",
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
            content=f"✅ **Forge {self.name}** → /{skill_name} done ({output_len} chars, runner={result.runner_used})\n📄 `{os.path.basename(output_file)}`\n```\n{preview}\n```",
            level="info",
            forge_name=self.name,
            skill_name=skill_name,
            data={"runner_used": result.runner_used, "output_file": output_file},
        ))

        logger.info("Forge %s: skill /%s done (runner=%s, %d chars)", self.name, skill_name, result.runner_used, output_len)

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
            post_run_timeout = int(skill.get("post_run_timeout", 300))
            post_run_output = await self._exec_hook(skill_name, "post_run", post_run_template, timeout_s=post_run_timeout)
            self.state.setdefault("step_outputs", {})["_post_run"] = post_run_output
            # Truncate for storage but keep full output available
            post_summary = post_run_output.strip()[-2000:] if len(post_run_output) > 2000 else post_run_output.strip()
            await self.bus.publish(Message(
                source="forge",
                content=f"🧪 **Forge {self.name}** → /{skill_name} post_run done\n```\n{post_summary[:500]}\n```",
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

    def format_workflow(self, with_backticks: bool = True) -> str:
        """Render the ritual as a human-readable arrow chain.

        Parallel steps render as `(a | b | c)` instead of leaking the dict literal.
        """
        def render(name: str) -> str:
            return f"`/{name}`" if with_backticks else f"/{name}"

        parts: List[str] = []
        for step in self.workflow:
            if isinstance(step, str):
                parts.append(render(step))
            elif self._is_parallel_step(step):
                inner = " | ".join(render(s) for s in self._get_parallel_skills(step))
                parts.append(f"({inner})")
            else:
                parts.append(str(step))
        return " → ".join(parts)

    async def _run_parallel_skills(self, skill_names: List[str]) -> None:
        """Run multiple skills in parallel using asyncio.gather."""
        await self.bus.publish(Message(
            source="forge",
            content=f"⚡ **Forge {self.name}** → Parallel start: {', '.join(f'/{s}' for s in skill_names)}",
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
                self._store_step_summary(name, output)

        if errors:
            raise RuntimeError(f"Parallel skills failed: {'; '.join(errors)}")

        # Set previous_output to combined outputs
        all_outputs = [self.state["step_outputs"][s] for s in skill_names if s in self.state["step_outputs"]]
        self.state["previous_output"] = "\n\n---\n\n".join(all_outputs)

        await self.bus.publish(Message(
            source="forge",
            content=f"✅ **Forge {self.name}** → Parallel done: {', '.join(f'/{s}' for s in skill_names)}",
            level="info",
            forge_name=self.name,
        ))

    # ------------------------------------------------------------------
    # Workflow engine
    # ------------------------------------------------------------------

    async def run_workflow(self, task: str, instructions: Optional[str] = None) -> None:
        run_number = self._next_run_number()
        logger.info("Forge %s: starting workflow run #%d", self.name, run_number)

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
            try:
                await self._execute_from_current()
            except Exception as exc:
                self.state["status"] = "error"
                self.state["error"] = str(exc)
                self._save_state()
                self._archive_run()
                logger.error("Forge %s: fatal error run #%d: %s", self.name, run_number, exc)
                await self.bus.publish(Message(
                    source="forge",
                    content=f"💥 **Forge {self.name}** — fatal error: {exc}",
                    level="error",
                    forge_name=self.name,
                ))
        finally:
            if self.dynamic_workspace:
                if self.state.get("status") == "completed":
                    await self._cleanup_dynamic_workspace()
                elif self._dynamic_workspace_dir:
                    logger.warning(
                        "Forge %s: preserving dynamic workspace %s (status=%s) — commits may exist",
                        self.name, self._dynamic_workspace_dir, self.state.get("status"),
                    )
                    await self.bus.publish(Message(
                        source="forge",
                        content=(
                            f"⚠️ **Forge {self.name}** — Workspace preserved (workflow did not complete):\n"
                            f"  📂 `{self._dynamic_workspace_dir}`\n"
                            f"  Use `!{self.name} reset` to clean up manually."
                        ),
                        level="warning",
                        forge_name=self.name,
                    ))
            if self.use_git_worktree and self.state.get("status") == "completed":
                await self._teardown_git_worktrees()

    async def run_single_skill(
        self,
        skill_name: str,
        task: str,
        instructions: Optional[str] = None,
    ) -> str:
        if skill_name not in self.spells:
            raise ValueError(f"Skill inconnu : {skill_name}")

        run_number = self._next_run_number()
        self.state["status"] = "running"
        self.state["task"] = task
        self.state["instructions"] = instructions
        self.state["current_skill"] = skill_name
        self.state["current_skill_started_at"] = time.time()
        self.state["run_number"] = run_number
        self._save_state()

        try:
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
        finally:
            if self.use_git_worktree and self.state.get("status") == "idle":
                await self._teardown_git_worktrees()

    async def _execute_from_current(self) -> None:
        while self.state["current_index"] < len(self.workflow):
            step = self.workflow[self.state["current_index"]]

            # --- Parallel step ---
            if self._is_parallel_step(step):
                parallel_skills = self._get_parallel_skills(step)
                self.state["current_skill"] = f"parallel:{','.join(parallel_skills)}"
                self.state["current_skill_started_at"] = time.time()
                self._save_state()

                await self.refresh_git_snapshot()
                await self._run_parallel_skills(parallel_skills)

                self.state["current_index"] += 1
                self._save_state()
                continue

            # --- Sequential step ---
            skill_name = step
            skill = self.spells[skill_name]
            self.state["current_skill"] = skill_name
            self.state["current_skill_started_at"] = time.time()
            self._save_state()

            # Refresh git snapshot before running so external commits show up in status
            await self.refresh_git_snapshot()

            output = await self._run_skill(skill_name)

            self._save_skill_output(skill_name, output)
            self.state["step_outputs"][skill_name] = output
            self.state["previous_output"] = output

            # Extract summary for context compression (inter-agent communication)
            self._store_step_summary(skill_name, output)

            # Dynamic workspace provisioning: clone repos after analyze step
            if self.dynamic_workspace and skill_name == "analyze":
                await self._provision_dynamic_workspace()

            pass_condition = skill.get("pass_condition")
            if pass_condition:
                passed = self._evaluate_pass_condition(pass_condition, output)

                if not passed:
                    retry_key = skill_name
                    retries = self.state["retries"].get(retry_key, 0) + 1
                    self.state["retries"][retry_key] = retries

                    if retries >= self.max_retries:
                        self.state["status"] = "failed"
                        self.state["error"] = f"{skill_name} failed after {retries} attempts"
                        self._save_state()
                        self._archive_run()
                        await self.bus.publish(Message(
                            source="forge",
                            content=f"💥 **Forge {self.name}** → {skill_name} failed after {retries} attempts. Workflow stopped.",
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
                            content=f"🔄 **Forge {self.name}** → {skill_name} rejected (attempt {retries}/{self.max_retries}). Back to /{next_on_fail}\n📋 Feedback passed to the next agent",
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
                    content=f"⏸ **Forge {self.name}** paused after /{skill_name}",
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
        self.state["current_skill_started_at"] = None
        self._save_state()
        self._archive_run()

        await self.bus.publish(Message(
            source="forge",
            content=f"🏁 **Forge {self.name}** — workflow completed successfully (run #{self.state['run_number']})",
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
        logger.info("Forge %s: abort requested", self.name)
        return True

    def reset(self) -> None:
        if self._running_task and not self._running_task.done():
            self._running_task.cancel()
        self._cleanup_dynamic_workspace_sync()
        self._teardown_git_worktrees_sync()
        self.state = {
            "status": "idle",
            "current_skill": None,
            "current_skill_started_at": None,
            "current_index": 0,
            "task": None,
            "instructions": None,
            "retries": {},
            "step_outputs": {},
            "previous_output": None,
            "error": None,
            "run_number": self.state.get("run_number", 0),
            "last_review": None,
            "git_snapshot": self.state.get("git_snapshot", {}),
            "external_snapshot": {},
        }
        self._feedback_buffer.clear()
        self._extra_instructions = None
        self._save_state()

    def reset_metrics(self) -> None:
        """Zero out persistent counters (skill_metrics, run_number) and reset state."""
        self.reset()
        self.state["skill_metrics"] = {}
        self.state["run_number"] = 0
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
            raise ValueError("No task in progress — run a full workflow first")

        previous_outputs = dict(self.state.get("step_outputs", {}))
        # Remove outputs from the restart index and all subsequent steps
        for i in range(start_index, len(self.workflow)):
            for name in self._get_step_skill_names(i):
                previous_outputs.pop(name, None)

        run_number = self._next_run_number()
        logger.info("Forge %s: resuming from /%s (run #%d)", self.name, skill_name, run_number)

        self.state["status"] = "running"
        self.state["instructions"] = instructions or self.state.get("instructions")
        self.state["current_index"] = start_index
        self.state["current_skill"] = skill_name
        self.state["current_skill_started_at"] = time.time()
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
            try:
                await self._execute_from_current()
            except Exception as exc:
                self.state["status"] = "error"
                self.state["error"] = str(exc)
                self._save_state()
                self._archive_run()
                logger.error("Forge %s: run #%d error: %s", self.name, run_number, exc)
                await self.bus.publish(Message(
                    source="forge",
                    content=f"💥 **Forge {self.name}** — fatal error: {exc}",
                    level="error",
                    forge_name=self.name,
                ))
        finally:
            if self.use_git_worktree and self.state.get("status") == "completed":
                await self._teardown_git_worktrees()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @staticmethod
    def _format_duration(seconds: float) -> str:
        s = max(0, int(seconds))
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m{s % 60:02d}s"
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"

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

        current = s.get("current_skill", "") or ""
        skill_info = ""
        if current and status == "running":
            started = s.get("current_skill_started_at")
            elapsed = f" ({self._format_duration(time.time() - started)})" if started else ""
            skill_info = f" `/{current}`{elapsed}"
        elif current and status == "paused":
            skill_info = f" \u23f8 `/{current}`"

        task_str = s.get("task") or ""
        jira_match = self._JIRA_RE.search(task_str)
        jira_part = f"{jira_match.group(1)} \u00b7 " if jira_match else ""
        # Strip JIRA prefix from task to avoid duplication
        task_clean = task_str
        if jira_match:
            task_clean = (task_str[:jira_match.start()] + task_str[jira_match.end():]).strip(" -:\u00b7")
        task_short = task_clean[:50]

        return f"{icon} **{self.name}** {bar} {pct}%{skill_info} \u2014 {jira_part}{task_short}"

    @property
    def status_summary(self) -> str:
        s = self.state
        completed_skills = set(
            k for k in s.get("step_outputs", {}) if not k.startswith("_")
        )
        current = s.get("current_skill")
        status = s.get("status", "idle")

        metrics = s.get("skill_metrics", {}) or {}

        def _step_suffix(name: str) -> str:
            m = metrics.get(name) or {}
            parts = []
            runner = m.get("runner_used")
            if runner:
                parts.append(runner)
            dur = m.get("duration_s")
            if isinstance(dur, (int, float)):
                parts.append(self._format_duration(dur))
            tok = m.get("tokens") or {}
            total = tok.get("total") if isinstance(tok, dict) else None
            if isinstance(total, (int, float)) and total > 0:
                parts.append(f"{int(total / 1000)}k tok" if total >= 1000 else f"{int(total)} tok")
            return f" _({', '.join(parts)})_" if parts else ""

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
                        icons.append(f"✅`/{s}`{_step_suffix(s)}")
                    elif is_current:
                        icons.append(f"🔄`/{s}`")
                    else:
                        icons.append(f"⬜`/{s}`")
                label = " | ".join(icons)
                if is_current:
                    steps.append(f"  ⚡ [{label}] ← parallel")
                elif all_done:
                    steps.append(f"  ⚡ [{label}]")
                else:
                    steps.append(f"  ⬜ [{label}]")
            else:
                skill = step
                if skill in completed_skills:
                    steps.append(f"  ✅ `/{skill}`{_step_suffix(skill)}")
                elif skill == current and status == "running":
                    started = self.state.get("current_skill_started_at")
                    elapsed = self._format_duration(time.time() - started) if started else "?"
                    steps.append(f"  🔄 `/{skill}` ← running ({elapsed})")
                elif skill == current and status == "paused":
                    steps.append(f"  ⏸ `/{skill}` ← paused")
                elif skill == current and status in ("error", "failed"):
                    steps.append(f"  ❌ `/{skill}` ← failed")
                else:
                    steps.append(f"  ⬜ `/{skill}`")

        task_str = self.state.get("task") or "—"
        jira_match = self._JIRA_RE.search(task_str)
        jira_id = jira_match.group(1) if jira_match else None
        header_task = f"[{jira_id}] {task_str[:120]}" if jira_id else task_str[:120]

        lines = [
            self.progress_bar,
            f"\U0001f4cb Task: {header_task}",
            "",
            "**Workflow :**",
        ]
        lines.extend(steps)

        # Last review verdict (set after any review skill with a verdict field)
        last_review = self.state.get("last_review")
        if last_review and last_review.get("verdict"):
            verdict = last_review.get("verdict") or "?"
            score = last_review.get("score")
            score_str = f"{score}/100" if isinstance(score, (int, float)) else "—"
            block_n = last_review.get("blocking_issues") or 0
            block_str = f" · {block_n} blocking" if block_n else ""
            review_skill = last_review.get("skill") or "review"
            lines.append(
                f"\n🔍 Dernière review (`/{review_skill}`) : **{score_str}** · {verdict}{block_str}"
            )

        # External snapshot (GitLab MR + pipeline) — on-demand via `!<forge> sync`
        ext_snap = self.state.get("external_snapshot") or {}
        if ext_snap:
            lines.append("\n**Remote** (GitLab):")
            mr_state_icon = {
                "opened": "🟢", "merged": "🟣", "closed": "⚫",
            }
            pipe_state_icon = {
                "success": "✅", "failed": "❌", "running": "🔄",
                "pending": "⏳", "canceled": "🚫", "skipped": "⏭",
                "manual": "✋", "created": "🆕",
            }
            for repo_name, info in ext_snap.items():
                mr = info.get("mr")
                if mr:
                    icon = mr_state_icon.get(mr.get("state"), "📋")
                    draft = " (draft)" if mr.get("draft") else ""
                    title = mr.get("title") or ""
                    url = mr.get("web_url") or ""
                    iid = mr.get("iid")
                    target = mr.get("target_branch") or ""
                    target_part = f" → `{target}`" if target else ""
                    mr_line = f"  • **{repo_name}** MR : {icon} !{iid} {mr.get('state')}{draft}{target_part} — {title[:60]}"
                    if url:
                        mr_line += f"\n    {url}"
                    lines.append(mr_line)
                else:
                    lines.append(f"  • **{repo_name}** MR : _(none for this branch)_")
                pipe = info.get("pipeline")
                if pipe:
                    icon = pipe_state_icon.get(pipe.get("status"), "❓")
                    sha = pipe.get("sha") or ""
                    sha_part = f" @ `{sha}`" if sha else ""
                    pipe_line = f"    Pipeline : {icon} {pipe.get('status')}{sha_part}"
                    if pipe.get("web_url"):
                        pipe_line += f" — {pipe.get('web_url')}"
                    lines.append(pipe_line)
            ext_refreshed = max(
                (i.get("refreshed_at", 0) for i in ext_snap.values()), default=0
            )
            if ext_refreshed:
                age = time.time() - ext_refreshed
                if age > 60:
                    lines.append(f"  _(remote snapshot age: {self._format_duration(age)})_")

        # Git snapshot — surface external work done outside Mycel
        git_snap = self.state.get("git_snapshot") or {}
        if git_snap:
            lines.append("\n**Git** (workspace state):")
            for repo_name, info in git_snap.items():
                branch = info.get("branch") or "(detached)"
                base = (info.get("base") or "").replace("origin/", "")
                ahead = info.get("ahead")
                behind = info.get("behind")
                ab_parts = []
                if isinstance(ahead, int) and isinstance(behind, int) and base:
                    if ahead or behind:
                        ab_parts.append(f"↑{ahead} ↓{behind} vs {base}")
                    else:
                        ab_parts.append(f"= {base}")
                dirty_count = info.get("dirty_count") or 0
                if dirty_count:
                    ab_parts.append(f"📝 {dirty_count} uncommitted")
                last_sha = info.get("last_sha") or ""
                last_msg = info.get("last_msg") or ""
                last_when = info.get("last_msg_when") or ""
                tail = f" — `{last_sha}` {last_msg} _({last_when})_" if last_sha else ""
                ab_str = f" ({', '.join(ab_parts)})" if ab_parts else ""
                lines.append(f"  • **{repo_name}** : `{branch}`{ab_str}{tail}")
            # Hint when refresh is stale (>5 min)
            refreshed = max(
                (i.get("refreshed_at", 0) for i in git_snap.values()), default=0
            )
            if refreshed and time.time() - refreshed > 300:
                lines.append(
                    f"  _(snapshot age: {self._format_duration(time.time() - refreshed)})_"
                )

        if s.get("error"):
            lines.append(f"\n⚠️ Error: {s['error']}")
        if s.get("retries"):
            retries_str = ", ".join(f"/{k}: {v}/{self.max_retries}" for k, v in s["retries"].items())
            lines.append(f"🔄 Retries: {retries_str}")

        # Hint for available actions
        if status in ("error", "failed"):
            lines.append(f"\n💡 `!{self.name} from <skill>` to resume from a step")
        elif status == "paused":
            lines.append(f"\n💡 `!{self.name} resume` to continue")

        return "\n".join(lines)
