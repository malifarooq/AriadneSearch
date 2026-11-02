"""Paths, API keys and limits, read from the environment and `.env`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Config:
    data_dir: Path
    openalex_api_key: str | None
    ieee_api_key: str | None
    openalex_daily_usd: float = 1.0
    ieee_daily_soft_limit: int = 180
    ieee_rate_per_second: int = 10
    citer_cap: int = 500

    @property
    def graph_db(self) -> Path:
        return self.data_dir / "graph.db"

    @property
    def ieee_db(self) -> Path:
        """Separate file, so deleting it removes all stored IEEE content."""
        return self.data_dir / "ieee.db"


def load_config() -> Config:
    load_dotenv(REPO_ROOT / ".env")
    data_dir = Path(os.getenv("ARIADNE_DATA_DIR") or REPO_ROOT / "data")
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir
    openalex_key = os.getenv("OPENALEX_API_KEY") or None
    default_budget = "1.0" if openalex_key else "0.1"
    return Config(
        data_dir=data_dir,
        openalex_api_key=openalex_key,
        ieee_api_key=os.getenv("IEEE_API_KEY") or None,
        openalex_daily_usd=float(os.getenv("OPENALEX_DAILY_USD") or default_budget),
    )
