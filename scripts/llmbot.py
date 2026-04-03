# scroll script — ollama LLM bot
#
# Configuration:
#
ENABLED = False                  # ← must be True to activate
SERVER  = "irc.example.net"      # hostname of the target server (must match exactly)
CHANNEL = "#mychan"              # channel to join and watch
MODEL   = "ministral-3:3b"       # ollama model name  (ollama list to confirm)
#
# Usage: the bot replies when addressed as  "botnick: your question"
#        or                                 "botnick, your question"
#
# Requires: pip install ollama httpx duckduckgo-search

import re
import threading
import httpx
import ollama
from ddgs import DDGS

from scroll.script import on, tui, echo

import datetime as _dt
_today = _dt.date.today().strftime("%Y-%m-%d")

_SYSTEM_PROMPT = (
    "You are a helpful IRC bot. You have tools to search the web and fetch pages. "
    "Your knowledge cutoff is 2024; today's date is %s. "
    "Your final response MUST be a single line of plain text — no newlines, "
    "no markdown, no bullet points, no formatting codes. "
    "Be concise; your entire reply will be sent as one IRC message."
) % _today

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web with DuckDuckGo and return the top results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Fetch the text content of a web page by URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                },
                "required": ["url"],
            },
        },
    },
]

_server = None   # _ServerHandle captured on connect


# ── connection ────────────────────────────────────────────────────────────────

@on("connect")
def _on_connect(e):
    global _server
    if not ENABLED:
        return
    if e.server.host == SERVER:
        _server = e.server
        _server.join(CHANNEL)


# ── message handler ───────────────────────────────────────────────────────────

@on("privmsg")
def _on_privmsg(e):
    if not ENABLED or _server is None:
        return
    if e.target != CHANNEL:
        return

    my_nick = _server.nick
    text    = e.text.strip()

    for sep in (":", ","):
        prefix = my_nick + sep
        if text.lower().startswith(prefix.lower()):
            prompt = text[len(prefix):].strip()
            break
    else:
        return

    if not prompt:
        return

    threading.Thread(target=_reply, args=(prompt,), daemon=True).start()


# ── inference loop ────────────────────────────────────────────────────────────

def _reply(prompt):
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": prompt},
    ]
    try:
        for _ in range(5):   # max tool-call rounds before giving up
            resp = ollama.chat(model=MODEL, messages=messages, tools=_TOOLS)
            msg  = resp.message

            if not msg.tool_calls:
                text = msg.content.strip()
                break

            messages.append(msg)

            for tc in msg.tool_calls:
                name   = tc.function.name
                args   = tc.function.arguments or {}
                result = _call_tool(name, args)
                echo(CHANNEL, "        \x037%s(%s) → %d chars\x03" % (name, args, len(result)))
                messages.append({"role": "tool", "content": result})
        else:
            text = "(hit tool-call limit without a final answer)"

    except Exception as exc:
        tui.server_msg("llmbot: inference error — %s" % exc)
        return

    # Flatten to one line even if the model ignored the system prompt.
    text = " ".join(text.split())

    # Trim to fit in one IRC line (512 bytes wire including \r\n).
    # Client sends: PRIVMSG #channel :text\r\n
    max_bytes = 512 - len(("PRIVMSG %s :" % CHANNEL).encode()) - 2
    encoded   = text.encode("utf-8")
    if len(encoded) > max_bytes:
        encoded = encoded[:max_bytes - 3] + b"..."
        text    = encoded.decode("utf-8", errors="ignore")

    echo(CHANNEL, "\x030<%s>\x03 \x0311%s\x03" % (_server.nick, text))
    _server.privmsg(CHANNEL, text)


# ── tools ─────────────────────────────────────────────────────────────────────

def _call_tool(name, args):
    try:
        if name == "web_search":
            return _web_search(args["query"])
        if name == "fetch_page":
            return _fetch_page(args["url"])
        return "unknown tool: %s" % name
    except Exception as exc:
        return "error: %s" % exc


def _web_search(query, max_results=5):
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
    lines = [
        "%s — %s — %s" % (r.get("title", ""), r.get("href", ""), r.get("body", ""))
        for r in results
    ]
    return "\n".join(lines)[:3000]


def _fetch_page(url):
    with httpx.Client(timeout=10, follow_redirects=True) as client:
        resp = client.get(url, headers={"User-Agent": "scroll-llmbot/1.0"})
    return _strip_html(resp.text)[:3000]


def _strip_html(html):
    html = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', ' ', html,
                  flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s+', ' ', html).strip()
