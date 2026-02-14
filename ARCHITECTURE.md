# MeowFi Architecture

## Overview
MeowFi is a Windows desktop app that shares internet/VPN over a hotspot.

Runtime flow:
1. GUI sends command to local service (`127.0.0.1:38777`).
2. Service calls backend functions for hotspot/NAT/routes.
3. If system DHCP is unavailable, service starts DHCP fallback.
4. GUI reads probe/status and renders connected clients/logs.

## Components

### `python_meowfi/gui.py`
- CustomTkinter desktop interface.
- User actions: load adapters, start/stop hotspot, start/stop sharing, diagnostics.
- Auto-starts local service if not reachable.

### `python_meowfi/service.py`
- TCP JSON-RPC service endpoint.
- Command router (`ping`, `adapters`, `hotspot_start`, `start_vpn_sharing`, etc.).
- Orchestrates:
  - hotspot start/stop
  - NetNat enable/disable
  - DHCP fallback lifecycle
  - probe payload creation

### `python_meowfi/backend.py`
- Windows networking operations.
- Uses:
  - WinRT (tethering) when available
  - `netsh` fallback where needed
  - PowerShell for adapter/routes/NAT inspection
- Dynamic subnet logic:
  - detects actual private adapter IPv4/prefix
  - applies NAT/route checks on detected subnet (not hardcoded only to `192.168.137.0/24`)

### `python_meowfi/dhcp_fallback.py`
- Minimal DHCP server for hotspot subnet.
- Binds to selected private gateway IP and serves leases in that subnet.
- Provides lease snapshot to GUI/service diagnostics.

### `python_meowfi/client.py` and `python_meowfi/common.py`
- RPC client and shared request/response models/constants.

## Networking model
- Public side: selected internet/VPN adapter.
- Private side: hotspot private adapter.
- Sharing path: hotspot + NetNat + DHCP fallback (when needed).
- Conflict mitigation: detects/removes conflicting VPN routes for private subnet.

## Typical request flow (`start_vpn_sharing`)
1. Service asks backend to start hotspot.
2. Service resolves private adapter candidate (if not manually selected).
3. Backend enables/reuses NetNat for private subnet.
4. Service starts DHCP fallback on detected private gateway/prefix.
5. Service returns status; GUI displays logs/clients.

## Packaging
- `pyinstaller --onefile --windowed` builds single EXE from `python_meowfi/gui.py`.
- EXE can run GUI and service mode (`--service`) from same binary.
