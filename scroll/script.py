# -*- coding: utf-8 -*-
"""
scroll scripting API.

Scripts import from this module:

    from scroll.script import on, irc, tui, echo

    @on("privmsg")
    def on_msg(e):
        if "hello" in e.text.lower():
            irc.privmsg(e.target, "hi, %s" % e.nick)

    @on("command:slap")
    def cmd_slap(args):
        irc.privmsg(irc.current_channel,
                    "\\x01ACTION slaps %s with a trout\\x01" % args)

Slash commands use the "command:<name>" event name.  The handler receives
the argument string directly (not an Event object).

Event attributes vary by event type — see the table below.

    Event       Attributes
    ─────────────────────────────────────────────────────────────
    connect     (none)
    privmsg     nick, target, text, raw
    notice      nick, target, text, raw
    action      nick, target, text, raw
    join        nick, channel, raw
    part        nick, channel, reason, raw
    quit        nick, reason, raw
    kick        nick, channel, kicked, reason, raw
    nick        old_nick, new_nick, raw
    topic       nick, channel, text, raw
    mode        nick, target, mode, raw
"""
import time

# ── internal state ────────────────────────────────────────────────────────────

_handlers        = {}    # event_name (lower) → [callable, ...]
_script_commands = set() # command names registered by scripts (for /reload)
_irc             = None  # IRCClient, populated by _setup()
_tui             = None  # ScrollTUI, populated by _setup()


def _setup(irc_obj, tui_obj):
    """Called once at startup after irc and tui are constructed."""
    global _irc, _tui
    _irc = irc_obj
    _tui = tui_obj


def _clear():
    """Remove all script-registered handlers and commands (used by /reload)."""
    global _handlers, _script_commands
    if _tui:
        for cmd in _script_commands:
            _tui.commands.pop(cmd, None)
    _handlers        = {}
    _script_commands = set()


# ── public API ────────────────────────────────────────────────────────────────

class Event:
    """Simple attribute bag passed to event handlers."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
    def __repr__(self):
        return "Event(%s)" % ", ".join("%s=%r" % i for i in self.__dict__.items())


def on(event):
    """
    Decorator.  Register a handler for an IRC event or a slash command.

        @on("privmsg")
        def handler(event): ...

        @on("command:hello")
        def cmd_hello(args): ...
    """
    def decorator(func):
        key = event.lower()
        _handlers.setdefault(key, []).append(func)
        if key.startswith("command:"):
            _register_command(key[8:], func)
        return func
    return decorator


def fire(event, **kwargs):
    """
    Dispatch *event* to all registered handlers.
    Slash-command handlers receive args (str); all other handlers receive
    an Event object.
    Called from ScrollTUI._fire() for IRC events.
    """
    key = event.lower()
    for func in list(_handlers.get(key, [])):
        try:
            if key.startswith("command:"):
                func(kwargs.get("args", ""))
            else:
                func(Event(**kwargs))
        except Exception as exc:
            if _tui:
                _tui.server_msg("Script error [%s]: %s" % (event, exc))


def echo(target, text):
    """Write *text* to buffer *target* without sending anything to IRC."""
    if _tui:
        buf = _tui.get_or_add_buffer(target)
        buf.add(time.strftime("%H:%M:%S"), "", text)


# ── helpers ───────────────────────────────────────────────────────────────────

def _register_command(name, func):
    """Register *func* as the handler for /<name>."""
    name = name.lower()
    _script_commands.add(name)
    if _tui:
        def _wrapper(args, _f=func):
            try:
                _f(args)
            except Exception as exc:
                _tui.server_msg("Script error [command:%s]: %s" % (name, exc))
        _wrapper.__doc__ = func.__doc__
        _tui.register_command(name, _wrapper)


# ── irc proxy ─────────────────────────────────────────────────────────────────

class _IRCProxy:
    """
    Exposes IRC actions to scripts.  Attributes are read live so scripts
    that import `irc` at module load time still see the connected state.
    """
    @property
    def nick(self):
        return _irc.nick if _irc else ""

    @property
    def current_channel(self):
        buf = _tui.current_buffer() if _tui else None
        return buf.name if buf and buf.name.startswith("#") else ""

    @property
    def connected(self):
        return bool(_irc and _irc.connected)

    def privmsg(self, target, text):
        if _irc:
            _irc.privmsg(target, text)

    def notice(self, target, text):
        if _irc:
            _irc.notice(target, text)

    def join(self, channel):
        if _irc:
            _irc.join(channel)

    def part(self, channel, reason=""):
        if _irc:
            _irc.part(channel, reason)

    def raw(self, line):
        if _irc:
            _irc.raw(line)


class _TUIProxy:
    """Exposes UI operations to scripts."""
    @property
    def buffers(self):
        return _tui.buffers if _tui else []

    def current_buffer(self):
        return _tui.current_buffer() if _tui else None

    def switch_to(self, index):
        if _tui:
            _tui.switch_to(index)

    def server_msg(self, text):
        if _tui:
            _tui.server_msg(text)

    def get_buffer(self, name):
        return _tui.get_buffer(name) if _tui else None

    def get_or_add_buffer(self, name):
        return _tui.get_or_add_buffer(name) if _tui else None


irc = _IRCProxy()
tui = _TUIProxy()
