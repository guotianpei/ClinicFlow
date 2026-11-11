"""Option B B1: a static, TRANSITIVE import-closure walker for the production fence (B-C1).

What it resolves, starting from a set of root modules under ``src/``:
- ``import a.b`` / ``from a.b import c`` (absolute and relative), including ``c`` when it is
  itself a submodule, and every parent package ``__init__`` along the way;
- LITERAL dynamic imports: ``importlib.import_module("x")`` (also with a literal package for
  a relative name) and ``__import__("x")``.

Aliases are resolved: ``from importlib import import_module as load`` (or
``builtins.__import__``) followed by a literal ``load("x")`` is followed, including inside
functions that only run later, since the whole module body is walked.

What it does NOT claim to see, and therefore reports as UNRESOLVED (a violation unless
allowlisted):
- a dynamic import whose module argument is not a string literal;
- any ESCAPED reference to an importer or opaque loader, i.e. a reference that is not the
  callee of a call (``loader = __import__``, ``map(importlib.import_module, ...)``);
- opaque loader calls, direct or aliased: ``spec_from_file_location``, ``module_from_spec``,
  ``find_spec``, ``spec_from_loader``, ``reload``, ``runpy`` and the bare
  ``exec``/``eval``/``compile`` builtins;
- any reference to ``path``/``meta_path``/``path_hooks`` on ``sys`` or an alias of ``sys``,
  and any ``from sys import`` of them. Reads and writes are not told apart, so mutation by
  method, assignment, slice, augmented assignment or ``del`` are all covered.

An AST walk cannot prove what those load, so the fence refuses them instead of trusting
them (Codex Option B v1 review B1; fdf00a8 review B1-F1). The runtime row in
``test_b1_units`` complements this.

Stdlib and third-party imports are recorded but not followed.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

DYNAMIC_IMPORT_FUNCS = frozenset({"import_module", "__import__"})
OPAQUE_LOADERS = frozenset({"spec_from_file_location", "module_from_spec", "run_module",
                            "run_path", "exec", "eval", "compile", "load_module",
                            "exec_module", "reload", "find_spec", "spec_from_loader"})
SYS_HOOKS = frozenset({"path", "meta_path", "path_hooks"})
# Only the bare builtins: ``re.compile(...)`` is not a loader.
BUILTIN_ONLY = frozenset({"exec", "eval", "compile"})


@dataclass
class Closure:
    modules: set[str] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)
    edges: dict[str, set[str]] = field(default_factory=dict)

    def path_to(self, target: str, roots: tuple[str, ...]) -> list[str]:
        """One import chain root -> target (for a readable failure)."""

        from collections import deque

        queue = deque([(r, [r]) for r in roots])
        seen: set[str] = set()
        while queue:
            node, chain = queue.popleft()
            if node == target:
                return chain
            if node in seen:
                continue
            seen.add(node)
            for nxt in sorted(self.edges.get(node, ())):
                queue.append((nxt, [*chain, nxt]))
        return []


class Tree:
    """Module name <-> file, for one source root (``src``) and one top package."""

    def __init__(self, src: Path, package: str = "haloflow") -> None:
        self.src = src
        self.package = package

    def file_for(self, module: str) -> Path | None:
        if module != self.package and not module.startswith(self.package + "."):
            return None
        base = self.src.joinpath(*module.split("."))
        if (base / "__init__.py").is_file():
            return base / "__init__.py"
        if base.with_suffix(".py").is_file():
            return base.with_suffix(".py")
        return None

    def is_package(self, module: str) -> bool:
        file = self.file_for(module)
        return file is not None and file.name == "__init__.py"


def _literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _resolve_relative(module: str, is_package: bool, level: int, name: str | None) -> str:
    parts = module.split(".")
    base = parts if is_package else parts[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join([*base, name] if name else base)


def _anchor_relative(name: str, package: str | None) -> str | None:
    if not name.startswith("."):
        return name
    if package is None:
        return None
    level = len(name) - len(name.lstrip("."))
    parts = package.split(".")
    base = parts[: len(parts) - (level - 1)] if level > 1 else parts
    rest = name.lstrip(".")
    return ".".join([*base, rest] if rest else base)


def imports_of(module: str, source: str, tree: Tree) -> tuple[set[str], list[str]]:
    """Return (imported module names, unresolved-dynamic findings) for one module.

    Alias-aware and conservative (Codex B1-F1, fdf00a8 review). The whole module body is
    walked, so a call inside a function that only runs later (a "latent" function) is
    seen like any other.

    - Aliases of the dynamic importers (``from importlib import import_module as load``,
      ``from builtins import __import__ as imp``) are resolved: a literal call through
      the alias is followed, and a non-literal one is unresolved.
    - Any reference to an importer or opaque loader that is NOT the callee of a call
      (``loader = __import__``, ``f(importlib.import_module)``) is an ESCAPED reference
      and unresolved: what it later loads cannot be known statically.
    - Any reference to ``path`` / ``meta_path`` / ``path_hooks`` on ``sys`` or an alias of
      ``sys``, or any ``from sys import`` of them, is unresolved. This covers mutation by
      method, assignment, subscript or slice assignment, augmented assignment and
      deletion, because read and write are not told apart.
    """

    found: set[str] = set()
    unresolved: list[str] = []
    is_pkg = tree.is_package(module)
    parsed = ast.parse(source)

    importers = set(DYNAMIC_IMPORT_FUNCS)  # names bound to an importer in this module
    loaders = {n for n in OPAQUE_LOADERS if n not in BUILTIN_ONLY} | set(BUILTIN_ONLY)
    sys_names = {"sys"}
    for node in ast.walk(parsed):
        if isinstance(node, ast.ImportFrom) and node.module in ("importlib", "builtins"):
            for alias in node.names:
                if alias.name in DYNAMIC_IMPORT_FUNCS:
                    importers.add(alias.asname or alias.name)
                elif alias.name in OPAQUE_LOADERS:
                    loaders.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module in ("runpy", "importlib.util"):
            for alias in node.names:
                if alias.name in OPAQUE_LOADERS:
                    loaders.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "sys":
            for alias in node.names:
                if alias.name in SYS_HOOKS or alias.name == "*":
                    unresolved.append(f"{module}:{node.lineno}: from sys import {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sys" and alias.asname:
                    sys_names.add(alias.asname)

    callees = {id(node.func) for node in ast.walk(parsed) if isinstance(node, ast.Call)}

    for node in ast.walk(parsed):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            target = (_resolve_relative(module, is_pkg, node.level, node.module)
                      if node.level else (node.module or ""))
            found.add(target)
            for alias in node.names:
                if alias.name != "*":
                    found.add(f"{target}.{alias.name}")  # kept only if it is a module
        elif isinstance(node, ast.Call):
            func = node.func
            is_importer = ((isinstance(func, ast.Name) and func.id in importers)
                           or (isinstance(func, ast.Attribute)
                               and func.attr in DYNAMIC_IMPORT_FUNCS))
            if is_importer:
                literal = _literal(node.args[0]) if node.args else None
                package = _literal(node.args[1]) if len(node.args) > 1 else None
                for kw in node.keywords:
                    if kw.arg == "name":
                        literal = _literal(kw.value)
                    elif kw.arg == "package":
                        package = _literal(kw.value)
                anchored = _anchor_relative(literal, package) if literal else None
                if anchored is None:
                    unresolved.append(f"{module}:{node.lineno}: non-literal dynamic import")
                else:
                    found.add(anchored)
            elif ((isinstance(func, ast.Name) and func.id in loaders)
                  or (isinstance(func, ast.Attribute) and func.attr in OPAQUE_LOADERS
                      and func.attr not in BUILTIN_ONLY)):
                unresolved.append(f"{module}:{node.lineno}: opaque loader call")
        elif isinstance(node, ast.Name) and id(node) not in callees and (
                node.id in importers or node.id in loaders):
            unresolved.append(f"{module}:{node.lineno}: escaped loader reference {node.id}")
        elif isinstance(node, ast.Attribute):
            if (isinstance(node.value, ast.Name) and node.value.id in sys_names
                    and node.attr in SYS_HOOKS):
                unresolved.append(f"{module}:{node.lineno}: sys.{node.attr} reference")
            elif id(node) not in callees and (
                    node.attr in DYNAMIC_IMPORT_FUNCS
                    or (node.attr in OPAQUE_LOADERS and node.attr not in BUILTIN_ONLY)):
                unresolved.append(f"{module}:{node.lineno}: escaped loader reference "
                                  f"{node.attr}")
    return found, unresolved


def closure(roots: tuple[str, ...], tree: Tree,
            sources: dict[str, str] | None = None) -> Closure:
    """The transitive in-package closure of ``roots``. ``sources`` overrides file reads
    (self-test fixtures)."""

    result = Closure()
    pending = list(roots)

    def source_of(module: str) -> str | None:
        if sources is not None and module in sources:
            return sources[module]
        file = tree.file_for(module)
        return file.read_text(encoding="utf-8") if file is not None else None

    def exists(module: str) -> bool:
        return (sources is not None and module in sources) or tree.file_for(module) is not None

    while pending:
        module = pending.pop()
        if module in result.modules:
            continue
        source = source_of(module)
        if source is None:
            continue
        result.modules.add(module)
        # Parent packages execute first.
        parts = module.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if exists(parent):
                result.edges.setdefault(module, set()).add(parent)
                pending.append(parent)
        names, unresolved = imports_of(module, source, tree)
        result.unresolved.extend(unresolved)
        for name in names:
            in_package = name == tree.package or name.startswith(tree.package + ".")
            if in_package and exists(name):
                result.edges.setdefault(module, set()).add(name)
                pending.append(name)
    return result
