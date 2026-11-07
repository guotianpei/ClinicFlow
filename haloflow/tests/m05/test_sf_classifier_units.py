"""L-1S package S-F — reply classifier and adapter, unit rows (no database).

Governing: S-F package v1 (Work Session 2026-11-07), owner Q-SF1 (2026-11-07 10:58 AM
ET: the platform reads replies with the demonstrator's Q-B3 rules, unchanged), Codex
ALIGN for tests 20261107T160125Z. Every S-F import is deferred into the row body
(class-B red rule).

Pinned API (`haloflow.m05.reply_classifier`):
- `classify(text) -> str` in {confirm, reschedule, cancel, stop, unclear}: exact
  `type(text) is str` (else `ReplyRefused("TEXT_INVALID")`); `len(text) > MAX_TEXT`
  (1600) -> unclear, checked BEFORE normalisation; then Python `text.strip().upper()`
  and an exact whole-string lookup in `REPLY_TOKENS` (read-only); else unclear.
- `InboundMessage(channel, provider_message_id, phone, text)` frozen; `text` not in repr.
- `ReplyIntake(intake).receive(context, message)`: classify, then
  `intake.register_inbound(context, InboundCommand(channel, provider_message_id, phone,
  reply_class))`, returning its result object unchanged. Invalid text -> ReplyRefused
  before any intake call. Logs nothing.

The 1600-character policy is a platform addition; map parity with the demonstrator
(SF-U05) is about the token map only.
"""

from __future__ import annotations

import ast
import logging
import pathlib
from typing import Any

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "haloflow"
MARKER = "ZQX-sf-raw-marker-7731"


def _mod() -> Any:
    import haloflow.m05.reply_classifier as module

    return module


TOKENS = {"1": "confirm", "YES": "confirm", "Y": "confirm", "2": "reschedule",
          "3": "cancel", "CANCEL": "cancel", "STOP": "stop", "STOPALL": "stop",
          "UNSUBSCRIBE": "stop", "END": "stop", "QUIT": "stop"}


def _variants(token: str) -> list[str]:
    return [token, token.lower(), token.capitalize(), f"  {token}  ", f"\t{token}\n",
            f"\r\n {token.lower()} \t"]


@pytest.mark.parametrize("token", sorted(TOKENS))
def test_sf_u01_token_table(token: str) -> None:
    module = _mod()
    for text in _variants(token):
        assert module.classify(text) == TOKENS[token], repr(text)


UNCLEAR = ["NO", "no", "START", "", "   ", "\n\t", "YES PLEASE", "STOP NOW", "YES.", "1 2",
           "12", "Y E S", "１", "Ｙ", "stop!", "cancel please", "0", "4",
           "STOP" + " " * 2000 + "x", "STOP" + "x" * 1597]


@pytest.mark.parametrize("text", UNCLEAR, ids=range(len(UNCLEAR)))
def test_sf_u02_unclear_table(text: str) -> None:
    assert _mod().classify(text) == "unclear"


def test_sf_u02b_python_unicode_case_control() -> None:
    """No new normalisation: Python str.upper() maps U+017F (long s) to S, as the
    demonstrator does; a full-width letter is not upper-cased into ASCII."""

    module = _mod()
    assert module.classify("ſtop") == "stop"
    assert module.classify("yeſ") == "confirm"
    assert module.classify("ｓｔｏｐ") == "unclear"


def test_sf_u02c_unicode_whitespace_is_stripped() -> None:
    """Python str.strip() removes Unicode whitespace (NBSP, em space, ideographic
    space, line/paragraph separators) — the demonstrator's behaviour, kept."""

    module = _mod()
    assert module.classify("\u00a0YES\u2003") == "confirm"
    assert module.classify("\u3000stop\u2028") == "stop"
    assert module.classify("\u2029 2 \u00a0") == "reschedule"
    assert module.classify("\u200bYES") == "unclear"  # zero-width space is not whitespace


def test_sf_u03_length_boundary() -> None:
    module = _mod()
    assert module.MAX_TEXT == 1600
    at_limit = " " * 1598 + "Y" + " "
    assert len(at_limit) == 1600 and module.classify(at_limit) == "confirm"
    over = " " * 1599 + "Y" + " "
    assert len(over) == 1601 and module.classify(over) == "unclear"
    assert module.classify("STOP" + " " * 1597) == "unclear"  # 1601: never truncated


def test_sf_u04_closed_vocabulary_and_read_only_map() -> None:
    module = _mod()
    classes = {"confirm", "reschedule", "cancel", "stop", "unclear"}
    assert dict(module.REPLY_TOKENS) == TOKENS
    assert set(module.REPLY_TOKENS.values()) | {"unclear"} == classes
    with pytest.raises(TypeError):
        module.REPLY_TOKENS["MAYBE"] = "confirm"
    for text in [*TOKENS, *UNCLEAR, "ſtop"]:
        assert module.classify(text) in classes


