# -*- coding: utf-8 -*-
"""
scroll TUI — irssi-inspired IRC interface built on window.py.

Layout (top → bottom):
  ┌─────────────────────────────────────────────────────┐
  │  topic bar  (1 row, green on black)                 │
  ├─────────────────────────────────────────────────────┤
  │                                         │           │
  │  message area  (EXPAND)                 │  nicklist │
  │                                         │  (EXPAND) │
  ├─────────────────────────────────────────────────────┤
  │  status bar  (1 row, white on blue)                 │
  ├─────────────────────────────────────────────────────┤
  │  input bar  (1 row)                                 │
  └─────────────────────────────────────────────────────┘
"""
import _curses
import time

from .window import (
    Window, Pane, palette,
    EXPAND, FIT,
    ALIGN_LEFT, ALIGN_RIGHT, ALIGN_CENTER,
    display_width, truncate_to_display_width, skip_display_cols,
)

# ── colour constants (initialised lazily on first use) ──────────────────────
#   irssi defaults:
#     topic bar   : white text, blue bg
#     status bar  : white text, blue bg
#     message area: terminal default
#     nick list   : terminal default

NICK_COLOURS = [
    "cyan", "green", "yellow", "magenta", "red", "white",
]

_PREFIX_ORDER = {c: i for i, c in enumerate("~&@%+")}


def nick_colour(nick):
    idx = sum(ord(c) for c in nick) % len(NICK_COLOURS)
    return NICK_COLOURS[idx]


def sort_nicks(nicks):
    """Sort nick list: ~ & @ % + then plain, alphabetically within each group."""
    def _key(n):
        prefix = n[0] if n and n[0] in _PREFIX_ORDER else ""
        return (_PREFIX_ORDER.get(prefix, len(_PREFIX_ORDER)), n.lstrip("~&@%+").lower())
    return sorted(nicks, key=_key)


def timestamp():
    return time.strftime("%H:%M:%S")


# ── Buffer ───────────────────────────────────────────────────────────────────

class Buffer:
    """Holds lines for one IRC context (server or channel/query)."""

    def __init__(self, name):
        self.name       = name      # e.g. "irc.rizon.net" or "#anime"
        self.lines      = []        # list of (timestamp_str, nick, text, attrs)
        self.nicks      = []        # list of nick strings (channels only)
        self.topic      = ""
        self.unread     = False
        self.kind       = "chat"    # "chat" | "doc"
        self.scroll_pos = 0         # first visible line (doc buffers only)
        self.irc        = None      # IRCClient this buffer belongs to
        self.is_server  = False     # True for the per-connection status buffer

    def add(self, ts, nick, text, attrs=0):
        self.lines.append((ts, nick, text, attrs))
        self.unread = True

    def _render_all(self, width):
        """Return every display line after hard-wrapping."""
        rendered = []
        for (ts, nick, text, _attrs) in self.lines:
            if ts is None:
                line = text
            elif nick:
                line = "%s <%s> %s" % (ts, nick, text)
            else:
                line = "%s  %s" % (ts, text)
            while display_width(line) > width > 0:
                rendered.append(line[:width])
                line = "    " + line[width:]
            rendered.append(line)
        return rendered

    def render_lines(self, width, height):
        """
        Return display lines for the visible area.
        Doc buffers: scroll_pos = first visible line from top.
        Chat buffers: scroll_pos = lines scrolled back from bottom (0 = live).
        """
        rendered = self._render_all(width)
        if self.kind == "doc":
            start = max(0, min(self.scroll_pos, max(0, len(rendered) - height)))
            self.scroll_pos = start
            return rendered[start:start + height] if height > 0 else rendered[start:]
        # chat: scroll_pos is offset back from the bottom
        total = len(rendered)
        max_back = max(0, total - height)
        self.scroll_pos = max(0, min(self.scroll_pos, max_back))
        end = total - self.scroll_pos
        return rendered[max(0, end - height):end] if height > 0 else rendered


# ── Panes ────────────────────────────────────────────────────────────────────

class TopicPane(Pane):
    geometry = [EXPAND, 1]

    def __init__(self):
        super().__init__("topic")
        self.topic = ""

    def update(self):
        attrs = palette("white", "blue")
        text  = " " + self.topic
        if self.width:
            text = truncate_to_display_width(text, self.width - 1)
            text = text + " " * max(0, self.width - display_width(text))
        self.content = [[text, ALIGN_LEFT, attrs]]


