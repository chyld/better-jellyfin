"""Guards on the deployment files."""
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_container_does_not_trust_forwarded_headers_from_anyone():
    dockerfile = (ROOT / "Dockerfile").read_text()
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
    assert "forwarded-allow-ips" not in cmd or '"*"' not in cmd


def test_container_runs_one_worker():
    """Scans and streams are managed in-process; more workers would each run their own."""
    cmd = next(line for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("CMD"))
    assert '"--workers", "1"' in cmd
