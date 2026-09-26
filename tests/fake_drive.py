"""A throw-away Google Drive layout + pipeline config for stage tests."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml

import fixtures
from core.config import load_config


class FakeDrive:
    """Temp dir shaped like My Drive/AIEditor with an inbox, a local work dir and a config file."""

    def __init__(self, changes: Optional[dict[str, Any]] = None):
        self._tmp = tempfile.TemporaryDirectory(prefix="aieditor-drive-")
        root = Path(self._tmp.name)
        self.drive_root = root / "MyDrive" / "AIEditor"
        self.inbox = self.drive_root / "inbox"
        self.work = self.drive_root / "work"
        self.local_work = root / "local"
        self.inbox.mkdir(parents=True)
        config = load_config()
        config["paths"]["drive_root"] = str(self.drive_root)
        config["paths"]["local_work"] = str(self.local_work)
        for key, value in (changes or {}).items():
            section, name = key.split(".")
            config[section][name] = value
        self.config_path = root / "pipeline.yaml"
        self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    def add(self, fixture: str, name: Optional[str] = None) -> str:
        name = name or f"{fixture}.mp4"
        shutil.copyfile(fixtures.path(fixture), self.inbox / name)
        return name

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def __enter__(self) -> "FakeDrive":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()