class MessagePane(Pane):
    geometry = [EXPAND, EXPAND]

    def __init__(self, buf_ref, tui_ref):
        super().__init__("messages")
        self._buf = buf_ref
        self._tui = tui_ref

    def update(self):
        buf = self._buf()
        if buf is None or self.height is None or self.width is None:
            return
        lines = buf.render_lines(self.width, self.height)
        self.content = [["\n".join(lines), ALIGN_LEFT, 0]]

    def process_input(self, character):
        if self._tui._focus != "input":
            return
        buf = self._buf()
        if buf is None:
            return
        h     = self.height or 24
        w     = self.width  or 80
        total = len(buf._render_all(w))

        if buf.kind == "doc":
            max_pos = max(0, total - h)
            if character == 259:    # Up
                buf.scroll_pos = max(0, buf.scroll_pos - 1)
            elif character == 258:  # Down
                buf.scroll_pos = min(max_pos, buf.scroll_pos + 1)
            elif character == 339:  # PgUp
                buf.scroll_pos = max(0, buf.scroll_pos - h)
            elif character == 338:  # PgDn
                buf.scroll_pos = min(max_pos, buf.scroll_pos + h)
            elif character == 262:  # Home
                buf.scroll_pos = 0
            elif character == 360:  # End
                buf.scroll_pos = max_pos
            else:
                return
        else:
            # chat buffer: scroll_pos = lines back from bottom; 0 = live
            max_back = max(0, total - h)
            if character == 339:    # PgUp
                buf.scroll_pos = min(max_back, buf.scroll_pos + h)
            elif character == 338:  # PgDn
                buf.scroll_pos = max(0, buf.scroll_pos - h)
            elif character == 262:  # Home
                buf.scroll_pos = max_back
            elif character == 360:  # End
                buf.scroll_pos = 0
            else:
                return
        self.window.window.clear()


class SepPane(Pane):
    """1-column │ separator between the message area and the nick list."""
    geometry = [1, EXPAND]

    def __init__(self):
        super().__init__("sep")

    def update(self):
        h     = max(1, self.height or 1)
        lines = ["│"] * h
        self.content = [["\n".join(lines), ALIGN_LEFT, 0]]


class NickPane(Pane):
    NICK_WIDTH = 16
    geometry   = [NICK_WIDTH, EXPAND]

    def __init__(self, buf_ref, tui_ref):
        super().__init__("nicks")
        self._buf     = buf_ref
        self._tui     = tui_ref
        self.selected = 0
        self._scroll  = 0   # index of top visible nick

    def _clamp_scroll(self, total):
        """Keep _scroll so that selected is always visible."""
        vis = max(1, self.height or 1)
        if self.selected < self._scroll:
            self._scroll = self.selected
        elif self.selected >= self._scroll + vis:
            self._scroll = self.selected - vis + 1
        self._scroll = max(0, min(self._scroll, max(0, total - vis)))

    def update(self):
        buf = self._buf()
        if buf is None:
            return
        focused = self._tui._focus == "nicks"
        nicks   = sort_nicks(buf.nicks)
        total   = len(nicks)
        self.selected = min(self.selected, max(0, total - 1))
        self._clamp_scroll(total)
        vis     = max(1, self.height or 1)
        visible = nicks[self._scroll:self._scroll + vis]
        self.content = []
        for i, n in enumerate(visible):
            text  = truncate_to_display_width(n, self.NICK_WIDTH - 1)
            text  = text + " " * max(0, self.NICK_WIDTH - 1 - display_width(text))
            attrs = palette("black", "white") if (focused and self._scroll + i == self.selected) else 0
            self.content.append([text + "\n", ALIGN_LEFT, attrs])

    def process_input(self, character):
        if self._tui._focus != "nicks":
            return
        buf   = self._buf()
        nicks = sort_nicks(buf.nicks) if buf else []
        total = len(nicks)
        vis   = max(1, self.height or 1)

        if character == 259:       # Up — scroll list if at top of view
            self.selected = max(0, self.selected - 1)
            self._clamp_scroll(total)
            self.window.window.clear()
        elif character == 258:     # Down — scroll list if at bottom of view
            self.selected = min(max(0, total - 1), self.selected + 1)
            self._clamp_scroll(total)
            self.window.window.clear()
        elif character == 339:     # PgUp
            if self.selected > self._scroll:
                self.selected = self._scroll          # first: jump to top of view
            else:
                self.selected = max(0, self.selected - vis)
            self._clamp_scroll(total)
            self.window.window.clear()
        elif character == 338:     # PgDn
            bottom = self._scroll + vis - 1
            if self.selected < bottom and self.selected < total - 1:
                self.selected = min(total - 1, bottom)  # first: jump to bottom of view
            else:
                self.selected = min(max(0, total - 1), self.selected + vis)
            self._clamp_scroll(total)
            self.window.window.clear()
        elif character == 262:     # Home — first nick
            self.selected = 0
            self._scroll  = 0
            self.window.window.clear()
        elif character == 360:     # End — last nick
            self.selected = max(0, total - 1)
            self._clamp_scroll(total)
            self.window.window.clear()
        elif character in (10, 13):  # Enter — open submenu
            if 0 <= self.selected < total:
                nick = nicks[self.selected].lstrip("~&@%+")
                menu = self.window.get("nickmenu")
                if menu:
                    menu.open(nick)
                    self._tui.set_focus("menu")
        elif character == 9:       # Tab — return to input (flag allows one Tab back)
            self._tui._nick_tab_pending = True
            self._tui.set_focus("input")
        elif character == 27:      # ESC — return to input
            self._tui.set_focus("input")


# ── Nick context menu (overlay, drawn directly via addstr) ──────────────────

