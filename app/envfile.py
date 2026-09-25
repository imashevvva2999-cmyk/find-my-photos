"""Reads the local .env file (KEY=VALUE lines). Kept separate from config.py so tools that
need only one value (like migrations) do not require the full, validated settings."""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """Variables already set in the real environment win over the file."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def update_env_file(values: dict[str, str | None], path: Path = ENV_FILE) -> None:
    """Set (or with None, remove) keys in place, keeping every other line. File mode 600."""
    lines = path.read_text().splitlines() if path.exists() else []
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            if key not in seen and values[key] is not None:
                out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen and v is not None]
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)
