import zipfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def mini_zip(tmp_path_factory) -> Path:
    """A pubinfo-shaped session zip assembled from the committed real-data
    fixture tree (tests/fixtures/mini/)."""
    out = tmp_path_factory.mktemp("mini") / "pubinfo_mini.zip"
    src = FIXTURES / "mini"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src).as_posix())
    return out
