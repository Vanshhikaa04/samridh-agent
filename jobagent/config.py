import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
load_dotenv(ROOT / ".env")


def env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def profile() -> dict:
    return yaml.safe_load((ROOT / "profile.yaml").read_text(encoding="utf-8"))


def search_cfg() -> dict:
    return profile()["search"]
