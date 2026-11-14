"""
This module implements an MCP server that exposes iTerm2 control to Claude.

All tools dispatch through the iTerm2 Python API
(https://iterm2.com/python-api/), which communicates with a running iTerm2
instance over a local WebSocket. A single ``iterm2.Connection`` is created
lazily on the first tool invocation and shared for the lifetime of the server.

Tool handlers are thin wrappers around ``iterm2.App`` and ``iterm2.Session``
methods. Most tools accept an optional ``session_id`` so Claude can target a
specific window/tab/pane; when omitted, the currently focused session is used.
"""

from __future__ import annotations

import asyncio

import iterm2
import iterm2.app
from mcp.server.mcpserver import MCPServer

# Workaround for an upstream recursion in the iterm2 library (reproduced on
# 2.15 through 2.20) when bootstrapping the App model against recent iTerm2
# builds. async_construct -> async_refresh_focus -> _async_focus_change reaches
# a `selected_tab` focus notification whose window isn't in the model yet, calls
# async_refresh -> async_refresh_focus, and loops with no base case until the
# stack overflows ("maximum recursion depth exceeded") — which takes down every
# tool, since they all reach App via async_get_app. Guarding async_refresh_focus
# against re-entry lets the first refresh populate the window list and settle.
_orig_async_refresh_focus = iterm2.app.App.async_refresh_focus


async def _guarded_async_refresh_focus(self) -> None:
    if getattr(self, "_reentrancy_guard", False):
        return
    self._reentrancy_guard = True
    try:
        await _orig_async_refresh_focus(self)
    finally:
        self._reentrancy_guard = False


iterm2.app.App.async_refresh_focus = _guarded_async_refresh_focus

mcp = MCPServer("iterm2-mcp")

_connection: iterm2.Connection | None = None
_conn_lock = asyncio.Lock()


async def _connection_singleton() -> iterm2.Connection:
    """Return the shared iTerm2 connection, creating it on first use."""
    global _connection
    async with _conn_lock:
        if _connection is None:
            _connection = await iterm2.Connection.async_create()
        return _connection


async def _app() -> iterm2.App:
    """Return the iTerm2 ``App`` handle, with a helpful error if unavailable."""
    conn = await _connection_singleton()
    app = await iterm2.async_get_app(conn)
    if app is None:
        raise RuntimeError(
            "iTerm2 returned no application object. Is iTerm2 running with "
            "Preferences → General → Magic → 'Enable Python API' enabled?"
        )
    return app


async def _session(session_id: str | None) -> iterm2.Session:
    """Resolve a session by ID, falling back to the currently active session.

    :param session_id: A specific session UUID to target, or ``None`` to use the
        currently focused window/tab/pane.
    """
    app = await _app()
    if session_id:
        sess = app.get_session_by_id(session_id)
        if sess is None:
            raise ValueError(f"No session found with ID {session_id!r}")
        return sess
    win = app.current_terminal_window
    if win is None:
        raise RuntimeError("No active iTerm2 window.")
    tab = win.current_tab
    if tab is None:
        raise RuntimeError("No active tab in the current window.")
    sess = tab.current_session
    if sess is None:
        raise RuntimeError("No active session in the current tab.")
    return sess


def _screen_text(contents: iterm2.ScreenContents) -> str:
    """Flatten a ``ScreenContents`` into plain text, stripping trailing blank lines."""
    lines = [contents.line(i).string for i in range(contents.number_of_lines)]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


@mcp.tool()
async def list_sessions() -> str:
    """List all iTerm2 windows, tabs, and sessions.

    Returns a tree with session IDs that can be passed to other tools. The
    ``*`` marker shows the currently focused window, tab, and session.
    """
    app = await _app()
    out: list[str] = []
    for win in app.terminal_windows:
        is_active_win = win is app.current_terminal_window
        out.append(f"Window {win.window_id}{' *' if is_active_win else ''}")
        for tab in win.tabs:
            is_active_tab = is_active_win and tab is win.current_tab
            out.append(f"  Tab {tab.tab_id}{' *' if is_active_tab else ''}")
            for sess in tab.sessions:
                is_active_sess = is_active_tab and sess is tab.current_session
                name = sess.name or ""
                out.append(
                    f"    Session {sess.session_id}{' *' if is_active_sess else ''}  [{name}]"
                )
    return "\n".join(out) if out else "(no iTerm2 windows open)"


