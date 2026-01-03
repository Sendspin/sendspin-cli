## Project Overview

Sendspin CLI is a synchronized audio player client for the [Sendspin Protocol](https://github.com/Sendspin/website/blob/main/src/spec.md). It connects to Sendspin servers via WebSocket, receives time-synchronized audio streams, and plays them back with precise timing to enable multi-room synchronized audio.

**Note**: If uncertain about how something in Sendspin is supposed to work, fetch and refer to the [protocol specification](https://github.com/Sendspin/website/blob/main/src/spec.md) for authoritative implementation details.

## Architecture

```
                            cli.py (main entry)
                                    │
                    ┌───────────────┼───────────────┐
                    │                               │
                    ▼                               ▼
        ┌─────────────────────┐         ┌─────────────────────┐
        │     app.py (TUI)    │         │   daemon.py (no UI) │
        │                     │         │                     │
        │ - Interactive mode  │         │ - Headless mode     │
        │ - Keyboard control  │         │ - Signal handlers   │
        │ - Rich UI display   │         │ - Print events      │
        └─────────────────────┘         └─────────────────────┘
                    │                               │
        ┌───────────┼───────────┐                   │
        │           │           │                   │
        ▼           ▼           ▼                   ▼
┌────────────┐ ┌─────────┐ ┌─────────┐      ┌─────────┐
│keyboard.py │ │  ui.py  │ │audio.py │      │audio.py │
│            │ │         │ │         │      │         │
│ - Keys     │ │ - TUI   │ │ - Sync  │      │ - Sync  │
│ - Commands │ │ - Panel │ │ - Output│      │ - Output│
└────────────┘ └─────────┘ └─────────┘      └─────────┘
        │           │           │                   │
        └───────────┼───────────┴───────────────────┘
                    ▼
        ┌─────────────────────┐
        │  aiosendspin        │
        │  (external library) │
        │                     │
        │ - WebSocket client  │
        │ - Protocol messages │
        │ - Time sync filter  │
        └─────────────────────┘
```

## File Responsibilities

### `README.md`
Project documentation, installation instructions, usage guide (including command line arguments), configuration options.

### `sendspin/cli.py`
Main entry point and orchestrator. Responsibilities:
- **Argument parsing**: Commands (`serve`, `daemon`), flags (`--url`, `--name`, `--id`, `--audio-device`, `--static-delay-ms`)
- **Command routing**: Routes to appropriate mode (TUI app, daemon, or server)
- **Device enumeration**: Lists audio devices and servers via `--list-audio-devices` and `--list-servers`
- **Backward compatibility**: Handles deprecated `--headless` flag with warning

### `sendspin/app.py`
Interactive TUI mode (default). Responsibilities:
- **Service discovery**: mDNS discovery of Sendspin servers
- **Connection management**: `ConnectionManager` handles reconnection with exponential backoff
- **State management**: `AppState` dataclass mirrors server state (playback, metadata, volume)
- **Audio stream handling**: `AudioStreamHandler` bridges between client and `AudioPlayer`
- **Event callbacks**: Routes server messages to appropriate handlers (metadata, group updates, commands)
- **UI coordination**: Manages `SendspinUI` and keyboard input loop

### `sendspin/daemon.py`
Daemon mode for headless operation (via `sendspin daemon` or deprecated `--headless`). Responsibilities:
- **Headless operation**: Runs without UI or keyboard input
- **Service discovery**: Same mDNS discovery as TUI mode
- **Connection management**: Uses same `ConnectionManager` from `app.py`
- **Audio playback**: Same synchronized audio playback
- **Event logging**: Prints events to stdout instead of UI
- **Signal handling**: SIGINT/SIGTERM for graceful shutdown

### `sendspin/keyboard.py`
Keyboard input handling for interactive control. Responsibilities:
- **Key capture**: Uses `readchar` library for single-keypress detection
- **Command parsing**: `CommandHandler` class parses and executes commands
- **Media commands**: play, pause, stop, next, previous, shuffle, repeat modes, switch group
- **Volume control**: Group volume (`vol+`/`vol-`/`mute`) and player volume (`pvol+`/`pvol-`/`pmute`)
- **Delay adjustment**: Real-time static delay adjustment via `delay` command
- **Keyboard shortcuts**: Arrow keys (prev/next/volume), space (toggle), m (mute), g (switch group), q (quit)

### `sendspin/ui.py`
Rich-based terminal UI for visual feedback. Responsibilities:
- **Now Playing panel**: Track title, artist, album with playback shortcuts
- **Volume panel**: Group and player volume with mute indicators
- **Progress bar**: Real-time track progress with interpolation during playback
- **Status line**: Connection status and quit shortcut
- **Shortcut highlighting**: Visual feedback when shortcuts are pressed (0.15s highlight)
- **State management**: `UIState` dataclass for all display state

### `sendspin/audio.py`
Time-synchronized audio playback engine. Responsibilities:
- **Audio output**: Uses `sounddevice` (PortAudio) for low-latency playback
- **Time synchronization**: DAC-to-loop time calibration for precise playback timing
- **Buffer management**: Queue-based buffering with gap/overlap handling
- **Sync correction**: Sample drop/insert for drift correction (Kalman-filtered)
- **Playback state machine**: INITIALIZING → WAITING_FOR_START → PLAYING → REANCHORING
- **Volume control**: Software volume with power curve for natural control

### `sendspin/__init__.py`
Package entry point, exports `main` from `cli.py`.

## Key Dependencies

- **aiosendspin**: Async Sendspin protocol client (WebSocket, time sync, messages)
- **sounddevice**: PortAudio wrapper for audio I/O
- **rich**: Terminal UI rendering
- **readchar**: Cross-platform keyboard input
- **zeroconf**: mDNS service discovery
- **numpy**: Audio sample processing

## Common Tasks

### Running the player
```bash
uv run sendspin                                # TUI mode with auto-discovery
uv run sendspin --url ws://host:port/sendspin  # TUI mode with direct connection
uv run sendspin daemon                         # Daemon mode (headless) with auto-discovery
uv run sendspin daemon --url ws://host:port/sendspin  # Daemon mode with direct connection
```

### Development commands
```bash
uv run ruff check --fix .    # Lint and auto-fix
uv run ruff format .         # Format
uv run mypy sendspin         # Type check
```

### Adding a new keyboard shortcut
1. Add key handler in `keyboard.py` `keyboard_loop()` function
2. Add UI highlight call: `ui.highlight_shortcut("shortcut_name")`
3. Execute command via `handler.execute("command")`
4. Update shortcut display in `ui.py` `_build_now_playing_panel()` or relevant panel

### Adding a new media command
1. Check if `MediaCommand` enum in aiosendspin supports it
2. Add command alias in `CommandHandler.execute()` in `keyboard.py`
3. Command is sent via `client.send_group_command(MediaCommand.XXX)`
