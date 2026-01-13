"""Settings persistence for the Sendspin CLI.

This module provides persistent storage for player settings. Settings are
automatically loaded from disk and saved with debouncing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class _UndefinedType:
    """Singleton for undefined/not-passed values."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNDEFINED"


UNDEFINED = _UndefinedType()

# Debounce delay for saving settings
SAVE_DEBOUNCE_SECONDS = 60.0


class SettingsMode(Enum):
    """Mode for settings file selection."""

    TUI = "tui"
    DAEMON = "daemon"


@dataclass
class Settings:
    """All persistent settings for the Sendspin CLI."""

    player_volume: int = 25
    player_muted: bool = False
    static_delay_ms: float = 0.0
    last_server_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert settings to a dictionary for serialization."""
        return {
            "player_volume": self.player_volume,
            "player_muted": self.player_muted,
            "static_delay_ms": self.static_delay_ms,
            "last_server_url": self.last_server_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Create settings from a dictionary."""
        return cls(
            player_volume=data.get("player_volume", 25),
            player_muted=data.get("player_muted", False),
            static_delay_ms=data.get("static_delay_ms", 0.0),
            last_server_url=data.get("last_server_url"),
        )


class SettingsManager:
    """Manages settings with debounced disk persistence.

    Changes are debounced and saved after 60 seconds of inactivity,
    or immediately on flush().
    """

    def __init__(self, settings_file: Path) -> None:
        """Initialize the settings manager.

        Args:
            settings_file: Path to the settings file.
        """
        self._settings_file = settings_file
        self._settings = Settings()
        self._debounce_save_handle: asyncio.TimerHandle | None = None

    async def load(self) -> None:
        """Load settings from disk."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._load)

    @property
    def player_volume(self) -> int:
        """Get the player volume (0-100)."""
        return self._settings.player_volume

    @property
    def player_muted(self) -> bool:
        """Get the player muted state."""
        return self._settings.player_muted

    @property
    def static_delay_ms(self) -> float:
        """Get the static delay in milliseconds."""
        return self._settings.static_delay_ms

    @property
    def last_server_url(self) -> str | None:
        """Get the last connected server URL."""
        return self._settings.last_server_url

    def update(
        self,
        *,
        player_volume: int | _UndefinedType = UNDEFINED,
        player_muted: bool | _UndefinedType = UNDEFINED,
        static_delay_ms: float | _UndefinedType = UNDEFINED,
        last_server_url: str | None | _UndefinedType = UNDEFINED,
    ) -> None:
        """Update settings fields. Only changed fields trigger a save.

        Args:
            player_volume: New player volume (0-100), or UNDEFINED to keep current.
            player_muted: New player muted state, or UNDEFINED to keep current.
            static_delay_ms: New static delay in ms, or UNDEFINED to keep current.
            last_server_url: New last server URL, or UNDEFINED to keep current.
        """
        changed = False

        # Handle player_volume separately due to clamping
        if not isinstance(player_volume, _UndefinedType):
            player_volume = max(0, min(100, player_volume))
            if self._settings.player_volume != player_volume:
                self._settings.player_volume = player_volume
                changed = True

        # Handle other fields generically
        fields = {
            "player_muted": player_muted,
            "static_delay_ms": static_delay_ms,
            "last_server_url": last_server_url,
        }
        for name, value in fields.items():
            if not isinstance(value, _UndefinedType):
                if getattr(self._settings, name) != value:
                    setattr(self._settings, name, value)
                    changed = True

        if changed:
            self._schedule_save()

    async def flush(self) -> None:
        """Immediately save any pending changes to disk."""
        if self._debounce_save_handle is not None:
            self._debounce_save_handle.cancel()
            self._debounce_save_handle = None
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._save)

    def _schedule_save(self) -> None:
        """Schedule a debounced save operation."""
        # Cancel existing timer if any
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
        if not self._settings_file.exists():
            logger.debug("Settings file does not exist: %s", self._settings_file)
            return

        try:
            data = json.loads(self._settings_file.read_text())
            self._settings = Settings.from_dict(data)
            logger.info(
                "Loaded settings from %s: volume=%d%%, muted=%s",
                self._settings_file,
                self._settings.player_volume,
                self._settings.player_muted,
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load settings from %s: %s", self._settings_file, e)

    def _save(self) -> None:
        """Save settings to the settings file (blocking I/O)."""
        try:
            self._settings_file.parent.mkdir(parents=True, exist_ok=True)
            self._settings_file.write_text(json.dumps(self._settings.to_dict(), indent=2) + "\n")
            logger.debug("Saved settings to %s", self._settings_file)
        except OSError as e:
            logger.warning("Failed to save settings to %s: %s", self._settings_file, e)


async def get_settings_manager(
    mode: SettingsMode, config_dir: Path | str | None = None
) -> SettingsManager:
    """Create and load a settings manager for the given mode.

    This should only be called once at startup. Pass the returned instance
    to components that need it.

    Args:
        mode: The settings mode (TUI or DAEMON).
        config_dir: Optional directory to store settings. Defaults to ~/.config/sendspin.

    Returns:
        A new SettingsManager instance with settings loaded from disk.
    """
    if config_dir is None:
        config_dir = Path.home() / ".config" / "sendspin"
    elif isinstance(config_dir, str):
        config_dir = Path(config_dir)
    settings_file = config_dir / f"settings-{mode.value}.json"
    manager = SettingsManager(settings_file)
    await manager.load()
    return manager
