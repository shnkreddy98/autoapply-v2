"""common.termui: color only on a terminal, byte-identical builtin output otherwise.
Streams are fakes, so this needs no tty, no DB and no network."""

import io
import logging

import pytest

from common import termui


class _Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    termui._consoles.clear()
    yield
    termui._consoles.clear()


def test_piped_output_is_exactly_the_builtin_print():
    buf = io.StringIO()
    termui.print("b4b614f4  ready", "score 72 -> 88", sep="  ", end="!\n", file=buf)
    assert buf.getvalue() == "b4b614f4  ready  score 72 -> 88!\n"


def test_terminal_output_is_highlighted_but_says_the_same_thing():
    tty = _Tty()
    termui.print("b4b614f4  failed  https://ex.dev/j/1  score 38 -> 64", file=tty)
    out = tty.getvalue()
    assert "\x1b[" in out
    assert "\x1b[1;35mb4b614f4" in out          # job id
    assert "\x1b[1;31mfailed" in out            # bad status
    assert "\x1b[1;36m38 -> 64" in out          # score
    import re

    assert re.sub(r"\x1b\[[0-9;]*m", "", out) == "b4b614f4  failed  https://ex.dev/j/1  score 38 -> 64\n"


def test_brackets_are_text_not_markup():
    tty = _Tty()
    termui.print("[3/100 words | base: swe]", file=tty)
    assert "[3/100 words | base: swe]" in tty.getvalue()


def test_no_color_turns_it_off_on_a_terminal(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    tty = _Tty()
    termui.print("ready", file=tty)
    assert tty.getvalue() == "ready\n"


def test_long_lines_are_never_wrapped():
    tty = _Tty()
    long = "x" * 500
    termui.print(long, file=tty)
    assert "\n" not in tty.getvalue().rstrip("\n")


def test_error_goes_to_stderr_with_a_prefix(capsys):
    termui.error("no job matching 'zz'")
    assert capsys.readouterr().err == "error: no job matching 'zz'\n"


def test_prompt_without_a_terminal_is_plain_input(monkeypatch):
    seen = {}
    monkeypatch.setattr("builtins.input", lambda text="": seen.setdefault("text", text) or "y")
    assert termui.prompt("  did you submit? [y/N]: ") == "  did you submit? [y/N]: "
    assert seen["text"] == "  did you submit? [y/N]: "


def test_log_handler_is_plain_off_a_terminal_and_rich_on_one():
    from rich.logging import RichHandler

    plain = termui.log_handler(io.StringIO())
    assert type(plain) is logging.StreamHandler
    assert isinstance(termui.log_handler(_Tty()), RichHandler)


def test_rich_handler_keeps_the_given_formatter():
    tty = _Tty()

    class Upper(logging.Formatter):
        def format(self, record):
            return super().format(record).upper()

    h = termui.log_handler(tty, fmt=Upper("%(message)s"))
    log = logging.getLogger("termui-test")
    log.handlers[:] = [h]
    log.propagate = False
    log.warning("secret=abc")
    import re

    assert "SECRET=ABC" in re.sub(r"\x1b\[[0-9;]*m", "", tty.getvalue())
