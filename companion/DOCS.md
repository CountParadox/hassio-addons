# Bitfocus Companion

[Bitfocus Companion](https://bitfocus.io/companion) turns Stream Decks and web
buttons into a control surface for broadcast, AV and streaming equipment.

This add-on runs the official Companion image
(`ghcr.io/bitfocus/companion/companion`) unchanged, with its data stored in the
add-on's config folder.

## Using it

1. Install and start the add-on.
2. Open the web interface with **Open Web UI** (inside Home Assistant), or
   browse to `http://<home-assistant-address>:8000`.

Turn on **Show in sidebar** on the add-on's Info page to get a Companion entry
in the Home Assistant sidebar.

The view inside Home Assistant uses ingress, so it also works through Home
Assistant remote access and is protected by your Home Assistant login. Tablets,
web buttons, Satellite and the HTTP API should use the direct address on port
8000.

## Configuration

| Option | Default | Description |
| --- | --- | --- |
| `admin_port` | `8000` | Port of the Companion web interface. |

The watchdog always checks port 8000, so turn it off if you change
`admin_port`. Ingress follows the port automatically.

Everything else is configured inside Companion.

## Network

The add-on uses the host network, the same as a Companion installed directly on
a computer. Devices on your LAN can reach it at the Home Assistant address:

| Port | Use |
| --- | --- |
| 8000 (or `admin_port`) | Web interface, web buttons and the HTTP API |
| 16622 | Companion Satellite (TCP) |
| 16623 | Companion Satellite (WebSocket) |

Other ports (OSC, TCP/UDP remote control, Art-Net and so on) are opened when
you enable those features in Companion's settings.

## Stream Decks and other USB surfaces

USB surfaces plugged into the Home Assistant machine are **not** available to
Companion when it runs in a container. Connect them through
[Companion Satellite](https://bitfocus.io/companion-satellite) on another
computer or a Raspberry Pi and point it at the Home Assistant address.

## Data and backups

Companion's database, its `config.yaml` launch options and downloaded modules
live in the add-on config folder (`/addon_configs/<id>_bitfocus_companion`).
That folder is included in Home Assistant backups of the add-on.

To move an existing Companion setup in, export it from the old installation
(**Import / Export**) and import the file in the add-on's web interface.

Earlier versions of this add-on stored data in `/config/companion`. That data
is not migrated; use export and import if you still need it.

## Updates

A GitHub workflow in this repository checks for new stable Companion releases
every day and publishes a matching add-on version. Updates then appear in Home
Assistant like any other add-on update. Companion upgrades its own database on
first start; take a backup before moving between major versions.

## Support

- Add-on problems: <https://github.com/CountParadox/hassio-addons/issues>
- Companion itself: <https://github.com/bitfocus/companion>
