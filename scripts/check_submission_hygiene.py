import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {".git", ".smartreco", ".venv", "__pycache__", ".pytest_cache", "htmlcov", "build", "dist"}
SECRET_PATTERNS = [
    re.compile(r"rsk_[A-Za-z0-9]{20,}"),
    re.compile(r"lsv2_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
]


def _git_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, text=True, capture_output=True, check=False
    )
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def _source_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.name == ".env" or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pyc", ".jpg", ".png", ".docx"}:
            continue
        yield path


def audit_submission_hygiene() -> dict:
    tracked = set(_git_files())
    forbidden_tracked = sorted(
        path for path in tracked
        if path == ".env"
        or path.startswith(".smartreco/")
        or path.endswith((".db", ".sqlite", ".sqlite3", ".pyc", ".log"))
    )
    secret_files: list[str] = []
    for path in _source_files():
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            secret_files.append(path.relative_to(ROOT).as_posix())
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    required_git_ignores = [".env", ".smartreco/", "*.db", "*.py[cod]"]
    required_docker_ignores = [".env", ".smartreco", "*.db", "*.py[cod]"]
    missing_git_ignores = [value for value in required_git_ignores if value not in gitignore]
    missing_docker_ignores = [value for value in required_docker_ignores if value not in dockerignore]
    ok = not (forbidden_tracked or secret_files or missing_git_ignores or missing_docker_ignores)
    return {
        "status": "pass" if ok else "fail",
        "env_tracked": ".env" in tracked,
        "forbidden_tracked_artifacts": forbidden_tracked,
        "source_files_with_secret_patterns": sorted(secret_files),
        "missing_gitignore_rules": missing_git_ignores,
        "missing_dockerignore_rules": missing_docker_ignores,
        "note": "The audit reports paths only and never prints discovered secret values.",
    }


def main() -> None:
    report = audit_submission_hygiene()
    print(json.dumps(report, indent=2))
    if report["status"] != "pass":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