MENU_ITEMS = [
    ("Query",  "query"),
    ("Whois",  "whois"),
    ("Op",     "op"),
    ("Deop",   "deop"),
    ("Kick",   "kick"),
    ("Ban",    "ban"),
    ("Slap",   "slap"),
]
MENU_WIDTH = max(len(label) for label, _ in MENU_ITEMS) + 4   # padding


class NickMenuPane(Pane):
    """
    Floating nick-context menu.  Always hidden from the normal layout engine;
    drawn after the main cycle via ScrollTUI.draw_overlays().
    Receives keyboard input when tui._focus == "menu".
    """
    geometry = [0, 0]
    hidden   = True      # keeps it out of layout & normal rendering

    def __init__(self, nick_pane_ref, tui_ref):
        super().__init__("nickmenu")
        self._nick_pane  = nick_pane_ref
        self._tui        = tui_ref
        self.selected    = 0
        self._nick       = ""
        self._just_opened = False   # swallow the Enter that triggered open()

    def open(self, nick):
        self._nick        = nick
        self.selected     = 0
        self._just_opened = True
        self.window.window.clear()

    def draw_overlay(self):
        """Called from ScrollTUI.draw_overlays() after the main draw cycle."""
        np = self._nick_pane
        if not np or not np.coords:
            return
        win_h = self.window.height or 24

        nick_top  = np.coords[0][0][0]
        nick_left = np.coords[0][0][1]
        sel_row   = nick_top + np.selected
        menu_h    = len(MENU_ITEMS)
        menu_top  = max(0, min(sel_row, win_h - menu_h - 1))
        menu_left = max(0, nick_left - MENU_WIDTH - 1)

        for i, (label, _) in enumerate(MENU_ITEMS):
            text  = " " + label + " " * max(0, MENU_WIDTH - len(label) - 2) + " "
            attrs = palette("black", "white") if i == self.selected else palette("white", "blue")
            self.window.addstr(menu_top + i, menu_left, text, attrs)

    def process_input(self, character):
        if self._just_opened:
            self._just_opened = False
            return
        if self._tui._focus != "menu":
            return
        if character == 259:       # up
            self.selected = max(0, self.selected - 1)
            self.window.window.clear()
        elif character == 258:     # down
            self.selected = min(len(MENU_ITEMS) - 1, self.selected + 1)
            self.window.window.clear()
        elif character in (10, 13):  # Enter — execute
            self._execute(MENU_ITEMS[self.selected][1])
        elif character == 9:       # Tab — close menu, return to input (flag allows one Tab back)
            self._tui._nick_tab_pending = True
            self._tui.set_focus("input")
            self.window.window.clear()
        elif character == 27:      # ESC — close menu, return to nick list
            self._tui.set_focus("nicks")
            self.window.window.clear()

    def _execute(self, action):
        nick    = self._nick
        buf     = self._tui.current_buffer()
        channel = buf.name if buf else ""
        irc     = self._tui.irc

        if action == "query":
            b   = self._tui.get_or_add_buffer(nick)
            idx = self._tui.buffers.index(b)
            self._tui.switch_to(idx)   # switch_to resets focus to "input"

        elif action == "whois":
            if irc:
                irc.raw("WHOIS %s" % nick)
            self._tui.set_focus("input")

        elif action == "op":
            if irc and channel.startswith("#"):
                irc.raw("MODE %s +o %s" % (channel, nick))
            self._tui.set_focus("nicks")

        elif action == "deop":
            if irc and channel.startswith("#"):
                irc.raw("MODE %s -o %s" % (channel, nick))
            self._tui.set_focus("nicks")

        elif action == "kick":
            if irc and channel.startswith("#"):
                irc.raw("KICK %s %s" % (channel, nick))
            self._tui.set_focus("nicks")

        elif action == "ban":
            if irc and channel.startswith("#"):
                irc.raw("MODE %s +b %s!*@*" % (channel, nick))
            self._tui.set_focus("nicks")

        elif action == "slap":
            if irc and channel.startswith("#"):
                irc.raw("PRIVMSG %s :\x01ACTION slaps %s around a bit with a large trout.\x01" % (channel, nick))
                buf = self._tui.current_buffer()
                if buf:
                    buf.add(timestamp(), "", "* %s slaps %s around a bit with a large trout." % (irc.nick, nick))
            self._tui.set_focus("nicks")

        self.window.window.clear()


class StatusPane(Pane):
    geometry = [EXPAND, 1]

    def __init__(self, state_ref):
        super().__init__("status")
        self._state = state_ref   # callable returning ScrollTUI

    def update(self):
        state = self._state()
        attrs = palette("white", "blue")
        buf   = state.current_buffer()
        idx   = state.buf_index + 1
        total = len(state.buffers)

        # Build tab list (irssi style: [1:#channel]  [2:server] …)
        tabs = []
        for i, b in enumerate(state.buffers):
            marker = "*" if b.unread and i != state.buf_index else ""
            tabs.append("[%d:%s%s]" % (i + 1, marker, b.name))
        tab_str = " ".join(tabs)

        left  = " [%s] " % (buf.name if buf else "")
        right = " (%d/%d) " % (idx, total)
        mid   = tab_str

        w = self.width or 80
        text = left + mid
        text = truncate_to_display_width(text, w - display_width(right))
        text = text + " " * max(0, w - display_width(text) - display_width(right))
        text = text + right
        text = truncate_to_display_width(text, w)
        self.content = [[text, ALIGN_LEFT, attrs]]


