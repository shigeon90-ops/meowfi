# MeowFi

MeowFi is a Windows app for sharing internet (including VPN traffic) over a Wi-Fi hotspot.

## Why I built this

I created MeowFi because VPN sharing over Wi-Fi on Windows was unreliable and painful for me in day-to-day use.  
I was tired of manually tweaking adapters, ICS/NAT behavior, and hotspot settings every time something broke.

MeowFi is a small open-source alternative to paid VPN sharing tools:
- one app instead of manual Windows network setup
- simpler adapter flow
- built-in diagnostics when sharing fails
- practical focus: make VPN hotspot sharing actually work

Current implementation is Python-based:
- `python_meowfi/service.py` - local control service (`127.0.0.1:38777`)
- `python_meowfi/gui.py` - desktop GUI (CustomTkinter)
- `python_meowfi/backend.py` - hotspot/NAT/network operations
- `python_meowfi/dhcp_fallback.py` - DHCP fallback for hotspot clients

## Features
- Start/stop hotspot
- Start/stop VPN sharing flow
- Adapter discovery and selection
- NetNat-based sharing path
- DHCP fallback for client IP assignment
- Live diagnostics/probe + connected clients view
- Single EXE build via PyInstaller

## Requirements
- Windows 10/11
- Python 3.10+
- Administrator rights (required for hotspot/network operations)

## Install
```powershell
python -m pip install --upgrade pip
python -m pip install customtkinter winsdk pyinstaller
```

## Run From Source
Run GUI (it will auto-start service when needed):
```powershell
python -m python_meowfi.gui
```

Optional: run service manually (Admin shell):
```powershell
python -m python_meowfi.service
```

## Build EXE
```powershell
pyinstaller --noconfirm --clean --onefile --windowed --name MeowFi `
  --paths . `
  --hidden-import python_meowfi.service `
  --hidden-import python_meowfi.client `
  --hidden-import python_meowfi.common `
  --hidden-import python_meowfi.backend `
  --hidden-import python_meowfi.dhcp_fallback `
  python_meowfi\gui.py
```

Output:
- `dist\MeowFi.exe`

Run EXE as Administrator.

## Usage
1. Click `CONNECT SERVICE`
2. Click `REFRESH ADAPTERS`
3. Select:
   - `Public Internet Source` = VPN adapter (or normal uplink)
   - `Hotspot Private Adapter` = hotspot private adapter
4. Set SSID/password/band
5. Click `INITIATE VPN SHARING`

## Troubleshooting
- Hotspot fails to start:
  - Check Wi-Fi adapter/radio enabled in Windows
  - Run app as Administrator
- Clients connect but no internet:
  - Verify correct public/private adapters
  - Check VPN kill-switch/LAN-block settings
  - Retry sharing start to refresh NAT/routes
- No IP on client:
  - Ensure DHCP fallback started (see app logs)

## Project Structure
```text
python_meowfi/
  backend.py
  client.py
  common.py
  dhcp_fallback.py
  gui.py
  service.py
  run_gui.bat
  run_service.bat
  run_service_admin.bat
```

## Download
- Latest releases: `https://github.com/shigeon90-ops/meowfi/releases`
- Direct page: open repository -> `Releases` (right sidebar)

## Update Project
For normal updates:
```powershell
git add .
git commit -m "Describe your change"
git push
```

## Publish EXE To Releases
1. Build `dist\MeowFi.exe`
2. Open: `https://github.com/shigeon90-ops/meowfi/releases`
3. Click `Draft a new release`
4. Set tag (example: `v1.0.0`)
5. Upload `MeowFi.exe` as release asset
6. Publish release
