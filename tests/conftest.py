"""The shared fakes (from fakes.py, where they live without pytest) and fixtures."""

import pytest
from fakes import (
    ACCOUNT,
    BERLIN,
    SMALL_USAGE,
    Clock,
    FakeCloudflare,
    FakeHA,
    FakeR2,
    make_config,
    options,
    unzip,
)

__all__ = ["ACCOUNT", "BERLIN", "SMALL_USAGE", "Clock", "FakeCloudflare", "FakeHA", "FakeR2", "make_config",
           "options", "unzip"]


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)
