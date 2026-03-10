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

    # expand $VAR and ${VAR} from the environment before anything else
    text = os.path.expandvars(text)

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
    import hashlib
    from . import script as _script
    for fname in sorted(os.listdir(scripts_dir)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(scripts_dir, fname)
        try:
            with open(path, "rb") as f:
                data = f.read()
            sha1 = hashlib.sha1(data).hexdigest()
            _script._begin_load(fname, sha1)
            ns = {"__file__": path, "__name__": fname[:-3]}
            exec(compile(data.decode(), path, "exec"), ns)
        except Exception as exc:
            tui.server_msg("Script load error (%s): %s" % (fname, exc))
        finally:
            _script._end_load()


def load_config():
    candidates = [
        os.path.expanduser("~/.scroll/config.hcl"),
        os.path.expanduser("~/.config/scroll/config.hcl"),
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.hcl"),  # dev fallback
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path) as f:
                return parse_hcl(f.read()), path
    return {}, None


# ── Command definitions ───────────────────────────────────────────────────────

def register_commands(tui, irc, cfg):

    def _irc():
        """Return the IRCClient for the current buffer, or None."""
        return tui.current_irc()

    def cmd_join(args):
        """Join a channel.  Usage: /join #channel"""
        channel = args.strip()
        if not channel:
            tui.server_msg("Usage: /join #channel")
            return
        if not channel.startswith("#"):
            channel = "#" + channel
        c = _irc()
        if c: c.join(channel)

    def cmd_part(args):
        """Leave a channel.  Usage: /part [#channel] [reason]"""
        buf   = tui.current_buffer()
        parts = args.split(" ", 1)
        channel = parts[0].strip() if parts[0].strip().startswith("#") else buf.name
        reason  = parts[-1] if len(parts) > 1 else "Leaving"
        c = _irc()
        if c: c.part(channel, reason)

    def cmd_msg(args):
        """Send a private message.  Usage: /msg <nick> <message>"""
        parts = args.split(" ", 1)
        if len(parts) < 2:
            tui.server_msg("Usage: /msg <nick> <message>")
            return
        target, text = parts
        c = _irc()
        if not c:
            tui.server_msg("msg: not connected")
            return
        c.privmsg(target, text)
        buf = tui.get_or_add_buffer(target, c)
        from .tui import timestamp
        buf.add(timestamp(), c.nick, text)

    def cmd_nick(args):
        """Change your nickname.  Usage: /nick <newnick>"""
        new = args.strip()
        if not new:
            tui.server_msg("Usage: /nick <newnick>")
            return
        c = _irc()
        if c: c.raw("NICK %s" % new)

    def cmd_quit(args):
        """Disconnect from all servers and exit.  Usage: /quit [message]"""
        reason = args.strip() or "scroll"
        seen = set()
        for buf in tui.buffers:
            if buf.irc and id(buf.irc) not in seen:
                seen.add(id(buf.irc))
                try:
                    buf.irc.disconnect(reason)
                except Exception:
                    pass
        if tui._window:
            tui._window.running = False

    def cmd_raw(args):
        """Send a raw IRC command.  Usage: /raw <command>"""
        if args.strip():
            c = _irc()
            if c: c.raw(args.strip())

    def cmd_me(args):
        """/me action.  Usage: /me <action>"""
        buf = tui.current_buffer()
        c   = _irc()
        if buf and not buf.is_server and c:
            c.raw("PRIVMSG %s :\x01ACTION %s\x01" % (buf.name, args))
            from .tui import timestamp
            buf.add(timestamp(), "", "* %s %s" % (c.nick, args))

    def cmd_topic(args):
        """View or set a channel topic.  Usage: /topic [new topic]"""
        buf = tui.current_buffer()
        if not buf or not buf.name.startswith("#"):
            tui.server_msg("Not in a channel.")
            return
        c = _irc()
        if not c: return
        if args.strip():
            c.raw("TOPIC %s :%s" % (buf.name, args.strip()))
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

    def cmd_list(args):
        """List channels.  Usage: /list [--min=<n>] [--max=<n>]"""
        min_users = None
        max_users = None
        for token in args.split():
            if token.startswith("--min="):
                try:
                    min_users = int(token[6:])
                except ValueError:
                    tui.server_msg("list: --min requires an integer")
                    return
            elif token.startswith("--max="):
                try:
                    max_users = int(token[6:])
                except ValueError:
                    tui.server_msg("list: --max requires an integer")
                    return
        tui._list_results = []
        tui._list_filters = {}
        if min_users is not None:
            tui._list_filters["min_users"] = min_users
        if max_users is not None:
            tui._list_filters["max_users"] = max_users
        tui.server_msg("-- requesting channel list...")
        c = _irc()
        if c: c.raw("LIST")

    def cmd_mode(args):
        """Set or view modes.  Usage: /mode [target] [modes] [params]"""
        args = args.strip()
        buf  = tui.current_buffer()
        c    = _irc()
        if not c: return
        if not args:
            target = buf.name if buf and buf.name.startswith("#") else c.nick
            c.raw("MODE %s" % target)
        else:
            parts = args.split()
            if parts[0].startswith("+") or parts[0].startswith("-"):
                target = buf.name if buf and buf.name.startswith("#") else c.nick
                c.raw("MODE %s %s" % (target, args))
            else:
                c.raw("MODE %s" % args)

    def cmd_connect(args):
        """Connect to a server.  Usage: /connect <host> [port]"""
        from .irc import IRCClient
        args = args.strip()
        if not args:
            c = _irc()
            if c and c.connected:
                tui.server_msg("Connected to %s:%s as %s" % (c.host, c.port, c.nick))
            else:
                tui.server_msg("Not connected.  Usage: /connect <host> [port]")
            return
        parts    = args.split()
        new_host = parts[0]
        try:
            new_port = int(parts[1]) if len(parts) > 1 else 6667
        except ValueError:
            tui.server_msg("connect: invalid port")
            return
        # Create a new client inheriting identity from the initial one
        new_client = IRCClient(new_host, new_port, irc.nick, irc.ident, irc.realname)
        buf = tui.add_server(new_client, new_host)
        tui.switch_to(tui.buffers.index(buf))
        new_client.handlers.append(
            lambda msg, c=new_client: tui.handle_irc(msg, c)
        )
        from . import script as _script
        tui.server_msg("Connecting to %s:%d as %s …" % (new_host, new_port, irc.nick),
                       client=new_client)
        def _do_connect():
            try:
                new_client.connect()
            except Exception as e:
                tui.server_msg("Connection failed: %s" % e, client=new_client)
        threading.Thread(target=_do_connect, daemon=True).start()

    def cmd_disconnect(args):
        """Disconnect from the current server.  Usage: /disconnect [message]"""
        c = _irc()
        if not c or not c.connected:
            tui.server_msg("Not connected.")
            return
        reason = args.strip() or "scroll"
        c.disconnect(reason)
        from .tui import timestamp
        tui._server_buf_for(c).add(timestamp(), "", "* Disconnected (%s)" % reason)

    def cmd_wc(args):
        """Close the current window.  Parts the channel if in one.  Usage: /wc"""
        buf = tui.current_buffer()
        if buf and buf.is_server:
            if buf.irc and buf.irc.connected:
                tui.server_msg("Cannot close a server buffer while connected.  Use /disconnect first.")
                return
            server_bufs = [b for b in tui.buffers if b.is_server]
            if len(server_bufs) <= 1:
                tui.server_msg("Cannot close the last server buffer.")
                return
        c = buf.irc if buf else None
        if buf and buf.name.startswith("#") and c and c.connected:
            c.part(buf.name)
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
        if not buf or buf.is_server:
            tui.server_msg("exec -o: not in a channel")
            return
        target = buf.name
        c      = _irc()
        if not c:
            tui.server_msg("exec -o: not connected")
            return

        from .tui import timestamp

        def send_lines_now():
            for line in lines:
                c.privmsg(target, line)
                buf.add(timestamp(), c.nick, line)

        def send_lines_staggered():
            def _worker():
                for line in lines:
                    c.privmsg(target, line)
                    buf.add(timestamp(), c.nick, line)
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
            # Reload exclusively the edited script
            from . import script as _script
            _script._clear_script(fname)
            try:
                with open(fpath, "rb") as f:
                    data = f.read()
                sha1 = hashlib.sha1(data).hexdigest()
                _script._begin_load(fname, sha1)
                ns = {"__file__": fpath, "__name__": fname[:-3]}
                exec(compile(data.decode(), fpath, "exec"), ns)
                tui.server_msg("Finished editing %s." % fname)
            except Exception as exc:
                tui.server_msg("Error reloading %s: %s" % (fname, exc))
            finally:
                _script._end_load()
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

        from . import script as _script

        # Build rows first so we know the max display-name width
        rows = []
        for fname in files:
            fpath = os.path.join(scripts_dir, fname)
            try:
                with open(fpath, "rb") as f:
                    digest = hashlib.sha1(f.read()).hexdigest()
            except OSError:
                digest = "(unreadable)"
            loaded = _script.loaded_sha1(fname)
            stale  = loaded is not None and loaded != digest
            rows.append((fname + ("*" if stale else ""), digest))

        col_w = max(len(name) for name, _ in rows)
        tui.server_msg("scripts in %s" % scripts_dir)
        tui.server_msg("  %-*s  sha1" % (col_w, "file"))
        tui.server_msg("  %s  %s" % ("-" * col_w, "-" * 40))
        for name, digest in rows:
            tui.server_msg("  %-*s  %s" % (col_w, name, digest))

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
        ("quote",  cmd_raw),   # alias
        ("list",   cmd_list),
        ("me",     cmd_me),
        ("topic",  cmd_topic),
        ("names",  cmd_names),
        ("clear",  cmd_clear),
        ("mode",       cmd_mode),
        ("connect",    cmd_connect),
        ("server",     cmd_connect),     # alias
        ("disconnect", cmd_disconnect),
        ("doc",        cmd_doc),
        ("wc",         cmd_wc),
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

        if character == 24:   # ^X — cycle between server buffers
            srv = [i for i, b in enumerate(tui.buffers) if b.is_server]
            if len(srv) > 1:
                later = [i for i in srv if i > tui.buf_index]
                tui.switch_to(later[0] if later else srv[0])
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
            if tui._focus == "menu":
                # Dispatch ESC immediately — no Alt+digit ambiguity in menu mode
                for pane in win:
                    if pane.active:
                        pane.process_input(27)
                return
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

from . import __version__

HELP_TEXT = """\
Usage: scroll [--help] [--version] [--headless]

scroll %s — a minimal irssi-inspired IRC client.""" % __version__ + """

Configuration is read from config.hcl (searched in the project directory,
~/.config/scroll/config.hcl, and ~/.scroll/config.hcl).

config.hcl keys:
  nick      = "yournick"
  realname  = "Your Name"
  ident     = "ident"
  servers   = [{ name = "Rizon", host = "irc.rizon.net", port = 6667 }]

Key bindings:
  Ctrl+N / Ctrl+P   next / previous buffer
  Ctrl+X            cycle between server buffers
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


def daemonize():
    """Fork twice to fully detach from the controlling terminal."""
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    # Redirect stdio to /dev/null
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (sys.stdin.fileno(), sys.stdout.fileno(), sys.stderr.fileno()):
        os.dup2(devnull, fd)
    os.close(devnull)


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(HELP_TEXT, end="")
        sys.exit(0)

    if "--version" in sys.argv or "-v" in sys.argv:
        print("scroll %s" % __version__)
        sys.exit(0)

    headless = "--headless" in sys.argv

    cfg, cfg_path = load_config()

    try:
        import pwd
        _unix_user = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        _unix_user = "scrolluser"

    nick     = cfg.get("nick",     _unix_user)
    realname = cfg.get("realname", nick)
    ident    = cfg.get("ident",    nick)
    servers  = cfg.get("servers",  [])

    if not servers:
        # Start without a connection; user can /connect manually.
        host = ""
        port = 6667
        name = "server"
    else:
        server = servers[0]
        host   = server.get("host", "irc.libera.chat")
        port   = int(server.get("port", 6667))
        name   = server.get("name", host)

    from .irc import IRCClient
    from .tui import ScrollTUI

    tui = ScrollTUI()
    irc = IRCClient(host, port, nick, ident, realname)
    # Wire the initial client into the placeholder server buffer
    tui.add_server(irc, name if name else "server")
    irc.handlers.append(lambda msg, c=irc: tui.handle_irc(msg, c))

    register_commands(tui, irc, cfg)

    from . import script as _script
    _script._setup(irc, tui)
    load_scripts(cfg.get("scripts_directory"), tui)

    def connect():
        tui.server_msg("Connecting to %s (%s:%d) as %s …" % (name, host, port, nick),
                       client=irc)
        try:
            irc.connect()
        except Exception as e:
            tui.server_msg("Connection failed: %s" % e, client=irc)

    # Graceful Ctrl+C / SIGINT — disconnect all clients
    _headless_running = [True]

    def handle_sigint(sig, frame):
        _headless_running[0] = False
        seen = set()
        for buf in tui.buffers:
            if buf.irc and id(buf.irc) not in seen:
                seen.add(id(buf.irc))
                try:
                    buf.irc.disconnect("scroll")
                except Exception:
                    pass
        if tui._window:
            tui._window.running = False

    signal.signal(signal.SIGINT,  handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    if headless:
        tui.server_msg = lambda *a, **kw: None
        if host:
            connect()
        daemonize()
        while _headless_running[0]:
            for buf in list(tui.buffers):
                if buf.is_server and buf.irc and buf.irc.connected:
                    buf.irc.poll()
            time.sleep(0.05)
        return

    win = tui.build_window()
    patch_alt_keys(win, tui)

    # Wire IRC poll + topic refresh into the cycle; poll every connected client.
    # Patch win.draw so overlays (nick menu) are composited after the base layout
    # but before process_input()'s getch() triggers the terminal refresh.
    original_draw = win.draw

    def patched_draw():
        original_draw()
        tui.draw_overlays()

    win.draw = patched_draw

    original_cycle = win.cycle  # cycle() calls self.draw() = patched_draw above

    def patched_cycle():
        for buf in list(tui.buffers):
            if buf.is_server and buf.irc and buf.irc.connected:
                buf.irc.poll()
        tui.refresh_topic()
        tui.refresh_side_panels()
        tui._focus_at_cycle_start = tui._focus
        original_cycle()

    win.cycle = patched_cycle

    if host:
        connect()
    else:
        tui.server_msg("No servers in config.  Use /connect <host> [port] to connect.")
    win.start()


if __name__ == "__main__":
    main()
