import pytest


@pytest.fixture(autouse=True)
def _never_touch_the_real_project_folder(tmp_path, monkeypatch):
    """Anything that falls back to the working directory ('.' is the default data folder) writes into a temp dir.
    (A test once wrote an empty state.db into the real data folder and blocked the real migration.)"""
    monkeypatch.chdir(tmp_path)
