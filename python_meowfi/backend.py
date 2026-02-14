from __future__ import annotations

"""Windows networking backend used by the MeowFi service.

This module wraps PowerShell/netsh/WinRT operations for hotspot, NAT, routes,
adapter inventory, and diagnostics.
"""


import asyncio
import ipaddress
import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


def ps_quote(text: str) -> str:
    return text.replace("'", "''")


try:
    from winsdk.windows.networking.connectivity import NetworkInformation
    from winsdk.windows.networking.networkoperators import (
        NetworkOperatorTetheringAccessPointConfiguration,
        NetworkOperatorTetheringManager,
        TetheringWiFiBand,
    )

    HAVE_WINSDK = True
except Exception:
    NetworkInformation = None  # type: ignore[assignment]
    NetworkOperatorTetheringAccessPointConfiguration = None  # type: ignore[assignment]
    NetworkOperatorTetheringManager = None  # type: ignore[assignment]
    TetheringWiFiBand = None  # type: ignore[assignment]
    HAVE_WINSDK = False


def run_command(cmd: list[str], timeout_sec: int = 20) -> CommandResult:
    """Run a command hidden on Windows and capture stdout/stderr."""
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        startupinfo=startupinfo,
    )
    return CommandResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


def run_powershell(script: str, timeout_sec: int = 25) -> CommandResult:
    return run_command(
        [
            "powershell",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        timeout_sec=timeout_sec,
    )


def run_netsh(args: list[str], timeout_sec: int = 15) -> CommandResult:
    return run_command(["netsh", *args], timeout_sec=timeout_sec)


def ps_json(script: str, timeout_sec: int = 25) -> tuple[bool, Any, str]:
    """Execute PowerShell and parse JSON result."""
    # Run the caller script in a script block first, then serialize output.
    # This avoids parser errors like "An empty pipe element is not allowed"
    # when multi-line snippets contain assignments/newlines.
    wrapped = f"$ErrorActionPreference='Stop'; & {{ {script} }} | ConvertTo-Json -Depth 8 -Compress"
    result = run_powershell(wrapped, timeout_sec=timeout_sec)
    if result.exit_code != 0:
        return False, None, result.stderr or result.stdout
    if not result.stdout:
        return True, None, ""
    try:
        return True, json.loads(result.stdout), ""
    except json.JSONDecodeError as ex:
        return False, None, f"JSON decode failed: {ex}. Raw: {result.stdout[:500]}"


def ensure_services_running(names: list[str]) -> list[str]:
    notes: list[str] = []
    for name in names:
        script = (
            "$svc = Get-Service -Name '{name}' -ErrorAction SilentlyContinue; "
            "if($null -eq $svc){{ Write-Output 'missing'; exit 0 }}; "
            "if($svc.Status -ne 'Running'){{ Start-Service -Name '{name}' -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 500 }}; "
            "$svc2 = Get-Service -Name '{name}' -ErrorAction SilentlyContinue; "
            "if($null -eq $svc2){{ Write-Output 'missing' }} else {{ Write-Output $svc2.Status }}"
        ).format(name=name)
        r = run_powershell(script, timeout_sec=10)
        status = (r.stdout or r.stderr or "unknown").strip()
        notes.append(f"{name}:{status}")
    return notes


def get_adapters() -> tuple[bool, list[dict[str, Any]], str]:
    """Enumerate adapters and attach heuristics used by GUI selection."""
    script = r"""
$adapters = Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | ForEach-Object {
    $cfg = $null
    try {
        $cfg = Get-NetIPConfiguration -InterfaceAlias $_.Name -ErrorAction Stop
    } catch {
        $cfg = $null
    }
    $ipv4 = @()
    $gw = @()
    if($null -ne $cfg) {
        $ipv4 = @($cfg.IPv4Address | ForEach-Object { $_.IPAddress })
        $gw = @($cfg.IPv4DefaultGateway | ForEach-Object { $_.NextHop })
    }
    [PSCustomObject]@{
        id = $_.InterfaceGuid
        name = $_.Name
        description = $_.InterfaceDescription
        type = $_.InterfaceType
        status = $_.Status.ToString()
        hasIpv4 = ($ipv4.Count -gt 0)
        hasGateway = ($gw.Count -gt 0)
        ipv4 = $ipv4
        gateway = $gw
    }
}
$adapters
"""
    ok, data, err = ps_json(script, timeout_sec=30)
    if not ok:
        return False, [], err
    if data is None:
        return True, [], ""
    if isinstance(data, dict):
        data = [data]

    result: list[dict[str, Any]] = []
    for item in data:
        name = str(item.get("name", ""))
        descr = str(item.get("description", ""))
        hay = f"{name} {descr}".lower()
        is_up = str(item.get("status", "")).lower() == "up"
        has_ipv4 = bool(item.get("hasIpv4", False))
        has_gw = bool(item.get("hasGateway", False))
        is_vpn = any(x in hay for x in ["wireguard", "wintun", "vpn", "openvpn", "tap", "amnezia", "ppp"])
        is_private = any(x in hay for x in ["wi-fi direct", "mobile hotspot", "hosted network"])
        is_public = is_up and has_ipv4 and (has_gw or is_vpn)
        result.append(
            {
                "id": str(item.get("id", "")).upper(),
                "name": name,
                "description": descr,
                "type": str(item.get("type", "Unknown")),
                "statusRaw": str(item.get("status", "")),
                "isUp": is_up,
                "hasIpv4Address": has_ipv4,
                "hasDefaultGateway": has_gw,
                "isLikelyVpn": is_vpn,
                "isLikelyHotspotPrivate": is_private,
                "isLikelyInternetSource": is_public,
                "ipv4": item.get("ipv4", []),
                "gateway": item.get("gateway", []),
            }
        )

    result.sort(key=lambda a: (not a["isLikelyInternetSource"], not a["isLikelyVpn"], a["name"].lower()))
    return True, result, ""


def _normalize_adapter_id(adapter_id: str | None) -> str | None:
    if not adapter_id:
        return None
    return adapter_id.strip().strip("{}").lower()


def _adapter_flags(adapter_id: str | None) -> tuple[bool, bool]:
    # returns: (is_vpn, is_wireless)
    nid = _normalize_adapter_id(adapter_id)
    if not nid:
        return False, False
    index = _adapter_index()
    item = index.get(nid)
    if item:
        return item["is_vpn"], item["is_wireless"]
    return False, False


def _adapter_index() -> dict[str, dict[str, bool]]:
    ok, adapters, _ = get_adapters()
    if not ok:
        return {}
    index: dict[str, dict[str, bool]] = {}
    for a in adapters:
        aid = _normalize_adapter_id(a.get("id"))
        if not aid:
            continue
        name = str(a.get("name", "")).lower()
        desc = str(a.get("description", "")).lower()
        index[aid] = {
            "is_vpn": bool(a.get("isLikelyVpn")),
            "is_wireless": ("wi-fi" in name or "wireless" in desc or "802.11" in desc),
            "is_ready": str(a.get("statusRaw", "")).lower() not in ("disabled", "not present"),
        }
    return index


def _enum_tail(value: Any) -> str:
    text = str(value)
    if "." in text:
        text = text.split(".")[-1]
    return text


def _winrt_profiles() -> list[Any]:
    profiles: list[Any] = []
    internet = NetworkInformation.get_internet_connection_profile()
    if internet is not None:
        profiles.append(internet)
    for p in list(NetworkInformation.get_connection_profiles()):
        profiles.append(p)

    unique: list[Any] = []
    seen: set[tuple[str | None, str | None]] = set()
    for p in profiles:
        na = p.network_adapter
        aid = None if na is None else str(na.network_adapter_id).lower()
        key = (aid, p.profile_name)
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def _select_winrt_profiles(adapter_id: str | None) -> list[Any]:
    all_profiles = _winrt_profiles()
    if not all_profiles:
        return []
    adapter_index = _adapter_index()

    target_id = _normalize_adapter_id(adapter_id)
    selected: list[Any] = []
    if target_id:
        for p in all_profiles:
            na = p.network_adapter
            aid = None if na is None else str(na.network_adapter_id).lower()
            if aid == target_id:
                selected.append(p)
                break

    is_vpn = adapter_index.get(_normalize_adapter_id(adapter_id) or "", {}).get("is_vpn", False)
    if not is_vpn:
        if selected:
            return selected
        return [all_profiles[0]]

    # VPN selected: prefer non-VPN wireless profiles.
    candidates: list[Any] = []
    seen_adapter: set[str] = set()
    for p in all_profiles:
        na = p.network_adapter
        pid = None if na is None else _normalize_adapter_id(str(na.network_adapter_id))
        p_is_vpn = adapter_index.get(pid or "", {}).get("is_vpn", False)
        p_is_wireless = adapter_index.get(pid or "", {}).get("is_wireless", False)
        if p_is_vpn or not p_is_wireless:
            continue
        cap = NetworkOperatorTetheringManager.get_tethering_capability_from_connection_profile(p)
        if _enum_tail(cap) == "ENABLED":
            if pid and pid in seen_adapter:
                continue
            if pid:
                seen_adapter.add(pid)
            candidates.append(p)

    if candidates:
        return candidates[:3]

    # Fallback: any enabled profile.
    any_enabled: list[Any] = []
    seen_adapter.clear()
    for p in all_profiles:
        na = p.network_adapter
        pid = None if na is None else _normalize_adapter_id(str(na.network_adapter_id))
        cap = NetworkOperatorTetheringManager.get_tethering_capability_from_connection_profile(p)
        if _enum_tail(cap) == "ENABLED":
            if pid and pid in seen_adapter:
                continue
            if pid:
                seen_adapter.add(pid)
            any_enabled.append(p)
    return (any_enabled[:3]) or all_profiles[:1]


def _recover_wireless_device_state() -> None:
    script = r"""
$ErrorActionPreference='SilentlyContinue'
Start-Service WlanSvc
Get-NetAdapter -IncludeHidden | Where-Object {
  $_.InterfaceDescription -match 'Wireless|Wi-Fi|802\.11|8812AU|MT7921' -or $_.Name -like 'Wi-Fi*'
} | ForEach-Object {
  Enable-NetAdapter -Name $_.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
  netsh interface set interface name="$($_.Name)" admin=enabled | Out-Null
}
Write-Output 'wireless-recovery-done'
"""
    run_powershell(script, timeout_sec=15)


def _wireless_status_snapshot() -> tuple[int, int, list[str]]:
    ok, adapters, _ = get_adapters()
    if not ok:
        return 0, 0, []
    wireless = []
    up_count = 0
    for a in adapters:
        name = str(a.get("name", "")).lower()
        desc = str(a.get("description", "")).lower()
        is_wireless = "wi-fi" in name or "wireless" in desc or "802.11" in desc
        if not is_wireless:
            continue
        wireless.append(str(a.get("name", "")))
        if str(a.get("statusRaw", "")).lower() not in ("disabled", "not present"):
            up_count += 1
    return len(wireless), up_count, wireless


def _netsh_hotspot_capability() -> tuple[bool, str]:
    r = run_netsh(["wlan", "show", "drivers"], timeout_sec=15)
    text = f"{r.stdout}\n{r.stderr}".lower()
    if "hosted network supported" in text:
        if "hosted network supported  : yes" in text or "hosted network supported : yes" in text:
            return True, "Enabled"
        return False, "Disabled (HostedNetwork unsupported by driver)"
    return True, "Unknown (driver output format differs)"


def _netsh_hotspot_status() -> dict[str, Any]:
    r = run_netsh(["wlan", "show", "hostednetwork"], timeout_sec=15)
    text = f"{r.stdout}\n{r.stderr}"
    lo = text.lower()

    if "status" in lo and "started" in lo:
        state = "On"
    elif "status" in lo and "not started" in lo:
        state = "Off"
    else:
        state = "Unknown"

    clients = 0
    m = re.search(r"Number of clients\s*:\s*(\d+)", text, flags=re.IGNORECASE)
    if m:
        clients = int(m.group(1))

    return {"state": state, "clientCount": clients, "message": text.strip()[:2000]}


def _netsh_hotspot_start(ssid: str, passphrase: str) -> tuple[bool, str]:
    ensure_services_running(["WlanSvc", "SharedAccess", "icssvc", "Dhcp", "NlaSvc"])

    set_res = run_netsh(
        ["wlan", "set", "hostednetwork", "mode=allow", f"ssid={ssid}", f"key={passphrase}"],
        timeout_sec=20,
    )
    if set_res.exit_code != 0:
        text = f"{set_res.stderr}\n{set_res.stdout}".lower()
        if "administrator privilege" in text or "requires elevation" in text:
            return False, "Administrator privileges are required. Start python_meowfi service as Administrator."
        return False, f"set hostednetwork failed: {set_res.stderr or set_res.stdout}"

    start_res = run_netsh(["wlan", "start", "hostednetwork"], timeout_sec=20)
    if start_res.exit_code != 0:
        text = f"{start_res.stderr}\n{start_res.stdout}".lower()
        if "administrator privilege" in text or "requires elevation" in text:
            return False, "Administrator privileges are required. Start python_meowfi service as Administrator."
        return False, f"start hostednetwork failed: {start_res.stderr or start_res.stdout}"

    st = _netsh_hotspot_status()
    if st["state"] != "On":
        return False, f"hotspot did not reach ON state. {st['message']}"

    return True, "Hotspot started"


def _netsh_hotspot_stop() -> tuple[bool, str]:
    res = run_netsh(["wlan", "stop", "hostednetwork"], timeout_sec=15)
    if res.exit_code != 0:
        return False, res.stderr or res.stdout or "stop failed"
    return True, "Hotspot stopped"


def hotspot_capability(adapter_id: str | None) -> tuple[bool, str]:
    if not HAVE_WINSDK:
        return _netsh_hotspot_capability()

    try:
        profiles = _select_winrt_profiles(adapter_id)
        if not profiles:
            return False, "No connection profile selected"
        cap = NetworkOperatorTetheringManager.get_tethering_capability_from_connection_profile(profiles[0])
        cap_name = _enum_tail(cap)
        if cap_name == "ENABLED":
            return True, "Enabled"
        return False, cap_name
    except Exception as ex:
        return False, f"WinRT capability check failed: {ex}"


def hotspot_status(adapter_id: str | None = None) -> dict[str, Any]:
    if not HAVE_WINSDK:
        return _netsh_hotspot_status()

    try:
        profiles = _select_winrt_profiles(adapter_id)
        if not profiles:
            return {"state": "Unknown", "clientCount": 0, "message": "No connection profile selected"}
        manager = NetworkOperatorTetheringManager.create_from_connection_profile(profiles[0])
        st_name = _enum_tail(manager.tethering_operational_state)
        state = {"ON": "On", "OFF": "Off", "IN_TRANSITION": "InTransition"}.get(st_name, "Unknown")
        return {"state": state, "clientCount": int(manager.client_count), "message": "OK"}
    except Exception as ex:
        return {"state": "Unknown", "clientCount": 0, "message": f"WinRT status failed: {ex}"}


def _parse_wifi_band(value: str | None):
    if not HAVE_WINSDK:
        return None
    text = (value or "auto").strip().lower()
    if text in ("5", "5g", "5ghz", "five", "five_ghz"):
        return TetheringWiFiBand.FIVE_GIGAHERTZ
    if text in ("2.4", "2.4g", "2.4ghz", "24", "two", "2g"):
        return TetheringWiFiBand.TWO_POINT_FOUR_GIGAHERTZ
    return TetheringWiFiBand.AUTO


def _band_label(value: str | None) -> str:
    text = (value or "auto").strip().lower()
    if text in ("5", "5g", "5ghz", "five", "five_ghz"):
        return "5GHz"
    if text in ("2.4", "2.4g", "2.4ghz", "24", "two", "2g"):
        return "2.4GHz"
    return "Auto"


def hotspot_start(ssid: str, passphrase: str, adapter_id: str | None = None, band: str | None = None) -> tuple[bool, str]:
    """Start hotspot via WinRT when available, fallback to netsh otherwise."""
    if not HAVE_WINSDK:
        return _netsh_hotspot_start(ssid, passphrase)

    ensure_services_running(["WlanSvc", "SharedAccess", "icssvc", "Dhcp", "NlaSvc"])
    total_wifi, up_wifi, wifi_names = _wireless_status_snapshot()
    if total_wifi == 0:
        return False, "No Wi-Fi adapters detected on this system."
    if up_wifi == 0:
        _recover_wireless_device_state()
        total_wifi, up_wifi, wifi_names = _wireless_status_snapshot()
        if up_wifi == 0:
            return False, f"All Wi-Fi adapters are down: {', '.join(wifi_names)}. Enable Wi-Fi adapter/radio in Windows first."

    async def _run() -> tuple[bool, str]:
        profiles = _select_winrt_profiles(adapter_id)
        if not profiles:
            return False, "No connection profile selected"

        selected_is_vpn, _ = _adapter_flags(adapter_id)
        errors: list[str] = []
        for p in profiles:
            pname = p.profile_name or "unnamed"
            try:
                cap = NetworkOperatorTetheringManager.get_tethering_capability_from_connection_profile(p)
                if _enum_tail(cap) != "ENABLED":
                    errors.append(f"{pname}: capability={_enum_tail(cap)}")
                    continue

                m = NetworkOperatorTetheringManager.create_from_connection_profile(p)
                cfg = NetworkOperatorTetheringAccessPointConfiguration()
                cfg.ssid = ssid
                cfg.passphrase = passphrase
                chosen_band = _parse_wifi_band(band)
                if chosen_band is not None:
                    cfg.band = chosen_band
                try:
                    await m.configure_access_point_async(cfg)
                except Exception:
                    # Fallback to Auto if explicit band is not accepted by driver/region.
                    cfg.band = TetheringWiFiBand.AUTO
                    await m.configure_access_point_async(cfg)

                res = await m.start_tethering_async()
                status = _enum_tail(res.status)
                if status == "WI_FI_DEVICE_OFF":
                    _recover_wireless_device_state()
                    await asyncio.sleep(1.2)
                    res = await m.start_tethering_async()
                    status = _enum_tail(res.status)
                if status == "SUCCESS":
                    if selected_is_vpn:
                        return True, f"Hotspot started (anchored on non-VPN profile, band={_band_label(band)})"
                    return True, f"Hotspot started (band={_band_label(band)})"
                msg = (res.additional_error_message or "").strip()
                errors.append(f"{pname}: start={status} {msg}".strip())
            except Exception as ex:
                errors.append(f"{pname}: {ex}")

        return False, " | ".join(errors) if errors else "Unknown hotspot start error"

    try:
        return asyncio.run(_run())
    except Exception as ex:
        return False, f"WinRT hotspot start failed: {ex}"


def hotspot_stop(adapter_id: str | None = None) -> tuple[bool, str]:
    if not HAVE_WINSDK:
        return _netsh_hotspot_stop()

    async def _run() -> tuple[bool, str]:
        profiles = _select_winrt_profiles(adapter_id)
        if not profiles:
            return False, "No connection profile selected"

        errors: list[str] = []
        for p in profiles:
            pname = p.profile_name or "unnamed"
            try:
                m = NetworkOperatorTetheringManager.create_from_connection_profile(p)
                res = await m.stop_tethering_async()
                status = _enum_tail(res.status)
                if status == "SUCCESS":
                    return True, "Hotspot stopped"
                msg = (res.additional_error_message or "").strip()
                errors.append(f"{pname}: stop={status} {msg}".strip())
            except Exception as ex:
                errors.append(f"{pname}: {ex}")

        return False, " | ".join(errors) if errors else "Unknown hotspot stop error"

    try:
        return asyncio.run(_run())
    except Exception as ex:
        return False, f"WinRT hotspot stop failed: {ex}"

def _adapter_name_by_id(adapter_id: str) -> str | None:
    safe = adapter_id.strip().strip("{}").upper()
    script = (
        "$a=Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | "
        f"Where-Object {{ ($_.InterfaceGuid.ToString().Trim('{{','}}').ToUpper()) -eq '{safe}' }} | "
        "Select-Object -First 1 -ExpandProperty Name; $a"
    )
    r = run_powershell(script, timeout_sec=10)
    name = (r.stdout or "").strip()
    return name if name else None


def ics_share(public_adapter_id: str, private_adapter_id: str) -> tuple[bool, str]:
    pub_name = _adapter_name_by_id(public_adapter_id)
    prv_name = _adapter_name_by_id(private_adapter_id)
    if not pub_name:
        return False, f"Public adapter not found: {public_adapter_id}"
    if not prv_name:
        return False, f"Private adapter not found: {private_adapter_id}"

    pub_guid = public_adapter_id.strip().strip("{}").upper()
    prv_guid = private_adapter_id.strip().strip("{}").upper()

    script = rf"""
$ErrorActionPreference='Stop'
$publicName = '{ps_quote(pub_name)}'
$privateName = '{ps_quote(prv_name)}'
$publicGuid = '{ps_quote(pub_guid)}'
$privateGuid = '{ps_quote(prv_guid)}'
$mgr = New-Object -ComObject HNetCfg.HNetShare
$lastError = ''

function Find-Conn([string]$name, [string]$guid) {{
  $conns = @($mgr.EnumEveryConnection())
  foreach($c in $conns) {{
    $p = $mgr.NetConnectionProps($c)
    $pGuid = ([string]$p.Guid).Trim('{{','}}').ToUpper()
    if($pGuid -eq $guid) {{ return $c }}
  }}
  foreach($c in $conns) {{
    $p = $mgr.NetConnectionProps($c)
    if($p.Name -eq $name) {{ return $c }}
  }}
  return $null
}}

for($attempt=1; $attempt -le 4; $attempt++) {{
  try {{
    $conns = @($mgr.EnumEveryConnection())
    foreach($c in $conns) {{
      try {{
        $cfg = $mgr.INetSharingConfigurationForINetConnection($c)
        if($cfg.SharingEnabled) {{ $cfg.DisableSharing() }}
      }} catch {{}}
    }}
    Start-Sleep -Milliseconds 500

    $pubConn = Find-Conn $publicName $publicGuid
    $prvConn = Find-Conn $privateName $privateGuid
    if($null -eq $pubConn) {{ throw "Public connection not found (name=$publicName guid=$publicGuid)" }}
    if($null -eq $prvConn) {{ throw "Private connection not found (name=$privateName guid=$privateGuid)" }}

    $pubCfg = $mgr.INetSharingConfigurationForINetConnection($pubConn)
    $prvCfg = $mgr.INetSharingConfigurationForINetConnection($prvConn)
    $pubCfg.EnableSharing(0)
    $prvCfg.EnableSharing(1)
    Write-Output ("ICS enabled: public='" + $publicName + "' private='" + $privateName + "' attempt=" + $attempt)
    exit 0
  }} catch {{
    $lastError = $_.Exception.Message
    Start-Sleep -Milliseconds 700
  }}
}}

throw ("ICS enable failed after retries: " + $lastError)
"""
    r = run_powershell(script, timeout_sec=30)
    if r.exit_code != 0:
        return False, r.stderr or r.stdout
    return True, r.stdout or "ICS enabled"


def ics_disable_all() -> tuple[bool, str]:
    script = r"""
$ErrorActionPreference='SilentlyContinue'
$mgr = New-Object -ComObject HNetCfg.HNetShare
$conns = @($mgr.EnumEveryConnection())
foreach($c in $conns) {
  $cfg = $mgr.INetSharingConfigurationForINetConnection($c)
  if($cfg.SharingEnabled) { $cfg.DisableSharing() }
}
Write-Output 'ICS disabled'
"""
    r = run_powershell(script, timeout_sec=25)
    if r.exit_code != 0:
        return False, r.stderr or r.stdout
    return True, r.stdout or "ICS disabled"


def netnat_enable(private_adapter_id: str) -> tuple[bool, str]:
    """Enable/reuse NetNat for the private hotspot subnet."""
    prv_name = _adapter_name_by_id(private_adapter_id)
    if not prv_name:
        return False, f"Private adapter not found: {private_adapter_id}"
    cfg = private_ipv4_config(private_adapter_id)
    if cfg is None:
        # Fallback: assign well-known hotspot subnet when private adapter has no IPv4 yet.
        script_assign = rf"""
$ErrorActionPreference='SilentlyContinue'
$ifAlias = '{ps_quote(prv_name)}'
$hasAny = Get-NetIPAddress -InterfaceAlias $ifAlias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object {{ $_.IPAddress -notlike '169.254.*' }} | Select-Object -First 1
if($null -eq $hasAny) {{
  New-NetIPAddress -InterfaceAlias $ifAlias -IPAddress 192.168.137.1 -PrefixLength 24 -ErrorAction SilentlyContinue | Out-Null
}}
"""
        run_powershell(script_assign, timeout_sec=12)
        cfg = private_ipv4_config(private_adapter_id)
    if cfg is None:
        cfg = {"cidr": "192.168.137.0/24", "ip": "192.168.137.1", "prefix": 24}

    cidr = str(cfg.get("cidr", "192.168.137.0/24"))
    script = rf"""
$ErrorActionPreference='Stop'
$natPrefix = 'MeowFiNAT'
$ifAlias = '{ps_quote(prv_name)}'
$cidr = '{ps_quote(cidr)}'

$existing = Get-NetNat -ErrorAction SilentlyContinue |
  Where-Object {{ $_.InternalIPInterfaceAddressPrefix -eq $cidr }} |
  Select-Object -First 1
if($null -ne $existing) {{
  Write-Output ('NetNat reused: ' + $existing.Name + '; private=' + $ifAlias + '; cidr=' + $cidr)
  exit 0
}}

Get-NetNat -ErrorAction SilentlyContinue |
  Where-Object {{ $_.Name -like ($natPrefix + '*') }} |
  Remove-NetNat -Confirm:$false -ErrorAction SilentlyContinue | Out-Null

$natName = $natPrefix + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8)
New-NetNat -Name $natName -InternalIPInterfaceAddressPrefix $cidr -ErrorAction Stop | Out-Null
Write-Output ('NetNat enabled: ' + $natName + '; private=' + $ifAlias + '; cidr=' + $cidr)
"""
    r = run_powershell(script, timeout_sec=30)
    if r.exit_code != 0:
        return False, r.stderr or r.stdout or "NetNat enable failed"
    return True, (r.stdout or "NetNat enabled").strip()


def netnat_disable_all() -> tuple[bool, str]:
    script = r"""
$ErrorActionPreference='SilentlyContinue'
Get-NetNat -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -like 'MeowFiNAT*' } |
  Remove-NetNat -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
Write-Output 'NetNat disabled'
"""
    r = run_powershell(script, timeout_sec=20)
    if r.exit_code != 0:
        return False, r.stderr or r.stdout or "NetNat disable failed"
    return True, (r.stdout or "NetNat disabled").strip()


def _private_alias_and_cidr(private_adapter_id: str | None) -> tuple[str | None, str | None]:
    if not private_adapter_id:
        return None, None
    private_alias = _adapter_name_by_id(private_adapter_id)
    if not private_alias:
        return None, None
    cfg = private_ipv4_config(private_adapter_id)
    cidr = None if cfg is None else str(cfg.get("cidr", "")).strip()
    return private_alias, cidr or None


def detect_137_route_conflicts(private_adapter_id: str | None) -> list[str]:
    # Kept for compatibility; now checks actual private subnet, not hardcoded 192.168.137.0/24.
    if not private_adapter_id:
        return []
    private_alias, cidr = _private_alias_and_cidr(private_adapter_id)
    if not private_alias or not cidr:
        return []

    script = rf"""
$privateAlias = '{ps_quote(private_alias)}'
$cidr = '{ps_quote(cidr)}'
Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object {{ $_.DestinationPrefix -eq $cidr -and $_.InterfaceAlias -ne $privateAlias }} |
  Select-Object -ExpandProperty InterfaceAlias
"""
    r = run_powershell(script, timeout_sec=10)
    if r.exit_code != 0 or not r.stdout.strip():
        return []
    return list({x.strip() for x in r.stdout.splitlines() if x.strip()})


def fix_137_vpn_route_conflicts(private_adapter_id: str | None) -> tuple[bool, str]:
    # Kept for compatibility; now fixes conflicts on actual private subnet.
    if not private_adapter_id:
        return False, "private adapter id is empty"
    private_alias, cidr = _private_alias_and_cidr(private_adapter_id)
    if not private_alias or not cidr:
        return False, "private adapter not found"

    script = rf"""
$ErrorActionPreference='SilentlyContinue'
$privateAlias = '{ps_quote(private_alias)}'
$cidr = '{ps_quote(cidr)}'
$routes = Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object {{ $_.DestinationPrefix -eq $cidr -and $_.InterfaceAlias -ne $privateAlias }}

$removed = @()
foreach($r in $routes) {{
  $alias = [string]$r.InterfaceAlias
  $hay = $alias.ToLowerInvariant()
  if($hay -like '*vpn*' -or $hay -like '*amnezia*' -or $hay -like '*wireguard*' -or $hay -like '*openvpn*') {{
    Remove-NetRoute -DestinationPrefix $r.DestinationPrefix -InterfaceIndex $r.InterfaceIndex -NextHop $r.NextHop -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    $removed += $alias
  }}
}}

if($removed.Count -eq 0) {{
  Write-Output 'no-vpn-conflict-route-removed'
}} else {{
  $uniq = @($removed | Select-Object -Unique | Sort-Object)
  Write-Output ('removed=' + [string]::Join(',', $uniq))
}}
"""
    r = run_powershell(script, timeout_sec=15)
    if r.exit_code != 0:
        return False, (r.stderr or r.stdout or "route fix failed").strip()
    return True, (r.stdout or "route-fix-ok").strip()


def private_has_137_ip(private_adapter_id: str | None) -> bool:
    # Kept for compatibility with GUI/service field names.
    if not private_adapter_id:
        return False
    return private_ipv4_config(private_adapter_id) is not None


def private_ipv4_config(private_adapter_id: str | None) -> dict[str, Any] | None:
    """Return active IPv4 settings of private adapter (ip/prefix/cidr/network/mask)."""
    if not private_adapter_id:
        return None
    prv_name = _adapter_name_by_id(private_adapter_id)
    if not prv_name:
        return None

    script = rf"""
$ifAlias = '{ps_quote(prv_name)}'
$ip = Get-NetIPAddress -InterfaceAlias $ifAlias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object {{
    $_.IPAddress -notlike '169.254.*' -and
    $_.IPAddress -notlike '127.*'
  }} |
  Sort-Object -Property PrefixLength -Descending |
  Select-Object -First 1
if($null -ne $ip) {{
  [PSCustomObject]@{{
    ip = [string]$ip.IPAddress
    prefix = [int]$ip.PrefixLength
  }}
}}
"""
    ok, data, _err = ps_json(script, timeout_sec=8)
    if not ok or data is None:
        return None
    if isinstance(data, list):
        if not data:
            return None
        data = data[0]
    ip = str(data.get("ip", "")).strip()
    try:
        prefix = int(data.get("prefix"))
    except Exception:
        return None
    if not ip:
        return None
    try:
        iface = ipaddress.ip_interface(f"{ip}/{prefix}")
    except Exception:
        return None
    network = iface.network
    return {
        "ip": ip,
        "prefix": prefix,
        "cidr": f"{network.network_address}/{network.prefixlen}",
        "network": str(network.network_address),
        "mask": str(network.netmask),
    }


def hotspot_clients(private_adapter_id: str | None) -> list[dict[str, str]]:
    """Read ARP/neighbor table and return clients within private subnet."""
    if not private_adapter_id:
        return []
    prv_name = _adapter_name_by_id(private_adapter_id)
    if not prv_name:
        return []

    cfg = private_ipv4_config(private_adapter_id)
    if cfg is None:
        return []
    network = cfg["network"]
    prefix = cfg["prefix"]
    gateway_ip = cfg["ip"]

    script = rf"""
$ifAlias = '{ps_quote(prv_name)}'
$gatewayIp = '{ps_quote(gateway_ip)}'
Get-NetNeighbor -InterfaceAlias $ifAlias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object {{
    $_.IPAddress -ne $gatewayIp -and
    $_.State -ne 'Unreachable'
  }} |
  Select-Object IPAddress,LinkLayerAddress,State
"""
    ok, data, _err = ps_json(script, timeout_sec=10)
    if not ok or data is None:
        return []
    if isinstance(data, dict):
        data = [data]

    clients: list[dict[str, str]] = []
    try:
        subnet = ipaddress.ip_network(f"{network}/{prefix}", strict=False)
    except Exception:
        return clients
    for item in data:
        ip = str(item.get("IPAddress", "")).strip()
        mac = str(item.get("LinkLayerAddress", "")).strip().lower()
        state = str(item.get("State", "")).strip()
        if not ip:
            continue
        try:
            if ipaddress.ip_address(ip) not in subnet:
                continue
        except Exception:
            continue
        clients.append({"ip": ip, "mac": mac, "state": state, "source": "arp"})
    return clients


def detect_private_candidate() -> str | None:
    ok, adapters, _ = get_adapters()
    if not ok:
        return None
    candidates = []
    for a in adapters:
        hay = f"{a['name']} {a['description']}".lower()
        if "wi-fi direct" in hay or "mobile hotspot" in hay or "hosted network" in hay:
            has_ipv4 = len(a.get("ipv4") or []) > 0
            candidates.append((has_ipv4, bool(a.get("isUp")), a["id"]))
    candidates.sort(reverse=True)
    if candidates:
        return candidates[0][2]
    return None


def services_probe() -> list[dict[str, str]]:
    names = ["WlanSvc", "SharedAccess", "icssvc", "Dhcp", "NlaSvc"]
    out: list[dict[str, str]] = []
    for name in names:
        script = f"$s=Get-Service -Name '{name}' -ErrorAction SilentlyContinue; if($null -eq $s){{'Missing'}} else {{$s.Status.ToString()}}"
        r = run_powershell(script, timeout_sec=7)
        status = (r.stdout or "Unknown").strip()
        out.append({"name": name, "status": status})
    return out


def recent_event(log_name: str, count: int = 3) -> str:
    script = f"Get-WinEvent -LogName '{log_name}' -MaxEvents {count} -ErrorAction SilentlyContinue | Select-Object TimeCreated,Id,LevelDisplayName,Message | Format-Table -AutoSize | Out-String"
    r = run_powershell(script, timeout_sec=8)
    return (r.stdout or "no events").strip() or "no events"



