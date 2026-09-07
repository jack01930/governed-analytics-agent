from pathlib import Path


def test_local_database_targets_export_safe_development_urls() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert "MIGRATION_DATABASE_URL ?= postgresql+psycopg://governed_admin:" in makefile
    assert "LOADER_DATABASE_URL ?= postgresql+psycopg://analytics_loader:" in makefile
    assert "DATABASE_URL ?= postgresql+asyncpg://analytics_readonly:" in makefile
    assert "export MIGRATION_DATABASE_URL" in makefile
    assert "export LOADER_DATABASE_URL" in makefile
    assert "export DATABASE_URL" in makefile


def test_makefile_does_not_hide_live_model_authorization() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8").lower()

    assert "--mode live" not in makefile
    assert "--live" not in makefile
    assert "eval-week2-fixture:" in makefile
    assert "governed-eval week2 --dataset tiny --mode fixture" in makefile


def test_makefile_exposes_only_fixture_week3_target() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8").lower()

    assert "eval-week3-fixture:" in makefile
    assert (
        "governed-eval week3 --dataset tiny --mode fixture --report-path-file "
        "artifacts/evals/week3/fixture-report-path.txt"
    ) in makefile
    assert "--mode live" not in makefile
    assert "--live" not in makefile
