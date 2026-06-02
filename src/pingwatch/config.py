from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Paths(BaseModel):
    db: Path = Field(default=Path("/data/pingwatch.db"))
    config: Path = Field(default=Path("/data/config.yaml"))
    logs_dir: Path = Field(default=Path("/data/logs"))
    backups_dir: Path = Field(default=Path("/data/backups"))
    usb_root: Path = Field(default=Path("/media"))


class Bind(BaseModel):
    host: str = "127.0.0.1"
    port: int = 5000


class WiFi(BaseModel):
    interface: str = "wlan0"
    expected_ssid: str | None = None


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PINGWATCH_", extra="ignore")

    db: Path = Path("/data/pingwatch.db")
    config: Path = Path("/data/config.yaml")
    bind_host: str = "127.0.0.1"
    bind_port: int = 5000
    wlan_if: str = "wlan0"
    usb_root: Path = Path("/media")
    timezone: str = "Europe/Berlin"


class Settings(BaseModel):
    paths: Paths
    bind: Bind
    wifi: WiFi
    timezone: str = "Europe/Berlin"

    @classmethod
    def from_env_and_yaml(cls) -> Settings:
        env = Env()
        cfg_data: dict = {}
        if env.config.exists():
            with env.config.open() as fh:
                cfg_data = yaml.safe_load(fh) or {}
        paths = Paths(
            db=env.db,
            config=env.config,
            usb_root=env.usb_root,
        )
        bind = Bind(host=env.bind_host, port=env.bind_port)
        wifi = WiFi(
            interface=env.wlan_if,
            expected_ssid=cfg_data.get("wifi", {}).get("expected_ssid"),
        )
        return cls(paths=paths, bind=bind, wifi=wifi, timezone=env.timezone)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env_and_yaml()
