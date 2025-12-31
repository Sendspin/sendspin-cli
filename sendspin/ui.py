"""Rich-based terminal UI for the Sendspin CLI."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Self

from aiosendspin.models.types import PlaybackStateType
from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


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
class DiscoveredServerInfo:
    """Information about a discovered server for display."""

    name: str
    url: str
    host: str
    port: int


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
    available_servers: list[DiscoveredServerInfo] = field(default_factory=list)
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

    # Delay
    delay_ms: float = 0.0

    # Shortcut highlight
    highlighted_shortcut: str | None = None
    highlight_time: float = 0.0


class SendspinUI:
    """Rich-based terminal UI for the Sendspin CLI."""

    def __init__(self) -> None:
        """Initialize the UI."""
        self._console = Console()
        self._state = UIState()
        self._live: Live | None = None
        self._running = False

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

    def highlight_shortcut(self, shortcut: str) -> None:
        """Highlight a shortcut temporarily."""
        self._state.highlighted_shortcut = shortcut
        self._state.highlight_time = time.monotonic()
        self.refresh()

    def _build_now_playing_panel(self, *, expand: bool = False) -> Panel:
        """Build the now playing panel."""
        # Show prompt when nothing is playing (5 lines total)
        if not self._state.title:
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
            line3 = Text()
            line3.append("Press ", style="dim")
            line3.append("[", style="bold cyan")
            line3.append(" and ", style="dim")
            line3.append("]", style="bold cyan")
            line3.append(" to adjust audio delay", style="dim")
            content.add_row(line3)
            content.add_row("")
            return Panel(content, title="Now Playing", border_style="blue", expand=expand)

        # Info grid with label/value columns
        info = Table.grid(padding=(0, 1))
        info.add_column(style="dim", width=8)
        info.add_column()

        info.add_row("Title:", Text(self._state.title, style="bold white"))
        info.add_row("Artist:", Text(self._state.artist or "Unknown artist", style="cyan"))
        info.add_row("Album:", Text(self._state.album or "Unknown album", style="dim"))

        # Vertical container for info + shortcuts (5 lines total)
        content = Table.grid()
        content.add_column()
        content.add_row(info)
        content.add_row("")  # Line 4: spacing

        # Line 5: playback shortcuts (always show when track is loaded)
        space_label = "pause" if self._state.playback_state == PlaybackStateType.PLAYING else "play"
        shortcuts = Text()
        shortcuts.append("←", style=self._shortcut_style("prev"))
        shortcuts.append(" prev  ", style="dim")
        shortcuts.append("<space>", style=self._shortcut_style("space"))
        shortcuts.append(f" {space_label}  ", style="dim")
        shortcuts.append("→", style=self._shortcut_style("next"))
        shortcuts.append(" next  ", style="dim")
        shortcuts.append("g", style=self._shortcut_style("switch"))
        shortcuts.append(" change group", style="dim")
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
            progress_ms = min(duration_ms, progress_ms + int(elapsed_ms))

        percentage = min(100, progress_ms / duration_ms * 100) if duration_ms > 0 else 0

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
        info.add_row("Player:", Text(pvol_text, style=pvol_style))

        # Vertical container for info + shortcuts (5 lines total)
        content = Table.grid()
        content.add_column()
        content.add_row(info)
        content.add_row("")  # Line 3: spacing
        content.add_row("")  # Line 4: spacing

        # Line 5: volume shortcuts
        shortcuts = Text()
        shortcuts.append("↑", style=self._shortcut_style("up"))
        shortcuts.append(" up  ", style="dim")
        shortcuts.append("↓", style=self._shortcut_style("down"))
        shortcuts.append(" down  ", style="dim")
        shortcuts.append("m", style=self._shortcut_style("mute"))
        shortcuts.append(" mute", style="dim")
        content.add_row(shortcuts)

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
        shortcuts.append(" connect", style="dim")
        content.add_row(shortcuts)

        return Panel(content, title="Select Server", border_style="cyan")

    def _build_layout(self) -> Table:
        """Build the complete UI layout."""
        # Get terminal width and leave 1 char margin to prevent wrapping
        width = self._console.width - 1

        # Main layout table
        layout = Table.grid(expand=False)
        layout.add_column(width=width)

        # Show server selector if active
        if self._state.show_server_selector:
            layout.add_row(self._build_server_selector_panel())
            return layout

        # Top row: Now Playing + Volume
        top_row = Table.grid(expand=True)
        top_row.add_column(ratio=2)
        top_row.add_column(ratio=1)
        top_row.add_row(
            self._build_now_playing_panel(expand=True),
            self._build_volume_panel(expand=True),
        )
        layout.add_row(top_row)

        # Progress bar
        layout.add_row(self._build_progress_bar(expand=True))

        # Status line at bottom
        layout.add_row(self._build_status_line())

        return layout

    def _build_status_line(self) -> Table:
        """Build the status line at the bottom."""
        # Left side: connection status + delay
        left = Text()
        left.append("  ")  # Align with panel content
        if self._state.connected and self._state.server_url:
            # Extract host from ws://host:port/path
            url = self._state.server_url
            host = url.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
            # Remove brackets from IPv6
            host = host.strip("[]")
            if self._state.group_name:
                left.append(f"Connected to {self._state.group_name} at {host}", style="dim")
            else:
                left.append(f"Connected to {host}", style="dim")
            # Add delay info
            delay = self._state.delay_ms
            if delay >= 0:
                left.append(f" · Delay: +{delay:.0f}ms", style="dim")
            else:
                left.append(f" · Delay: {delay:.0f}ms", style="dim")
        else:
            left.append(self._state.status_message, style="dim yellow")

        # Right side: delay shortcuts + server selector + quit shortcut
        right = Text()
        right.append("[", style=self._shortcut_style("delay-"))
        right.append("/", style="dim")
        right.append("]", style=self._shortcut_style("delay+"))
        right.append(" delay  ", style="dim")
        right.append("s", style=self._shortcut_style("server"))
        right.append(" server  ", style="dim")
        right.append("q", style=self._shortcut_style("quit"))
        right.append(" quit", style="dim")

        # Use grid for left/right alignment with padding column
        line = Table.grid(expand=True)
        line.add_column(ratio=1)
        line.add_column(justify="right")
        line.add_column(width=2)  # Right padding to align with panel interior
        line.add_row(left, right, "")
        return line

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

    def set_delay(self, delay_ms: float) -> None:
        """Update the delay display."""
        self._state.delay_ms = delay_ms
        self.refresh()

    def show_server_selector(self, servers: list[DiscoveredServerInfo]) -> None:
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

    def get_selected_server(self) -> DiscoveredServerInfo | None:
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
