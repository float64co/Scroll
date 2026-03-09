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

_handlers        = {}   # event_name → [(func, script_name, script_sha1), ...]
_script_commands = {}   # command_name → script_name
_script_loaded   = {}   # script_name → sha1 at load time
_irc             = None
_tui             = None

# Set by _begin_load() before exec'ing a script so @on() can tag handlers.
_current_script_name = None
_current_script_sha1 = None


def _setup(irc_obj, tui_obj):
    """Called once at startup after irc and tui are constructed."""
    global _irc, _tui
    _irc = irc_obj
    _tui = tui_obj


def _clear():
    """Remove all script-registered handlers and commands (used by /reload)."""
    global _handlers, _script_commands, _script_loaded
    if _tui:
        for cmd in _script_commands:
            _tui.commands.pop(cmd, None)
    _handlers        = {}
    _script_commands = {}
    _script_loaded   = {}


def _clear_script(fname):
    """Remove handlers and commands registered by *fname* only."""
    global _handlers, _script_commands, _script_loaded
    for key in list(_handlers):
        _handlers[key] = [(f, sn, sh) for f, sn, sh in _handlers[key] if sn != fname]
        if not _handlers[key]:
            del _handlers[key]
    to_remove = [cmd for cmd, sn in _script_commands.items() if sn == fname]
    for cmd in to_remove:
        del _script_commands[cmd]
        if _tui:
            _tui.commands.pop(cmd, None)
    _script_loaded.pop(fname, None)


def _begin_load(fname, sha1):
    """Mark *fname* as the script currently being loaded."""
    global _current_script_name, _current_script_sha1
    _current_script_name = fname
    _current_script_sha1 = sha1
    _script_loaded[fname] = sha1


def _end_load():
    """Clear the current-script context after exec."""
    global _current_script_name, _current_script_sha1
    _current_script_name = None
    _current_script_sha1 = None


def loaded_sha1(fname):
    """Return the sha1 stored when *fname* was last loaded, or None."""
    return _script_loaded.get(fname)


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
        _handlers.setdefault(key, []).append((func, _current_script_name, _current_script_sha1))
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
    for func, _sn, _sh in list(_handlers.get(key, [])):
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
    _script_commands[name] = _current_script_name
    if _tui:
        def _wrapper(args, _f=func):
            try:
                _f(args)
            except Exception as exc:
                _tui.server_msg("Script error [command:%s]: %s" % (name, exc))
        _wrapper.__doc__ = func.__doc__
        _tui.register_command(name, _wrapper)


# ── per-connection handle (passed to connect event as e.server) ───────────────

class _ServerHandle:
    """
    Thin wrapper around one IRCClient, safe to hand to script event handlers.
    Passed as e.server in connect events so scripts can capture a reference
    to a specific server independent of which one is currently active.
    """
    def __init__(self, client):
        self._c = client

    @property
    def host(self):       return self._c.host
    @property
    def port(self):       return self._c.port
    @property
    def nick(self):       return self._c.nick
    @property
    def connected(self):  return self._c.connected

    def raw(self, line):               self._c.raw(line)
    def privmsg(self, target, text):   self._c.privmsg(target, text)
    def notice(self, target, text):    self._c.notice(target, text)
    def join(self, channel):           self._c.join(channel)
    def part(self, channel, reason=""): self._c.part(channel, reason)


# ── irc proxy ─────────────────────────────────────────────────────────────────

class _IRCProxy:
    """
    Exposes IRC actions to scripts, always resolving against the server
    associated with the currently visible buffer.
    """
    @property
    def _client(self):
        if _tui:
            c = _tui.current_irc()
            if c:
                return c
        return _irc   # fallback to initial client

    @property
    def nick(self):
        c = self._client
        return c.nick if c else ""

    @property
    def current_channel(self):
        buf = _tui.current_buffer() if _tui else None
        return buf.name if buf and buf.name.startswith("#") else ""

    @property
    def connected(self):
        c = self._client
        return bool(c and c.connected)

    def privmsg(self, target, text):
        c = self._client
        if c: c.privmsg(target, text)

    def notice(self, target, text):
        c = self._client
        if c: c.notice(target, text)

    def join(self, channel):
        c = self._client
        if c: c.join(channel)

    def part(self, channel, reason=""):
        c = self._client
        if c: c.part(channel, reason)

    def raw(self, line):
        c = self._client
        if c: c.raw(line)


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
