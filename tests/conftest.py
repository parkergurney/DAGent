"""Verify gate and scout delivery write to ./data/ (see README.md). Run every test from a
throwaway cwd so that never touches the repo's real data/ dir.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
