from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_temporal_compose_pins_services_persists_postgres_and_binds_to_loopback():
    compose_path = PROJECT_ROOT / "deploy" / "compose.yaml"

    with compose_path.open(encoding="utf-8") as stream:
        compose = yaml.safe_load(stream)

    services = compose["services"]
    assert services["postgres"]["image"] == "postgres:16"
    assert services["temporal"]["image"] == "temporalio/server:1.32.2"
    assert services["admin-tools"]["image"] == "temporalio/admin-tools:1.32.2"
    assert services["ui"]["image"] == "temporalio/ui:2.56.1"

    postgres = services["postgres"]
    assert postgres["healthcheck"]["test"] == ["CMD-SHELL", "pg_isready -U temporal -d postgres"]
    assert "ports" not in postgres
    assert postgres["volumes"] == ["${KITE_DATA_DIR:-/var/lib/kite}/postgres:/var/lib/postgresql/data"]
    assert postgres["environment"]["POSTGRES_PASSWORD"] == "${TEMPORAL_POSTGRES_PASSWORD:?set in deploy/.env}"

    temporal = services["temporal"]
    assert temporal["ports"] == ["127.0.0.1:7233:7233"]
    assert temporal["depends_on"]["admin-tools"]["condition"] == "service_completed_successfully"
    assert services["admin-tools"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert temporal["healthcheck"]["test"] == ["CMD", "nc", "-z", "localhost", "7233"]
    assert "kaggle" not in services

    ui = services["ui"]
    assert ui["profiles"] == ["operator-ui"]
    assert ui["ports"] == ["127.0.0.1:8080:8080"]
    assert ui["depends_on"]["temporal"]["condition"] == "service_healthy"
