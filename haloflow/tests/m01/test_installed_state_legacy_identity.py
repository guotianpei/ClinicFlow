"""CP2-2b 2B-U06 (R-B9.6): the legacy function verifier stays byte-unchanged.

Test cases v3 section 3.1, U06. Status before and after the change: EB (passes).

`legacy_span` is the ONE extraction path. It returns the full source lines of a
target, from its first decorator (if any) through `end_lineno`, with line endings
and indentation kept. The pins below were computed on `fa22700f` with exactly
this method (test cases v3, U06 table). They are literals, not derived from the
candidate, and no Git object is needed.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

PROVISIONING = Path("src/haloflow/m01/provisioning")
LEGACY_IF = "unit.verification is not None"

# (file, target, pinned sha256 of the decorator-inclusive full-line span)
PINS: tuple[tuple[str, str, str], ...] = (
    ("verification.py", "AclEntry",
     "44a699ed494b138cffddab4f5e04b5673ba1ffa43b2ef5e6d25a99a79155a4c9"),
    ("verification.py", "FunctionExpectation",
     "adef27d57c9da65ccaef7f56b2c810165ecc232ed5e2d4c08d7d558485397385"),
    ("verification.py", "FunctionMetadataVerification",
     "74f0fcc09dd0a0ea520cf730f7adc67871147b260c8b725356317abc6bdf8047"),
    ("verification.py", "FUNCTION_METADATA_QUERY",
     "93945c74f620d6f9c262b1fae20dd8b9266a57bbadffbdd2ea48b882a85c9eb4"),
    ("verification.py", "VerificationMismatch",
     "fa5df7662cdfd33657b387c1e2f44723af5c6a8f29f20c3dbbb319e726c37dd9"),
    ("verification.py", "compare_function_metadata",
     "7b892adf57e5edea097e9c5456f4f280680295a1a490eb677f5c5796c66acc2e"),
    ("checksum.py", "unit_payload",
     "b851d78e1dd04a81447e8134c8f817706c8a2c62d0ae779b9ae13b6f39e30744"),
    ("checksum.py", "canonical_json",
     "ac89e536d6d78f8c6f5da202197bd27c5d01d330d1ab0e3a60c9cb1c5ccc8307"),
    ("checksum.py", "digest",
     "b0a1801df44dd1c3c07de851d1a1736685fbecdad393204aa1c84588b7b2f605"),
    ("runner.py", LEGACY_IF,
     "2247ca40763d314792511b50983b56f10b3dc7ddc9aee8d371a8b9fae9eb0a66"),
)
QUERY_VALUE_SHA256 = "557928c92a5c1cc12ad4447d02883d279361f73d9b3e21a34b3103329f65f5c1"
DECORATED = ("AclEntry", "FunctionExpectation", "FunctionMetadataVerification")


class TargetNotFound(AssertionError):
    """A pinned target is missing or ambiguous: a failure, never a skip."""


def _top_level_name(node: ast.stmt) -> str | None:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return node.name
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def legacy_span(path: Path, target: str) -> str:
    """Full source lines of `target` in `path`, decorators included. Unique or it raises."""

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    if target == LEGACY_IF:
        found: list[ast.stmt] = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.If) and ast.unparse(node.test) == LEGACY_IF
        ]
    else:
        found = [node for node in tree.body if _top_level_name(node) == target]
    if len(found) != 1:
        raise TargetNotFound(f"{path.name}:{target}: found {len(found)}")
    node = found[0]
    decorators = getattr(node, "decorator_list", [])
    start = min([node.lineno, *(d.lineno for d in decorators)])
    end = node.end_lineno
    assert end is not None
    lines = source.splitlines(keepends=True)
    return "".join(lines[start - 1 : end])


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(("filename", "target", "pinned"), PINS, ids=[p[1] for p in PINS])
def test_2b_u06_legacy_span_is_byte_identical_to_the_baseline(
    filename: str, target: str, pinned: str
) -> None:
    assert sha256(legacy_span(PROVISIONING / filename, target)) == pinned


def test_2b_u06_the_legacy_query_value_is_byte_identical_to_the_baseline() -> None:
    from haloflow.m01.provisioning.verification import FUNCTION_METADATA_QUERY

    assert sha256(FUNCTION_METADATA_QUERY) == QUERY_VALUE_SHA256


# --- alteration controls: the REAL extraction path over an EDITED source file ---


def _copy(tmp_path: Path, filename: str) -> Path:
    destination = tmp_path / filename
    destination.write_text((PROVISIONING / filename).read_text(encoding="utf-8"), "utf-8")
    return destination


def _flip_one_letter_inside(copy: Path, span: str, *, offset: int) -> None:
    """Swap exactly one ASCII letter inside `span` for another letter.

    Letter-for-letter keeps the file parseable (an identifier, keyword-free word or
    string character changes), so the control reaches the hash comparison instead
    of failing earlier on syntax. `span` must occur once in the file.
    """

    text = copy.read_text(encoding="utf-8")
    assert text.count(span) == 1
    index = text.index(span) + offset
    assert text[index].isascii() and text[index].isalpha(), repr(text[index])
    replacement = "q" if text[index] != "q" else "z"
    copy.write_text(text[:index] + replacement + text[index + 1 :], "utf-8")


def _last_letter(span: str) -> int:
    return max(i for i, ch in enumerate(span) if ch.isascii() and ch.isalpha())


@pytest.mark.parametrize(("filename", "target", "pinned"), PINS, ids=[p[1] for p in PINS])
def test_2b_u06_control_a_a_body_byte_change_is_detected(
    tmp_path: Path, filename: str, target: str, pinned: str
) -> None:
    copy = _copy(tmp_path, filename)
    span = legacy_span(copy, target)
    _flip_one_letter_inside(copy, span, offset=_last_letter(span))
    assert sha256(legacy_span(copy, target)) != pinned


@pytest.mark.parametrize("target", DECORATED)
def test_2b_u06_control_b_a_decorator_byte_change_is_detected(
    tmp_path: Path, target: str
) -> None:
    copy = _copy(tmp_path, "verification.py")
    span = legacy_span(copy, target)
    assert span.lstrip().startswith("@"), "the pinned span must begin at its decorator"
    _flip_one_letter_inside(copy, span, offset=span.index("@") + 1)
    pinned = dict((t, p) for _, t, p in PINS)[target]
    assert sha256(legacy_span(copy, target)) != pinned


@pytest.mark.parametrize("filename", ["verification.py", "checksum.py", "runner.py"])
def test_2b_u06_control_c_a_change_outside_every_span_leaves_every_pin(
    tmp_path: Path, filename: str
) -> None:
    copy = _copy(tmp_path, filename)
    copy.write_text("# out-of-span specificity control\n" + copy.read_text("utf-8"), "utf-8")
    for pinned_file, target, pinned in PINS:
        if pinned_file == filename:
            assert sha256(legacy_span(copy, target)) == pinned, target


@pytest.mark.parametrize(("filename", "target", "pinned"), PINS, ids=[p[1] for p in PINS])
def test_2b_u06_control_d_a_removed_target_raises(
    tmp_path: Path, filename: str, target: str, pinned: str
) -> None:
    copy = _copy(tmp_path, filename)
    span = legacy_span(copy, target)
    text = copy.read_text(encoding="utf-8")
    copy.write_text(text.replace(span, "", 1), "utf-8")
    with pytest.raises(TargetNotFound):
        legacy_span(copy, target)
