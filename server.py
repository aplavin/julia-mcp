import asyncio
import atexit
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from io import TextIOWrapper
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DEFAULT_TIMEOUT = 60.0
DEFAULT_JULIA_ARGS = ("--threads=auto",)
DEFAULT_IDLE_TIMEOUT = 30 * 60.0
IDLE_SWEEP_INTERVAL = 60.0
PKG_PATTERN = re.compile(r"\bPkg\.")
TEMP_SESSION_KEY = "__temp__"

mcp = FastMCP("julia")


def _idle_timeout() -> float:
    raw = os.environ.get("JULIA_MCP_IDLE_TIMEOUT")
    if raw is None:
        return DEFAULT_IDLE_TIMEOUT
    return float(raw)


class SessionNotFoundError(LookupError):
    pass


class SessionEnvMismatchError(ValueError):
    pass


class JuliaSession:
    def __init__(
        self,
        env_dir: str,
        sentinel: str,
        *,
        is_temp: bool = False,
        is_test: bool = False,
        julia_args: tuple[str, ...] = DEFAULT_JULIA_ARGS,
        julia_cmd: str | None = None,
        log_file: TextIOWrapper | None = None,
        session_id: str | None = None,
    ):
        self.env_dir = env_dir
        self.sentinel = sentinel
        self.is_temp = is_temp
        self.is_test = is_test
        self.julia_args = julia_args
        self.julia_cmd = julia_cmd
        self.session_id = session_id
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self._log_file = log_file
        self.last_used = time.monotonic()

    def touch(self) -> None:
        self.last_used = time.monotonic()

    @property
    def project_path(self) -> str:
        if self.is_test:
            return str(Path(self.env_dir).parent)
        return self.env_dir

    @property
    def init_code(self) -> str | None:
        if self.is_test:
            return "using TestEnv; TestEnv.activate()"
        return None

    async def start(self) -> None:
        parts = shlex.split(self.julia_cmd) if self.julia_cmd else ["julia"]
        executable = parts[0]
        remaining = parts[1:]
        # juliaup +channel must be the first arg after executable
        if remaining and remaining[0].startswith("+"):
            channel_args = [remaining[0]]
            extra_flags = remaining[1:]
        else:
            channel_args = []
            extra_flags = remaining

        if not os.path.isabs(executable):
            resolved = shutil.which(executable)
            if resolved is None:
                raise RuntimeError(
                    f"'{executable}' not found in PATH. Install Julia from https://julialang.org/downloads/"
                )
            executable = resolved

        cmd = [
            executable,
            *channel_args,
            "-i",
            *self.julia_args,
            *extra_flags,
            f"--project={self.project_path}",
        ]

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.env_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            limit=64 * 1024 * 1024,  # 64 MB readline buffer
        )

        # Wait for readiness
        await self._execute_raw(
            "",
            timeout=120.0,  # generous startup timeout
        )

        # Auto-load Revise so code changes are picked up without restarting
        await self._execute_raw(
            "try; using Revise; catch; end",
            timeout=120.0,
        )

        if self.init_code:
            await self._execute_raw(self.init_code, timeout=None)

        self.touch()

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def execute(self, code: str, timeout: float | None) -> str:
        async with self.lock:
            if not self.is_alive():
                raise RuntimeError("Julia session has died unexpectedly")
            # hex-encode to avoid string escaping issues; include_string for sequential parse-eval (macros work)
            hex_encoded = code.encode().hex()
            wrapped = (
                f'try; Revise.revise(); catch; end;'
                f'include_string(Main, String(hex2bytes("{hex_encoded}")));'
                f'nothing'
            )
            if self._log_file:
                ts = time.strftime("%H:%M:%S")
                self._log_file.write(f"[{ts}] julia> {code}\n")
                self._log_file.flush()
            output = await self._execute_raw(wrapped, timeout)
            if self._log_file and output:
                self._log_file.write(f"{output}\n\n")
                self._log_file.flush()
            self.touch()
            return output

    async def _execute_raw(self, code: str, timeout: float | None) -> str:
        assert self.process is not None
        assert self.process.stdin is not None

        sentinel_cmd = (
            f'flush(stderr); write(stdout, "\\n"); println(stdout, "{self.sentinel}"); flush(stdout)'
        )
        payload = code + "\n" + sentinel_cmd + "\n"
        self.process.stdin.write(payload.encode())
        await self.process.stdin.drain()

        lines: list[str] = []

        async def read_until_sentinel() -> str:
            while True:
                raw = await self.process.stdout.readline()
                if not raw:
                    collected = "\n".join(lines)
                    raise RuntimeError(
                        f"Julia process died during execution.\n"
                        f"Output before death:\n{collected}"
                    )
                line = raw.decode().rstrip("\n").rstrip("\r")
                if line == self.sentinel:
                    break
                lines.append(line)
            # The extra \n before sentinel may leave a trailing empty line
            if lines and lines[-1] == "":
                lines.pop()
            return "\n".join(lines)

        if timeout is not None:
            try:
                return await asyncio.wait_for(read_until_sentinel(), timeout=timeout)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
                partial = "\n".join(lines)
                msg = f"Execution timed out after {timeout}s. Session killed; it will restart on next call."
                if partial:
                    msg += f"\n\nOutput before timeout:\n{partial}"
                raise RuntimeError(msg)
        else:
            return await read_until_sentinel()

    async def kill(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        if self.is_temp and os.path.isdir(self.env_dir):
            shutil.rmtree(self.env_dir, ignore_errors=True)


class SessionManager:
    def __init__(self, julia_args: tuple[str, ...] = DEFAULT_JULIA_ARGS):
        self.julia_args = julia_args
        self._sessions: dict[str, JuliaSession] = {}
        self._create_locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._log_dir = tempfile.mkdtemp(prefix="julia-mcp-logs-")
        self._log_files: dict[str, TextIOWrapper] = {}
        self._eviction_task: asyncio.Task | None = None
        self._idle_timeout = _idle_timeout()
        atexit.register(self._cleanup_logs)

    def _get_log_file(self, key: str) -> TextIOWrapper:
        if key not in self._log_files:
            safe_name = key.replace("/", "_").replace("\\", "_").strip("_") or "temp"
            path = os.path.join(self._log_dir, f"{safe_name}.log")
            self._log_files[key] = open(path, "a")
        return self._log_files[key]

    def _cleanup_logs(self) -> None:
        for f in self._log_files.values():
            try:
                f.close()
            except Exception:
                pass
        shutil.rmtree(self._log_dir, ignore_errors=True)

    def _legacy_key(self, env_path: str | None) -> str:
        if env_path is None:
            return TEMP_SESSION_KEY
        return str(Path(env_path).resolve())

    def _key(self, env_path: str | None) -> str:
        return self._legacy_key(env_path)

    def _resolve_env_dir(self, env_path: str | None) -> tuple[str, bool, bool]:
        is_temp = env_path is None
        if is_temp:
            env_dir = tempfile.mkdtemp(prefix="julia-mcp-")
            is_test = False
        else:
            resolved = Path(env_path).resolve()
            env_dir = str(resolved)
            is_test = resolved.name == "test"
        return env_dir, is_temp, is_test

    def _ensure_eviction_task(self) -> None:
        if self._eviction_task is None or self._eviction_task.done():
            self._eviction_task = asyncio.create_task(self._eviction_loop())

    async def _evict_idle_sessions(self) -> None:
        now = time.monotonic()
        expired_keys = [
            key
            for key, session in self._sessions.items()
            if now - session.last_used > self._idle_timeout
        ]
        for key in expired_keys:
            session = self._sessions.pop(key, None)
            if session is not None:
                await session.kill()

    async def _eviction_loop(self) -> None:
        while True:
            await asyncio.sleep(IDLE_SWEEP_INTERVAL)
            await self._evict_idle_sessions()

    async def _spawn_session(
        self,
        env_path: str | None,
        *,
        key: str,
        session_id: str | None = None,
        julia_cmd: str | None = None,
    ) -> JuliaSession:
        sentinel = f"__JULIA_MCP_{uuid.uuid4().hex}__"
        env_dir, is_temp, is_test = self._resolve_env_dir(env_path)
        session = JuliaSession(
            env_dir,
            sentinel,
            is_temp=is_temp,
            is_test=is_test,
            julia_args=self.julia_args,
            julia_cmd=julia_cmd,
            log_file=self._get_log_file(key),
            session_id=session_id,
        )
        await session.start()
        self._sessions[key] = session
        self._ensure_eviction_task()
        return session

    async def create_session(
        self,
        env_path: str | None,
        julia_cmd: str | None = None,
    ) -> tuple[str, JuliaSession]:
        session_id = uuid.uuid4().hex

        async with self._global_lock:
            if session_id not in self._create_locks:
                self._create_locks[session_id] = asyncio.Lock()
            create_lock = self._create_locks[session_id]

        async with create_lock:
            session = await self._spawn_session(
                env_path,
                key=session_id,
                session_id=session_id,
                julia_cmd=julia_cmd,
            )
            return session_id, session

    async def get_session(
        self,
        session_id: str,
        env_path: str | None = None,
    ) -> JuliaSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(
                f"Session not found: {session_id}. "
                "Call julia_create_session to create a new session."
            )

        if env_path is not None:
            expected_dir = self._legacy_key(env_path)
            if session.env_dir != expected_dir:
                raise SessionEnvMismatchError(
                    f"session_id={session_id} is bound to env_path={session.env_dir}, "
                    f"not {expected_dir}."
                )

        if not session.is_alive():
            await session.kill()
            del self._sessions[session_id]
            raise SessionNotFoundError(
                f"Session {session_id} has died. Call julia_create_session to create a new session."
            )

        session.touch()
        return session

    async def get_or_create(
        self,
        env_path: str | None,
        julia_cmd: str | None = None,
    ) -> JuliaSession:
        key = self._legacy_key(env_path)

        # Fast path
        if key in self._sessions and self._sessions[key].is_alive():
            if self._sessions[key].julia_cmd == julia_cmd:
                self._sessions[key].touch()
                return self._sessions[key]
            # julia_cmd mismatch — restart with the requested config
            await self._sessions[key].kill()
            del self._sessions[key]

        # Get per-key creation lock
        async with self._global_lock:
            if key not in self._create_locks:
                self._create_locks[key] = asyncio.Lock()
            create_lock = self._create_locks[key]

        async with create_lock:
            # Double-check
            if key in self._sessions and self._sessions[key].is_alive():
                if self._sessions[key].julia_cmd == julia_cmd:
                    self._sessions[key].touch()
                    return self._sessions[key]
                await self._sessions[key].kill()
                del self._sessions[key]

            # Clean up dead session
            if key in self._sessions:
                await self._sessions[key].kill()
                del self._sessions[key]

            return await self._spawn_session(env_path, key=key, julia_cmd=julia_cmd)

    async def restart(
        self,
        env_path: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """Kill a session, returning True if one was found."""
        if session_id is not None:
            key = session_id
        else:
            key = self._legacy_key(env_path)

        if key in self._sessions:
            await self._sessions[key].kill()
            del self._sessions[key]
            return True
        return False

    def list_sessions(self) -> list[dict]:
        now = time.monotonic()
        result = []
        for key, session in self._sessions.items():
            info = {
                "env_path": session.env_dir,
                "alive": session.is_alive(),
                "temp": session.is_temp,
                "idle_seconds": round(now - session.last_used, 1),
            }
            if session.session_id is not None:
                info["session_id"] = session.session_id
            if session.julia_cmd is not None:
                info["julia_cmd"] = session.julia_cmd
            if key in self._log_files:
                info["log_file"] = self._log_files[key].name
            result.append(info)
        return result

    async def shutdown(self) -> None:
        if self._eviction_task is not None and not self._eviction_task.done():
            self._eviction_task.cancel()
            try:
                await self._eviction_task
            except asyncio.CancelledError:
                pass
            self._eviction_task = None

        for session in self._sessions.values():
            await session.kill()
        self._sessions.clear()
        self._cleanup_logs()


manager = SessionManager()


@mcp.tool()
async def julia_create_session(
    env_path: str | None = None,
    julia_cmd: str | None = None,
) -> str:
    """Create a dedicated Julia session for multi-agent isolation.

    When multiple agents may work on the same project concurrently, each agent
    should call this once at the start of its work and pass the returned
    session_id on all subsequent julia_eval and julia_restart calls.

    Args:
        env_path: Julia project directory path. Omit for a temporary environment.
        julia_cmd: Custom Julia command. Examples: "julia +1.11", "julia --threads=1".
    """
    session_id, session = await manager.create_session(env_path, julia_cmd=julia_cmd)
    label = env_path if env_path is not None else "(temp)"
    return (
        f"Session created. session_id={session_id} env_path={label}. "
        f"Pass session_id on all subsequent julia_eval and julia_restart calls."
    )


@mcp.tool()
async def julia_eval(
    code: str,
    env_path: str | None = None,
    session_id: str | None = None,
    timeout: float | None = None,
    julia_cmd: str | None = None,
) -> str:
    """ALWAYS use this tool to run Julia code. NEVER run julia via command line.

    Persistent REPL session with state preserved between calls.
    Each env_path gets its own session, started lazily.
    For multi-agent isolation, call julia_create_session first and pass session_id.
    Do not type `Pkg.activate()` explicitly in your code; instead, specify the env_path argument to select the environment.

    Args:
        code: Julia code to evaluate. Use display(...)/println(...) to see output.
        env_path: Julia project directory path. Omit for a temporary environment.
        session_id: Session ID from julia_create_session. When set, env_path is optional.
        timeout: Seconds (default: 60). Auto-disabled for Pkg operations.
        julia_cmd: Custom Julia command, should be used rarely, only when explicitly requested. Examples: "julia +1.11", "julia --check-bounds=yes", "/path/to/julia".
    """
    if timeout is None:
        effective_timeout: float | None = (
            None if PKG_PATTERN.search(code) else DEFAULT_TIMEOUT
        )
    else:
        effective_timeout = timeout if timeout > 0 else None

    try:
        if session_id is not None:
            session = await manager.get_session(session_id, env_path=env_path)
        else:
            session = await manager.get_or_create(env_path, julia_cmd=julia_cmd)
        output = await session.execute(code, timeout=effective_timeout)
        return output if output else "(no output)"
    except (SessionNotFoundError, SessionEnvMismatchError) as e:
        return f"Error: {e}"
    except RuntimeError as e:
        # Clean up dead session so next call starts fresh
        if session_id is not None:
            if session_id in manager._sessions and not manager._sessions[session_id].is_alive():
                del manager._sessions[session_id]
        else:
            key = manager._legacy_key(env_path)
            if key in manager._sessions and not manager._sessions[key].is_alive():
                del manager._sessions[key]
        return f"Error: {e}"


@mcp.tool()
async def julia_restart(
    env_path: str | None = None,
    session_id: str | None = None,
) -> str:
    """Restart a Julia session, clearing all state.

    IMPORTANT: Restarting is slow and loses all session state. Very rarely needed.
    Revise.jl is loaded automatically in every session, so code changes to loaded packages are picked up without restarting.
    Only restart as a last resort when the session is truly broken, or code changes that Revise cannot fix.
    Do NOT restart just because source files were edited between script or test runs — Revise picks up those changes automatically.

    Args:
        env_path: Environment to restart. If omitted, restarts the temporary session
            (NOT every active session) — most callers should pass the same env_path
            they used in julia_eval.
        session_id: Session ID from julia_create_session. When set, env_path is optional.
    """
    if session_id is not None:
        label = f"session_id={session_id}"
    else:
        label = env_path if env_path is not None else "temporary"

    killed = await manager.restart(env_path=env_path, session_id=session_id)
    if killed:
        if session_id is not None:
            return (
                f"Session restarted ({label}). "
                "Call julia_create_session to obtain a new session_id."
            )
        return f"Session restarted ({label}). A fresh session will start on next julia_eval call."
    active = [s.get("session_id") or s["env_path"] for s in manager.list_sessions()]
    if active:
        return (
            f"No active session for {label} — nothing to restart. "
            f"Active sessions: {active}"
        )
    return f"No active session for {label} — nothing to restart."


@mcp.tool()
async def julia_list_sessions() -> str:
    """List all active Julia sessions and their environments."""
    sessions = manager.list_sessions()
    if not sessions:
        return "No active Julia sessions."
    lines = []
    for s in sessions:
        status = "alive" if s["alive"] else "dead"
        label = f"{s['env_path']} (temp)" if s["temp"] else s["env_path"]
        sid = f" session_id={s['session_id']}" if "session_id" in s else ""
        idle = f" idle={s['idle_seconds']}s"
        julia = f" julia_cmd={s['julia_cmd']}" if "julia_cmd" in s else ""
        log = f" log={s['log_file']}" if "log_file" in s else ""
        lines.append(f"  {label}:{sid} {status}{idle}{julia}{log}")
    return "Active Julia sessions:\n" + "\n".join(lines)


def main():
    global manager
    julia_args = tuple(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_JULIA_ARGS
    manager = SessionManager(julia_args=julia_args)
    print(f"Julia MCP log directory: {manager._log_dir}", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
