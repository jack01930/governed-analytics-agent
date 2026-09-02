"""Guard the database CI job against accidental expensive or external data work."""

from pathlib import Path


def test_database_ci_uses_only_local_tiny_data_evidence() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "governed-data generate --scale tiny" in workflow
    assert "governed-data verify --scale tiny" in workflow
    assert "pytest tests/unit/metrics tests/integration/metrics -v" in workflow
    assert "--scale full" not in workflow
    assert "curl " not in workflow
    assert "http://" not in workflow and "https://" not in workflow
