"""Mycel — multi-agent orchestrator for Discord-driven rituals.

The package directory is itself named ``mycel``, which would normally make
``from mycel import Mycel`` resolve back to this still-initializing package
instead of the sibling ``mycel.py`` module. We expose ``Mycel`` lazily via
``__getattr__`` and load the file module by absolute path to break the cycle.
"""
from __future__ import annotations

__all__ = ["Mycel"]


def __getattr__(name: str):  # noqa: D401 — module-level lazy attribute
    if name == "Mycel":
        import importlib.util
        import os

        path = os.path.join(os.path.dirname(__file__), "mycel.py")
        spec = importlib.util.spec_from_file_location("_mycel_core", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.Mycel
    raise AttributeError(name)