def _demo_classes() -> dict[str, str]:
    """Read the demonstrator's `_CLASSES` literal by AST (no import: the demonstrator's
    dependencies are outside the locked CI environment)."""

    path = SRC.parents[1] / "demo" / "haloflow_demo" / "inbound.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "_CLASSES"):
            assert isinstance(node.value, ast.Dict)
            out: dict[str, str] = {}
            for key, value in zip(node.value.keys, node.value.values, strict=True):
                assert isinstance(key, ast.Constant) and isinstance(value, ast.Attribute)
                out[str(key.value)] = value.attr
            return out
    raise AssertionError("demonstrator _CLASSES not found")


def test_sf_u05_map_parity_with_demonstrator() -> None:
    """Q-SF1: the platform map equals the demonstrator's `_CLASSES` (token map only;
    the 1600-character policy is platform-only)."""

    names = {"CONFIRM": "confirm", "RESCHEDULE": "reschedule", "CANCEL_REQUEST": "cancel",
             "OPT_OUT": "stop"}
    demo_map = {token: names[cls] for token, cls in _demo_classes().items()}
    assert demo_map == TOKENS  # the demonstrator still holds the approved rules
    assert dict(_mod().REPLY_TOKENS) == demo_map


class _Str(str):
    pass


@pytest.mark.parametrize("bad", [None, b"YES", 1, ["YES"], _Str("YES")],
                         ids=["none", "bytes", "int", "list", "str-subclass"])
def test_sf_u06_text_refused(bad: Any) -> None:
    module = _mod()
    with pytest.raises(module.ReplyRefused) as caught:
        module.classify(bad)
    assert caught.value.code == "TEXT_INVALID"
    assert caught.value.args == ("TEXT_INVALID",)


def test_sf_u06b_message_repr_hides_text() -> None:
    module = _mod()
    message = module.InboundMessage(channel="sms", provider_message_id="m-1",
                                    phone="+15555550123", text=f"call me {MARKER}")
    assert MARKER not in repr(message) and MARKER not in str(message)
    assert message.text.endswith(MARKER)


# --- adapter spy ------------------------------------------------------------------------------


class _SpyIntake:
    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.result = result if result is not None else object()
        self.error = error

    async def register_inbound(self, context: Any, command: Any) -> Any:
        self.calls.append((context, command))
        if self.error is not None:
            raise self.error
        return self.result


async def test_sf_u07_adapter_passes_context_metadata_and_result(
        caplog: pytest.LogCaptureFixture) -> None:
    from haloflow.m05.intake import InboundCommand

    module = _mod()
    spy = _SpyIntake()
    context = object()
    adapter = module.ReplyIntake(spy)  # type: ignore[arg-type]
    message = module.InboundMessage(channel="sms", provider_message_id="pm-77",
                                    phone="+15555550123", text=f"  maybe {MARKER} ")
    with caplog.at_level(logging.DEBUG):
        result = await adapter.receive(context, message)
    assert result is spy.result
    ((seen_context, command),) = spy.calls
    assert seen_context is context
    assert command == InboundCommand(channel="sms", provider_message_id="pm-77",
                                     phone="+15555550123", reply_class="unclear")
    assert all(MARKER not in r.getMessage() for r in caplog.records)
    assert MARKER not in repr(command)


async def test_sf_u07b_invalid_text_never_reaches_intake() -> None:
    module = _mod()
    spy = _SpyIntake()
    adapter = module.ReplyIntake(spy)  # type: ignore[arg-type]
    message = module.InboundMessage(channel="sms", provider_message_id="pm-78",
                                    phone="+15555550123", text=None)  # type: ignore[arg-type]
    with pytest.raises(module.ReplyRefused) as caught:
        await adapter.receive(object(), message)
    assert caught.value.code == "TEXT_INVALID"
    assert spy.calls == []


async def test_sf_u07c_intake_error_propagates_without_text() -> None:
    from haloflow.m05.intake import IntakeFailed

    module = _mod()
    failure = IntakeFailed("INBOUND_CONFLICT")
    spy = _SpyIntake(error=failure)
    adapter = module.ReplyIntake(spy)  # type: ignore[arg-type]
    message = module.InboundMessage(channel="sms", provider_message_id="pm-79",
                                    phone="+15555550123", text=f"2 {MARKER}")
    with pytest.raises(IntakeFailed) as caught:
        await adapter.receive(object(), message)
    assert caught.value is failure
    assert MARKER not in str(caught.value) and MARKER not in repr(caught.value.args)


# --- SF-S01 -----------------------------------------------------------------------------------


def test_sf_s01_not_composed() -> None:
    targets = [SRC / "startup.py", SRC / "composition.py", SRC / "main.py",
               *sorted((SRC / "platform").glob("*.py"))]
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                names.add(base)
                names |= {f"{base}.{a.name}" for a in node.names}  # incl. `import x as y`
        assert not any("reply_classifier" in name for name in names), path
    assert pathlib.Path(_mod().__file__).name == "reply_classifier.py"
