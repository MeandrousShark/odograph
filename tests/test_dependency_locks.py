from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _pinned_requirements(path: Path) -> set[str]:
    return {
        line
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    }


def test_development_lock_contains_the_runtime_lock_and_pinned_pytest():
    runtime = _pinned_requirements(ROOT / "requirements.lock")
    development = _pinned_requirements(ROOT / "requirements-dev.lock")

    assert runtime <= development
    assert {
        "iniconfig==2.3.0",
        "packaging==25.0",
        "pluggy==1.6.0",
        "Pygments==2.19.2",
        "pytest==9.1.1",
    } <= development


def test_openpyxl_uses_the_locked_lxml_xml_backend():
    import openpyxl

    runtime_source = _pinned_requirements(ROOT / "requirements.txt")
    runtime = _pinned_requirements(ROOT / "requirements.lock")
    development = _pinned_requirements(ROOT / "requirements-dev.lock")

    assert "lxml>=6.1.3" in runtime_source
    assert "lxml==6.1.3" in runtime
    assert "lxml==6.1.3" in development
    assert openpyxl.LXML is True
