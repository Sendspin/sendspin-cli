"""Settings persistence for the Sendspin CLI.

This module provides persistent storage for player settings. Settings are
automatically loaded from disk and saved with debouncing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, ClassVar, Literal

logger = logging.getLogger(__name__)

# Debounce delay for saving settings
SAVE_DEBOUNCE_SECONDS = 60.0


@dataclass
class BaseSettings:
    """Base class for settings with persistence support.

    Changes are debounced and saved after 60 seconds of inactivity,
    or immediately on flush().
    """

    # Common fields
    name: str | None = None
    log_level: str | None = None
    listen_port: int | None = None

    # Internal state (not serialized)
    _settings_file: Path | None = field(default=None, repr=False, compare=False)
    _debounce_save_handle: asyncio.TimerHandle | None = field(
        default=None, repr=False, compare=False
    )

    # Fields to exclude from serialization
    _internal_fields: ClassVar[set[str]] = {"_settings_file", "_debounce_save_handle"}

    def to_dict(self) -> dict[str, Any]:
        """Convert settings to a dictionary for serialization."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name not in self._internal_fields
        }

    def _update_fields(self, updates: dict[str, Any]) -> bool:
        """Update fields and return whether any changed."""
        changed = False
        for field_name, value in updates.items():
            if value is not None and getattr(self, field_name) != value:
                setattr(self, field_name, value)
                changed = True
        return changed

    async def load(self) -> None:
        """Load settings from disk."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._load)

    async def flush(self) -> None:
        """Immediately save any pending changes to disk."""
        if self._debounce_save_handle is not None:
            self._debounce_save_handle.cancel()
            self._debounce_save_handle = None
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._save)

    def _schedule_save(self) -> None:
        """Schedule a debounced save operation."""
        if self._debounce_save_handle is not None:
            self._debounce_save_handle.cancel()

        loop = asyncio.get_running_loop()
        self._debounce_save_handle = loop.call_later(
            SAVE_DEBOUNCE_SECONDS, self._debounced_save, loop
        )

    def _debounced_save(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called by the timer to save settings in executor."""
        self._debounce_save_handle = None
        loop.run_in_executor(None, self._save)

    def _load(self) -> None:
        """Load settings from the settings file (blocking I/O)."""
        raise NotImplementedError

    def _save(self) -> None:
        """Save settings to the settings file (blocking I/O)."""
        if self._settings_file is None:
            return
        try:
            self._settings_file.parent.mkdir(parents=True, exist_ok=True)
            self._settings_file.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
            logger.debug("Saved settings to %s", self._settings_file)
        except OSError as e:
            logger.warning("Failed to save settings to %s: %s", self._settings_file, e)


@dataclass
class ClientSettings(BaseSettings):
    """Settings for TUI and daemon modes."""

    player_volume: int = 25
    player_muted: bool = False
    static_delay_ms: float = 0.0
    last_server_url: str | None = None
    client_id: str | None = None
    audio_device: str | None = None
    use_mpris: bool = True

    def update(
        self,
        *,
        player_volume: int | None = None,
        player_muted: bool | None = None,
        static_delay_ms: float | None = None,
        last_server_url: str | None = None,
        name: str | None = None,
        client_id: str | None = None,
        audio_device: str | None = None,
        log_level: str | None = None,
        listen_port: int | None = None,
        use_mpris: bool | None = None,
    ) -> None:
        """Update settings fields. Only changed fields trigger a save."""
        changed = False

        # Handle player_volume separately due to clamping
        if player_volume is not None:
            player_volume = max(0, min(100, player_volume))
            if self.player_volume != player_volume:
                self.player_volume = player_volume
                changed = True

        # Handle other fields generically
        changed = (
            self._update_fields(
                {
                    "player_muted": player_muted,
                    "static_delay_ms": static_delay_ms,
                    "last_server_url": last_server_url,
                    "name": name,
                    "client_id": client_id,
                    "audio_device": audio_device,
                    "log_level": log_level,
                    "listen_port": listen_port,
                    "use_mpris": use_mpris,
                }
            )
            or changed
        )

        if changed:
            self._schedule_save()

    def _load(self) -> None:
        """Load settings from the settings file (blocking I/O)."""
        if self._settings_file is None or not self._settings_file.exists():
            logger.debug("Settings file does not exist: %s", self._settings_file)
            return

        try:
            data = json.loads(self._settings_file.read_text())
            # Update fields from loaded data
            self.name = data.get("name")
            self.log_level = data.get("log_level")
            self.listen_port = data.get("listen_port")
            self.player_volume = data.get("player_volume", 25)
            self.player_muted = data.get("player_muted", False)
            self.static_delay_ms = data.get("static_delay_ms", 0.0)
            self.last_server_url = data.get("last_server_url")
            self.client_id = data.get("client_id")
            self.audio_device = data.get("audio_device")
            self.use_mpris = data.get("use_mpris", True)
            logger.info(
                "Loaded settings from %s: volume=%d%%, muted=%s",
                self._settings_file,
                self.player_volume,
                self.player_muted,
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load settings from %s: %s", self._settings_file, e)


@dataclass
class ServeSettings(BaseSettings):
    """Settings for serve mode."""

    source: str | None = None

    def update(
        self,
        *,
        name: str | None = None,
        log_level: str | None = None,
        listen_port: int | None = None,
        source: str | None = None,
    ) -> None:
        """Update settings fields. Only changed fields trigger a save."""
        changed = self._update_fields(
            {
                "name": name,
                "log_level": log_level,
                "listen_port": listen_port,
                "source": source,
            }
        )

        if changed:
            self._schedule_save()

    def _load(self) -> None:
        """Load settings from the settings file (blocking I/O)."""
        if self._settings_file is None or not self._settings_file.exists():
            logger.debug("Settings file does not exist: %s", self._settings_file)
            return

        try:
            data = json.loads(self._settings_file.read_text())
            self.name = data.get("name")
            self.log_level = data.get("log_level")
            self.listen_port = data.get("listen_port")
            self.source = data.get("source")
            logger.info("Loaded settings from %s", self._settings_file)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load settings from %s: %s", self._settings_file, e)


async def get_client_settings(
    mode: Literal["tui", "daemon"], config_dir: str | None = None
) -> ClientSettings:
    """Create and load client settings for TUI or daemon mode.

    Args:
        mode: The client mode ("tui" or "daemon").
        config_dir: Optional directory to store settings. Defaults to ~/.config/sendspin.

    Returns:
        ClientSettings instance with settings loaded from disk.
    """
    config_path = Path(config_dir) if config_dir else Path.home() / ".config" / "sendspin"
    settings = ClientSettings(_settings_file=config_path / f"settings-{mode}.json")
    await settings.load()
    return settings


async def get_serve_settings(config_dir: str | None = None) -> ServeSettings:
    """Create and load serve settings.

    Args:
        config_dir: Optional directory to store settings. Defaults to ~/.config/sendspin.

    Returns:
        ServeSettings instance with settings loaded from disk.
    """
    config_path = Path(config_dir) if config_dir else Path.home() / ".config" / "sendspin"
    settings = ServeSettings(_settings_file=config_path / "settings-serve.json")
    await settings.load()
    return settings
