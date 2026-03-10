![scroll](img/scroll2.png)

A terminal IRC client written in Python.

![screenshot](img/scroll_screenshot.png)
![screenshot](img/scroll_screenshot.gif)

```
Usage: scroll [--help] [--version] [--headless]

scroll 0.0.3 — a minimal irssi-inspired IRC client.

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
```

## Scripting

Scripts are Python files placed in `~/.scroll/scripts/`. They are loaded on
startup and can be reloaded at runtime with `/reload`, or individually edited
and reloaded via `/script edit <file.py>`.

There are [15 examples in the scripting reference](https://github.com/LukeB42/Scroll/blob/master/scroll/docs/scripting.txt).

## Headless mode

```
scroll --headless
```

Connects to all configured servers, loads scripts, and daemonizes immediately
with no TUI. Useful for running scroll as a bot. Send SIGTERM to stop it.

## Install

```
pip install .
```

This creates `~/.scroll/` with a default `config.hcl`, a `scripts/` directory
containing the bundled scripts, and a `docs/` directory for user documentation.
Existing files are never overwritten.

Edit `~/.scroll/config.hcl` to set your nick and servers.

For development:

```
pip install -e .
```
