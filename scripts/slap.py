from scroll.script import on, irc, echo


@on("command:slap")
def cmd_slap(args):
    """/slap <nick> — slap someone with a large trout."""
    nick = args.strip()
    if not nick:
        from scroll.script import tui
        tui.server_msg("Usage: /slap <nick>")
        return
    irc.raw("PRIVMSG %s :\x01ACTION slaps %s around a bit with a large trout\x01"
            % (irc.current_channel, nick))
    echo(irc.current_channel, "* %s slaps %s around a bit with a large trout" % (irc.nick, nick))
