from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

logger = logging.getLogger("mycel.familiar")

StreamCallback = Callable[[str], Coroutine[Any, Any, None]]


async def kill_proc_group(proc: Optional[asyncio.subprocess.Process]) -> None:
    """Best-effort kill of a subprocess and its children.

    Subprocesses are launched with ``start_new_session=True``, so they own a
    process group: ``killpg`` reaches grandchildren too. Escalates SIGTERM ->
    SIGKILL with bounded waits so a process that ignores SIGTERM can never hang
    the event loop (the old ``proc.kill(); await proc.wait()`` could block
    forever).
    """
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
            logger.error("Subprocess pid=%s did not die after SIGKILL", proc.pid)


def _parse_token_usage(stderr: str) -> Dict[str, int]:
    """Try to extract token usage from Claude CLI stderr output.

    Claude CLI may output lines like:
      input_tokens: 1234
      output_tokens: 567
      total_cost: 0.0123
    """
    import re as _re
    usage: Dict[str, int] = {}
    for key in ("input_tokens", "output_tokens"):
        match = _re.search(rf"{key}[:\s]+(\d+)", stderr)
        if match:
            usage[key] = int(match.group(1))
    return usage


class RunnerResult:
    """Result of a runner execution."""

    def __init__(self, stdout: str, stderr: str, returncode: int, runner_used: str) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.runner_used = runner_used
        self.token_usage = _parse_token_usage(stderr)

    @property
    def success(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return self.stdout.strip() if self.stdout else self.stderr.strip()


# ------------------------------------------------------------------
# Base streaming helper
# ------------------------------------------------------------------

async def _run_streaming(
    proc: asyncio.subprocess.Process,
    prompt: str,
    timeout: int,
    on_output: StreamCallback,
    runner_name: str,
) -> Optional[RunnerResult]:
    """Shared streaming logic: write stdin, stream stdout, drain stderr."""
    assert proc.stdin is not None
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()

    stdout_parts: list[str] = []
    batch: list[str] = []
    batch_size = 0
    BATCH_THRESHOLD = 1500

    async def _read_stdout() -> None:
        nonlocal batch, batch_size
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace")
            stdout_parts.append(decoded)
            batch.append(decoded)
            batch_size += len(decoded)
            if batch_size >= BATCH_THRESHOLD:
                await on_output("".join(batch))
                batch = []
                batch_size = 0
        if batch:
            await on_output("".join(batch))

    stderr_chunks: list[bytes] = []

    async def _read_stderr() -> None:
        assert proc.stderr is not None
        data = await proc.stderr.read()
        stderr_chunks.append(data)

    stdout_task = asyncio.create_task(_read_stdout())
    stderr_task = asyncio.create_task(_read_stderr())
    reader_tasks = (stdout_task, stderr_task)

    async def _drain_readers() -> None:
        for t in reader_tasks:
            if not t.done():
                t.cancel()
        # return_exceptions=True so CancelledError from the readers is retrieved
        # and asyncio doesn't log "exception was never retrieved".
        await asyncio.gather(*reader_tasks, return_exceptions=True)

    try:
        done, pending = await asyncio.wait(reader_tasks, timeout=timeout)
    except asyncio.CancelledError:
        await kill_proc_group(proc)
        await _drain_readers()
        raise

    if pending:
        await kill_proc_group(proc)
        await _drain_readers()
        return None

    # Surface any unexpected reader exception (other than cancellation).
    for t in done:
        exc = t.exception()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            await proc.wait()
            raise exc

    await proc.wait()
    stderr_full = b"".join(stderr_chunks).decode("utf-8", errors="replace")

    if proc.returncode != 0:
        return None

    return RunnerResult(
        stdout="".join(stdout_parts),
        stderr=stderr_full,
        returncode=proc.returncode or 0,
        runner_used=runner_name,
    )


# ------------------------------------------------------------------
# Claude Runner
# ------------------------------------------------------------------

class ClaudeRunner:
    """Runs prompts via Claude Code CLI (claude -p)."""

    name = "claude"

    def __init__(
        self,
        timeout: int = 180,
        allowed_tools: Optional[str] = None,
        mcp_config: Optional[str] = None,
        permission_mode: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
    ) -> None:
        self.timeout = timeout
        self._claude_path: Optional[str] = shutil.which("claude")
        self._allowed_tools = allowed_tools
        self._mcp_config = mcp_config
        self._permission_mode = permission_mode
        self._extra_args = extra_args or []

    def _build_args(self) -> List[str]:
        """Build the CLI arguments list."""
        args = [self._claude_path, "-p"]
        if self._allowed_tools is not None:
            args.extend(["--allowedTools", self._allowed_tools])
        if self._mcp_config:
            args.extend(["--mcp-config", self._mcp_config])
        if self._permission_mode:
            args.extend(["--permission-mode", self._permission_mode])
        args.extend(self._extra_args)
        return args

    @property
    def available(self) -> bool:
        return self._claude_path is not None

    async def run(
        self,
        prompt: str,
        timeout: Optional[int] = None,
        on_output: Optional[StreamCallback] = None,
        cwd: Optional[str] = None,
    ) -> RunnerResult:
        if not self.available:
            logger.error("Claude Code CLI introuvable dans le PATH")
            return RunnerResult(stdout="", stderr="Claude Code CLI not found in PATH", returncode=1, runner_used=self.name)

        effective_timeout = timeout or self.timeout
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)

        args = self._build_args()
        cwd_label = os.path.basename(cwd) if cwd else "."
        logger.info("Lancement %s (timeout=%ds, cwd=%s)", " ".join(os.path.basename(a) for a in args[:4]), effective_timeout, cwd_label)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,  # own process group → kill children too
        )

        if on_output is not None:
            result = await _run_streaming(proc, prompt, effective_timeout, on_output, self.name)
            if result is not None:
                logger.info("Claude runner (streaming) termine (code=%s)", result.returncode)
                return result
            return RunnerResult(stdout="", stderr=f"Claude runner timed out after {effective_timeout}s", returncode=-1, runner_used=self.name)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            await kill_proc_group(proc)
            logger.warning("Claude runner timeout apres %ds", effective_timeout)
            return RunnerResult(stdout="", stderr=f"Claude runner timed out after {effective_timeout}s", returncode=-1, runner_used=self.name)
        except asyncio.CancelledError:
            await kill_proc_group(proc)
            raise

        logger.info("Claude runner termine (code=%s)", proc.returncode)
        return RunnerResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            returncode=proc.returncode or 0,
            runner_used=self.name,
        )


