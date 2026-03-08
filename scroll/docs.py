# -*- coding: utf-8 -*-
"""
Doc file discovery and loading.

Docs are plain .txt files stored in scroll/docs/ (shipped with the package)
and optionally in ~/.scroll/docs/ (user-supplied).  The user directory takes
precedence when names collide.
"""
import os

_PKG_DOCS  = os.path.join(os.path.dirname(__file__), "docs")
_USER_DOCS = os.path.expanduser("~/.scroll/docs")


def list_docs():
    """Return sorted list of available doc names (without .txt extension)."""
    names = set()
    for directory in (_PKG_DOCS, _USER_DOCS):
        try:
            for f in os.listdir(directory):
                if f.endswith(".txt"):
                    names.add(f[:-4])
        except OSError:
            pass
    return sorted(names)


def load_doc(name):
    """
    Return the text of doc *name*, or None if not found.
    User docs directory is checked first.
    """
    for directory in (_USER_DOCS, _PKG_DOCS):
        path = os.path.join(directory, name + ".txt")
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            pass
    return None
