from __future__ import annotations

import importlib.util
import sys
import warnings
from pathlib import Path
from types import ModuleType


_CANONICAL_ROOT = Path(__file__).resolve().parents[2] / "python-sdk" / "aztea"
_PKG_NAME = "_aztea_canonical"
_WARNED = False


def warn_once() -> None:
    global _WARNED
    if _WARNED:
        return
    warnings.warn(
        "sdks/python is deprecated; use the canonical SDK in sdks/python-sdk.",
        DeprecationWarning,
        stacklevel=3,
    )
    _WARNED = True


def load_module(module_name: str) -> ModuleType:
    warn_once()
    if _PKG_NAME not in sys.modules:
        package_spec = importlib.util.spec_from_file_location(
            _PKG_NAME,
            _CANONICAL_ROOT / "__init__.py",
            submodule_search_locations=[str(_CANONICAL_ROOT)],
        )
        if package_spec is None or package_spec.loader is None:
            raise ImportError("Cannot load canonical aztea package.")
        package = importlib.util.module_from_spec(package_spec)
        sys.modules[_PKG_NAME] = package
        package_spec.loader.exec_module(package)
    if module_name == "__init__":
        return sys.modules[_PKG_NAME]
    cache_key = f"{_PKG_NAME}.{module_name}"
    cached = sys.modules.get(cache_key)
    if isinstance(cached, ModuleType):
        return cached
    target = _CANONICAL_ROOT / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(cache_key, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load canonical aztea module '{module_name}'.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[cache_key] = module
    spec.loader.exec_module(module)
    return module
