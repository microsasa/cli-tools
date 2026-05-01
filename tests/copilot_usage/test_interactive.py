"""Tests for copilot_usage.interactive — write_prompt and draw_home contracts."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from copilot_usage.interactive import draw_home, write_prompt


# ---------------------------------------------------------------------------
# write_prompt
# ---------------------------------------------------------------------------


def test_write_prompt_writes_to_stdout_no_newline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """write_prompt writes the prompt string exactly — no trailing newline."""
    write_prompt("Enter session #: ")
    out, _ = capsys.readouterr()
    assert out == "Enter session #: "
    assert "\n" not in out


def test_write_prompt_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    """write_prompt calls sys.stdout.flush() so prompt appears immediately."""
    flushed: list[bool] = []

    class _FakeStdout:
        """Fake stdout that records flush() calls."""

        def write(self, s: str) -> None: ...

        def flush(self) -> None:
            flushed.append(True)

    monkeypatch.setattr(
        "copilot_usage.interactive.sys.stdout", _FakeStdout()
    )
    write_prompt("prompt> ")
    assert flushed == [True]


# ---------------------------------------------------------------------------
# draw_home
# ---------------------------------------------------------------------------


def test_draw_home_calls_clear_before_any_output() -> None:
    """console.clear() must be the first call so the previous screen is erased
    before new content is rendered (prevents flash)."""
    calls: list[str] = []
    mock_console = MagicMock()
    mock_console.clear.side_effect = lambda: calls.append("clear")
    mock_console.print.side_effect = lambda *a, **kw: calls.append("print")

    with patch(
        "copilot_usage.interactive.print_version_header",
        lambda c: calls.append("header"),
    ):
        with patch(
            "copilot_usage.interactive.render_full_summary",
            lambda s, **kw: calls.append("summary"),
        ):
            with patch(
                "copilot_usage.interactive.render_session_list",
                lambda c, s: calls.append("list"),
            ):
                draw_home(mock_console, [])

    assert calls[0] == "clear", f"Expected clear() first; got {calls}"
    assert calls == ["clear", "header", "summary", "print", "list"]
