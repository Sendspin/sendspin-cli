## Project Overview

Sendspin CLI is a synchronized audio player client for the [Sendspin Protocol](https://github.com/Sendspin-Protocol/spec). It connects to Sendspin servers via WebSocket, receives time-synchronized audio streams, and plays them back with precise timing to enable multi-room synchronized audio.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         cli.py (main)                           │
│  - Entry point, argument parsing, async event loop              │
│  - mDNS service discovery                                       │
│  - Connection management with auto-reconnect                    │
│  - State management (CLIState)                                  │
│  - Coordinates all other modules                                │
└─────────────────────────────────────────────────────────────────┘
          │                    │                    │
          ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│   keyboard.py   │  │     ui.py       │  │    audio.py     │
│                 │  │                 │  │                 │
│ - Key capture   │  │ - Rich TUI      │  │ - Audio output  │
│ - Command parse │  │ - Now Playing   │  │ - Time sync     │
│ - Media control │  │ - Volume panel  │  │ - Buffer mgmt   │
└─────────────────┘  └─────────────────┘  └─────────────────┘
          │                    │                    │
          └────────────────────┼────────────────────┘
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

### `sendspin/cli.py`
Main entry point and orchestrator. Responsibilities:
- **Argument parsing**: `--url`, `--name`, `--id`, `--audio-device`, `--static-delay-ms`, etc.
- **Service discovery**: mDNS discovery of Sendspin servers via `ServiceDiscovery` class
- **Connection management**: `ConnectionManager` handles reconnection with exponential backoff
- **State management**: `CLIState` dataclass mirrors server state (playback, metadata, volume)
- **Audio stream handling**: `AudioStreamHandler` bridges between client and `AudioPlayer`
- **Event callbacks**: Routes server messages to appropriate handlers (metadata, group updates, commands)

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
uv run sendspin              # Auto-discover server
uv run sendspin --url ws://host:port/sendspin  # Direct connection
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
