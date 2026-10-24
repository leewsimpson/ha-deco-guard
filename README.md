# Deco Guard

A Home Assistant integration (HACS) for **TP-Link Deco** mesh routers that adds two things the Deco app can't do properly:

1. **Parental blocking that sticks.** Every device gets a *Network access* switch. Turning it off blocks the device's MAC at the router, the same way the Deco app's own *Block* does, so the router refuses it. The Deco app's parental controls filter by website or category, which a VPN or custom DNS gets around. A router-level MAC block doesn't depend on which sites the device visits, so a VPN doesn't help. Schedule the switches with ordinary HA automations (e.g. "turn off `switch.kids_laptop_network_access` at 21:00 on school nights").
2. **New-device quarantine.** Every poll (60 s by default), any MAC the integration has never seen is blocked immediately. It then appears on the **Deco Guard** sidebar page until an admin clicks **Allow** or **Keep blocked**. This also closes the obvious workaround for #1: if a blocked device switches to a random/private MAC, it shows up as a new device and is blocked again.

Tested with a **Deco BE85** (firmware 1.2.4). Other Deco models that share the luci web API and the Deco app's management protocol (e.g. X / XE / BE series) may work too, but they haven't been tested.

## Install

1. HACS → ⋮ → *Custom repositories* → add `https://github.com/leewsimpson/ha-deco-guard` (type *Integration*).
2. Install **Deco Guard** and restart Home Assistant.
3. *Settings → Devices & services → Add integration → Deco Guard*. Enter the **main** Deco's address (your gateway, e.g. `http://192.168.68.1`), and the **TP-Link ID email and password** that own the Deco (the account you sign in to the Deco app with; the password is the same one the Deco web UI takes).

**Upgrading from 0.1.x:** Home Assistant asks you to re-authenticate once. Enter the TP-Link ID email and password.

On first start, every device that is currently connected or already blocked is recorded as **approved**, so installing the integration doesn't block your whole house. Only devices that join after that are quarantined.

## What you get

| Thing | Purpose |
| --- | --- |
| Sidebar page **Deco Guard** (admins only) | Approve or deny new devices, block/unblock/rename/forget any device, toggle auto-block |
| `switch.<device>_network_access` | On = allowed, off = blocked at the router. One per approved device, attached to the matching HA device by MAC where one exists |
| `switch.deco_guard_auto_block_new_devices` | Turn quarantine of new devices on/off. When off, new devices are approved automatically |
| `sensor.deco_guard_pending_devices` | Number of quarantined devices (attribute `devices` lists them) |
| `sensor.deco_guard_blocked_devices` / `online_devices` | Counts |
| Event `deco_guard_new_device` | Fired when a device is quarantined (`mac`, `name`, `ip`, `client_type`). Use it to send a mobile notification |
| Actions `deco_guard.block_device`, `unblock_device`, `approve_device`, `deny_device`, `forget_device` | Each takes `mac` (`AA-BB-…` or `aa:bb:…`) |

A persistent notification with a link to the page is also created for every quarantined device.

### Device states

- **approved**: allowed on the network. Can still be parental-blocked with its switch.
- **pending**: new and quarantined (blocked) until reviewed. Re-blocked every poll if someone unblocks it in the Deco app.
- **denied**: reviewed and kept blocked. Also re-blocked every poll.

Unblocking a pending or denied device approves it.

**Blocks are enforced from Home Assistant.** Every poll re-applies any block that has gone missing from the router (someone unblocked it in the Deco app, a router reset, etc.). A block added in the Deco app is adopted and enforced the same way. To unblock, use Home Assistant: the switch, the Deco Guard page or `deco_guard.unblock_device`. *Forget* unblocks the device and deletes the record, so if it connects again it's treated as new.

## Notes and limits

- The block list is enforced by the Deco itself, so blocks keep working while Home Assistant is down. Quarantine of *new* devices only happens while HA is polling.
- A brand-new device can have up to one poll interval of access before it is blocked (configurable in the integration options, minimum 15 s).
- The Deco only reports connected clients, so names for blocked devices come from what the integration saw before blocking them.
- Deco mesh nodes are never blocked.
- A blocked device can still reach other devices on the same LAN switch (e.g. a ping from a wired PC). The block cuts it off at the Deco, which includes the internet.
- **How blocking works.** Reads go over the Deco's local web API (luci). Block and unblock go over the Deco app's own management channel: SSH on port 20001 of the main Deco, signed in with your TP-Link ID (username = SHA-1 of the email). That's the only route where a block is enforced and an unblock lifts it cleanly. A block written through the web API shows in the block list but the router doesn't enforce it, and a web-API unblock leaves an enforced block in place.
- A blocked device can't reach the Deco at all, so Home Assistant must not run on a device you block.
- If the stored login stops working, polling stops and HA asks you to re-authenticate. A rejected TP-Link ID login is never retried automatically. Repeated bad logins lock the account for a while.
- The integration keeps a logged-in admin session on the Deco, much like having the web UI open, and opens a short SSH session for each block or unblock.

## Development

```bash
pip install -r requirements_test.txt
pytest -q
```

Tests use a fake router (`tests/conftest.py`) and need Linux or macOS. HA's test harness imports POSIX-only modules.

## Credits

The Deco local API client (login handshake, AES/RSA request signing, block list calls) is adapted from **[msageha/tplink-deco-be85-api](https://github.com/msageha/tplink-deco-be85-api)** by [@msageha](https://github.com/msageha). Thank you! That project reverse-engineered the BE85's luci API. Its code is GPL-3.0-or-later, so this integration is licensed under **GPL-3.0-or-later** too (see `LICENSE`).

The Deco app protocol (TMP) client was written from the protocol descriptions in **[mthsg/pydeco-tmp](https://github.com/mthsg/pydeco-tmp)** and **[roger-/tmpkit](https://github.com/roger-/tmpkit)** (itself based on [ropbear/tmpcli](https://github.com/ropbear/tmpcli)). Thanks to them for the SSH login scheme and the TMP / appv2 framing.
