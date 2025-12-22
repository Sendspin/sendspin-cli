# sendspin

[![pypi_badge](https://img.shields.io/pypi/v/sendspin.svg)](https://pypi.python.org/pypi/sendspin)

Connect to any [Sendspin](https://www.sendspin-audio.com) server and instantly turn your computer into an audio target that can participate in multi-room audio. Sendspin CLI includes both a TUI and a headless mode.

<img width="1144" height="352" alt="image" src="https://github.com/user-attachments/assets/5a649bde-76f6-486f-b3aa-0af5e49e0ac7" />

[![A project from the Open Home Foundation](https://www.openhomefoundation.org/badges/ohf-project.png)](https://www.openhomefoundation.org/)

## Quick Start

**Run directly with [uv](https://docs.astral.sh/uv/getting-started/installation/):**
```bash
uvx sendspin
```

## Installation

**With pip:**
```bash
pip install sendspin
```

**With uv:**
```bash
uv tool install sendspin
```

<details>
<summary>Install from source</summary>

```bash
git clone https://github.com/Sendspin-Protocol/sendspin.git
cd sendspin
pip install .
```

</details>

<details>
<summary>Installing on Raspbian</summary>
If you're installing Sendspin on a Raspberry Pi running Raspbian without any user-interface, you'll need to run the following commands before you run `pip`:

```
sudo apt update # Updates the apt repositories to the latest version
sudo apt install python3-pip # Makes sure that you can install via Pip
sudo apt install libffi-dev libportaudio2 # Ensure that libffi and portaudio are available
```

Once you've done this, you can create a virual environment and install Sendspin as a dedicated user:

```
useradd -m -G audio sendspin
su - sendspin
python3 -m venv .venv
source .venv/bin/activate
pip install sendspin
```

To run Sendspin, either switch to the user via SSH and run `source .venv/bin/activate && sendspin` or copy the following serivce file to `/etc/systemd/system/sendspin.service`:

```
[Unit]
Description=Sendspin Media Player
After=network.target
StartLimitIntervalSec=0
[Service]
Type=simple
Restart=always
RestartSec=1
User=player
ExecStart=/home/sendspin/.venv/bin/sendspin --headless

[Install]
WantedBy=multi-user.target
```

Once you've done that, run `systemctl daemon-reload && systemctl enable sendspin`, this will install the service and start it at boot.

If you need to change any of the command line arguments, just edit the file and re-run the `systemctl daemon-reload` command.
</details>

**After installation, run:**
```bash
sendspin
```

The player will automatically connect to a Sendspin server on your local network and be available for playback.

## Configuration Options

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

This is particularly useful for headless devices or when you want to route audio to a specific output.

### Adjusting Playback Delay

The player supports adjusting playback delay to compensate for audio hardware latency or achieve better synchronization across devices.

```bash
sendspin --static-delay-ms -100
```

> **Note:** Based on limited testing, the delay value is typically a negative number (e.g., `-100` or `-150`) to compensate for audio hardware buffering.

### Headless Mode

To run the player without the interactive terminal UI (useful for background services or scripts):

```bash
sendspin --headless
```

In headless mode, status messages are printed to stdout instead of the TUI.

### Debugging & Troubleshooting

If you experience synchronization issues or audio glitches, you can enable detailed logging to help diagnose the problem:

```bash
sendspin --log-level DEBUG
```

This provides detailed information about time synchronization. The output can be helpful when reporting issues.

## Limitations & Known Issues

This player is highly experimental and has several known limitations:

- **Format Support**: Currently fixed to uncompressed 44.1kHz 16-bit stereo PCM
- **Configuration Persistence**: Settings are not persistently stored; delay must be reconfigured on each restart using the `--static-delay-ms` option
