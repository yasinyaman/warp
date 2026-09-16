"""The package version matches the installed distribution metadata."""

import importlib.metadata

import pytest

import warp


def test_version_matches_distribution() -> None:
    try:
        installed = importlib.metadata.version("warp-engine")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("warp-engine is not installed in this environment")
    assert warp.__version__ == installed


def test_version_is_pre_1_0() -> None:
    assert warp.__version__.startswith("0.")
