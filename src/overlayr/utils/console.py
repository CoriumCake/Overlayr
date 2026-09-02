"""Keep logging from killing the pipeline.

The app reads and prints text in whatever language the user pointed it at, but
a Windows console is usually cp1252, which cannot encode most of them. A single
`print` of a Japanese line - or of a stray glyph the neural reader produced -
raises UnicodeEncodeError from inside whatever was running at the time, and a
debug line takes down the frame it was describing.

CLAUDE.md section 10 asks for a log line rather than a swallowed exception.
That only holds if logging cannot itself throw.
"""
from __future__ import annotations

import sys


def make_output_safe() -> None:
    """Replace unencodable characters in console output instead of raising.

    Call once from an entry point. Streams that do not support reconfiguring -
    a pipe, or the absent console of a --noconsole build - are left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError) as e:
            print(f"Could not make console output safe: {type(e).__name__}: {e}")
