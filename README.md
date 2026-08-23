# julia-mcp

MCP server that gives AI assistants access to efficient Julia code execution. Avoids Julia's startup and compilation costs by keeping sessions alive across calls, and persists state (variables, functions, loaded packages) between them — so each iteration is fast.

- Sessions start on demand, persist state between calls, and recover from crashes — no manual management
- Each project directory gets its own isolated Julia process
- Pure stdio transport — no open ports or sockets


## Tools

- **julia_eval(code, env_path?, timeout?, julia_cmd?, name?)** — execute Julia code in a persistent session. `env_path` sets the Julia project directory (omit for a temporary session). `timeout` defaults to 60s and is auto-disabled for `Pkg` operations. `julia_cmd` picks a different Julia binary or extra flags, e.g. `julia +1.11`. `name` labels the session so later calls can pass `name` alone instead of repeating `env_path` and `julia_cmd`.
- **julia_restart(env_path?, julia_cmd?, name?)** — restart a session, clearing all state. If all are omitted, restarts the temporary session.
- **julia_list_sessions** — list active sessions with their names, environments, full Julia command lines and status

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

Append Julia flags after `server.py` to override the default (`--threads=auto`):

```bash
claude mcp add --scope user julia -- uv run --directory /any_directory/julia-mcp python server.py --threads=1 --startup-file=no
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

Append Julia flags after `server.py` to override the default (`--threads=auto`):

```json
{
  "mcpServers": {
    "julia": {
      "command": "uv",
      "args": ["run", "--directory", "/any_directory/julia-mcp", "python", "server.py", "--threads=1", "--startup-file=no"]
    }
  }
}
```
</details>

### Codex CLI

User-wide — makes Julia available in all projects: 
```
codex mcp add julia -- uv run --directory /any_directory/julia-mcp server.py
```

<details>
<summary>Custom Julia CLI arguments</summary>

Append Julia flags after `server.py` to override the default (`--threads=auto`):

```
codex mcp add julia -- uv run --directory /any_directory/julia-mcp server.py --threads=1 --startup-file=no
```
</details>

### VS Code Copilot

Add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "julia": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/julia-mcp", "python", "server.py"]
    }
  }
}
```

<details>
<summary>Custom Julia CLI arguments</summary>

Append Julia flags after `server.py` to override the default (`--threads=auto`):

```json
{
  "servers": {
    "julia": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/julia-mcp", "python", "server.py", "--threads=1", "--startup-file=no"]
    }
  }
}
```
</details>

### GitHub Copilot CLI

Edit `$HOME/.copilot/mcp-config.json`, and enter

```json
{
  "mcpServers": {
    "julia": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/julia-mcp", "python", "-u", "server.py"]
    }
  }
}
```
Beware that on Windows, `\` must be escaped (so write as `C:\\my_folder\\...`)

### GitHub Copilot Cloud Agent

To enable the MCP for a single repo, go to Settings, then scroll down the left panel until you get to Copilot, open that dropdown and select Cloud agent. Then scroll down to the section Model Context Protocol (MCP) and add the following

```json
{
  "mcpServers": {
    "julia": {
      "type": "local",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/aplavin/julia-mcp",
        "julia-mcp"
      ],
      "tools": ["*"]
    }
  }
}
```

## Details

- Each `env_path` gets its own isolated Julia session, started lazily. Calling with a different `julia_cmd` restarts it, unless the session has a name (see below). Omitting `env_path` uses a temporary session that is cleaned up on MCP shutdown.
- Sessions on the same `env_path` share its `Project.toml` and `Manifest.toml`, so a `Pkg` operation in one re-resolves the manifest the other is using. To keep two Julia versions apart, create a [version-specific manifest](https://pkgdocs.julialang.org/v1/toml-files/#Manifest.toml) per version (`touch Manifest-v1.11.toml`, supported by Julia 1.11 and newer, and by recent 1.10 patch releases), which Pkg then writes to instead of `Manifest.toml`.
- A session can be given a `name` (which requires `env_path`), after which `name` alone addresses it. Naming a session that is already running adopts it, keeping its state, as long as the settings passed along match the ones it was started with. Named sessions are never restarted implicitly: a call that would replace one has to name it, so several Julia versions can run on one project at the same time. Passing a name together with different settings restarts that session with them, while a name that is still free starts another session alongside. `julia_list_sessions` shows the mapping.
- If `env_path` ends in `/test/`, the parent directory is used as the project and `TestEnv` is activated automatically. For this to work, `TestEnv` must be installed in the base environment.
- Julia is launched with `--threads=auto` by default. Pass custom Julia CLI flags after `server.py` to override this default entirely.


## Alternatives

Other projects that give AI agents access to Julia:

- [MCPRepl.jl](https://github.com/hexaeder/MCPRepl.jl) and [REPLicant.jl](https://github.com/MichaelHatherly/REPLicant.jl) require you to manually start and manage Julia sessions. `julia-mcp` handles this automatically.
- [DaemonConductor.jl](https://github.com/tecosaur/DaemonConductor.jl) (linux only) runs Julia scripts, but calls are independent and don't share variables. `julia-mcp` retains state between calls.
