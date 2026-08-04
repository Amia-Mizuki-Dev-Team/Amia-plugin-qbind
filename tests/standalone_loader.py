"""Load the isolated qbind and sibling amia-core as ``src.plugins`` packages."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path


def _install_namespace(core_root: Path) -> None:
    src_package = sys.modules.setdefault("src", types.ModuleType("src"))
    src_package.__path__ = [str(core_root.parent)]
    plugins_package = sys.modules.setdefault(
        "src.plugins", types.ModuleType("src.plugins")
    )
    plugins_package.__path__ = [str(core_root.parent)]


def load_core():
    if "src.plugins.amia_core" in sys.modules:
        return sys.modules["src.plugins.amia_core"]

    root = Path(__file__).resolve().parents[2] / "amia_core"
    if not (root / "__init__.py").is_file():
        raise ModuleNotFoundError("sibling isolated amia-core was not found")
    _install_namespace(root)
    spec = importlib.util.spec_from_file_location(
        "src.plugins.amia_core",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load sibling isolated amia-core")
    module = importlib.util.module_from_spec(spec)
    sys.modules["src.plugins.amia_core"] = module
    spec.loader.exec_module(module)
    return module


def load_qbind():
    if "src.plugins.qbind" in sys.modules:
        return sys.modules["src.plugins.qbind"]

    root = Path(__file__).resolve().parents[1]
    core_root = Path(__file__).resolve().parents[2] / "amia_core"
    _install_namespace(core_root)
    spec = importlib.util.spec_from_file_location(
        "src.plugins.qbind",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load isolated qbind")
    module = importlib.util.module_from_spec(spec)
    sys.modules["src.plugins.qbind"] = module
    spec.loader.exec_module(module)
    return module
