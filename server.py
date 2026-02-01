import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DEFAULT_TIMEOUT = 60.0
PKG_PATTERN = re.compile(r"\b(Pkg\.|using |import )")
TEMP_SESSION_KEY = "__temp__"

mcp_server = FastMCP("julia")


class JuliaSession:
    def __init__(
        self,
        project_path: str,
        sentinel: str,
        *,
        is_temp: bool = False,
        init_code: str | None = None,
    ):
        self.project_path = project_path
        self.sentinel = sentinel
        self.is_temp = is_temp
        self.init_code = init_code
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        julia = shutil.which("julia")
        if julia is None:
            raise RuntimeError(
                "Julia not found in PATH. Install from https://julialang.org/downloads/"
            )

        cmd = [
            julia,
            "-i",
            "--startup-file=no",
            f"--project={self.project_path}",
        ]

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.project_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # Wait for readiness
        await self._execute_raw(
            "",
            timeout=120.0,  # generous startup timeout
        )

        if self.init_code:
            await self._execute_raw(self.init_code, timeout=None)

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def execute(self, code: str, timeout: float | None) -> str:
        async with self.lock:
            if not self.is_alive():
                raise RuntimeError("Julia session has died unexpectedly")
            return await self._execute_raw(code, timeout)

    async def _execute_raw(self, code: str, timeout: float | None) -> str:
        assert self.process is not None
        assert self.process.stdin is not None

        sentinel_cmd = (
            f'flush(stderr); println(stdout, "{self.sentinel}"); flush(stdout)'
        )
        payload = code + "\n" + sentinel_cmd + "\n"
        self.process.stdin.write(payload.encode())
        await self.process.stdin.drain()

        async def read_until_sentinel() -> str:
            lines: list[str] = []
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
            return "\n".join(lines)

        if timeout is not None:
            try:
                return await asyncio.wait_for(read_until_sentinel(), timeout=timeout)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
                raise RuntimeError(
                    f"Execution timed out after {timeout}s. "
                    "Session killed; it will restart on next call."
                )
        else:
            return await read_until_sentinel()

    async def kill(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        if self.is_temp and os.path.isdir(self.project_path):
            shutil.rmtree(self.project_path, ignore_errors=True)


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, JuliaSession] = {}
        self._create_locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

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
            init_code = None
            if is_temp:
                project_path = tempfile.mkdtemp(prefix="julia-mcp-")
            else:
                resolved = Path(env_path).resolve()
                if resolved.name == "test":
                    project_path = str(resolved.parent)
                    init_code = "using TestEnv; TestEnv.activate()"
                else:
                    project_path = str(resolved)

            session = JuliaSession(
                project_path, sentinel, is_temp=is_temp, init_code=init_code,
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
            env_path = session.project_path if session.is_temp else key
            result.append(
                {
                    "env_path": env_path,
                    "alive": session.is_alive(),
                    "temp": session.is_temp,
                }
            )
        return result

    async def shutdown(self) -> None:
        for session in self._sessions.values():
            await session.kill()
        self._sessions.clear()


manager = SessionManager()


@mcp_server.tool()
async def julia_eval(
    code: str,
    env_path: str | None = None,
    timeout: float | None = None,
) -> str:
    """ALWAYS use this tool to run Julia code. NEVER run julia via command line.

    Persistent REPL session with state preserved between calls.
    Each env_path gets its own session, started lazily.

    Args:
        code: Julia code to evaluate. Result of the last expression is displayed.
        env_path: Julia project directory path. Omit for a temporary environment.
        timeout: Seconds (default: 60). Auto-disabled for Pkg/using/import.
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


@mcp_server.tool()
async def julia_restart(env_path: str | None = None) -> str:
    """Restart a Julia session, clearing all state.
    You usually don't need this, preserve persistent sessions when possible for performance.
    Only restart when you think the session in a bad or inconsistent state.

    Args:
        env_path: Environment to restart. If omitted, restarts the temporary session.
    """
    await manager.restart(env_path)
    return "Session restarted. A fresh session will start on next julia_eval call."


@mcp_server.tool()
async def julia_list_sessions() -> str:
    """List all active Julia sessions and their environments."""
    sessions = manager.list_sessions()
    if not sessions:
        return "No active Julia sessions."
    lines = []
    for s in sessions:
        status = "alive" if s["alive"] else "dead"
        label = f"{s['env_path']} (temp)" if s["temp"] else s["env_path"]
        lines.append(f"  {label}: {status}")
    return "Active Julia sessions:\n" + "\n".join(lines)


def main():
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
