# CLAUDE.md

## Project

MCP server exposing iTerm2 control via the iTerm2 Python API. Single-module server (`src/iterm2_mcp/server.py`) using `FastMCP`.

## Commands

```bash
uv sync                    # Install dependencies
uv run ruff check          # Lint
uv run ruff format         # Format
uv run ty check            # Type check
uv run iterm2-mcp          # Run the server locally
```

## Code style

- **Ruff** for linting and formatting: line length 100, rules `E`, `F`, `I`, `UP`, `B`
- **ty** for type checking, targeting Python 3.12
- Type hints on all function signatures
- Sphinx-style `:param` docstrings — document the "why", not the "what"
- Comments above the code they describe, not at end of line
- Imports: stdlib first, then third-party, then local

## Architecture

- All tools are async functions decorated with `@mcp.tool()` in `server.py`
- A single `iterm2.Connection` is created lazily and shared via `_connection_singleton()`
- `_session(session_id)` resolves a session by ID or falls back to the active session — most tools accept an optional `session_id`
- `run_command` uses `iterm2.PromptMonitor` for real completion detection (requires shell integration)
