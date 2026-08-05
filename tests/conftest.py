import zipfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


def _zip_tree(src: Path, out: Path) -> Path:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src).as_posix())
    return out


@pytest.fixture(scope="session")
def mini_zip(tmp_path_factory) -> Path:
    """A pubinfo-shaped session zip assembled from the committed real-data
    fixture tree (tests/fixtures/mini/)."""
    return _zip_tree(FIXTURES / "mini",
                     tmp_path_factory.mktemp("mini") / "pubinfo_mini.zip")


@pytest.fixture(scope="session")
def archive_zips_dir(tmp_path_factory) -> Path:
    """pubinfo_1989.zip + pubinfo_1999.zip built from the committed
    archive fixture trees (chaptered-only era; all-versions era)."""
    d = tmp_path_factory.mktemp("archive_zips")
    for year in ("1989", "1999"):
        _zip_tree(FIXTURES / "archive" / year, d / f"pubinfo_{year}.zip")
    return d
