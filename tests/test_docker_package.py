from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_copies_all_runtime_python_modules():
    """Prevent startup imports from being omitted from the production image."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    required_copy = "COPY app.py model_api.py railway_start.py ./"

    assert required_copy in dockerfile.splitlines(), (
        "The production image must copy app.py, model_api.py, and "
        "railway_start.py into /app."
    )