@mcp.tool()
async def get_active_session() -> str:
    """Return the ID and name of the currently focused iTerm2 session."""
    sess = await _session(None)
    return f"{sess.session_id}  {sess.name or ''}".rstrip()


@mcp.tool()
async def focus_session(session_id: str) -> str:
    """Bring a session to the foreground.

    :param session_id: The session UUID to activate. Its window is raised and
        its tab/pane is selected.
    """
    sess = await _session(session_id)
    await sess.async_activate(select_tab=True, order_window_front=True)
    return f"Activated session {session_id}"


@mcp.tool()
async def write_to_terminal(
    text: str,
    session_id: str | None = None,
    add_newline: bool = True,
) -> str:
    """Send text to an iTerm2 session.

    :param text: The characters to send.
    :param session_id: Target session UUID. Defaults to the active session.
    :param add_newline: If True (the default), appends ``\\n`` so commands
        execute. Set to False to type without submitting, e.g. when filling an
        interactive prompt.
    """
    sess = await _session(session_id)
    payload = text + "\n" if add_newline else text
    await sess.async_send_text(payload, suppress_broadcast=True)
    return f"Sent {len(payload)} characters to session {sess.session_id}"


@mcp.tool()
async def send_control_character(
    letter: str,
    session_id: str | None = None,
) -> str:
    """Send a control character to an iTerm2 session.

    Supported values: any single letter ``A``-``Z`` (Ctrl-A through Ctrl-Z),
    ``ESC``/``ESCAPE`` for Escape (0x1B), and ``]`` for the telnet escape
    (0x1D).

    :param letter: The control key name; see above for accepted forms.
    :param session_id: Target session UUID. Defaults to the active session.
    """
    _SPECIAL_KEYS = {"ESC": 0x1B, "ESCAPE": 0x1B, "]": 0x1D}
    key = letter.strip().upper()
    if key in _SPECIAL_KEYS:
        code = _SPECIAL_KEYS[key]
    elif len(key) == 1 and "A" <= key <= "Z":
        code = ord(key) - ord("A") + 1
    else:
        raise ValueError(f"Unrecognized control key {letter!r}")
    sess = await _session(session_id)
    await sess.async_send_text(chr(code), suppress_broadcast=True)
    return f"Sent Ctrl-{key} to session {sess.session_id}"


@mcp.tool()
async def send_escape_sequence(
    sequence: str,
    session_id: str | None = None,
) -> str:
    """Send a raw escape sequence to an iTerm2 session.

    Python string escapes in ``sequence`` are interpreted before sending, so
    values like ``\\x1b[2J`` (clear screen) or ``\\x1b[H`` (cursor home) work
    as expected.

    :param sequence: The escape sequence string.
    :param session_id: Target session UUID. Defaults to the active session.
    """
    # Round-trip through latin-1 so Python interprets escape sequences like \x1b.
    # This only works for characters in the latin-1 range (0x00-0xFF).
    cooked = sequence.encode("latin-1").decode("unicode_escape")
    sess = await _session(session_id)
    await sess.async_send_text(cooked, suppress_broadcast=True)
    return f"Sent {len(cooked)} bytes to session {sess.session_id}"


@mcp.tool()
async def read_screen(
    session_id: str | None = None,
    max_lines: int | None = None,
) -> str:
    """Read the visible contents of an iTerm2 session as plain text.

    ANSI escape codes are stripped. Trailing blank lines are trimmed.

    :param session_id: Target session UUID. Defaults to the active session.
    :param max_lines: If set, return only the last N lines.
    """
    sess = await _session(session_id)
    contents = await sess.async_get_screen_contents()
    text = _screen_text(contents)
    if max_lines is not None and max_lines > 0:
        text = "\n".join(text.splitlines()[-max_lines:])
    return text or "(screen is empty)"


