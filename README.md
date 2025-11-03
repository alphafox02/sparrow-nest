# Sparrow-Nest

Sparrow-Nest is a lightweight FastAPI-based hub that connects one or more Sparrow-WiFi agents under a single API endpoint. It forwards requests for Wi-Fi, Bluetooth (Ubertooth + LE), GPS, and HackRF spectrum scanning while automatically handling agent failover when one node goes offline.

## Overview

Sparrow-Nest simplifies multi-agent setups. Instead of pointing the Sparrow-WiFi GUI or Falcon tools directly at an agent, you point them to Sparrow-Nest, which takes care of routing requests to the right node based on configuration and live status.

## Requirements (Ubuntu 24.04 or newer)

Most dependencies are available via apt:

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-fastapi python3-uvicorn python3-httpx python3-yaml
```

If any packages are missing, install via pip:

```bash
pip3 install fastapi uvicorn httpx pyyaml
```

## Example hub_policy.yml

```yaml
agents:
  #east:  http://127.0.0.1:8090
  west: http://192.168.68.73:8020

mode:
  networks: single
  preferred_agent: west

spectrum:
  source_agent_24: west
  source_agent_5:  west

defaults:
  falcon_targets: [west]
  bt_targets:     [west]
  record_targets: [west]

limits:
  top_ssids_per_channel: 50
  min_rssi_dbm: -92

safety:
  allow_multi_deauth: false

control:
  propagate_deauth: true
  propagate_bt: true
  propagate_record: false
```

## Running the Hub

```bash
python3 sparrow_hub.py --port 8020 --policy ./hub_policy.yml
```

By default, Sparrow-Nest will bind to `0.0.0.0:8020` and read configuration from the provided YAML file. Once running, point the Sparrow-WiFi GUI to the hub’s address instead of a specific agent.

## Key API Routes

### Wireless / Falcon
- `/wireless/interfaces`
- `/wireless/start`
- `/falcon/scan`

### Bluetooth (Ubertooth + LE)
- `/bluetooth/running`
- `/bluetooth/scanstart` / `/bluetooth/scanstop`
- `/bluetooth/beaconstart` / `/bluetooth/beaconstop`
- `/bluetooth/discoverystartp` (promiscuous)
- `/bluetooth/discoverystarta` (LE advertisement)
- `/bluetooth/discoverystop`
- `/bluetooth/discoverystatus`

### Spectrum / HackRF
- `/spectrum/hackrfstatus`
- `/spectrum/scanstart24`
- `/spectrum/scanstart5`
- `/spectrum/scanstop`
- `/spectrum/scanstatus`

### Admin
- `/admin/set_preferred?agent=<name>`
- `/admin/refresh`

## Health-Aware Routing

If a configured agent is unreachable, Sparrow-Nest automatically tries the next available one:
1. Explicitly specified agent (`?agent=<name>`)
2. `preferred_agent` from the YAML file
3. Any agent that responds to `/wireless/interfaces`

## Example Usage

```bash
curl http://192.168.68.100:8020/bluetooth/running
curl http://192.168.68.100:8020/bluetooth/discoverystarta
curl http://192.168.68.100:8020/spectrum/scanstart24
curl http://192.168.68.100:8020/spectrum/scanstop
curl http://192.168.68.100:8020/admin/refresh
```

## Optional Systemd Service

```ini
[Unit]
Description=Sparrow-Nest Hub
After=network-online.target

[Service]
User=dragon
WorkingDirectory=/usr/src/sparrow-wifi
ExecStart=/usr/bin/python3 /usr/src/sparrow-wifi/sparrow_hub.py --port 8020 --policy /usr/src/sparrow-wifi/hub_policy.yml
Restart=on-failure
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sparrownest
```

## Troubleshooting

- **404 on Bluetooth routes:** Ensure latest Sparrow-Nest with full BT passthrough routes.
- **No discovery results:** Confirm the remote agent has working BT/Ubertooth hardware.
- **Agent offline:** Test connectivity to the agent’s port (e.g. `curl http://192.168.68.73:8090/wireless/interfaces`).

## License
MIT License

## Author
Aaron (Alpha Fox)
