"""Put the scale-up dir on sys.path so tests can ``import registry`` / ``import
browsers``. The package dir is "scale-up" (a hyphen), which is not importable
as a dotted name, so the modules are imported flat.

Every test gets its own school database in a tmp dir: nothing ever opens the
real ``~/.ainl/ainl.db``."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _no_trouble_files(monkeypatch):
    """No failure scenes, CDP captures or owner messages from tests."""
    monkeypatch.setenv("AINL_TROUBLE", "0")
    # The by-hand approval is off in production (auto); its tests keep it on.
    monkeypatch.setenv("AINL_AUTO_APPROVE", "0")


@pytest.fixture(autouse=True)
def school_db(tmp_path_factory):
    import schools
    from schooldb import SchoolDB
    db = SchoolDB(base=tmp_path_factory.mktemp("schooldb"))
    schools.use(db)
    yield db
    schools.use(None)
