# julia-mcp

MCP server providing persistent Julia REPL sessions. State is preserved between calls, sessions are created lazily per project directory, and dead sessions are automatically recreated.

## Tools

- **julia_eval** — execute Julia code in a persistent session
- **julia_restart** — restart a session, clearing all state
- **julia_list_sessions** — list active sessions

## Requirements

- Python ≥ 3.10
- Julia (must be in `PATH`)

## Installation

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "julia": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/julia-mcp", "python", "server.py"]
    }
  }
}
```

### Claude Code

```bash
claude mcp add julia -- uv run --directory /path/to/julia-mcp python server.py
```

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