@mcp.tool()
async def get_cursor_position(session_id: str | None = None) -> str:
    """Return the cursor coordinates ``x``, ``y`` within the visible screen.

    :param session_id: Target session UUID. Defaults to the active session.
    """
    sess = await _session(session_id)
    contents = await sess.async_get_screen_contents()
    coord = contents.cursor_coord
    return f"x={coord.x} y={coord.y}"


@mcp.tool()
async def run_command(
    command: str,
    session_id: str | None = None,
    timeout_seconds: float = 30.0,
) -> str:
    """Run a command and wait for it to finish, returning the resulting screen.

    Completion is detected via iTerm2's ``PromptMonitor`` ``COMMAND_END``
    notification, which requires iTerm2 shell integration to be installed in
    the target shell. If the prompt-end event does not arrive within
    ``timeout_seconds``, the current screen is returned with a ``[timeout]``
    marker.

    :param command: The command line to run.
    :param session_id: Target session UUID. Defaults to the active session.
    :param timeout_seconds: How long to wait for ``COMMAND_END`` before
        giving up.
    """
    sess = await _session(session_id)
    conn = await _connection_singleton()

    try:
        async with iterm2.PromptMonitor(
            conn,
            sess.session_id,
            modes=[iterm2.PromptMonitor.Mode.COMMAND_END],
        ) as mon:
            # Subscribe before sending the command to avoid missing fast completions.
            await sess.async_send_text(command + "\n", suppress_broadcast=True)

            try:
                mode, info = await asyncio.wait_for(mon.async_get(), timeout=timeout_seconds)
                if mode == iterm2.PromptMonitor.Mode.COMMAND_END:
                    # For COMMAND_END, ``info`` is the exit-status int directly;
                    # see PromptMonitor.async_get docstring in the iterm2 package.
                    reason = f"prompt-end (exit {info})"
                else:
                    reason = f"unexpected monitor mode {mode}"
            except (TimeoutError, asyncio.TimeoutError):
                reason = f"timeout after {timeout_seconds}s"
    except (TimeoutError, asyncio.TimeoutError):
        # Catches timeouts from PromptMonitor setup itself (distinct from the
        # inner wait_for timeout which covers the command execution).
        reason = f"timeout after {timeout_seconds}s"
    except Exception as exc:  # noqa: BLE001 — surface as reason, don't crash the tool
        reason = f"prompt-monitor error ({exc!r}); is shell integration installed?"

    contents = await sess.async_get_screen_contents()
    return f"[{reason}]\n{_screen_text(contents)}"


@mcp.tool()
async def create_window(
    command: str | None = None,
    profile: str | None = None,
) -> str:
    """Create a new iTerm2 window.

    :param command: Optional shell command to run in the new session.
    :param profile: Name of the iTerm2 profile to use. Defaults to the default
        profile.
    """
    conn = await _connection_singleton()
    win = await iterm2.Window.async_create(conn, profile=profile, command=command)
    if win is None:
        return "Error: failed to create window."
    sess = win.current_tab.current_session if win.current_tab else None
    suffix = f", session {sess.session_id}" if sess else ""
    return f"Created window {win.window_id}{suffix}"


@mcp.tool()
async def create_tab(
    window_id: str | None = None,
    command: str | None = None,
    profile: str | None = None,
) -> str:
    """Create a new tab in a window.

    :param window_id: Target window ID. Defaults to the current window.
    :param command: Optional shell command to run in the new tab's session.
    :param profile: Name of the iTerm2 profile to use.
    """
    app = await _app()
    if window_id:
        win = next((w for w in app.terminal_windows if w.window_id == window_id), None)
        if win is None:
            return f"Error: no window with ID {window_id!r}"
    else:
        win = app.current_terminal_window
        if win is None:
            return "Error: no active iTerm2 window."
    tab = await win.async_create_tab(profile=profile, command=command)
    if tab is None:
        return "Error: failed to create tab."
    sess = tab.current_session
    return f"Created tab {tab.tab_id}, session {sess.session_id if sess else '?'}"