# ------------------------------------------------------------------
# Gemini Runner
# ------------------------------------------------------------------

class GeminiRunner:
    """Runs prompts via Google Gemini CLI."""

    name = "gemini"

    def __init__(self, timeout: int = 180, **claude_kwargs: Any) -> None:
        self.timeout = timeout
        self._gemini_path: Optional[str] = shutil.which("gemini")
        self._fallback = ClaudeRunner(timeout=timeout, **claude_kwargs)

    @property
    def available(self) -> bool:
        return self._gemini_path is not None

    async def run(
        self,
        prompt: str,
        timeout: Optional[int] = None,
        on_output: Optional[StreamCallback] = None,
        cwd: Optional[str] = None,
    ) -> RunnerResult:
        if not self.available:
            logger.info("Gemini CLI introuvable, fallback sur Claude")
            result = await self._fallback.run(prompt, timeout=timeout, on_output=on_output, cwd=cwd)
            result.runner_used = "claude (fallback from gemini)"
            return result

        effective_timeout = timeout or self.timeout
        env = os.environ.copy()
        # Gemini CLI accepts GEMINI_API_KEY or GOOGLE_API_KEY — propagate both
        gemini_key = env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY", "")
        if gemini_key:
            env.setdefault("GEMINI_API_KEY", gemini_key)
            env.setdefault("GOOGLE_API_KEY", gemini_key)

        logger.info("Lancement gemini (timeout=%ds, cwd=%s)", effective_timeout, os.path.basename(cwd) if cwd else ".")

        proc = await asyncio.create_subprocess_exec(
            self._gemini_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,  # own process group → kill children too
        )

        if on_output is not None:
            result = await _run_streaming(proc, prompt, effective_timeout, on_output, self.name)
            if result is not None:
                logger.info("Gemini runner (streaming) termine (code=%s)", result.returncode)
                return result
            # Fallback on failure
            logger.warning("Gemini streaming echoue, fallback sur Claude")
            result = await self._fallback.run(prompt, timeout=timeout, on_output=on_output, cwd=cwd)
            result.runner_used = "claude (fallback after gemini error)"
            return result

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            await kill_proc_group(proc)
            logger.warning("Gemini timeout, fallback sur Claude")
            result = await self._fallback.run(prompt, timeout=timeout, cwd=cwd)
            result.runner_used = "claude (fallback after gemini timeout)"
            return result
        except asyncio.CancelledError:
            await kill_proc_group(proc)
            raise

        if proc.returncode != 0:
            logger.warning("Gemini erreur (code=%s), fallback sur Claude", proc.returncode)
            result = await self._fallback.run(prompt, timeout=timeout, cwd=cwd)
            result.runner_used = "claude (fallback after gemini error)"
            return result

        logger.info("Gemini runner termine (code=%s)", proc.returncode)
        return RunnerResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            returncode=proc.returncode or 0,
            runner_used=self.name,
        )


