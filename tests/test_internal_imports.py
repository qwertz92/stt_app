"""Every `from .module import name` inside the package names something that exists.

Two branches that each pass can break this together: one moves a name, the
other imports it under the old one. An import inside a function is the
dangerous case, because the module still imports and only the call fails
(2026-11-25: the controller's engine-name helper and the history labels both
imported `_ENGINE_LABELS` after it had moved to `config.ENGINE_LABELS`).
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import stt_app

PACKAGE_ROOT = Path(stt_app.__file__).parent


def _module_name(path: Path, root: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(("stt_app", *parts))


def unresolved_relative_imports(root: Path) -> list[str]:
    """`file:line imports 'name' from module` for every name that is missing."""
    problems = []
    for path in sorted(root.rglob("*.py")):
        module_name = _module_name(path, root)
        is_package = path.name == "__init__.py"
        package = module_name if is_package else module_name.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level == 0:
                continue
            target = importlib.import_module(
                "." * node.level + (node.module or ""), package
            )
            for alias in node.names:
                if alias.name == "*" or hasattr(target, alias.name):
                    continue
                try:
                    importlib.import_module(f"{target.__name__}.{alias.name}")
                except ImportError:
                    problems.append(
                        f"{path.relative_to(root).as_posix()}:{node.lineno} imports "
                        f"{alias.name!r} from {target.__name__}"
                    )
    return problems


def test_every_relative_import_names_something_that_exists() -> None:
    assert unresolved_relative_imports(PACKAGE_ROOT) == []
