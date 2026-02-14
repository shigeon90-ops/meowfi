# MeowFi Python Rewrite

Python rewrite of the whole project with the same core flow:
- `service.py`: background control service (TCP JSON RPC on 127.0.0.1:38777)
- `gui.py`: desktop GUI (`tkinter`)
- `backend.py`: hotspot/ICS/diag logic over PowerShell + netsh

## Requirements
- Windows 10/11
- Python 3.11+ (3.10 also works)
- Python package: `winsdk` (`python -m pip install winsdk`)
- Run `service.py` as Administrator (required for hotspot/ICS)

## Run
1. Start service (Admin):
   - `python -m python_meowfi.service`
2. Start GUI:
   - `python -m python_meowfi.gui`

## Commands implemented
- `ping`
- `adapters`
- `capability`
- `status`
- `hotspot_start`
- `hotspot_stop`
- `ics_share`
- `ics_disable`
- `start_vpn_sharing`
- `stop_vpn_sharing`
- `probe_step`
- `diag_snapshot`

## Notes
- Hotspot control uses `netsh wlan hostednetwork`.
- ICS control uses `HNetCfg.HNetShare` COM from PowerShell.
- If your Wi-Fi driver does not support hosted network, hotspot start will fail and diagnostic text will show why.
