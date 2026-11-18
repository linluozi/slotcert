"""Conservative ``__slots__`` analysis, source rewriting and rollback.

The analyzer only accepts classes whose ``__init__`` is a straight-line list of
literal ``self.<name> = ...`` assignments, whose only base is ``object``, and
which never touch ``__dict__`` or dynamic attributes.  Everything else is
reported as unsafe with a machine-readable reason.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "__pycache__", "tests", "test", "build", "dist", "node_modules"}
BAD_NAMES = {"__getattr__", "__setattr__", "__delattr__", "__dict__"}


@dataclass
class ClassReport:
    name: str
    lineno: int
    slots: tuple[str, ...] = ()
    safe: bool = False
    already_slotted: bool = False
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "lineno": self.lineno,
            "slots": list(self.slots),
            "safe": self.safe,
            "already_slotted": self.already_slotted,
            "reasons": list(self.reasons),
        }


@dataclass
class FileOutcome:
    path: Path
    classes: list[str] = field(default_factory=list)
    slots: dict[str, list[str]] = field(default_factory=dict)
    new_source: str | None = None
    diff: str = ""


def _is_self_attr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _literal_slots(node: ast.ClassDef) -> tuple[str, ...] | None:
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "__slots__":
                    try:
                        value = ast.literal_eval(stmt.value)
                    except (ValueError, SyntaxError):
                        return ()
                    if isinstance(value, str):
                        return (value,)
                    if isinstance(value, (list, tuple)):
                        return tuple(str(v) for v in value)
                    return ()
    return None


def _init_attrs(init: ast.FunctionDef, reasons: list[str]) -> list[str]:
    """Return literal attribute names in assignment order, or add a reason."""
    attrs: list[str] = []
    body = list(init.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call = stmt.value
            func = call.func
            is_super = (
                isinstance(func, ast.Attribute)
                and func.attr == "__init__"
                and isinstance(func.value, ast.Call)
                and isinstance(func.value.func, ast.Name)
                and func.value.func.id == "super"
            )
            if is_super and not call.args and not call.keywords:
                continue
            reasons.append(f"non-trivial call at line {stmt.lineno}")
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if not targets or not all(_is_self_attr(t) for t in targets):
                reasons.append(f"non-self assignment at line {stmt.lineno}")
                continue
            for target in targets:
                name = target.attr  # type: ignore[attr-defined]
                if name in ("__dict__", "__weakref__"):
                    reasons.append(f"reserved attribute {name!r}")
                elif name not in attrs:
                    attrs.append(name)
            continue
        reasons.append(f"complex __init__ statement {type(stmt).__name__} at line {stmt.lineno}")
    return attrs


def analyze_class(node: ast.ClassDef) -> ClassReport:
    reasons: list[str] = []
    existing = _literal_slots(node)
    if existing is not None:
        return ClassReport(node.name, node.lineno, existing, False, True, ("already defines __slots__",))

    if node.decorator_list:
        reasons.append("decorated class")
    if node.keywords:
        reasons.append("class keywords (metaclass or options)")
    bases = [ast.unparse(b) for b in node.bases]
    if any(b != "object" for b in bases):
        reasons.append("inherits from " + ", ".join(bases))

    methods = [s for s in node.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for method in methods:
        if method.name in BAD_NAMES:
            reasons.append(f"defines {method.name}")
    if any(isinstance(s, ast.AsyncFunctionDef) and s.name == "__init__" for s in methods):
        reasons.append("async __init__")

    init = next((s for s in methods if s.name == "__init__" and isinstance(s, ast.FunctionDef)), None)
    if init is None:
        reasons.append("no __init__")

    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "__dict__":
            reasons.append("uses __dict__")
            break
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            first_is_self = bool(sub.args) and (
                _is_self_attr(sub.args[0])
                or (isinstance(sub.args[0], ast.Name) and sub.args[0].id == "self")
            )
            if sub.func.id in ("setattr", "delattr") and first_is_self:
                reasons.append(f"dynamic {sub.func.id}(self, ...)")
            if sub.func.id == "vars" and sub.args and _is_self_attr(sub.args[0]):
                reasons.append("vars(self)")
            if sub.func.id == "getattr" and sub.args and _is_self_attr(sub.args[0]) \
                    and len(sub.args) > 1 and not isinstance(sub.args[1], ast.Constant):
                reasons.append("getattr(self, <dynamic>)")

    attrs = _init_attrs(init, reasons) if init is not None else []
    if init is not None and not attrs and not reasons:
        reasons.append("__init__ assigns no instance attributes")

    return ClassReport(node.name, node.lineno, tuple(attrs), not reasons, False, tuple(dict.fromkeys(reasons)))


def analyze_source(source: str) -> list[ClassReport]:
    tree = ast.parse(source)
    return [analyze_class(node) for node in tree.body if isinstance(node, ast.ClassDef)]


def analyze_path(path: Path) -> list[ClassReport]:
    return analyze_source(Path(path).read_text(encoding="utf-8"))


def _insert_index(node: ast.ClassDef, lines: list[str]) -> int:
    first = node.body[0]
    if first.lineno == node.lineno:
        raise ValueError(f"class {node.name} is a one-line class")
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
            and isinstance(first.value.value, str):
        index = first.end_lineno  # type: ignore[assignment]
        while index < len(lines) and not lines[index].strip():
            index += 1
        return index
    return first.lineno - 1


def rewrite_source(source: str, class_names: set[str] | None = None) -> FileOutcome:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    inserts: list[tuple[int, str, str, list[str]]] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if class_names is not None and node.name not in class_names:
            continue
        report = analyze_class(node)
        if not report.safe:
            continue
        index = _insert_index(node, lines)
        indent = " " * node.body[0].col_offset
        names = list(report.slots) + ["__weakref__"]
        assignment = f"{indent}__slots__ = ({', '.join(repr(n) for n in names)})\n"
        inserts.append((index, node.name, assignment, names))

    if not inserts:
        return FileOutcome(path=Path("<memory>"))
    new_lines = list(lines)
    for index, _name, assignment, _names in sorted(inserts, key=lambda item: item[0], reverse=True):
        new_lines.insert(index, assignment)
    new_source = "".join(new_lines)
    ast.parse(new_source)  # fail closed: never emit unparsable code
    diff = "".join(difflib.unified_diff(
        lines, new_lines, fromfile="a/" + "<source>", tofile="b/" + "<source>", n=3))
    return FileOutcome(
        path=Path("<memory>"),
        classes=[name for _, name, _, _ in sorted(inserts, key=lambda item: item[0])],
        slots={name: names for _, name, _, names in inserts},
        new_source=new_source,
        diff=diff,
    )


def _iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        yield path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patch_tree(root: Path, class_names: set[str] | None = None,
               state_dir: Path | None = None) -> dict:
    """Rewrite safe classes in *root* in place; back up every touched file."""
    root = Path(root).resolve()
    state_dir = Path(state_dir) if state_dir else root / ".slotcert"
    backup_root = state_dir / "backup"
    manifest_path = state_dir / "manifest.json"
    if manifest_path.exists():
        raise RuntimeError(f"{manifest_path} already exists; run rollback before patching again")
    manifest: dict = {
        "root": str(root),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": [],
    }
    for path in _iter_py_files(root):
        before = path.read_text(encoding="utf-8")
        outcome = rewrite_source(before, class_names)
        if not outcome.classes:
            continue
        rel = path.relative_to(root)
        backup = backup_root / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        path.write_text(outcome.new_source, encoding="utf-8")
        manifest["files"].append({
            "path": str(rel),
            "backup": str(backup),
            "sha256_before": hashlib.sha256(before.encode()).hexdigest(),
            "sha256_after": hashlib.sha256(outcome.new_source.encode()).hexdigest(),
            "classes": outcome.classes,
            "slots": outcome.slots,
        })
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def rollback(state_dir: Path) -> dict:
    """Restore every file recorded in the manifest and verify its hash."""
    state_dir = Path(state_dir)
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    root = Path(manifest["root"])
    restored = []
    for entry in manifest["files"]:
        target = root / entry["path"]
        if target.exists() and _sha256(target) != entry["sha256_after"]:
            raise RuntimeError(f"{entry['path']} changed after patching; refusing to overwrite")
        backup = Path(entry["backup"])
        shutil.copy2(backup, target)
        if _sha256(target) != entry["sha256_before"]:
            raise RuntimeError(f"rollback hash mismatch for {entry['path']}")
        restored.append(entry["path"])
    return {"root": str(root), "restored": restored}
