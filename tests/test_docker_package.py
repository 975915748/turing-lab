from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_copies_all_runtime_python_modules():
    """Prevent startup imports from being omitted from the production image."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    required_modules = {"app.py", "model_api.py", "railway_start.py", "points.py", "social.py", "match_orchestration.py"}
    copy_commands = [
        line.split()
        for line in dockerfile.splitlines()
        if line.startswith("COPY ")
    ]

    assert any(
        command[-1] == "./" and required_modules.issubset(set(command[1:-1]))
        for command in copy_commands
    ), (
        "The production image must copy app.py, model_api.py, railway_start.py, "
        "points.py, and social.py into /app."
    )
