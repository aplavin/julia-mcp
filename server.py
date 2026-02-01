import asyncio
import atexit
import os
import re
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
DEFAULT_JULIA_ARGS = ("--startup-file=no", "--threads=auto")
PKG_PATTERN = re.compile(r"\bPkg\.")
TEMP_SESSION_KEY = "__temp__"

mcp = FastMCP("julia")


class JuliaSession:
    def __init__(
        self,
        env_dir: str,
        sentinel: str,
        *,
        is_temp: bool = False,
        is_test: bool = False,
        julia_args: tuple[str, ...] = DEFAULT_JULIA_ARGS,
        log_file: TextIOWrapper | None = None,
    ):
        self.env_dir = env_dir
        self.sentinel = sentinel
        self.is_temp = is_temp
        self.is_test = is_test
        self.julia_args = julia_args
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self._log_file = log_file

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
        julia = shutil.which("julia")
        if julia is None:
            raise RuntimeError(
                "Julia not found in PATH. Install from https://julialang.org/downloads/"
            )

        cmd = [
            julia,
            "-i",
            *self.julia_args,
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

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def execute(self, code: str, timeout: float | None) -> str:
        async with self.lock:
            if not self.is_alive():
                raise RuntimeError("Julia session has died unexpectedly")
            # hex-encode to avoid string escaping issues; include_string for sequential parse-eval (macros work)
            hex_encoded = code.encode().hex()
            wrapped = f'include_string(Main, String(hex2bytes("{hex_encoded}"))); nothing'
            if self._log_file:
                ts = time.strftime("%H:%M:%S")
                self._log_file.write(f"[{ts}] julia> {code}\n")
                self._log_file.flush()
            output = await self._execute_raw(wrapped, timeout)
            if self._log_file and output:
                self._log_file.write(f"{output}\n\n")
                self._log_file.flush()
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

    def _key(self, env_path: str | None) -> str:
        if env_path is None:
            return TEMP_SESSION_KEY
        return str(Path(env_path).resolve())

    async def get_or_create(self, env_path: str | None) -> JuliaSession:
        key = self._key(env_path)

        # Fast path
        if key in self._sessions and self._sessions[key].is_alive():
            return self._sessions[key]

        # Get per-key creation lock
        async with self._global_lock:
            if key not in self._create_locks:
                self._create_locks[key] = asyncio.Lock()
            create_lock = self._create_locks[key]

        async with create_lock:
            # Double-check
            if key in self._sessions and self._sessions[key].is_alive():
                return self._sessions[key]

            # Clean up dead session
            if key in self._sessions:
                await self._sessions[key].kill()
                del self._sessions[key]

            # Create new session
            sentinel = f"__JULIA_MCP_{uuid.uuid4().hex}__"
            is_temp = env_path is None
            if is_temp:
                env_dir = tempfile.mkdtemp(prefix="julia-mcp-")
                is_test = False
            else:
                resolved = Path(env_path).resolve()
                env_dir = str(resolved)
                is_test = resolved.name == "test"

            session = JuliaSession(
                env_dir, sentinel, is_temp=is_temp, is_test=is_test,
                julia_args=self.julia_args,
                log_file=self._get_log_file(key),
            )
            await session.start()
            self._sessions[key] = session
            return session

    async def restart(self, env_path: str | None) -> None:
        key = self._key(env_path)
        if key in self._sessions:
            await self._sessions[key].kill()
            del self._sessions[key]

    def list_sessions(self) -> list[dict]:
        result = []
        for key, session in self._sessions.items():
            info = {
                "env_path": session.env_dir,
                "alive": session.is_alive(),
                "temp": session.is_temp,
            }
            if key in self._log_files:
                info["log_file"] = self._log_files[key].name
            result.append(info)
        return result

    async def shutdown(self) -> None:
        for session in self._sessions.values():
            await session.kill()
        self._sessions.clear()
        self._cleanup_logs()


manager = SessionManager()


@mcp.tool()
async def julia_eval(
    code: str,
    env_path: str | None = None,
    timeout: float | None = None,
) -> str:
    """ALWAYS use this tool to run Julia code. NEVER run julia via command line.

    Persistent REPL session with state preserved between calls.
    Each env_path gets its own session, started lazily.

    Args:
        code: Julia code to evaluate. Use display(...)/println(...) to see output.
        env_path: Julia project directory path. Omit for a temporary environment.
        timeout: Seconds (default: 60). Auto-disabled for Pkg operations.
    """
    if timeout is None:
        effective_timeout: float | None = (
            None if PKG_PATTERN.search(code) else DEFAULT_TIMEOUT
        )
    else:
        effective_timeout = timeout if timeout > 0 else None

    try:
        session = await manager.get_or_create(env_path)
        output = await session.execute(code, timeout=effective_timeout)
        return output if output else "(no output)"
    except RuntimeError as e:
        # Clean up dead session so next call starts fresh
        key = manager._key(env_path)
        if key in manager._sessions and not manager._sessions[key].is_alive():
            del manager._sessions[key]
        return f"Error: {e}"


@mcp.tool()
async def julia_restart(env_path: str | None = None) -> str:
    """Restart a Julia session, clearing all state.

    IMPORTANT: Restarting is slow and loses all session state. Very rarely needed.
    Revise.jl is loaded automatically in every session, so code changes to loaded packages are picked up without restarting.
    Only restart as a last resort when the session is truly broken, or code changes that Revise cannot fix.
    Do NOT restart just because source files were edited between script or test runs — Revise picks up those changes automatically.

    Args:
        env_path: Environment to restart. If omitted, restarts the temporary session.
    """
    await manager.restart(env_path)
    return "Session restarted. A fresh session will start on next julia_eval call."


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
        log = f" log={s['log_file']}" if "log_file" in s else ""
        lines.append(f"  {label}: {status}{log}")
    return "Active Julia sessions:\n" + "\n".join(lines)


def main():
    global manager
    julia_args = tuple(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_JULIA_ARGS
    manager = SessionManager(julia_args=julia_args)
    print(f"Julia MCP log directory: {manager._log_dir}", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
