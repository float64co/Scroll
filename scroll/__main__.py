# -*- coding: utf-8 -*-
"""
scroll — a minimal irssi-inspired IRC client.
Entry point: parses config.hcl, connects, runs the TUI.
"""
import importlib
import os
import re
import signal
import subprocess
import sys
import threading
import time


# ── Config parser ─────────────────────────────────────────────────────────────

def parse_hcl(text):
    """
    Bare-minimum HCL parser for the subset used by scroll's config:
      key = "value"
      servers = [ { name = "…", host = "…", port = N }, … ]
    Returns a dict.
    """
    cfg = {}

    # strip comments
    text = re.sub(r'//[^\n]*', '', text)
    text = re.sub(r'#[^\n]*',  '', text)

    # servers block: grab the raw bracketed list
    srv_match = re.search(r'servers\s*=\s*\[([^\]]*)\]', text, re.DOTALL)
    servers = []
    if srv_match:
        block = srv_match.group(1)
        for obj in re.findall(r'\{([^}]*)\}', block, re.DOTALL):
            entry = {}
            for m in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', obj):
                entry[m.group(1)] = m.group(2)
            for m in re.finditer(r'(\w+)\s*=\s*(\d+)', obj):
                entry[m.group(1)] = int(m.group(2))
            if entry:
                servers.append(entry)
        text = text[:srv_match.start()] + text[srv_match.end():]

    cfg["servers"] = servers

    # scalar string values
    for m in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', text):
        cfg[m.group(1)] = m.group(2)

    # scalar int values
    for m in re.finditer(r'(\w+)\s*=\s*(\d+)\b', text):
        if m.group(1) not in cfg:
            cfg[m.group(1)] = int(m.group(2))

    return cfg


