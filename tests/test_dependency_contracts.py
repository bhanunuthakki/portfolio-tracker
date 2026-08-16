from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_snaptrade_sdk_stays_on_the_verified_constructor_contract() -> None:
    """A fresh install must not silently select the incompatible v13 client API."""
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(raw) for raw in project["project"]["dependencies"]]
    snaptrade = next(req for req in requirements if req.name == "snaptrade-python-sdk")

    assert snaptrade.specifier.contains("11.0.196")
    assert not snaptrade.specifier.contains("12.0.0")
    assert not snaptrade.specifier.contains("13.0.2")