# ------------------------------------------------------------------
# Cursor Runner
# ------------------------------------------------------------------

class CursorRunner:
    """Runs prompts via Cursor Agent CLI (cursor agent -p) with fallback to Claude."""

    name = "cursor"

    # Full path — shutil.which won't find it if not symlinked
    _CURSOR_PATHS = [
        "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
    ]

    def __init__(self, timeout: int = 180, **claude_kwargs: Any) -> None:
        self.timeout = timeout
        self._cursor_path: Optional[str] = self._find_cursor()
        self._fallback = ClaudeRunner(timeout=timeout, **claude_kwargs)

    @classmethod
    def _find_cursor(cls) -> Optional[str]:
        found = shutil.which("cursor")
        if found:
            return found
        for path in cls._CURSOR_PATHS:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None

    @property
    def available(self) -> bool:
        return self._cursor_path is not None

    async def run(
        self,
        prompt: str,
        timeout: Optional[int] = None,
        on_output: Optional[StreamCallback] = None,
        cwd: Optional[str] = None,
    ) -> RunnerResult:
        if not self.available:
            logger.info("Cursor introuvable, fallback sur Claude")
            result = await self._fallback.run(prompt, timeout=timeout, on_output=on_output, cwd=cwd)
            result.runner_used = "claude (fallback from cursor)"
            return result

        effective_timeout = timeout or self.timeout
        env = os.environ.copy()

        logger.info("Lancement cursor agent -p (timeout=%ds, cwd=%s)", effective_timeout, os.path.basename(cwd) if cwd else ".")

        proc = await asyncio.create_subprocess_exec(
            self._cursor_path, "agent", "-p",
            "--trust", "--force", "--approve-mcps",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,  # own process group → kill children too
        )

        if on_output is not None:
            result = await _run_streaming(proc, prompt, effective_timeout, on_output, self.name)
            if result is not None:
                logger.info("Cursor runner (streaming) termine (code=%s)", result.returncode)
                return result
            logger.warning("Cursor streaming echoue, fallback sur Claude")
            result = await self._fallback.run(prompt, timeout=timeout, on_output=on_output, cwd=cwd)
            result.runner_used = "claude (fallback after cursor error)"
            return result

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            await kill_proc_group(proc)
            logger.warning("Cursor timeout, fallback sur Claude")
            result = await self._fallback.run(prompt, timeout=timeout, cwd=cwd)
            result.runner_used = "claude (fallback after cursor timeout)"
            return result
        except asyncio.CancelledError:
            await kill_proc_group(proc)
            raise

        if proc.returncode != 0:
            logger.warning("Cursor erreur (code=%s), fallback sur Claude", proc.returncode)
            result = await self._fallback.run(prompt, timeout=timeout, cwd=cwd)
            result.runner_used = "claude (fallback after cursor error)"
            return result

        logger.info("Cursor runner termine (code=%s)", proc.returncode)
        return RunnerResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            returncode=proc.returncode or 0,
            runner_used=self.name,
        )


# ------------------------------------------------------------------
# Factory & checks
# ------------------------------------------------------------------

RUNNERS = {"claude": ClaudeRunner, "gemini": GeminiRunner, "cursor": CursorRunner}


def get_runner(name: str, timeout: int = 180, **kwargs: Any) -> Union[ClaudeRunner, GeminiRunner, CursorRunner]:
    """Factory: return the appropriate runner by name.

    Extra kwargs (allowed_tools, mcp_config, permission_mode, extra_args)
    are forwarded to ClaudeRunner (and to fallback runners).
    """
    cls = RUNNERS.get(name, ClaudeRunner)
    if cls is ClaudeRunner:
        return cls(timeout=timeout, **kwargs)
    # Gemini/Cursor accept **claude_kwargs for their fallback
    return cls(timeout=timeout, **kwargs)


def check_runners() -> Dict[str, bool]:
    """Check availability of CLI runners. Returns {{name: available}}."""
    status: Dict[str, bool] = {}

    claude_path = shutil.which("claude")
    status["claude"] = claude_path is not None
    if claude_path:
        logger.info("Claude CLI trouve : %s", claude_path)
    else:
        logger.warning("Claude CLI absent dans le PATH")

    gemini_path = shutil.which("gemini")
    status["gemini"] = gemini_path is not None
    if gemini_path:
        logger.info("Gemini CLI trouve : %s", gemini_path)
    else:
        logger.info("Gemini CLI absent (fallback Claude)")

    cursor_path = CursorRunner._find_cursor()
    status["cursor"] = cursor_path is not None
    if cursor_path:
        logger.info("Cursor CLI trouve : %s", cursor_path)
    else:
        logger.info("Cursor CLI absent (fallback Claude)")

    return status