def load_scripts(scripts_dir, tui):
    """Exec every .py file in scripts_dir, in sorted order."""
    if not scripts_dir:
        return
    scripts_dir = os.path.expanduser(scripts_dir)
    if not os.path.isdir(scripts_dir):
        return
    for fname in sorted(os.listdir(scripts_dir)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(scripts_dir, fname)
        try:
            with open(path) as f:
                code = f.read()
            ns = {"__file__": path, "__name__": fname[:-3]}
            exec(compile(code, path, "exec"), ns)
        except Exception as exc:
            tui.server_msg("Script load error (%s): %s" % (fname, exc))


def load_config():
    # Look for config.hcl next to the package, then in ~/.config/scroll/
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.hcl"),
        os.path.expanduser("~/.config/scroll/config.hcl"),
        os.path.expanduser("~/.scroll/config.hcl"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path) as f:
                return parse_hcl(f.read()), path
    return {}, None


# ── Command definitions ───────────────────────────────────────────────────────

def register_commands(tui, irc, cfg):

    def cmd_join(args):
        """Join a channel.  Usage: /join #channel"""
        channel = args.strip()
        if not channel:
            tui.server_msg("Usage: /join #channel")
            return
        if not channel.startswith("#"):
            channel = "#" + channel
        irc.join(channel)

    def cmd_part(args):
        """Leave a channel.  Usage: /part [#channel] [reason]"""
        buf = tui.current_buffer()
        parts = args.split(" ", 1)
        channel = parts[0].strip() if parts[0].strip().startswith("#") else buf.name
        reason  = parts[-1] if len(parts) > 1 else "Leaving"
        irc.part(channel, reason)

    def cmd_msg(args):
        """Send a private message.  Usage: /msg <nick> <message>"""
        parts = args.split(" ", 1)
        if len(parts) < 2:
            tui.server_msg("Usage: /msg <nick> <message>")
            return
        nick, text = parts
        irc.privmsg(nick, text)
        buf = tui.get_or_add_buffer(nick)
        from .tui import timestamp
        buf.add(timestamp(), irc.nick, text)

    def cmd_nick(args):
        """Change your nickname.  Usage: /nick <newnick>"""
        new = args.strip()
        if not new:
            tui.server_msg("Usage: /nick <newnick>")
            return
        irc.raw("NICK %s" % new)

    def cmd_quit(args):
        """Disconnect and exit.  Usage: /quit [message]"""
        reason = args.strip() or "scroll"
        irc.disconnect(reason)
        if tui._window:
            tui._window.running = False   # let start() call stop() once

    def cmd_raw(args):
        """Send a raw IRC command.  Usage: /raw <command>"""
        if args.strip():
            irc.raw(args.strip())

    def cmd_me(args):
        """/me action.  Usage: /me <action>"""
        buf = tui.current_buffer()
        if buf and buf.name != "server":
            irc.raw("PRIVMSG %s :\x01ACTION %s\x01" % (buf.name, args))
            from .tui import timestamp
            buf.add(timestamp(), "", "* %s %s" % (irc.nick, args))

    def cmd_topic(args):
        """View or set a channel topic.  Usage: /topic [new topic]"""
        buf = tui.current_buffer()
        if not buf or not buf.name.startswith("#"):
            tui.server_msg("Not in a channel.")
            return
        if args.strip():
            irc.raw("TOPIC %s :%s" % (buf.name, args.strip()))
        else:
            tui.server_msg("Topic: %s" % buf.topic)

    def cmd_names(args):
        """List nicks in the current channel.  Usage: /names"""
        buf = tui.current_buffer()
        if buf and buf.nicks:
            tui.server_msg("Nicks in %s: %s" % (buf.name, " ".join(buf.nicks)))
        else:
            tui.server_msg("No nick list available.")

    def cmd_clear(args):
        """Clear the current buffer.  Usage: /clear"""
        buf = tui.current_buffer()
        if buf:
            buf.lines = []
        if tui._window and tui._window.window:
            tui._window.window.clear()

    def cmd_server(args):
        """Show current server info.  Usage: /server"""
        tui.server_msg("Connected to %s:%s as %s" % (irc.host, irc.port, irc.nick))

    def cmd_wc(args):
        """Close the current window.  Parts the channel if in one.  Usage: /wc"""
        buf = tui.current_buffer()
        if tui.buf_index == 0:
            tui.server_msg("Cannot close the status window.")
            return
        if buf.name.startswith("#") and irc.connected:
            irc.part(buf.name)
        tui.remove_buffer(buf)

    def cmd_exec(args):
        """/exec [-o] <shell command> — run a shell command.
Without -o: show output in current buffer only.
With -o: send output to the current channel (>2 lines prompts for stagger)."""
        send_output = False
        if args.startswith("-o "):
            send_output = True
            args = args[3:]
        elif args.strip() == "-o":
            tui.server_msg("Usage: /exec -o <command>")
            return

        if not args.strip():
            tui.server_msg("Usage: /exec [-o] <command>")
            return

        try:
            result = subprocess.run(
                args, shell=True, capture_output=True, text=True, timeout=30
            )
            output = result.stdout
            if result.stderr:
                output += result.stderr
        except subprocess.TimeoutExpired:
            tui.server_msg("exec: command timed out")
            return
        except Exception as e:
            tui.server_msg("exec: %s" % e)
            return

        lines = [l for l in output.splitlines() if l.strip() or not l.endswith("")]
        # keep blank lines but drop a single trailing empty line
        if lines and lines[-1] == "":
            lines = lines[:-1]
        if not lines:
            tui.server_msg("exec: (no output)")
            return

        if not send_output:
            for line in lines:
                tui.server_msg(line)
            return

        # -o path: need a channel/query target
        buf = tui.current_buffer()
        if not buf or buf.name == "server":
            tui.server_msg("exec -o: not in a channel")
            return
        target = buf.name

        from .tui import timestamp

        def send_lines_now():
            for line in lines:
                irc.privmsg(target, line)
                buf.add(timestamp(), irc.nick, line)

        def send_lines_staggered():
            def _worker():
                for line in lines:
                    irc.privmsg(target, line)
                    buf.add(timestamp(), irc.nick, line)
                    time.sleep(2)
            threading.Thread(target=_worker, daemon=True).start()

        if len(lines) <= 2:
            send_lines_now()
        else:
            tui.ask(
                "exec: %d lines of output. Send staggered (1 line/2s)?" % len(lines),
                yes_cb=send_lines_staggered,
                no_cb=lambda: tui.server_msg("exec: cancelled"),
            )

    def cmd_script(args):
        """/script [edit <file.py>] — list scripts with checksums, or open one in $EDITOR."""
        import hashlib, _curses

        scripts_dir = cfg.get("scripts_directory", "")
        if not scripts_dir:
            tui.server_msg("script: scripts_directory not set in config.hcl")
            return
        scripts_dir = os.path.expanduser(scripts_dir)
        if not os.path.isdir(scripts_dir):
            tui.server_msg("script: directory not found: %s" % scripts_dir)
            return

        argv = args.strip().split(None, 1)
        subcmd = argv[0].lower() if argv else ""

        # ── /script edit <file.py> ────────────────────────────────────────────
        if subcmd == "edit":
            if len(argv) < 2:
                tui.server_msg("Usage: /script edit <file.py>")
                return
            fname = argv[1].strip()
            if os.sep in fname or fname.startswith(".."):
                tui.server_msg("script: invalid filename")
                return
            if not fname.endswith(".py"):
                fname += ".py"
            fpath = os.path.join(scripts_dir, fname)
            editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
            if not editor:
                tui.server_msg("script: $EDITOR is not set")
                return
            win = tui._window
            if not win or not win.window:
                tui.server_msg("script: window not ready")
                return
            # Suspend curses, hand the terminal to the editor, then resume.
            _curses.endwin()
            try:
                subprocess.run([editor, fpath])
            finally:
                win.window.refresh()
                win.window.clear()
            tui.server_msg("script: returned from editor (%s)" % fname)
            return

        # ── /script (no args) — list with sha1sums ────────────────────────────
        try:
            files = sorted(f for f in os.listdir(scripts_dir) if f.endswith(".py"))
        except OSError as e:
            tui.server_msg("script: %s" % e)
            return

        if not files:
            tui.server_msg("script: no scripts in %s" % scripts_dir)
            return

        col_w = max(len(f) for f in files)
        tui.server_msg("scripts in %s" % scripts_dir)
        tui.server_msg("  %-*s  sha1" % (col_w, "file"))
        tui.server_msg("  %s  %s" % ("-" * col_w, "-" * 40))
        for fname in files:
            fpath = os.path.join(scripts_dir, fname)
            try:
                with open(fpath, "rb") as f:
                    digest = hashlib.sha1(f.read()).hexdigest()
            except OSError:
                digest = "(unreadable)"
            tui.server_msg("  %-*s  %s" % (col_w, fname, digest))

    def cmd_reload(args):
        """Reload all scripts from scripts_directory.  Usage: /reload"""
        from . import script as _script
        _script._clear()
        load_scripts(cfg.get("scripts_directory"), tui)
        tui.server_msg("Scripts reloaded.")

    def cmd_doc(args):
        """Open a documentation buffer.  Usage: /doc [topic]"""
        from .docs import list_docs, load_doc
        topic = args.strip().lower()
        if not topic:
            available = list_docs()
            tui.server_msg("Available docs: %s  (use /doc <topic>)" % ", ".join(available))
            return
        text = load_doc(topic)
        if text is None:
            available = list_docs()
            tui.server_msg("No doc '%s'.  Available: %s" % (topic, ", ".join(available)))
            return
        tui.open_doc(topic, text)

    def cmd_help(args):
        """Show available commands and their descriptions.  Usage: /help [command]"""
        args = args.strip().lower().lstrip("/")
        if args and args in tui.commands:
            func = tui.commands[args]
            doc  = (func.__doc__ or "No description.").strip()
            tui.server_msg("/%s — %s" % (args, doc))
        else:
            tui.server_msg("Available commands:")
            for name in sorted(tui.commands):
                func = tui.commands[name]
                doc  = (func.__doc__ or "").strip().split("\n")[0]
                tui.server_msg("  /%s — %s" % (name, doc))

    for name, func in [
        ("join",   cmd_join),
        ("j",      cmd_join),   # alias
        ("part",   cmd_part),
        ("msg",    cmd_msg),
        ("nick",   cmd_nick),
        ("quit",   cmd_quit),
        ("exit",   cmd_quit),   # alias
        ("raw",    cmd_raw),
        ("me",     cmd_me),
        ("topic",  cmd_topic),
        ("names",  cmd_names),
        ("clear",  cmd_clear),
        ("server", cmd_server),
        ("doc",    cmd_doc),
        ("wc",     cmd_wc),
        ("exec",   cmd_exec),
        ("script", cmd_script),
        ("reload", cmd_reload),
        ("help",   cmd_help),
    ]:
        tui.register_command(name, func)


# ── Alt+digit state machine injected into the window ─────────────────────────

def patch_alt_keys(win, tui):
    """
    Intercept the window's process_input to catch ESC + digit sequences
    for alt+1..9 buffer switching.
    """
    _esc_pending = [False]
    original_pi  = win.process_input

    def patched_pi():
        try:
            character = win.window.getch()
        except Exception:
            character = -1

        if character in win.exit_keys:
            win.stop()
            return

        if character == 12:   # ^L
            win.window.clear()
            return

        if _esc_pending[0]:
            if character == -1:
                # No follow-up: treat the earlier ESC as a standalone ESC
                _esc_pending[0] = False
                character = 27
            elif ord('1') <= character <= ord('9'):
                _esc_pending[0] = False
                tui.switch_to(character - ord('1'))
                win.window.clear()
                return
            else:
                _esc_pending[0] = False
                # fall through and dispatch character normally
        elif character == 27:
            _esc_pending[0] = True
            return

        if character != -1:
            for pane in win:
                if pane.active:
                    pane.process_input(character)

            if win.debug:
                win.addstr(win.height - 1, win.width // 2, "    ")
                s = str(character)
                win.addstr(win.height - 1, win.width // 2 - len(s) // 2, s)

    win.process_input = patched_pi


# ── Main ──────────────────────────────────────────────────────────────────────

HELP_TEXT = """\
Usage: scroll [--help]

scroll is a minimal irssi-inspired IRC client.

Configuration is read from config.hcl (searched in the project directory,
~/.config/scroll/config.hcl, and ~/.scroll/config.hcl).

config.hcl keys:
  nick      = "yournick"
  realname  = "Your Name"
  ident     = "ident"
  servers   = [{ name = "Rizon", host = "irc.rizon.net", port = 6667 }]

Key bindings:
  Ctrl+N / Ctrl+P   next / previous buffer
  Alt+1 .. Alt+9    jump directly to buffer N
  Ctrl+W            delete last word in input
  Ctrl+U            clear input line
  Ctrl+L            force redraw
  Enter             send message / execute command

Commands (type /help inside scroll for full list):
  /join #channel    join a channel
  /part [reason]    leave current channel
  /msg nick text    send a private message
  /nick newnick     change nickname
  /me action        send a CTCP ACTION
  /topic [text]     view or set channel topic
  /names            list nicks in current channel
  /clear            clear current buffer
  /raw command      send raw IRC line
  /server           show connection info
  /quit [message]   disconnect and exit  (alias: /exit)
  /help [command]   show this help
"""


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(HELP_TEXT, end="")
        sys.exit(0)

    cfg, cfg_path = load_config()

    nick     = cfg.get("nick",     "scrolluser")
    realname = cfg.get("realname", nick)
    ident    = cfg.get("ident",    nick)
    servers  = cfg.get("servers",  [])

    if not servers:
        print("scroll: no servers defined in config.hcl", file=sys.stderr)
        sys.exit(1)

    server = servers[0]
    host   = server.get("host", "irc.libera.chat")
    port   = int(server.get("port", 6667))
    name   = server.get("name", host)

    from .irc import IRCClient
    from .tui import ScrollTUI

    tui = ScrollTUI()
    irc = IRCClient(host, port, nick, ident, realname)
    tui.irc = irc
    irc.handlers.append(tui.handle_irc)

    register_commands(tui, irc, cfg)

    from . import script as _script
    _script._setup(irc, tui)
    load_scripts(cfg.get("scripts_directory"), tui)

    # Update the server buffer name
    tui.buffers[0].name = name

    def connect():
        tui.server_msg("Connecting to %s (%s:%d) as %s …" % (name, host, port, nick))
        try:
            irc.connect()
            tui.server_msg("Connected.")
        except Exception as e:
            tui.server_msg("Connection failed: %s" % e)

    # Graceful Ctrl+C / SIGINT
    def handle_sigint(sig, frame):
        try:
            irc.disconnect("scroll")
        except Exception:
            pass
        if tui._window:
            tui._window.running = False   # let start() call stop() once

    signal.signal(signal.SIGINT, handle_sigint)

    win = tui.build_window()
    patch_alt_keys(win, tui)

    # Wire IRC poll + topic refresh into the cycle
    original_cycle = win.cycle

    def patched_cycle():
        if irc.connected:
            irc.poll()
        tui.refresh_topic()
        tui.refresh_side_panels()
        original_cycle()
        tui.draw_overlays()

    win.cycle = patched_cycle

    connect()
    win.start()


if __name__ == "__main__":
    main()
