from pathlib import Path

import pytest


@pytest.fixture
def tmp_hfao_dir(tmp_path: Path) -> Path:
    d = tmp_path / "hfao"
    d.mkdir()
    return d
