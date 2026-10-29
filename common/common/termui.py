"""Terminal styling shared by the CLIs: colored output, log handlers and prompts.

Color only ever reaches a terminal. Anything piped or captured (`autoapply ask ... |
pbcopy`, `profile export > p.json`, pytest's capsys, the JSON line ask/cover read back
from ai-assist) goes through the builtin print untouched, so scripts and tests see
byte-identical text. NO_COLOR (https://no-color.org) turns it off on a terminal too.

Styling is by pattern, not by call site: `print` here runs the line through a
highlighter that colors job ids, pipeline statuses, scores, URLs, warnings and section
headings. Existing output gets colors by swapping the import, without restyling every
message one by one.

`rich` is imported lazily and is optional: common itself does not depend on it, so
packages that never print (api) do not pull it in. Without rich everything stays plain.
"""

from __future__ import annotations

import builtins
import logging
import os
import sys

_PLAIN_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

# Highlighter rules, in order; a later match restyles an earlier one where they overlap.
_HIGHLIGHTS = [
    # leading 8-hex job id column (`jobs list`, `status`) and "job <uuid>" lines
    r"^\s*(?P<jobid>[0-9a-f]{8})\b",
    r"(?P<jobid>\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b)",
    r"\b(?P<good>jd_ready|ready|submitted|captured|connected|parsed|added|converted)\b",
    # "tailoring:" is the summary line's label, not a status
    r"\b(?P<pending>jd_pending|jd_capturing|pending|tailoring(?!:)|duplicate\(s\)|skipped)\b",
    r"\b(?P<bad>failed|error|refused|blocked|not yours|not jd_ready)\b",
    # "score 38 -> 64", "60 -> 80"
    r"(?P<score>\b\d+ -> \d+\b)",
    r"(?P<url>\bhttps?://[^\s)\]]+)",
    r"(?P<heading>^\s*-- .+ --\s*$)",
    r"(?P<warn>^\s*(?:warning:|\(!\)|!) .*$)",
    r"(?P<error>^\s*(?:error:|[a-z]+ failed:?) .*$)",
    # `resume bases` default-role marker
    r"^(?P<mark>\*) ",
]

_THEME = {
    "aa.jobid": "bold magenta",
    "aa.good": "green",
    "aa.pending": "yellow",
    "aa.bad": "bold red",
    "aa.score": "bold cyan",
    "aa.url": "cyan underline",
    "aa.heading": "bold blue",
    "aa.warn": "yellow",
    "aa.error": "bold red",
    "aa.mark": "bold green",
}

_consoles: dict[int, object] = {}


def color_enabled(stream=None) -> bool:
    """True when `stream` (default stdout) is a terminal, NO_COLOR is unset, TERM is not
    "dumb", and rich is installed."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):  # closed or fake streams
        return False
    try:
        import rich  # noqa: F401
    except ImportError:
        return False
    return True


def console(stream=None):
    """A rich Console bound to `stream`, or None when that stream gets no color.
    Cached per stream: pytest and redirects swap sys.stdout, so the stream object
    (not "stdout") is the key."""
    stream = stream or sys.stdout
    if not color_enabled(stream):
        return None
    key = id(stream)
    con = _consoles.get(key)
    if con is None:
        from rich.console import Console
        from rich.highlighter import RegexHighlighter
        from rich.theme import Theme

        class _Highlighter(RegexHighlighter):
            base_style = "aa."
            highlights = _HIGHLIGHTS

        con = Console(
            file=stream,
            theme=Theme(_THEME),
            highlighter=_Highlighter(),
            # soft_wrap: never insert line breaks into long URLs or JSON
            soft_wrap=True,
        )
        _consoles[key] = con
    return con


def print(*args, sep: str = " ", end: str = "\n", file=None, flush: bool = False) -> None:  # noqa: A001
    """Drop-in for the builtin print: highlighted on a terminal, builtin otherwise."""
    stream = file or sys.stdout
    con = console(stream)
    if con is None:
        builtins.print(*args, sep=sep, end=end, file=file, flush=flush)
        return
    # markup/emoji off: messages carry literal [brackets] and :colons: of their own
    con.print(sep.join(str(a) for a in args), end=end, markup=False, emoji=False)
    if flush:
        stream.flush()


def prompt(text: str) -> str:
    """Drop-in for input(): the label is styled on a terminal, a "[default]" in it is
    dimmed. Reads with the builtin input either way, so EOF and readline behave the
    same."""
    con = console(sys.stdout)
    if con is None:
        return builtins.input(text)
    from rich.text import Text

    styled = Text(text, style="bold cyan")
    styled.highlight_regex(r"\[[^\]]*\]\s*:?\s*$", "dim")
    con.print(styled, end="")
    return builtins.input()


def error(message: str) -> None:
    """`error: <message>` on stderr, red on a terminal."""
    print(f"error: {message}", file=sys.stderr)


def log_handler(stream=None, fmt: logging.Formatter | None = None) -> logging.Handler:
    """A console log handler: rich (colored level, time column) on a terminal, a plain
    StreamHandler otherwise. `fmt` formats the message part on both paths, so a
    scrubbing formatter keeps scrubbing; the plain path also gets its timestamp/level
    from it, the rich path draws those as columns."""
    stream = stream or sys.stderr
    con = console(stream)
    if con is None:
        handler: logging.Handler = logging.StreamHandler(stream)
        handler.setFormatter(fmt or logging.Formatter(_PLAIN_LOG_FORMAT, "%H:%M:%S"))
        return handler
    from rich.logging import RichHandler

    handler = RichHandler(
        console=con,
        show_path=False,
        markup=False,
        rich_tracebacks=True,
        log_time_format="%H:%M:%S",
    )
    if fmt is not None:
        handler.setFormatter(fmt)
    return handler


def setup_logging(level: str | int | None = None) -> None:
    """Root logging for a CLI process: one console handler (see log_handler) at
    LOG_LEVEL (default INFO), with the chatty HTTP client loggers held at WARNING."""
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(log_handler())
    root.setLevel(level or os.getenv("LOG_LEVEL", "INFO").upper())
    for noisy in ("httpx", "httpcore", "urllib3", "botocore", "boto3", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
