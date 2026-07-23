"""Load config.yaml + feeds.yaml and expose them as simple objects."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_feeds() -> list[dict]:
    with open(ROOT / "feeds.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f).get("feeds", [])


def env(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "")
    if required and not val:
        raise SystemExit(
            f"Missing environment variable {name}. "
            f"Set it as a GitHub Actions secret (repo Settings -> Secrets and "
            f"variables -> Actions) and reference it in the workflow env block."
        )
    return val
