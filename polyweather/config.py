"""Configuration loading. Single YAML file + environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(os.environ.get("POLYWEATHER_ROOT", Path(__file__).resolve().parent.parent))
CONFIG_PATH = Path(os.environ.get("POLYWEATHER_CONFIG", ROOT / "config" / "cities.yaml"))
DATA_DIR = Path(os.environ.get("POLYWEATHER_DATA", ROOT / "data"))
DB_URL = os.environ.get("POLYWEATHER_DB", f"sqlite:///{DATA_DIR / 'polyweather.db'}")

KNMI_API_KEY = os.environ.get("KNMI_API_KEY", "")

USER_AGENT = "polyweather/0.1 (personal research; contact via repo)"


@dataclass
class CityConfig:
    key: str
    raw: dict[str, Any]

    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def tz(self) -> str:
        return self.raw["tz"]

    @property
    def icao(self) -> str:
        return self.raw["resolution_station"]["icao"]

    @property
    def lat(self) -> float:
        return self.raw["lat"]

    @property
    def lon(self) -> float:
        return self.raw["lon"]

    @property
    def elevation_m(self) -> float:
        return self.raw.get("elevation_m", 0.0)

    @property
    def fast_sources(self) -> list[dict]:
        return self.raw.get("fast_sources", [])

    @property
    def upstream(self) -> list[dict]:
        return self.raw.get("upstream", [])

    @property
    def breeze(self) -> dict:
        return self.raw["breeze"]

    @property
    def models(self) -> list[str]:
        return self.raw["openmeteo_models"]

    @property
    def market_slug(self) -> str:
        return self.raw["market_city_slug"]

    @property
    def sampling_deficit(self) -> float:
        return self.raw["resolution_station"].get("sampling_deficit_c", 0.1)

    def peak_window(self, month: int, summer_months: list[int]) -> tuple[str, str]:
        season = "summer" if month in summer_months else "winter"
        w = self.raw["peak_window"][season]
        return w[0], w[1]


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            return cls(raw=yaml.safe_load(f))

    @property
    def cities(self) -> dict[str, CityConfig]:
        return {k: CityConfig(key=k, raw=v) for k, v in self.raw["cities"].items()}

    @property
    def grid(self) -> dict:
        return self.raw["grid"]

    @property
    def engine(self) -> dict:
        return self.raw["engine"]

    @property
    def summer_months(self) -> list[int]:
        return self.raw["seasons"]["summer_months"]

    @property
    def cadence(self) -> dict:
        return self.raw["scheduler"]["cadence"]

    @property
    def polymarket(self) -> dict:
        return self.raw["polymarket"]


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.load()
    return _config