class InputPane(Pane):
    geometry = [EXPAND, 1]

    def __init__(self, buf_ref, tui_ref):
        super().__init__("input")
        self.buffer      = ""
        self.cursor      = 0
        self._on_submit  = None   # set by ScrollTUI
        self._buf        = buf_ref
        self._tui        = tui_ref
        self._tab_state  = None   # completion state while cycling
        self._history    = []     # submitted lines, oldest first
        self._hist_pos   = -1     # -1 = not browsing history
        self._hist_draft = ""     # saved current input while browsing
        self._view_start = 0      # display-column offset for horizontal scroll

    def update(self):
        attrs  = 0
        buf    = self._buf()
        name   = buf.name if buf else ""
        prompt = "[%s] " % name
        w        = self.width or 80
        prompt_w = display_width(prompt)
        avail_w  = max(1, w - prompt_w)

        # Cursor position in display columns within the buffer
        buf_cursor_col = display_width(self.buffer[:self.cursor])

        # Clamp view start so the cursor stays visible
        if buf_cursor_col < self._view_start:
            self._view_start = buf_cursor_col
        elif buf_cursor_col >= self._view_start + avail_w:
            self._view_start = buf_cursor_col - avail_w + 1
        self._view_start = max(0, self._view_start)

        # Render: prompt + the visible slice of the buffer
        visible = truncate_to_display_width(
            skip_display_cols(self.buffer, self._view_start), avail_w)
        text = prompt + visible
        text = text + " " * max(0, w - display_width(text))
        self.content = [[text, ALIGN_LEFT, attrs]]

        # Claim the hardware cursor when this pane has focus
        if self._tui._focus == "input" and self.window and self.coords:
            top, left = self.coords[0][0]
            screen_col = left + prompt_w + (buf_cursor_col - self._view_start)
            self.window.cursor_pos = (top, min(screen_col, w - 1))

    def process_input(self, character):
        if self._tui._focus_at_cycle_start != "input":
            return

        if character == 9:   # Tab
            if self._tui._nick_tab_pending:
                # User just tabbed here from the nick list — send them back
                self._tui._nick_tab_pending = False
                self._tui.set_focus("nicks")
            elif not self._try_complete():
                # No completion candidate; switch to nick list (and remember it)
                self._tui._nick_tab_pending = True
                self._tui.set_focus("nicks")
            return

        # Any non-Tab key resets completion state and the nick-tab flag
        self._tab_state = None
        self._tui._nick_tab_pending = False

        # Force a full redraw on every keypress so the cursor is always
        # repositioned to the input bar by the next draw cycle.
        if self.window and self.window.window:
            self.window.window.clear()

        if character in (10, 13):           # Enter → submit
            line = self.buffer
            self.buffer = ""
            self.cursor = 0
            self._hist_pos   = -1
            self._hist_draft = ""
            if self._on_submit and line.strip():
                if not self._history or self._history[-1] != line:
                    self._history.append(line)
                self._on_submit(line)
        elif character == 260:              # Left
            self.cursor = max(0, self.cursor - 1)
            self._hist_pos = -1
        elif character == 261:              # Right
            self.cursor = min(len(self.buffer), self.cursor + 1)
            self._hist_pos = -1
        elif character == 259:              # Up — older history
            if self._history:
                if self._hist_pos == -1:
                    self._hist_draft = self.buffer
                    self._hist_pos = len(self._history) - 1
                elif self._hist_pos > 0:
                    self._hist_pos -= 1
                self.buffer = self._history[self._hist_pos]
                self.cursor = len(self.buffer)
        elif character == 258:              # Down — newer history
            if self._hist_pos != -1:
                if self._hist_pos < len(self._history) - 1:
                    self._hist_pos += 1
                    self.buffer = self._history[self._hist_pos]
                else:
                    self._hist_pos = -1
                    self.buffer = self._hist_draft
                self.cursor = len(self.buffer)
        elif character in (263, 127, 8):    # Backspace — delete before cursor
            if self.cursor > 0:
                self.buffer = self.buffer[:self.cursor - 1] + self.buffer[self.cursor:]
                self.cursor -= 1
                self._hist_pos = -1
        elif character == 23:               # Ctrl+W — kill word before cursor
            before     = self.buffer[:self.cursor].rstrip()
            cut        = before.rsplit(" ", 1)
            new_before = cut[0] + " " if len(cut) > 1 else ""
            self.buffer    = new_before + self.buffer[self.cursor:]
            self.cursor    = len(new_before)
            self._hist_pos = -1
        elif character == 21:               # Ctrl+U — kill to start of line
            self.buffer    = self.buffer[self.cursor:]
            self.cursor    = 0
            self._hist_pos = -1
        elif character == 262:              # Home — start of line
            buf = self._buf()
            if not buf or getattr(buf, "kind", None) != "doc":
                self.cursor = 0
        elif character == 360:              # End — end of line
            buf = self._buf()
            if not buf or getattr(buf, "kind", None) != "doc":
                self.cursor = len(self.buffer)
        elif 32 <= character < 127 or 160 <= character < 256:
            try:
                ch = chr(character)
                self.buffer = self.buffer[:self.cursor] + ch + self.buffer[self.cursor:]
                self.cursor += 1
                self._hist_pos = -1
            except Exception:
                pass

    # ── tab completion ───────────────────────────────────────────────────────

    def _try_complete(self):
        """Attempt completion.  Returns True if a candidate was applied."""
        tui = self._tui

        # Cycle through existing candidates
        if self._tab_state is not None:
            s = self._tab_state
            s["index"] = (s["index"] + 1) % len(s["candidates"])
            self._apply_tab(s)
            return True

        # ── Command completion: /partial with no space yet ──────────────────
        if self.buffer.startswith("/") and " " not in self.buffer:
            partial    = self.buffer[1:].lower()
            candidates = sorted(n for n in tui.commands if n.startswith(partial))
            if not candidates:
                return False
            s = {"type": "cmd", "candidates": candidates, "index": 0,
                 "word_start": 0, "first": False}
            self._tab_state = s
            self._apply_tab(s)
            return True

        # ── Nick completion: last word in the input ──────────────────────────
        buf = tui.current_buffer()
        if not buf:
            return False
        clean_nicks = [n.lstrip("@+%&~!") for n in buf.nicks]
        space_idx   = self.buffer.rfind(" ")
        word_start  = space_idx + 1
        partial     = self.buffer[word_start:]
        if not partial:
            return False
        candidates = [n for n in clean_nicks if n.lower().startswith(partial.lower())]
        if not candidates:
            return False
        s = {"type": "nick", "candidates": candidates, "index": 0,
             "word_start": word_start, "first": (word_start == 0)}
        self._tab_state = s
        self._apply_tab(s)
        return True

    def _apply_tab(self, s):
        candidate = s["candidates"][s["index"]]
        if s["type"] == "cmd":
            self.buffer = "/" + candidate
        else:
            suffix = ": " if s["first"] else " "
            self.buffer = self.buffer[:s["word_start"]] + candidate + suffix
        self.cursor = len(self.buffer)
        if self.window:
            self.window.window.clear()


