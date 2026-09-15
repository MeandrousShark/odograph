"""Keep supported database defaults and security scans on the same artifact."""

from pathlib import Path
import re

import pytest
import yaml


pytestmark = pytest.mark.ops
ROOT = Path(__file__).resolve().parents[1]


def test_database_defaults_and_scans_use_the_compose_digest():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    expected = compose["services"]["db"]["image"]
    assert re.fullmatch(
        r"ghcr\.io/meandrousshark/odograph-postgis@sha256:[0-9a-f]{64}",
        expected,
    )

    for filename in ("test_db.sh", "devsite.sh", "release_preflight_smoke.sh"):
        source = (ROOT / "scripts" / filename).read_text()
        defaults = re.findall(r"\$\{(?:TEST_DB_IMAGE|DEVSITE_DB_IMAGE|POSTGIS_IMAGE):-([^}]+)\}", source)
        assert defaults == [expected], filename

    def check_references(value):
        if isinstance(value, dict):
            return sum(check_references(child) for child in value.values())
        if isinstance(value, list):
            return sum(check_references(child) for child in value)
        if isinstance(value, str) and "ghcr.io/meandrousshark/odograph-postgis@" in value:
            assert value == expected
            return 1
        return 0

    for filename in ("test.yml", "release.yml", "release-preflight.yml", "security.yml"):
        workflow = yaml.load(
            (ROOT / ".github" / "workflows" / filename).read_text(),
            Loader=yaml.BaseLoader,
        )
        assert check_references(workflow) > 0, filename
        for job in workflow["jobs"].values():
            for service in job.get("services", {}).values():
                assert service["image"] == expected, filename
