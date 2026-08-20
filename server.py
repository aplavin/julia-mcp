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
PKG_PATTERN = re.compile(r"\bPkg\.")
TEMP_SESSION_KEY = "__temp__"

SessionKey = tuple[str, str | None]

mcp = FastMCP("julia")


def normalize_julia_cmd(julia_cmd: str | None) -> str | None:
    return shlex.join(shlex.split(julia_cmd)) if julia_cmd else None


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
        name: str | None = None,
        log_file: TextIOWrapper | None = None,
    ):
        self.env_dir = env_dir
        self.sentinel = sentinel
        self.is_temp = is_temp
        self.is_test = is_test
        self.julia_args = julia_args
        self.julia_cmd = julia_cmd
        self.name = name
        self.cmd: list[str] | None = None
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
        self.cmd = cmd

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
            marker = "__JULIA_MCP_INIT_ERROR__"
            wrapped = f'try; {self.init_code}; catch __e; print("{marker}"); showerror(stdout, __e); end'
            out = await self._execute_raw(wrapped, timeout=None)
            if marker in out:
                await self.kill()
                raise RuntimeError(
                    f"Failed to initialize environment {self.env_dir!r} "
                    f"(init code {self.init_code!r}):\n{out.replace(marker, '').strip()}"
                )

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
                line = raw.decode("utf-8").rstrip("\n").rstrip("\r")
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
        self._sessions: dict[SessionKey, JuliaSession] = {}
        self._names: dict[str, SessionKey] = {}
        self._create_locks: dict[SessionKey, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._log_dir = tempfile.mkdtemp(prefix="julia-mcp-logs-")
        self._log_files: dict[SessionKey, TextIOWrapper] = {}
        atexit.register(self._cleanup_logs)

    def _get_log_file(self, key: SessionKey) -> TextIOWrapper:
        if key not in self._log_files:
            env_path, julia_cmd = key
            safe_name = env_path.replace("/", "_").replace("\\", "_").strip("_") or "temp"
            if julia_cmd:
                safe_name += "_" + re.sub(r"[^A-Za-z0-9.+-]+", "_", julia_cmd).strip("_")
            path = os.path.join(self._log_dir, f"{safe_name}.log")
            self._log_files[key] = open(path, "a", encoding="utf-8")
        return self._log_files[key]

    def _cleanup_logs(self) -> None:
        for f in self._log_files.values():
            try:
                f.close()
            except Exception:
                pass
        shutil.rmtree(self._log_dir, ignore_errors=True)

    def _key(self, env_path: str | None, julia_cmd: str | None = None) -> SessionKey:
        path = TEMP_SESSION_KEY if env_path is None else str(Path(env_path).resolve())
        return (path, normalize_julia_cmd(julia_cmd))

    def _forget(self, key: SessionKey) -> None:
        self._sessions.pop(key, None)
        for name, named_key in list(self._names.items()):
            if named_key == key:
                del self._names[name]

    def _env_label(self, env: str) -> str:
        return "The temporary environment" if env == TEMP_SESSION_KEY else env

    def _describe(self, key: SessionKey) -> str:
        env_path, julia_cmd = key
        label = self._env_label(env_path)
        return f"{label} with julia_cmd={julia_cmd!r}" if julia_cmd else label

    def _sessions_on(self, env: str) -> list[tuple[SessionKey, JuliaSession]]:
        return [(k, s) for k, s in self._sessions.items() if k[0] == env]

    def _checked_name(self, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("Session name must not be empty.")
        return name

    def resolve_key(
        self,
        env_path: str | None = None,
        julia_cmd: str | None = None,
        name: str | None = None,
    ) -> SessionKey:
        """Address an existing session, without replacing or creating anything."""
        if name is not None:
            name = self._checked_name(name)
            if name not in self._names:
                known = sorted(self._names)
                hint = f" Known names: {known}." if known else ""
                raise ValueError(f"Unknown session name {name!r}.{hint}")
            return self._names[name]

        key = self._key(env_path, julia_cmd)
        if julia_cmd is not None or key in self._sessions:
            return key
        on_env = self._sessions_on(key[0])
        if len(on_env) == 1:
            return on_env[0][0]
        if on_env:
            raise ValueError(
                f"{self._env_label(key[0])} has several sessions "
                f"{sorted(self._describe(k) for k, _ in on_env)}. "
                f"Pass name or julia_cmd to pick one."
            )
        return key

    def plan(
        self,
        env_path: str | None = None,
        julia_cmd: str | None = None,
        name: str | None = None,
    ) -> tuple[SessionKey, list[SessionKey]]:
        """Pick the session to run in, plus the sessions that have to be killed to get there."""
        if name is None:
            key = self._key(env_path, julia_cmd)
            if key in self._sessions:
                return key, []
            unnamed = [k for k, s in self._sessions_on(key[0]) if s.name is None]
            if unnamed:
                return key, unnamed
            named = sorted(s.name for _, s in self._sessions_on(key[0]))
            if named:
                raise ValueError(
                    f"{self._env_label(key[0])} already has named session(s) {named} with "
                    f"different settings, which are never replaced automatically. Pass the name "
                    f"of one to restart it with these settings, or a new name to run alongside it."
                )
            return key, []

        name = self._checked_name(name)
        if env_path is None:
            if julia_cmd is not None:
                raise ValueError(
                    f"Cannot name a temporary session: pass env_path together with name={name!r}."
                )
            if name not in self._names:
                known = sorted(self._names)
                hint = f" Known names: {known}." if known else ""
                raise ValueError(
                    f"Unknown session name {name!r}. Pass env_path to create it.{hint}"
                )
            return self._names[name], []

        key = self._key(env_path, julia_cmd)
        existing = self._sessions.get(key)
        if existing is not None and existing.name is not None and existing.name != name:
            raise ValueError(
                f"{self._describe(key)} already belongs to session {existing.name!r}. "
                f"Use name={existing.name!r} instead of {name!r}."
            )
        claimed = self._names.get(name)
        if claimed is not None and claimed != key:
            return key, [claimed]
        return key, []

    def _adopt(self, session: JuliaSession, key: SessionKey, name: str | None) -> JuliaSession:
        if name is not None:
            session.name = name
            self._names[name] = key
        return session

    async def get_or_create(
        self,
        env_path: str | None = None,
        julia_cmd: str | None = None,
        name: str | None = None,
    ) -> JuliaSession:
        key, doomed = self.plan(env_path, julia_cmd, name)
        name = self._checked_name(name) if name else None

        # Fast path
        if not doomed:
            existing = self._sessions.get(key)
            if existing is not None and existing.is_alive():
                return self._adopt(existing, key, name)

        # Get per-key creation lock
        async with self._global_lock:
            if key not in self._create_locks:
                self._create_locks[key] = asyncio.Lock()
            create_lock = self._create_locks[key]

        async with create_lock:
            # Double-check
            key, doomed = self.plan(env_path, julia_cmd, name)
            for doomed_key in doomed:
                await self._sessions[doomed_key].kill()
                self._forget(doomed_key)

            existing = self._sessions.get(key)
            if existing is not None and existing.is_alive():
                return self._adopt(existing, key, name)

            # Clean up dead session
            if existing is not None:
                await existing.kill()
                self._forget(key)

            # Create new session
            sentinel = f"__JULIA_MCP_{uuid.uuid4().hex}__"
            is_temp = key[0] == TEMP_SESSION_KEY
            if is_temp:
                env_dir = tempfile.mkdtemp(prefix="julia-mcp-")
                is_test = False
            else:
                env_dir = key[0]
                is_test = Path(env_dir).name == "test"

            session = JuliaSession(
                env_dir, sentinel, is_temp=is_temp, is_test=is_test,
                julia_args=self.julia_args,
                julia_cmd=key[1],
                name=name,
                log_file=self._get_log_file(key),
            )
            await session.start()
            self._sessions[key] = session
            if name is not None:
                self._names[name] = key
            return session

    async def restart(
        self,
        env_path: str | None = None,
        julia_cmd: str | None = None,
        name: str | None = None,
    ) -> bool:
        """Kill the addressed session, returning True if one was found."""
        key = self.resolve_key(env_path, julia_cmd, name)
        if key in self._sessions:
            await self._sessions[key].kill()
            self._forget(key)
            return True
        return False

    def list_sessions(self) -> list[dict]:
        result = []
        for key, session in self._sessions.items():
            info = {
                "env_path": session.env_dir,
                "alive": session.is_alive(),
                "temp": session.is_temp,
            }
            if session.name is not None:
                info["name"] = session.name
            if session.julia_cmd is not None:
                info["julia_cmd"] = session.julia_cmd
            if session.cmd is not None:
                info["cmd"] = shlex.join(session.cmd)
            if key in self._log_files:
                info["log_file"] = self._log_files[key].name
            result.append(info)
        return result

    async def shutdown(self) -> None:
        for session in self._sessions.values():
            await session.kill()
        self._sessions.clear()
        self._names.clear()
        self._cleanup_logs()


manager = SessionManager()


@mcp.tool()
async def julia_eval(
    code: str,
    env_path: str | None = None,
    timeout: float | None = None,
    julia_cmd: str | None = None,
    name: str | None = None,
) -> str:
    """ALWAYS use this tool to run Julia code. NEVER run julia via command line.

    Persistent REPL session with state preserved between calls.
    Each env_path gets its own session, started lazily.
    Do not type `Pkg.activate()` explicitly in your code; instead, specify the env_path argument to select the environment.

    Args:
        code: Julia code to evaluate. Use display(...)/println(...) to see output.
        env_path: Julia project directory path. Omit for a temporary environment.
        timeout: Seconds (default: 60). Auto-disabled for Pkg operations.
        julia_cmd: Custom Julia command, should be used rarely, only when explicitly requested. Examples: "julia +1.11", "julia --check-bounds=yes", "/path/to/julia".
        name: Short label for the session, to avoid repeating env_path and julia_cmd.
            Pass it with env_path (and julia_cmd) once to name the session, then pass
            name alone on later calls. A named session is never restarted implicitly,
            so naming lets several sessions share one env_path, e.g. the same project
            under two Julia versions.
    """
    if timeout is None:
        effective_timeout: float | None = (
            None if PKG_PATTERN.search(code) else DEFAULT_TIMEOUT
        )
    else:
        effective_timeout = timeout if timeout > 0 else None

    try:
        session = await manager.get_or_create(env_path, julia_cmd=julia_cmd, name=name)
        output = await session.execute(code, timeout=effective_timeout)
        return output if output else "(no output)"
    except ValueError as e:
        return f"Error: {e}"
    except RuntimeError as e:
        # Clean up dead sessions so the next call starts fresh
        for key, session in list(manager._sessions.items()):
            if not session.is_alive():
                manager._forget(key)
        return f"Error: {e}"


@mcp.tool()
async def julia_restart(
    env_path: str | None = None,
    julia_cmd: str | None = None,
    name: str | None = None,
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
        julia_cmd: Same julia_cmd that was used to start the session, if any.
        name: Name of the session to restart, instead of env_path and julia_cmd.
    """
    try:
        killed = await manager.restart(env_path, julia_cmd=julia_cmd, name=name)
    except ValueError as e:
        return f"Error: {e}"
    label = name if name is not None else (env_path if env_path is not None else "temporary")
    if killed:
        return f"Session restarted ({label}). A fresh session will start on next julia_eval call."
    active = [s["env_path"] for s in manager.list_sessions()]
    if active:
        return (
            f"No active session for {label} — nothing to restart. "
            f"Active sessions: {active}"
        )
    return f"No active session for {label} — nothing to restart."


@mcp.tool()
async def julia_list_sessions() -> str:
    """List all active Julia sessions, their names, environments and Julia commands."""
    header = (
        "Julia args applied to every session: "
        f"{shlex.join(manager.julia_args) if manager.julia_args else '(none)'}"
    )
    sessions = manager.list_sessions()
    if not sessions:
        return f"No active Julia sessions.\n{header}"
    lines = []
    for s in sessions:
        status = "alive" if s["alive"] else "dead"
        label = f"{s['env_path']} (temp)" if s["temp"] else s["env_path"]
        name = f"[{s['name']}] " if "name" in s else ""
        julia = f" julia_cmd={s['julia_cmd']}" if "julia_cmd" in s else ""
        full = f"\n      cmd: {s['cmd']}" if "cmd" in s else ""
        log = f" log={s['log_file']}" if "log_file" in s else ""
        lines.append(f"  {name}{label}: {status}{julia}{log}{full}")
    return "Active Julia sessions:\n" + "\n".join(lines) + f"\n{header}"


def main():
    global manager
    julia_args = tuple(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_JULIA_ARGS
    manager = SessionManager(julia_args=julia_args)
    print(f"Julia MCP log directory: {manager._log_dir}", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