# ── Main TUI controller ──────────────────────────────────────────────────────

class ScrollTUI:
    """
    Owns the Window, all Panes, and the list of Buffers.
    Wired to an IRCClient by the caller.
    """

    def __init__(self):
        self.buffers    = []
        self.buf_index  = 0
        self.irc        = None     # set by caller
        self.commands   = {}       # name → (func, docstring)
        self._window    = None
        self._confirm          = None     # pending confirmation: {"prompt", "yes_cb", "no_cb"}
        self._focus            = "input"  # "input" | "nicks" | "menu"
        self._list_results     = None     # collecting 322 replies; None = not in a /list
        self._list_filters     = {}       # min_users, max_users
        self._nick_tab_pending    = False   # one Tab from input returns to nick list
        self._focus_at_cycle_start = "input"  # snapshot before pane dispatch

        # server buffer is always index 0; irc is wired later by caller
        buf = self._add_buffer("server")
        buf.is_server = True

    # ── buffer management ────────────────────────────────────────────────────

    def _add_buffer(self, name):
        buf = Buffer(name)
        self.buffers.append(buf)
        return buf

    def get_buffer(self, name):
        for b in self.buffers:
            if b.name.lower() == name.lower():
                return b
        return None

    def add_server(self, client, name):
        """Create (or reuse index-0 placeholder) a server buffer for *client*."""
        # Reuse the placeholder created by __init__ if it has no client yet
        placeholder = self.buffers[0] if self.buffers and self.buffers[0].irc is None else None
        if placeholder:
            placeholder.name      = name
            placeholder.irc       = client
            placeholder.is_server = True
            return placeholder
        buf = self._add_buffer(name)
        buf.irc       = client
        buf.is_server = True
        return buf

    def current_irc(self):
        """Return the IRCClient associated with the currently visible buffer."""
        buf = self.current_buffer()
        if buf and buf.irc:
            return buf.irc
        # fall back to first connected client
        for b in self.buffers:
            if b.irc and b.irc.connected:
                return b.irc
        # fall back to any client
        for b in self.buffers:
            if b.irc:
                return b.irc
        return self.irc   # legacy

    def _server_buf_for(self, client):
        """Return the server buffer that belongs to *client*."""
        for b in self.buffers:
            if b.is_server and b.irc is client:
                return b
        return self.buffers[0]

    def get_or_add_buffer(self, name, irc_client=None):
        if irc_client:
            for b in self.buffers:
                if b.name.lower() == name.lower() and b.irc is irc_client:
                    return b
            b = self._add_buffer(name)
            b.irc = irc_client
            return b
        b = self.get_buffer(name)
        return b if b else self._add_buffer(name)

    def current_buffer(self):
        if not self.buffers:
            return None
        return self.buffers[self.buf_index]

    def set_focus(self, target):
        """Switch keyboard focus: 'input', 'nicks', or 'menu'."""
        self._focus = target
        if self._window and self._window.window:
            self._window.window.clear()

    def switch_to(self, index):
        if 0 <= index < len(self.buffers):
            self.buf_index = index
            self.current_buffer().unread = False
            self._focus = "input"   # always return to input on buffer switch
            if self._window and self._window.window:
                self._window.window.clear()

    def next_buffer(self):
        self.switch_to((self.buf_index + 1) % len(self.buffers))

    def prev_buffer(self):
        self.switch_to((self.buf_index - 1) % len(self.buffers))

    # ── message helpers ──────────────────────────────────────────────────────

    def server_msg(self, text, attrs=0, client=None):
        ts  = timestamp()
        c   = client or self.current_irc()
        buf = self._server_buf_for(c) if c else self.buffers[0]
        buf.add(ts, "", text, attrs)

    def channel_msg(self, target, nick, text, attrs=0, irc_client=None):
        ts  = timestamp()
        buf = self.get_or_add_buffer(target, irc_client)
        buf.add(ts, nick, text, attrs)
        if buf is not self.current_buffer():
            buf.unread = True

    # ── script event firing ───────────────────────────────────────────────────

    def _fire(self, event, **kwargs):
        """Fire a scripting event.  Failures are silently swallowed."""
        try:
            from . import script as _script
            if event == "connect" and "irc_client" in kwargs:
                kwargs["server"] = _script._ServerHandle(kwargs.pop("irc_client"))
            _script.fire(event, **kwargs)
        except Exception:
            pass

    # ── IRC event dispatch ───────────────────────────────────────────────────

    def handle_irc(self, msg, irc_client=None):
        """Called for every inbound IRC message."""
        cmd      = msg["command"]
        prefix   = msg["prefix"]
        params   = msg["params"]
        trailing = msg["trailing"]
        raw      = msg.get("raw", "")
        nick     = prefix.split("!")[0] if "!" in prefix else prefix
        c        = irc_client or self.irc   # the client that sent this message

        if cmd in ("001", "002", "003", "004", "372", "375", "376",
                   "251", "252", "253", "254", "255"):
            self.server_msg(trailing or " ".join(params), client=c)
            if cmd == "001":
                self._fire("connect", irc_client=c)

        elif cmd == "NOTICE":
            target = params[0] if params else ""
            if target.startswith("#"):
                self.channel_msg(target, "-" + nick + "-", trailing, irc_client=c)
            else:
                self.server_msg("-%s- %s" % (nick, trailing), client=c)
            self._fire("notice", nick=nick, target=target, text=trailing, raw=raw)

        elif cmd == "JOIN":
            channel = trailing or (params[0] if params else "")
            buf = self.get_or_add_buffer(channel, c)
            buf.add(timestamp(), "", "* %s has joined %s" % (nick, channel))
            if nick == (c.nick if c else ""):
                self.switch_to(self.buffers.index(buf))
            self._fire("join", nick=nick, channel=channel, raw=raw)

        elif cmd == "PART":
            channel = params[0] if params else ""
            buf = self.get_or_add_buffer(channel, c)
            buf.add(timestamp(), "", "* %s has left %s (%s)" % (nick, channel, trailing))
            self._fire("part", nick=nick, channel=channel, reason=trailing, raw=raw)

        elif cmd == "QUIT":
            for buf in self.buffers:
                if buf.irc is c and nick in buf.nicks:
                    buf.nicks.remove(nick)
                    buf.add(timestamp(), "", "* %s has quit (%s)" % (nick, trailing))
            self._fire("quit", nick=nick, reason=trailing, raw=raw)

        elif cmd == "PRIVMSG":
            target = params[0] if params else ""
            text   = trailing

            if text.startswith("\x01") and text.endswith("\x01"):
                ctcp = text[1:-1]
                ctcp_cmd, _, ctcp_arg = ctcp.partition(" ")
                ctcp_cmd = ctcp_cmd.upper()

                if ctcp_cmd == "ACTION":
                    line = "* %s %s" % (nick, ctcp_arg)
                    if target.startswith("#"):
                        self.channel_msg(target, "", line, irc_client=c)
                    else:
                        self.channel_msg(nick, "", line, irc_client=c)
                    self._fire("action", nick=nick, target=target, text=ctcp_arg, raw=raw)

                elif ctcp_cmd == "VERSION":
                    c.notice(nick, "\x01VERSION scroll\x01")
                    self.server_msg("CTCP VERSION from %s" % nick, client=c)

                elif ctcp_cmd == "PING":
                    c.notice(nick, "\x01PING %s\x01" % ctcp_arg)
                    self.server_msg("CTCP PING from %s" % nick, client=c)

                else:
                    self.server_msg("CTCP %s from %s (ignored)" % (ctcp_cmd, nick), client=c)

            elif target.startswith("#"):
                self.channel_msg(target, nick, text, irc_client=c)
                self._fire("privmsg", nick=nick, target=target, text=text, raw=raw)
            else:
                self.channel_msg(nick, nick, text, irc_client=c)
                self._fire("privmsg", nick=nick, target=nick, text=text, raw=raw)

        elif cmd == "353":  # RPL_NAMREPLY
            channel = params[2] if len(params) > 2 else ""
            buf = self.get_or_add_buffer(channel, c)
            nicks = trailing.split()
            for n in nicks:
                clean = n.lstrip("@+%&~!")
                if clean not in buf.nicks:
                    buf.nicks.append(n)

        elif cmd == "366":  # RPL_ENDOFNAMES
            channel = params[1] if len(params) > 1 else ""
            buf = self.get_or_add_buffer(channel, c)
            buf.add(timestamp(), "", "%d nicks in %s" % (len(buf.nicks), channel))

        elif cmd == "332":  # RPL_TOPIC
            channel = params[1] if len(params) > 1 else ""
            buf = self.get_or_add_buffer(channel, c)
            buf.topic = trailing
            buf.add(timestamp(), "", "Topic: %s" % trailing)

        elif cmd == "TOPIC":
            channel = params[0] if params else ""
            buf = self.get_or_add_buffer(channel, c)
            buf.topic = trailing
            buf.add(timestamp(), "", "* %s changed topic to: %s" % (nick, trailing))
            self._fire("topic", nick=nick, channel=channel, text=trailing, raw=raw)

        elif cmd == "KICK":
            channel = params[0] if params else ""
            kicked  = params[1] if len(params) > 1 else ""
            buf = self.get_or_add_buffer(channel, c)
            buf.add(timestamp(), "", "* %s was kicked from %s by %s (%s)" % (
                kicked, channel, nick, trailing))
            if kicked == (c.nick if c else ""):
                buf.nicks = []
            self._fire("kick", nick=nick, channel=channel, kicked=kicked, reason=trailing, raw=raw)

        elif cmd == "NICK":
            new_nick = trailing or (params[0] if params else "")
            for buf in self.buffers:
                if buf.irc is c and nick in [n.lstrip("@+%&~!") for n in buf.nicks]:
                    buf.add(timestamp(), "", "* %s is now known as %s" % (nick, new_nick))
            if c and nick == c.nick:
                c.nick = new_nick
            self._fire("nick", old_nick=nick, new_nick=new_nick, raw=raw)

        elif cmd == "MODE":
            target = params[0] if params else ""
            mode   = params[1] if len(params) > 1 else ""
            extra  = " ".join(params[2:])
            buf = self.get_or_add_buffer(target, c) if target.startswith("#") \
                  else self._server_buf_for(c)
            buf.add(timestamp(), "", "* Mode %s [%s %s] by %s" % (target, mode, extra, nick))
            self._fire("mode", nick=nick, target=target, mode=mode + " " + extra, raw=raw)

        elif cmd == "321":  # RPL_LISTSTART
            self._list_results = []

        elif cmd == "322":  # RPL_LIST  — channel, user count, topic
            channel   = params[1] if len(params) > 1 else ""
            try:
                users = int(params[2]) if len(params) > 2 else 0
            except ValueError:
                users = 0
            topic = trailing
            if self._list_results is not None:
                self._list_results.append((channel, users, topic))

        elif cmd == "323":  # RPL_LISTEND
            if self._list_results is not None:
                results = self._list_results
                self._list_results = None
                mn = self._list_filters.get("min_users")
                mx = self._list_filters.get("max_users")
                self._list_filters = {}
                if mn is not None:
                    results = [r for r in results if r[1] >= mn]
                if mx is not None:
                    results = [r for r in results if r[1] <= mx]
                results.sort(key=lambda r: r[1], reverse=True)
                if not results:
                    self.server_msg("-- /list: no channels matched", client=c)
                else:
                    col = max(len(r[0]) for r in results)
                    self.server_msg("  %-*s  users  topic" % (col, "channel"), client=c)
                    self.server_msg("  %s  -----  -----" % ("-" * col), client=c)
                    for channel, users, topic in results:
                        self.server_msg("  %-*s  %-5d  %s" % (col, channel, users, topic), client=c)
                    self.server_msg("-- %d channel(s)" % len(results), client=c)

        elif cmd == "433":  # ERR_NICKNAMEINUSE
            self.server_msg("* Nickname in use, trying with _", client=c)
            if c:
                c.nick = c.nick + "_"
                c.raw("NICK %s" % c.nick)

        elif cmd == "ERROR":
            self.server_msg("ERROR: %s" % trailing, client=c)

        else:
            if raw:
                self.server_msg(raw, client=c)

    # ── command dispatch ─────────────────────────────────────────────────────

    def register_command(self, name, func):
        self.commands[name.lower()] = func

    def dispatch_command(self, line):
        """Parse and execute a /command line."""
        parts = line[1:].split(" ", 1)
        name  = parts[0].lower()
        args  = parts[1] if len(parts) > 1 else ""
        func  = self.commands.get(name)
        if func:
            func(args)
        else:
            self.server_msg("Unknown command: /%s  (try /help)" % name)

    def ask(self, prompt, yes_cb, no_cb=None):
        """Display a yes/no prompt; route the next input line to the callbacks."""
        self._confirm = {"yes_cb": yes_cb, "no_cb": no_cb}
        self.server_msg(prompt + " [y/n]")

    def handle_input(self, line):
        """Called when the user presses Enter in the input pane."""
        if self._confirm is not None:
            cb   = self._confirm
            self._confirm = None
            ans  = line.strip().lower()
            if ans in ("y", "yes"):
                if cb["yes_cb"]:
                    cb["yes_cb"]()
            else:
                if cb["no_cb"]:
                    cb["no_cb"]()
                else:
                    self.server_msg("Cancelled.")
            return
        if line.startswith("/"):
            self.dispatch_command(line)
        else:
            buf    = self.current_buffer()
            client = self.current_irc()
            if buf and not buf.is_server and buf.kind != "doc" and client and client.connected:
                client.privmsg(buf.name, line)
                buf.add(timestamp(), client.nick, line)
            else:
                self.server_msg("Not in a channel.  Use /join #channel")

    # ── window construction & run ────────────────────────────────────────────

    def open_doc(self, name, text):
        """Open or switch to a documentation buffer populated with *text*."""
        buf_name = "[%s]" % name
        buf = self.get_buffer(buf_name)
        if buf is None:
            buf = self._add_buffer(buf_name)
            buf.kind = "doc"
            for line in text.splitlines():
                buf.add(None, "", line)
        self.switch_to(self.buffers.index(buf))

    def remove_buffer(self, buf):
        """Remove a buffer and switch to the nearest remaining one."""
        if buf not in self.buffers:
            return
        idx = self.buffers.index(buf)
        self.buffers.remove(buf)
        self.buf_index = max(0, idx - 1)
        self.current_buffer().unread = False
        self._focus = "input"
        if self._window and self._window.window:
            self._window.window.clear()

    def refresh_side_panels(self):
        """Show/hide the separator and nick list based on the current buffer."""
        if not self._window:
            return
        buf     = self.current_buffer()
        visible = bool(buf and buf.name.startswith("#"))
        sep  = self._window.get("sep")
        nick = self._window.get("nicks")
        changed = False
        for pane in (sep, nick):
            if pane and bool(pane.hidden) == visible:   # hidden != desired visible
                pane.hidden = not visible
                changed = True
        if changed and self._window.window:
            self._window.window.clear()

    def draw_overlays(self):
        """Draw floating panes (nick menu) after the main cycle completes."""
        if self._focus != "menu" or not self._window:
            return
        menu = self._window.get("nickmenu")
        if menu:
            menu.draw_overlay()

    def build_window(self):
        win = Window(blocking=False)
        win.delay = 0.05

        topic_pane  = TopicPane()
        msg_pane    = MessagePane(self.current_buffer, self)
        sep_pane    = SepPane()
        nick_pane   = NickPane(self.current_buffer, self)
        status_pane = StatusPane(lambda: self)
        input_pane  = InputPane(self.current_buffer, self)
        input_pane._on_submit = self.handle_input

        # Nick list hidden initially until a channel buffer is active
        sep_pane.hidden  = True
        nick_pane.hidden = True

        win.add(topic_pane)
        win.add([msg_pane, sep_pane, nick_pane])
        win.add(status_pane)
        win.add(input_pane)

        # Invisible panes: nav (ctrl+n/p) and nick menu overlay
        for pane in (_NavPane(self), NickMenuPane(nick_pane, self)):
            pane.active = True
            pane.window = win
            win.panes.append(pane)

        self._window     = win
        self._topic_pane = topic_pane
        return win

    def refresh_topic(self):
        buf = self.current_buffer()
        if self._topic_pane and buf:
            self._topic_pane.topic = buf.topic

    def run(self, connect_func):
        """
        connect_func() is called once the window is up.
        Poll IRC in the window's cycle via monkey-patching cycle().
        """
        win = self.build_window()
        original_cycle = win.cycle

        def patched_cycle():
            # Poll IRC
            if self.irc:
                self.irc.poll()
            self.refresh_topic()
            original_cycle()

        win.cycle = patched_cycle
        connect_func()
        win.start()


# ── Navigation pane (invisible, handles ctrl+n/p and alt+N) ─────────────────

class _NavPane(Pane):
    geometry = [0, 0]
    hidden   = True

    def __init__(self, tui):
        super().__init__("_nav")
        self._tui = tui

    def process_input(self, character):
        # Ctrl+N = 14, Ctrl+P = 16
        if character == 14:
            self._tui.next_buffer()
        elif character == 16:
            self._tui.prev_buffer()
        # Alt+1..9 come through as ESC sequences: 27 then '1'..'9'
        # curses delivers them as KEY_xxx or as raw escape; handle both.
        # In many terminals alt+digit arrives as a single value >= 0x100
        # or as the two-character ESC+digit sequence stored as 27 in the
        # buffer.  We handle via a small state machine in the window hook
        # instead — see _AltHandler below.
