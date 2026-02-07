# julia-mcp

MCP server that gives AI assistants access to efficient Julia code execution. Avoids Julia's startup and compilation costs by keeping sessions alive across calls, and persists state (variables, functions, loaded packages) between them — so each iteration is fast.

- Sessions start on demand, persist state between calls, and recover from crashes — no manual management
- Each project directory gets its own isolated Julia process
- Pure stdio transport — no open ports or sockets


## Tools

- **julia_eval(code, env_path?, timeout?)** — execute Julia code in a persistent session. `env_path` sets the Julia project directory (omit for a temporary session). `timeout` defaults to 60s and is auto-disabled for `Pkg` operations.
- **julia_restart(env_path?)** — restart a session, clearing all state. If `env_path` is omitted, restarts the temporary session.
- **julia_list_sessions** — list active sessions and their status

## Requirements

- [uv](https://docs.astral.sh/uv/) (you might already have it installed)
- Julia – any version, `julia` binary must be in `PATH`
  - Recommended packages – used automatically if available in the global environment:
  - [Revise.jl](https://github.com/timholy/Revise.jl) - to pick code changes up without restarting
  - [TestEnv.jl](https://github.com/JuliaTesting/TestEnv.jl) — to properly activate test environment when `env_path` points to `/test/`

The server itself is written in Python since the Python MCP protocol implementation is very mature.


# Usage

First, clone the repository:

```bash
cd /any_directory
git clone https://github.com/aplavin/julia-mcp.git
```
Then register the server with your client of choice (see below).

That's it! Your AI assistant can now execute Julia code more efficiently, saving of TTFX.

### Claude Code

User-wide (recommended — makes Julia available in all projects):

```bash
claude mcp add --scope user julia -- uv run --directory /any_directory/julia-mcp python server.py
```

Project-scoped (only available in the current project):

```bash
claude mcp add --scope project julia -- uv run --directory /any_directory/julia-mcp python server.py
```

<details>
<summary>Custom Julia CLI arguments</summary>

Append Julia flags after `server.py` to override the defaults (`--startup-file=no --threads=auto`):

```bash
claude mcp add --scope user julia -- uv run --directory /any_directory/julia-mcp python server.py --threads=1 --startup-file=yes
```
</details>

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "julia": {
      "command": "uv",
      "args": ["run", "--directory", "/any_directory/julia-mcp", "python", "server.py"]
    }
  }
}
```

<details>
<summary>Custom Julia CLI arguments</summary>

Append Julia flags after `server.py` to override the defaults (`--startup-file=no --threads=auto`):

```json
{
  "mcpServers": {
    "julia": {
      "command": "uv",
      "args": ["run", "--directory", "/any_directory/julia-mcp", "python", "server.py", "--threads=1", "--startup-file=yes"]
    }
  }
}
```
</details>

### Codex CLI

Add the follwing to `~/.codex/config.toml` (user-wide) or to the project-scoped `.codex/config.toml`:
```toml
[mcp_servers.juliaMPC]
command = "uv"
args = ["run", "--directory", "/Users/scheidan/MCP/julia-mcp", "python", "server.py"]
startup_timeout_sec = 30 # default 10 sec
tool_timeout_sec = 600   # default 60 sec
```
This timeouts here define the maximum duration that Codex is waiting. You can call the server with a shorter but not longer durations.

<details>
<summary>Custom Julia CLI arguments</summary>

Append Julia flags after `server.py` to override the defaults (`--startup-file=no --threads=auto`):

```
codex mcp add julia -- uv run --directory /any_directory/julia-mcp server.py --threads=1 --startup-file=yes
```
</details>

### VS Code Copilot

Add to `.vscode/settings.json`:

```json
{
  "mcp": {
    "servers": {
      "julia": {
        "command": "uv",
        "args": ["run", "--directory", "/path/to/julia-mcp", "python", "server.py"]
      }
    }
  }
}
```

<details>
<summary>Custom Julia CLI arguments</summary>

Append Julia flags after `server.py` to override the defaults (`--startup-file=no --threads=auto`):

```json
{
  "mcp": {
    "servers": {
      "julia": {
        "command": "uv",
        "args": ["run", "--directory", "/path/to/julia-mcp", "python", "server.py", "--threads=1", "--startup-file=yes"]
      }
    }
  }
}
```
</details>

## Details

- Each unique `env_path` gets its own isolated Julia session. Omitting `env_path` uses a temporary session that is cleaned up on MCP shutdown.
- If `env_path` ends in `/test/`, the parent directory is used as the project and `TestEnv` is activated automatically. For this to work, `TestEnv` must be installed in the base environment.
- Julia is launched with `--threads=auto` and `--startup-file=no` by default. Pass custom Julia CLI flags after `server.py` to override these defaults entirely.


## Alternatives

Other projects that give AI agents access to Julia:

- [MCPRepl.jl](https://github.com/hexaeder/MCPRepl.jl) and [REPLicant.jl](https://github.com/MichaelHatherly/REPLicant.jl) require you to manually start and manage Julia sessions. `julia-mcp` handles this automatically.
- [DaemonConductor.jl](https://github.com/tecosaur/DaemonConductor.jl) (linux only) runs Julia scripts, but calls are independent and don't share variables. `julia-mcp` retains state between calls.
