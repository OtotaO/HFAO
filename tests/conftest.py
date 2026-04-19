import pytest

@pytest.fixture
def tmp_hfao_dir(tmp_path):
    d = tmp_path / "hfao"
    d.mkdir()
    return d
