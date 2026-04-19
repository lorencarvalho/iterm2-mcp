# iterm2-mcp

An MCP server that provides full control over iTerm2.

## Prerequisites

1. iTerm2 running on macOS.
2. **Preferences > General > Magic > "Enable Python API"** must be checked.
3. Install iTerm2 [shell integration](https://iterm2.com/documentation-shell-integration.html)
   in your shell. `run_command` and session variables like `path`/`jobName` depend on it.
   Without it, `run_command` falls back to its timeout.

The first time the server connects, iTerm2 will prompt you to approve the binary. Approve once; subsequent launches are automatic.

## Install

```bash
uv tool install iterm2-mcp
```

Or from source:

```bash
git clone https://github.com/lorencarvalho/iterm2-mcp.git
cd iterm2-mcp
uv sync
```

## Register with Claude

**Claude Code:**

```bash
claude mcp add iterm2 -- uvx iterm2-mcp
```

**Claude Desktop** — add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "iterm2": {
      "command": "uvx",
      "args": ["iterm2-mcp"]
    }
  }
}
```

## Security

This server can type into terminals, run commands, and close sessions — anything
you can do in iTerm2. Only connect it to MCP clients you trust. It has no
sandboxing beyond what iTerm2 itself provides.

## Tools

Most tools accept an optional `session_id` — omit it to target the currently active
session.

| Tool | Purpose |
| --- | --- |
| `list_sessions` | Tree of windows/tabs/sessions with IDs |
| `get_active_session` | ID and name of the focused session |
| `focus_session` | Bring a session to the foreground |
| `write_to_terminal` | Send text (optionally with newline) |
| `send_control_character` | Send Ctrl-C, Ctrl-D, Ctrl-Z, ESC, etc. |
| `send_escape_sequence` | Send a raw ANSI escape (e.g. `\x1b[2J`) |
| `read_screen` | Read the visible screen as plain text |
| `get_cursor_position` | Current cursor `(x, y)` |
| `run_command` | Send a command and wait for `COMMAND_END` |
| `create_window` | Open a new iTerm2 window |
| `create_tab` | Open a new tab |
| `split_pane` | Split a pane horizontally or vertically |
| `close_session` | Close a specific session |
| `set_session_name` | Rename a session |
| `set_badge` | Set the iTerm2 badge text |
| `clear_buffer` | Clear screen and scrollback |
| `list_profiles` | Enumerate iTerm2 profiles |
| `get_variable` | Read an iTerm2 session variable |

## Development

```bash
uv sync
uv run ruff check
uv run ruff format
uv run ty check
```

## License

MIT
