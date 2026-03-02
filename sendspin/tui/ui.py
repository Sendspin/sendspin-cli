"""Rich-based terminal UI for the Sendspin CLI."""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import Any, Self

from aiosendspin.models.types import PlaybackStateType, RepeatMode
from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sendspin.discovery import DiscoveredServer


class _RefreshableLayout:
    """A renderable that rebuilds on each render cycle."""

    def __init__(self, ui: SendspinUI) -> None:
        self._ui = ui

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Rebuild and yield the layout on each render."""
        yield self._ui._build_layout()  # noqa: SLF001


# Duration in seconds to highlight a pressed shortcut
SHORTCUT_HIGHLIGHT_DURATION = 0.15


@dataclass
class UIState:
    """Holds state for the UI display."""

    # Connection
    server_url: str | None = None
    connected: bool = False
    status_message: str = "Initializing..."
    group_name: str | None = None

    # Server selector
    show_server_selector: bool = False
    available_servers: list[DiscoveredServer] = field(default_factory=list)
    selected_server_index: int = 0

    # Playback
    playback_state: PlaybackStateType | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    track_progress_ms: int | None = None
    track_duration_ms: int | None = None
    progress_updated_at: float = 0.0  # time.monotonic() when progress was updated

    # Volume
    volume: int | None = None
    muted: bool = False
    player_volume: int = 100
    player_muted: bool = False
    use_hardware_volume: bool = False

    # Audio format
    audio_codec: str | None = None
    audio_sample_rate: int = 0
    audio_bit_depth: int = 0
    audio_channels: int = 0

    # Delay
    delay_ms: float = 0.0

    # Repeat / Shuffle
    repeat_mode: RepeatMode | None = None
    shuffle: bool | None = None

    # Shortcut highlight
    highlighted_shortcut: str | None = None
    highlight_time: float = 0.0


class SendspinUI:
    """Rich-based terminal UI for the Sendspin CLI."""

    def __init__(
        self,
        delay_ms: float,
        *,
        player_volume: int = 100,
        player_muted: bool = False,
        use_hardware_volume: bool = False,
    ) -> None:
        """Initialize the UI."""
        self._console = Console()
        self._state = UIState(
            delay_ms=delay_ms,
            volume=player_volume,
            player_volume=player_volume,
            player_muted=player_muted,
            use_hardware_volume=use_hardware_volume,
        )
        self._live: Live | None = None
        self._running = False
        self._panel_cache: dict[str, tuple[tuple[Any, ...], Panel]] = {}

    @property
    def state(self) -> UIState:
        """Get the UI state for external updates."""
        return self._state

    def _format_time(self, ms: int | None) -> str:
        """Format milliseconds as MM:SS."""
        if ms is None:
            return "--:--"
        seconds = ms // 1000
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes:02d}:{secs:02d}"

    def _is_highlighted(self, shortcut: str) -> bool:
        """Check if a shortcut should be highlighted."""
        if self._state.highlighted_shortcut != shortcut:
            return False
        elapsed = time.monotonic() - self._state.highlight_time
        return elapsed < SHORTCUT_HIGHLIGHT_DURATION

    def _shortcut_style(self, shortcut: str) -> str:
        """Get the style for a shortcut key."""
        return "bold yellow reverse" if self._is_highlighted(shortcut) else "bold cyan"

    def _cached_panel(self, name: str, key: tuple[Any, ...], builder: Callable[[], Panel]) -> Panel:
        """Return cached panel if key matches, otherwise rebuild and cache."""
        entry = self._panel_cache.get(name)
        if entry is not None and entry[0] == key:
            return entry[1]
        panel = builder()
        self._panel_cache[name] = (key, panel)
        return panel

    def highlight_shortcut(self, shortcut: str) -> None:
        """Highlight a shortcut temporarily."""
        self._state.highlighted_shortcut = shortcut
        self._state.highlight_time = time.monotonic()
        self.refresh()

    def _build_now_playing_panel(self, *, expand: bool = False) -> Panel:
        """Build the now playing panel."""
        is_active = self._state.playback_state is not None or self._state.title is not None

        # Show prompt when nothing is playing
        if not is_active:
            content = Table.grid()
            content.add_column()
            content.add_row("")
            line1 = Text()
            line1.append("Press ", style="dim")
            line1.append("<space>", style="bold cyan")
            line1.append(" to start playing", style="dim")
            content.add_row(line1)
            line2 = Text()
            line2.append("Press ", style="dim")
            line2.append("g", style="bold cyan")
            line2.append(" to join an existing session", style="dim")
            content.add_row(line2)
            content.add_row("")
            content.add_row("")
            return Panel(content, title="Now Playing", border_style="blue", expand=expand)

        # Info grid with label/value columns
        info = Table.grid(padding=(0, 1))
        info.add_column(style="dim", width=8)
        info.add_column()

        if self._state.title:
            info.add_row("Title:", Text(self._state.title, style="bold white"))
            info.add_row("Artist:", Text(self._state.artist or "Unknown artist", style="cyan"))
            info.add_row("Album:", Text(self._state.album or "Unknown album", style="dim"))
        else:
            state_label = (
                self._state.playback_state.value.capitalize()
                if self._state.playback_state
                else "Active"
            )
            info.add_row("Status:", Text(state_label, style="bold white"))
            info.add_row("", Text("No metadata available", style="dim"))
            info.add_row("")

        # Vertical container for info + shortcuts (5 lines total)
        content = Table.grid()
        content.add_column()
        content.add_row(info)
        content.add_row("")  # Line 4: spacing

        # Line 5: playback shortcuts (always show when active)
        space_label = "pause" if self._state.playback_state == PlaybackStateType.PLAYING else "play"
        shortcuts = Text()
        shortcuts.append("←", style=self._shortcut_style("prev"))
        shortcuts.append(" prev  ", style="dim")
        shortcuts.append("<space>", style=self._shortcut_style("space"))
        shortcuts.append(f" {space_label}  ", style="dim")
        shortcuts.append("→", style=self._shortcut_style("next"))
        shortcuts.append(" next", style="dim")
        content.add_row(shortcuts)

        return Panel(content, title="Now Playing", border_style="blue", expand=expand)

    def _build_progress_bar(self, *, expand: bool = False) -> Panel:
        """Build the progress bar panel."""
        progress_ms = self._state.track_progress_ms or 0
        duration_ms = self._state.track_duration_ms or 0

        # Interpolate progress if playing
        if (
            self._state.playback_state == PlaybackStateType.PLAYING
            and self._state.progress_updated_at > 0
            and duration_ms > 0
        ):
            elapsed_ms = (time.monotonic() - self._state.progress_updated_at) * 1000
            progress_ms += int(elapsed_ms)

        if duration_ms > 0:
            progress_ms = max(0, min(progress_ms, duration_ms))

        percentage = progress_ms / duration_ms * 100 if duration_ms > 0 else 0

        # Time text (fixed width)
        time_str = f"{self._format_time(progress_ms)} / {self._format_time(duration_ms)}"

        # Calculate bar width: terminal - panel borders (4) - time text - spacing
        bar_width = max(10, self._console.width - 4 - len(time_str) - 5)
        filled = int(bar_width * percentage / 100)
        empty = bar_width - filled

        bar = Text()
        bar.append("[", style="dim")
        bar.append("=" * filled, style="green bold")
        if filled < bar_width:
            bar.append(">", style="green bold")
            bar.append("-" * max(0, empty - 1), style="dim")
        bar.append("] ", style="dim")

        time_text_styled = Text()
        time_text_styled.append(self._format_time(progress_ms), style="cyan")
        time_text_styled.append(" / ", style="dim")
        time_text_styled.append(self._format_time(duration_ms), style="cyan")

        # Use grid to keep bar and time on same line
        content = Table.grid(expand=True, padding=0)
        content.add_column()
        content.add_column(justify="right", no_wrap=True)
        content.add_row(bar, time_text_styled)

        return Panel(content, title="Progress", border_style="green", expand=expand)

    def _build_volume_panel(self, *, expand: bool = False) -> Panel:
        """Build the volume panel."""
        # Info grid with label/value columns
        info = Table.grid(padding=(0, 2))
        info.add_column()
        info.add_column()

        # Group volume
        vol = self._state.volume if self._state.volume is not None else 0
        vol_style = "red" if self._state.muted else "cyan"
        vol_text = f"{vol}%" + (" [MUTED]" if self._state.muted else "")
        info.add_row("Group:", Text(vol_text, style=vol_style))

        # Player volume
        pvol = self._state.player_volume
        pvol_style = "red" if self._state.player_muted else "cyan"
        pvol_text = f"{pvol}%" + (" [MUTED]" if self._state.player_muted else "")
        player_label = "Hardware:" if self._state.use_hardware_volume else "Player:"
        info.add_row(player_label, Text(pvol_text, style=pvol_style))

        # Vertical container for info + shortcuts
        content = Table.grid()
        content.add_column()
        content.add_row(info)
        content.add_row("")  # Spacing

        # Player volume shortcuts
        player_sc = Text()
        player_sc.append("↑", style=self._shortcut_style("up"))
        player_sc.append("/", style="dim")
        player_sc.append("↓", style=self._shortcut_style("down"))
        player_sc.append(" player  ", style="dim")
        player_sc.append("m", style=self._shortcut_style("mute"))
        player_sc.append(" mute", style="dim")
        content.add_row(player_sc)

        # Group volume shortcuts
        group_sc = Text()
        group_sc.append("[", style=self._shortcut_style("group-down"))
        group_sc.append("/", style="dim")
        group_sc.append("]", style=self._shortcut_style("group-up"))
        group_sc.append(" group  ", style="dim")
        group_sc.append("M", style=self._shortcut_style("group-mute"))
        group_sc.append(" mute", style="dim")
        content.add_row(group_sc)

        return Panel(content, title="Volume", border_style="magenta", expand=expand)

    def _build_connection_panel(self, *, expand: bool = False) -> Panel:
        """Build the connection status panel."""
        content = Table.grid(padding=(0, 1))
        content.add_column(style="dim", width=8)
        content.add_column()

        if self._state.connected and self._state.server_url:
            status = Text("Connected", style="green bold")
            url = Text(self._state.server_url, style="cyan")
        else:
            status = Text("Disconnected", style="red bold")
            url = Text(self._state.status_message, style="yellow")

        content.add_row("Status:", status)
        content.add_row("Server:", url)

        return Panel(content, title="Connection", border_style="yellow", expand=expand)

    def _build_server_selector_panel(self) -> Panel:
        """Build the server selector panel."""
        content = Table.grid()
        content.add_column()

        if not self._state.available_servers:
            content.add_row("")
            content.add_row(Text("Searching for servers...", style="dim"))
            content.add_row("")
        else:
            for i, server in enumerate(self._state.available_servers):
                is_selected = i == self._state.selected_server_index
                is_current = server.url == self._state.server_url

                line = Text()
                if is_selected:
                    line.append(" > ", style="bold cyan")
                else:
                    line.append("   ")

                # Server name
                name_style = "bold white" if is_selected else "white"
                line.append(server.name, style=name_style)

                # Current server indicator
                if is_current:
                    line.append(" (current)", style="dim green")

                content.add_row(line)

                # Show URL below name
                url_line = Text()
                url_line.append("   ")
                url_style = "cyan" if is_selected else "dim"
                url_line.append(f"   {server.host}:{server.port}", style=url_style)
                content.add_row(url_line)

        content.add_row("")

        # Shortcuts
        shortcuts = Text()
        shortcuts.append("↑", style=self._shortcut_style("selector-up"))
        shortcuts.append("/", style="dim")
        shortcuts.append("↓", style=self._shortcut_style("selector-down"))
        shortcuts.append(" navigate  ", style="dim")
        shortcuts.append("<enter>", style=self._shortcut_style("selector-enter"))
        shortcuts.append(" connect  ", style="dim")
        shortcuts.append("r", style=self._shortcut_style("selector-enter"))
        shortcuts.append(" refresh  ", style="dim")
        shortcuts.append("q", style=self._shortcut_style("selector-enter"))
        shortcuts.append(" back", style="dim")
        content.add_row(shortcuts)

        return Panel(content, title="Select Server", border_style="cyan")

    def _build_playback_panel(self, *, expand: bool = False, min_info_rows: int = 0) -> Panel:
        """Build the playback panel with repeat/shuffle status."""
        info = Table.grid(padding=(0, 2))
        info.add_column(style="dim", width=8)
        info.add_column()

        repeat = self._state.repeat_mode
        info.add_row(
            "Repeat:",
            Text(repeat.value if repeat is not None else "—", style="cyan" if repeat else "dim"),
        )

        shuffle = self._state.shuffle
        if shuffle is not None:
            shuffle_text = Text("on" if shuffle else "off", style="cyan")
        else:
            shuffle_text = Text("—", style="dim")
        info.add_row("Shuffle:", shuffle_text)
        info_rows = 2

        content = Table.grid()
        content.add_column()
        content.add_row(info)
        for _ in range(max(0, min_info_rows - info_rows)):
            content.add_row("")
        content.add_row("")  # Spacing before shortcuts

        # Shortcuts
        shortcuts = Text()
        shortcuts.append("r", style=self._shortcut_style("repeat"))
        shortcuts.append(" repeat  ", style="dim")
        shortcuts.append("x", style=self._shortcut_style("shuffle"))
        shortcuts.append(" shuffle", style="dim")
        content.add_row(shortcuts)

        return Panel(content, title="Playback", border_style="yellow", expand=expand)

    def _build_stream_quality_panel(self, *, expand: bool = False, min_info_rows: int = 0) -> Panel:
        """Build the stream quality panel."""
        info = Table.grid(padding=(0, 1))
        info.add_column(style="dim")
        info.add_column()

        if self._state.audio_sample_rate > 0:
            codec_label = (self._state.audio_codec or "PCM").upper()
            info.add_row("Codec:", Text(codec_label, style="cyan"))
            rate_khz = self._state.audio_sample_rate / 1000
            info.add_row("Rate:", Text(f"{rate_khz:.1f}kHz", style="cyan"))
            info.add_row("Depth:", Text(f"{self._state.audio_bit_depth}bit", style="cyan"))
            ch_label = (
                "Stereo" if self._state.audio_channels == 2 else f"{self._state.audio_channels}ch"
            )
            info.add_row("Channels:", Text(ch_label, style="cyan"))
        else:
            info.add_row("Codec:", Text("—", style="dim"))
            info.add_row("Rate:", Text("—", style="dim"))
            info.add_row("Depth:", Text("—", style="dim"))
            info.add_row("Channels:", Text("—", style="dim"))

        delay = self._state.delay_ms
        delay_str = f"+{delay:.0f}ms" if delay >= 0 else f"{delay:.0f}ms"
        info.add_row("Delay:", Text(delay_str, style="cyan"))
        info_rows = 5

        content = Table.grid()
        content.add_column()
        content.add_row(info)
        for _ in range(max(0, min_info_rows - info_rows)):
            content.add_row("")
        content.add_row("")  # Spacing before shortcuts

        # Shortcuts
        shortcuts = Text()
        shortcuts.append(",", style=self._shortcut_style("delay-"))
        shortcuts.append("/", style="dim")
        shortcuts.append(".", style=self._shortcut_style("delay+"))
        shortcuts.append(" adjust delay", style="dim")
        content.add_row(shortcuts)

        return Panel(content, title="Stream", border_style="yellow", expand=expand)

    def _build_server_panel(self, *, expand: bool = False, min_info_rows: int = 0) -> Panel:
        """Build the server panel."""
        info = Table.grid(padding=(0, 1))
        info.add_column(style="dim")
        info.add_column()

        if self._state.connected and self._state.server_url:
            parsed = urlparse(self._state.server_url)
            host = parsed.hostname or ""
            port = str(parsed.port) if parsed.port else ""
            path = parsed.path or "/"
            info.add_row("Status:", Text("Connected", style="green bold"))
            info.add_row("Host:", Text(host, style="cyan"))
            if port:
                info.add_row("Port:", Text(port, style="cyan"))
            info.add_row("Path:", Text(path, style="cyan"))
            if self._state.group_name:
                info.add_row("Group:", Text(self._state.group_name, style="cyan"))
            info_rows = 3 + (1 if port else 0) + (1 if self._state.group_name else 0)
        else:
            info.add_row("Status:", Text("Disconnected", style="red bold"))
            info.add_row("Host:", Text(self._state.status_message, style="yellow"))
            info_rows = 2

        content = Table.grid()
        content.add_column()
        content.add_row(info)
        for _ in range(max(0, min_info_rows - info_rows)):
            content.add_row("")
        # Shortcuts
        shortcut_group = Text()
        shortcut_group.append("g", style=self._shortcut_style("switch"))
        shortcut_group.append(" change group", style="dim")
        content.add_row(shortcut_group)
        shortcut_server = Text()
        shortcut_server.append("s", style=self._shortcut_style("server"))
        shortcut_server.append(" change server", style="dim")
        content.add_row(shortcut_server)

        return Panel(content, title="Server", border_style="yellow", expand=expand)

    def _build_layout(self) -> Table:
        """Build the complete UI layout."""
        # Get terminal width and leave 1 char margin to prevent wrapping
        width = self._console.width - 1

        # Main layout table
        layout = Table.grid(expand=False)
        layout.add_column(width=width)

        # Show server selector if active
        if self._state.show_server_selector:
            selector = self._cached_panel(
                "server_selector",
                (
                    tuple(s.url for s in self._state.available_servers),
                    self._state.selected_server_index,
                    self._state.server_url,
                    self._is_highlighted("selector-up"),
                    self._is_highlighted("selector-down"),
                    self._is_highlighted("selector-enter"),
                ),
                self._build_server_selector_panel,
            )
            layout.add_row(selector)
            return layout

        narrow = width < 80

        # Now Playing panel
        now_playing = self._cached_panel(
            "now_playing",
            (
                self._state.playback_state,
                self._state.title,
                self._state.artist,
                self._state.album,
                self._is_highlighted("prev"),
                self._is_highlighted("space"),
                self._is_highlighted("next"),
            ),
            lambda: self._build_now_playing_panel(expand=True),
        )

        # Volume panel
        volume = self._cached_panel(
            "volume",
            (
                self._state.volume,
                self._state.muted,
                self._state.player_volume,
                self._state.player_muted,
                self._state.use_hardware_volume,
                self._is_highlighted("up"),
                self._is_highlighted("down"),
                self._is_highlighted("mute"),
                self._is_highlighted("group-down"),
                self._is_highlighted("group-up"),
                self._is_highlighted("group-mute"),
            ),
            lambda: self._build_volume_panel(expand=True),
        )

        # Progress bar — only cache when not playing (interpolation needs fresh renders)
        if self._state.playback_state == PlaybackStateType.PLAYING:
            progress = self._build_progress_bar(expand=True)
        else:
            progress = self._cached_panel(
                "progress",
                (self._state.track_progress_ms, self._state.track_duration_ms, width),
                lambda: self._build_progress_bar(expand=True),
            )

        # Bottom panels
        min_rows = 0 if narrow else 5

        playback = self._cached_panel(
            "playback",
            (
                narrow,
                self._state.repeat_mode,
                self._state.shuffle,
                self._is_highlighted("repeat"),
                self._is_highlighted("shuffle"),
            ),
            lambda: self._build_playback_panel(expand=True, min_info_rows=min_rows),
        )

        stream = self._cached_panel(
            "stream",
            (
                narrow,
                self._state.audio_codec,
                self._state.audio_sample_rate,
                self._state.audio_bit_depth,
                self._state.audio_channels,
                self._state.delay_ms,
                self._is_highlighted("delay-"),
                self._is_highlighted("delay+"),
            ),
            lambda: self._build_stream_quality_panel(expand=True, min_info_rows=min_rows),
        )

        server = self._cached_panel(
            "server",
            (
                narrow,
                self._state.connected,
                self._state.server_url,
                self._state.status_message,
                self._state.group_name,
                self._is_highlighted("switch"),
                self._is_highlighted("server"),
            ),
            lambda: self._build_server_panel(expand=True, min_info_rows=min_rows),
        )

        if narrow:
            layout.add_row(now_playing)
            layout.add_row(volume)
        else:
            top_row = Table.grid(expand=True)
            top_row.add_column(ratio=2)
            top_row.add_column(ratio=1)
            top_row.add_row(now_playing, volume)
            layout.add_row(top_row)

        layout.add_row(progress)

        if narrow:
            layout.add_row(playback)
            layout.add_row(stream)
            layout.add_row(server)
        else:
            bottom_row = Table.grid(expand=True)
            bottom_row.add_column(ratio=1)
            bottom_row.add_column(ratio=1)
            bottom_row.add_column(ratio=1)
            bottom_row.add_row(playback, stream, server)
            layout.add_row(bottom_row)

        # Quit shortcut below boxes
        quit_line = Text(justify="right")
        quit_line.append("q", style=self._shortcut_style("quit"))
        quit_line.append(" quit  ", style="dim")
        layout.add_row(quit_line)

        return layout

    def add_event(self, _message: str) -> None:
        """Add an event (no-op, events panel removed)."""

    def refresh(self) -> None:
        """Request a UI refresh."""
        if self._live is not None:
            self._live.refresh()

    def set_connected(self, url: str) -> None:
        """Update connection status to connected."""
        self._state.connected = True
        self._state.server_url = url
        self._state.status_message = f"Connected to {url}"
        self.refresh()

    def set_group_name(self, name: str | None) -> None:
        """Update the group name."""
        self._state.group_name = name
        self.refresh()

    def set_disconnected(self, message: str = "Disconnected") -> None:
        """Update connection status to disconnected."""
        self._state.connected = False
        self._state.status_message = message
        self.refresh()

    def set_playback_state(self, state: PlaybackStateType) -> None:
        """Update playback state."""
        # When leaving PLAYING, capture interpolated progress so display doesn't jump
        if (
            self._state.playback_state == PlaybackStateType.PLAYING
            and state != PlaybackStateType.PLAYING
            and self._state.progress_updated_at > 0
            and self._state.track_duration_ms
        ):
            elapsed_ms = (time.monotonic() - self._state.progress_updated_at) * 1000
            interpolated = (self._state.track_progress_ms or 0) + int(elapsed_ms)
            self._state.track_progress_ms = min(self._state.track_duration_ms, interpolated)
            # Reset timestamp so resume starts fresh from captured position
            self._state.progress_updated_at = time.monotonic()

        self._state.playback_state = state
        self.refresh()

    def set_metadata(
        self,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
    ) -> None:
        """Update track metadata."""
        self._state.title = title
        self._state.artist = artist
        self._state.album = album
        self.refresh()

    def set_progress(self, progress_ms: int | None, duration_ms: int | None) -> None:
        """Update track progress."""
        self._state.track_progress_ms = progress_ms
        self._state.track_duration_ms = duration_ms
        self._state.progress_updated_at = time.monotonic()
        self.refresh()

    def clear_progress(self) -> None:
        """Clear track progress completely, preventing any interpolation."""
        self._state.track_progress_ms = None
        self._state.track_duration_ms = None
        self._state.progress_updated_at = 0.0
        self.refresh()

    def set_volume(self, volume: int | None, *, muted: bool | None = None) -> None:
        """Update group volume."""
        if volume is not None:
            self._state.volume = volume
        if muted is not None:
            self._state.muted = muted
        self.refresh()

    def set_player_volume(self, volume: int, *, muted: bool) -> None:
        """Update player volume."""
        self._state.player_volume = volume
        self._state.player_muted = muted
        self.refresh()

    def set_audio_format(
        self, codec: str | None, sample_rate: int, bit_depth: int, channels: int
    ) -> None:
        """Update audio format display."""
        self._state.audio_codec = codec
        self._state.audio_sample_rate = sample_rate
        self._state.audio_bit_depth = bit_depth
        self._state.audio_channels = channels
        self.refresh()

    def set_delay(self, delay_ms: float) -> None:
        """Update the delay display."""
        self._state.delay_ms = delay_ms
        self.refresh()

    def set_repeat_shuffle(
        self,
        repeat_mode: RepeatMode | None,
        shuffle: bool | None,
    ) -> None:
        """Update repeat mode and shuffle state."""
        self._state.repeat_mode = repeat_mode
        self._state.shuffle = shuffle
        self.refresh()

    def show_server_selector(self, servers: list[DiscoveredServer]) -> None:
        """Show the server selector with available servers."""
        self._state.available_servers = servers
        self._state.selected_server_index = 0
        self._state.show_server_selector = True
        self.refresh()

    def hide_server_selector(self) -> None:
        """Hide the server selector."""
        self._state.show_server_selector = False
        self.refresh()

    def is_server_selector_visible(self) -> bool:
        """Check if the server selector is currently visible."""
        return self._state.show_server_selector

    def move_server_selection(self, delta: int) -> None:
        """Move the server selection by delta (-1 for up, +1 for down)."""
        if not self._state.available_servers:
            return
        new_index = self._state.selected_server_index + delta
        self._state.selected_server_index = max(
            0, min(len(self._state.available_servers) - 1, new_index)
        )
        self.refresh()

    def get_selected_server(self) -> DiscoveredServer | None:
        """Get the currently selected server."""
        if not self._state.available_servers:
            return None
        if 0 <= self._state.selected_server_index < len(self._state.available_servers):
            return self._state.available_servers[self._state.selected_server_index]
        return None

    def start(self) -> None:
        """Start the live display."""
        self._console.clear()
        self._live = Live(
            _RefreshableLayout(self),
            console=self._console,
            refresh_per_second=4,
            screen=True,
        )
        self._live.start()
        self._running = True

    def stop(self) -> None:
        """Stop the live display."""
        self._running = False
        if self._live is not None:
            self._live.stop()
            self._live = None

    def __enter__(self) -> Self:
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        """Context manager exit."""
        self.stop()
