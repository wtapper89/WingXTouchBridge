# WING X-Touch Bridge

A browser-configurable bridge that lets a Behringer X-Touch control a Behringer WING or WING Rack through a Raspberry Pi. It runs alongside Bitfocus Companion on a separate web port.

## Features

- Map X-Touch channel strips 1-8 to any WING input channel, or leave a strip unassigned.
- Use the ninth master fader for Main 1-4, Matrix 1-8, Aux 1-8, DCA 1-16, or nothing. It defaults to Main 1.
- Keep motorized faders and mute LEDs synchronized with changes made on the WING.
- Show live channel meters on the X-Touch.
- Read channel names and colors from the WING and show them in browser dropdowns.
- Match scribble-strip names and colors, with per-strip overrides.
- Reconnect and restore the surface automatically after the X-Touch is power-cycled.
- Configure everything from a browser on port `8088` by default.

## Requirements

- Raspberry Pi running Raspberry Pi OS or the Bitfocus Companion Pi image
- Python 3
- Behringer X-Touch connected to the Pi by USB
- WING and Pi on the same network

## Quick Start

On the Raspberry Pi:

```bash
git clone https://github.com/wtapper89/WingXTouchBridge.git
cd WingXTouchBridge
chmod +x install_on_pi.sh
sudo ./install_on_pi.sh
```

The installer prints the setup-page address when it finishes. Open that address from a browser, normally:

```text
http://PI-IP-ADDRESS:8088/
```

Then:

1. Enter the WING IP address.
2. Choose the X-Touch MIDI input and output, or leave both on Auto.
3. Choose `CTRL USB` for working scribble-strip colors.
4. Press **Refresh WING sources**.
5. Assign the eight channel strips and choose the **Master Fader Target**.
6. Press **Save settings**.

Settings are stored in `/etc/wing-xtouch-bridge/config.json` and survive restarts.

## X-Touch Mode

Power on the X-Touch while holding the first channel Select button to change its operating mode.

- **CTRL USB** is recommended. It supports the tested Behringer color messages. Its channel meters display a moving level LED because that is how the X-Touch firmware handles meter messages in CTRL mode.
- **MC USB** can display filled meter bars, but the X-Touch does not apply the tested scribble-strip color commands in this mode.

The physical mode and the **Surface Mode** choice on the setup page must match.

## Master Fader

The large ninth fader controls Main Output 1 by default. The setup page can assign it to:

- Main Output 1-4
- Matrix 1-8
- Aux 1-8
- DCA 1-16
- None

The bridge listens for WING feedback, so moving the selected target in WING Edit or on the console also moves the X-Touch master fader.

## Service Commands

```bash
sudo systemctl status wing-xtouch-bridge
sudo systemctl restart wing-xtouch-bridge
sudo journalctl -u wing-xtouch-bridge -f
```

## Updating

```bash
cd WingXTouchBridge
git pull
sudo ./install_on_pi.sh
```

The installer keeps an existing configuration file when updating.

## Troubleshooting

- If the surface is blank, confirm the MIDI input and output on the setup page. The bridge checks for reconnects automatically.
- If colors do not change, confirm both the physical X-Touch mode and browser setting are `CTRL USB`.
- If faders move to the wrong level, adjust **Physical 0 dB Position**. The tested default is `0.731`.
- If no WING sources appear, verify the WING IP and that TCP port `2222` and OSC port `2223` are reachable.
- Companion and this bridge can run together because the bridge uses web port `8088`.

## Security

The setup page has no login. Run it only on a trusted control network and do not expose port `8088` to the public internet.