@mcp.tool()
async def split_pane(
    session_id: str | None = None,
    vertical: bool = True,
    profile: str | None = None,
    command: str | None = None,
) -> str:
    """Split an iTerm2 pane horizontally or vertically.

    :param session_id: Session UUID to split. Defaults to the active session.
    :param vertical: If True, split left/right; if False, split top/bottom.
    :param profile: Name of the iTerm2 profile to use for the new pane.
    :param command: Optional command to run in the new pane after the split.
    """
    sess = await _session(session_id)
    new_sess = await sess.async_split_pane(vertical=vertical, profile=profile)
    if new_sess is None:
        return "Error: failed to split pane."
    if command:
        await new_sess.async_send_text(command + "\n", suppress_broadcast=True)
    return f"Split created session {new_sess.session_id}"


@mcp.tool()
async def close_session(session_id: str, force: bool = False) -> str:
    """Close a specific session.

    :param session_id: The session UUID to close.
    :param force: If True, skip iTerm2's "running process" confirmation prompt.
    """
    sess = await _session(session_id)
    await sess.async_close(force=force)
    return f"Closed session {session_id}"


@mcp.tool()
async def set_session_name(name: str, session_id: str | None = None) -> str:
    """Set the title/name of an iTerm2 session.

    :param name: The new session name.
    :param session_id: Target session UUID. Defaults to the active session.
    """
    sess = await _session(session_id)
    await sess.async_set_name(name)
    return f"Renamed session {sess.session_id} to {name!r}"


@mcp.tool()
async def set_badge(badge: str, session_id: str | None = None) -> str:
    """Set the iTerm2 session badge (translucent overlay text).

    The profile's Badge field must reference ``\\(user.badge)`` for the value
    to show up; this is the default in stock profiles.

    :param badge: Text to display as the badge.
    :param session_id: Target session UUID. Defaults to the active session.
    """
    sess = await _session(session_id)
    await sess.async_set_variable("user.badge", badge)
    return f"Set badge on session {sess.session_id} to {badge!r}"


@mcp.tool()
async def clear_buffer(session_id: str | None = None) -> str:
    """Clear both the visible screen and the scrollback buffer.

    Uses the iTerm2-proprietary ``OSC 1337 ; ClearScrollback`` control sequence
    (https://iterm2.com/documentation-escape-codes.html).

    :param session_id: Target session UUID. Defaults to the active session.
    """
    sess = await _session(session_id)
    await sess.async_send_text("\x1b]1337;ClearScrollback\x07", suppress_broadcast=True)
    return f"Cleared buffer of session {sess.session_id}"


@mcp.tool()
async def list_profiles() -> str:
    """List all iTerm2 profiles with their names and GUIDs."""
    conn = await _connection_singleton()
    profiles = await iterm2.PartialProfile.async_query(conn)
    lines = [f"{p.name}  [{p.guid}]" for p in profiles]
    return "\n".join(lines) if lines else "(no profiles)"


@mcp.tool()
async def get_variable(name: str, session_id: str | None = None) -> str:
    """Read an iTerm2 session variable.

    Commonly useful names: ``jobName``, ``jobPid``, ``path`` (requires shell
    integration), ``username``, ``hostname``, ``rows``, ``columns``,
    ``autoName``. Prefix with ``user.`` for variables set via iTerm2 custom
    control sequences.

    :param name: The variable name to read.
    :param session_id: Target session UUID. Defaults to the active session.
    """
    sess = await _session(session_id)
    value = await sess.async_get_variable(name)
    return f"{name}={value!r}"


def main() -> None:
    """Entry point for the ``iterm2-mcp`` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
