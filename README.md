# sendspin

[![pypi_badge](https://img.shields.io/pypi/v/sendspin.svg)](https://pypi.python.org/pypi/sendspin)

Connect to any [Sendspin](https://www.sendspin-audio.com) server and instantly turn your computer into an audio target that can participate in multi-room audio.

Sendspin CLI includes three apps:

- **`sendspin`** - Terminal client for interactive use
- **`sendspin daemon`** - Background daemon for headless devices
- **`sendspin serve`** - Host a Sendspin party to demo Sendspin

<img width="1144" height="352" alt="image" src="https://github.com/user-attachments/assets/5a649bde-76f6-486f-b3aa-0af5e49e0ac7" />

[![A project from the Open Home Foundation](https://www.openhomefoundation.org/badges/ohf-project.png)](https://www.openhomefoundation.org/)

## Quick Start

**Run directly with [uv](https://docs.astral.sh/uv/getting-started/installation/):**

Start client

```bash
uvx sendspin
```

Host a Sendspin party

```bash
uvx sendspin serve --demo
uvx sendspin serve /path/to/media.mp3
uvx sendspin serve https://retro.dancewave.online/retrodance.mp3
```

## Installation

**With uv:**
```bash
uv tool install sendspin
```

**Install as daemon (Linux):**
```bash
curl -fsSL https://raw.githubusercontent.com/Sendspin/sendspin-cli/refs/heads/main/scripts/systemd/install-systemd.sh | sudo bash
```

**With pip:**
```bash
pip install sendspin
```

<details>
<summary>Install from source</summary>

```bash
git clone https://github.com/Sendspin-Protocol/sendspin.git
cd sendspin
pip install .
```

</details>

**After installation, run:**
```bash
sendspin
```

The player will automatically connect to a Sendspin server on your local network and be available for playback.

## Updating

To update to the latest version of Sendspin:

**If installed with uv:**
```bash
uv tool upgrade sendspin
```

**If installed with pip:**
```bash
pip install --upgrade sendspin
```

**If installed as systemd daemon:**

The systemd daemon preserves your configuration during updates. Simply upgrade the package:

```bash
# Upgrade sendspin (the daemon installer uses uv by default)
uv tool upgrade sendspin

# Or if you installed with pip
pip install --upgrade sendspin

# Restart the service to use the new version
sudo systemctl restart sendspin
```

Your client name, audio device selection, and other settings in `/etc/default/sendspin` are preserved during the update.

> **Note:** You do **not** need to uninstall and reinstall when updating. Your configuration (client name, audio device, delay settings) is stored separately and will be preserved.

## Configuration Options

Sendspin stores settings in JSON configuration files that persist between sessions. All command-line arguments can also be set in the config file, with CLI arguments taking precedence over stored settings.

### Configuration File

Settings are stored in `~/.config/sendspin/`:
- `settings-tui.json` - Settings for the interactive TUI client
- `settings-daemon.json` - Settings for daemon mode
- `settings-serve.json` - Settings for serve mode

**Example configuration file (TUI/daemon):**
```json
{
  "player_volume": 50,
  "player_muted": false,
  "static_delay_ms": -100.0,
  "last_server_url": "ws://192.168.1.100:8927/sendspin",
  "name": "Living Room",
  "client_id": "sendspin-living-room",
  "audio_device": "2",
  "log_level": "INFO",
  "listen_port": 8927,
  "use_mpris": true
}
```

**Example configuration file (serve):**
```json
{
  "log_level": "INFO",
  "listen_port": 8927,
  "name": "My Sendspin Server",
  "source": "/path/to/music.mp3",
  "clients": ["ws://192.168.1.50:8927/sendspin", "ws://192.168.1.51:8927/sendspin"]
}
```

**Available settings:**

| Setting | Type | Mode | Description |
|---------|------|------|-------------|
| `player_volume` | integer (0-100) | TUI/daemon | Player output volume percentage |
| `player_muted` | boolean | TUI/daemon | Whether the player is muted |
| `static_delay_ms` | float | TUI/daemon | Extra playback delay in milliseconds |
| `last_server_url` | string | TUI/daemon | Server URL (used as default for `--url`) |
| `name` | string | All | Friendly name for client or server (`--name`) |
| `client_id` | string | TUI/daemon | Unique client identifier (`--id`) |
| `audio_device` | string | TUI/daemon | Audio device index or name prefix (`--audio-device`) |
| `log_level` | string | All | Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `listen_port` | integer | daemon/serve | Listen port (`--port`, default: 8927) |
| `use_mpris` | boolean | TUI/daemon | Enable MPRIS integration (default: true) |
| `source` | string | serve | Default audio source (file path or URL) |
| `clients` | array | serve | Client URLs to connect to (`--client`) |

Settings are automatically saved when changed through the TUI. You can also edit the JSON file directly while the client is not running.

### Server Connection

By default, the player automatically discovers Sendspin servers on your local network using mDNS. You can also connect directly to a specific server:

```bash
sendspin --url ws://192.168.1.100:8080/sendspin
```

**List available servers on the network:**
```bash
sendspin --list-servers
```

### Client Identification

If you want to run multiple players on the **same computer**, you can specify unique identifiers:

```bash
sendspin --id my-client-1 --name "Kitchen"
sendspin --id my-client-2 --name "Bedroom"
```

- `--id`: A unique identifier for this client (optional; defaults to `sendspin-<hostname>`, useful for running multiple instances on one computer)
- `--name`: A friendly name displayed on the server (optional; defaults to hostname)

### Audio Output Device Selection

By default, the player uses your system's default audio output device. You can list available devices or select a specific device:

**List available audio devices:**
```bash
sendspin --list-audio-devices
```

This displays all audio output devices with their IDs, channel configurations, and sample rates. The default device is marked.

**Select a specific audio device by index:**
```bash
sendspin --audio-device 2
```

**Or by name prefix:**
```bash
sendspin --audio-device "MacBook"
```

This is particularly useful when running `sendspin daemon` on headless devices or when you want to route audio to a specific output.

### Adjusting Playback Delay

The player supports adjusting playback delay to compensate for audio hardware latency or achieve better synchronization across devices.

```bash
sendspin --static-delay-ms -100
```

> **Note:** Based on limited testing, the delay value is typically a negative number (e.g., `-100` or `-150`) to compensate for audio hardware buffering.

### Daemon Mode

To run the player as a background daemon without the interactive TUI (useful for headless devices or scripts):

```bash
sendspin daemon
```

The daemon runs in the background and logs status messages to stdout. It accepts the same connection and audio options as the TUI client:

```bash
sendspin daemon --name "Kitchen" --audio-device 2
```

### Debugging & Troubleshooting

If you experience synchronization issues or audio glitches, you can enable detailed logging to help diagnose the problem:

```bash
sendspin --log-level DEBUG
```

This provides detailed information about time synchronization. The output can be helpful when reporting issues.

## Limitations & Known Issues

This player is highly experimental and has several known limitations:

- **Format Support**: Currently fixed to uncompressed 44.1kHz 16-bit stereo PCM

## Install as Daemon (systemd, Linux)

For headless devices like Raspberry Pi, you can install `sendspin daemon` as a systemd service that starts automatically on boot.

**Install:**
```bash
curl -fsSL https://raw.githubusercontent.com/Sendspin/sendspin-cli/refs/heads/main/scripts/systemd/install-systemd.sh | sudo bash
```

The installer will:
- Check and offer to install dependencies (libportaudio2, uv)
- Install sendspin via `uv tool install`
- Prompt for client name and audio device selection
- Create systemd service and configuration

**Manage the service:**
```bash
sudo systemctl start sendspin    # Start the service
sudo systemctl stop sendspin     # Stop the service
sudo systemctl status sendspin   # Check status
journalctl -u sendspin -f        # View logs
```

**Configuration:** Edit `/etc/default/sendspin` to change client name, audio device, or delay settings.

**Uninstall:**
```bash
curl -fsSL https://raw.githubusercontent.com/Sendspin/sendspin-cli/refs/heads/main/scripts/systemd/uninstall-systemd.sh | sudo bash
```

## Sendspin Party

The Sendspin client includes a mode to enable hosting a Sendspin Party. This will start a Sendspin server playing a specified audio file or URL in a loop, allowing nearby Sendspin clients to connect and listen together. It also hosts a web interface for easy playing and sharing. Fire up that home or office 🔥

```bash
# Demo mode
sendspin serve --demo
# Local file
sendspin serve /path/to/media.mp3
# Remote URL
sendspin serve https://retro.dancewave.online/retrodance.mp3
# Without pre-installing Sendspin
uvx sendspin serve /path/to/media.mp3
# Connect to specific clients
sendspin serve --demo --client ws://192.168.1.50:8927/sendspin --client ws://192.168.1.51:8927/sendspin
```
