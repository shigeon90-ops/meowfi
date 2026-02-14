from __future__ import annotations

"""Local JSON-RPC service for MeowFi.

Main responsibility: receive GUI commands and orchestrate backend + DHCP fallback.
"""


import json
import logging
import os
import socketserver
import traceback
from typing import Any

try:
    from .common import PipeResponse, SERVICE_HOST, SERVICE_PORT, SERVICE_VERSION, utc_now_iso
    from . import backend
    from .dhcp_fallback import MiniDhcpServer
except ImportError:  # script mode fallback
    from common import PipeResponse, SERVICE_HOST, SERVICE_PORT, SERVICE_VERSION, utc_now_iso
    import backend
    from dhcp_fallback import MiniDhcpServer


LOG = logging.getLogger("meowfi.service")
DHCP_FALLBACK = MiniDhcpServer()
DEBUG_MODE = os.getenv("MEOWFI_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _arg(args: dict[str, Any] | None, key: str) -> str | None:
    if not args:
        return None
    value = args.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _public_error(user_msg: str, debug_msg: str | None = None) -> str:
    if DEBUG_MODE and debug_msg:
        return f"{user_msg}: {debug_msg}"
    return user_msg


class RequestHandler(socketserver.StreamRequestHandler):
    """One TCP request handler: read one JSON line, execute command, send response."""

    def handle(self) -> None:
        raw = self.rfile.readline().decode("utf-8", errors="replace").strip()
        if not raw:
            return

        try:
            req = json.loads(raw)
            cmd = str(req.get("command", "")).strip().lower()
            args = req.get("args")
        except Exception:
            self._send(PipeResponse(False, error="Invalid request"))
            return

        try:
            resp = self.dispatch(cmd, args)
        except Exception as ex:
            LOG.exception("Command failed: %s", cmd)
            resp = PipeResponse(False, error=f"Unhandled error: {ex}")

        self._send(resp)

    def _send(self, resp: PipeResponse) -> None:
        payload = resp.to_json_line().encode("utf-8")
        self.wfile.write(payload)

    def dispatch(self, cmd: str, args: dict[str, Any] | None) -> PipeResponse:
        """Route command to corresponding backend operation."""
        if cmd == "ping":
            return PipeResponse(True, version=SERVICE_VERSION, state="Ready", message="Service reachable")

        if cmd == "adapters":
            ok, adapters, err = backend.get_adapters()
            return PipeResponse(ok, error=None if ok else err, version=SERVICE_VERSION, state="Ready" if ok else "Error", message=f"Found {len(adapters)} adapters" if ok else None, data=adapters)

        if cmd == "capability":
            ok, msg = backend.hotspot_capability(_arg(args, "adapterId"))
            return PipeResponse(ok, error=None if ok else msg, version=SERVICE_VERSION, state="Ready" if ok else "Error", message=msg)

        if cmd == "status":
            st = backend.hotspot_status(_arg(args, "adapterId"))
            return PipeResponse(True, version=SERVICE_VERSION, state=st["state"], message=st["message"], data=st)

        if cmd == "hotspot_start":
            adapter_id = _arg(args, "adapterId")
            ssid = _arg(args, "ssid")
            pwd = _arg(args, "passphrase")
            band = _arg(args, "band")
            if not ssid or not pwd:
                return PipeResponse(False, error="ssid and passphrase are required")
            if len(pwd) < 8 or len(pwd) > 63:
                return PipeResponse(False, error="passphrase must be 8..63 chars")
            ok, msg = backend.hotspot_start(ssid, pwd, adapter_id=adapter_id, band=band)
            state = "Running" if ok else "Error"
            return PipeResponse(ok, error=None if ok else msg, version=SERVICE_VERSION, state=state, message=msg)

        if cmd == "hotspot_stop":
            ok, msg = backend.hotspot_stop(_arg(args, "adapterId"))
            return PipeResponse(ok, error=None if ok else msg, version=SERVICE_VERSION, state="Stopped" if ok else "Error", message=msg)

        if cmd == "ics_share":
            return PipeResponse(True, version=SERVICE_VERSION, state="Ready", message="ICS path disabled. Use NetNat + DHCP fallback.")

        if cmd == "ics_disable":
            nat_ok, nat_msg = backend.netnat_disable_all()
            _dhcp_ok, dhcp_msg = DHCP_FALLBACK.stop()
            ok = nat_ok
            msg = f"NetNat: {nat_msg}; DHCP: {dhcp_msg}"
            return PipeResponse(ok, error=None if ok else msg, version=SERVICE_VERSION, state="Ready" if ok else "Error", message=msg)

        if cmd == "start_vpn_sharing":
            public_id = _arg(args, "publicAdapterId")
            private_id = _arg(args, "privateAdapterId")
            ssid = _arg(args, "ssid")
            pwd = _arg(args, "passphrase")
            band = _arg(args, "band")
            if not public_id or not ssid or not pwd:
                return PipeResponse(False, error="publicAdapterId, ssid, passphrase are required")

            ok_hs, msg_hs = backend.hotspot_start(ssid, pwd, adapter_id=public_id, band=band)
            if not ok_hs:
                return PipeResponse(False, error=f"Hotspot start failed: {msg_hs}")

            private_id = private_id or backend.detect_private_candidate()
            if not private_id:
                return PipeResponse(False, error="Private adapter not found after hotspot start")

            nat_ok, nat_msg = backend.netnat_enable(private_id)
            if not nat_ok:
                return PipeResponse(False, error=_public_error("NAT setup failed", nat_msg))

            cfg = backend.private_ipv4_config(private_id)
            bind_ip = str(cfg.get("ip")) if cfg else "192.168.137.1"
            prefix = int(cfg.get("prefix")) if cfg else 24
            dhcp_ok, dhcp_msg = DHCP_FALLBACK.ensure_running(bind_ip, prefix_len=prefix)
            if not dhcp_ok:
                for _ in range(6):
                    if backend.private_has_137_ip(private_id):
                        cfg = backend.private_ipv4_config(private_id)
                        bind_ip = str(cfg.get("ip")) if cfg else bind_ip
                        prefix = int(cfg.get("prefix")) if cfg else prefix
                        dhcp_ok, dhcp_msg = DHCP_FALLBACK.ensure_running(bind_ip, prefix_len=prefix)
                        if dhcp_ok:
                            break

            ready = backend.private_has_137_ip(private_id)
            cfg = backend.private_ipv4_config(private_id)
            private_cidr = str(cfg.get("cidr")) if cfg else "unknown"
            conflicts = backend.detect_137_route_conflicts(private_id)
            conflict_msg = ""
            if conflicts:
                fixed, fix_msg = backend.fix_137_vpn_route_conflicts(private_id)
                remaining = backend.detect_137_route_conflicts(private_id)
                if remaining:
                    conflict_msg = (
                        " Route conflict on "
                        + private_cidr
                        + " via: "
                        + ", ".join(remaining)
                        + ". This can block client internet."
                    )
                else:
                    conflict_msg = f" Route conflict fixed ({fix_msg})."
            if dhcp_ok:
                dhcp_part = f"DHCP: {dhcp_msg}"
            else:
                dhcp_part = _public_error(
                    "DHCP fallback not started (hotspot still running, use static IP temporarily)",
                    dhcp_msg,
                )
            msg = f"VPN sharing started. private={private_id}, privateReady={ready}, subnet={private_cidr}. NetNat: {nat_msg}. {dhcp_part}.{conflict_msg}"
            return PipeResponse(True, version=SERVICE_VERSION, state="Running", message=msg)

        if cmd == "stop_vpn_sharing":
            hs_ok, hs_msg = backend.hotspot_stop()
            nat_ok, nat_msg = backend.netnat_disable_all()
            _dhcp_ok, dhcp_msg = DHCP_FALLBACK.stop()
            ok = hs_ok and nat_ok
            msg = f"Hotspot: {hs_msg}; NetNat: {nat_msg}; DHCP: {dhcp_msg}"
            return PipeResponse(ok, error=None if ok else msg, version=SERVICE_VERSION, state="Stopped", message=msg)

        if cmd == "probe_step":
            phase = _arg(args, "phase") or "manual"
            public_id = _arg(args, "publicAdapterId")
            private_id = _arg(args, "privateAdapterId")
            hs = backend.hotspot_status()
            cfg = backend.private_ipv4_config(private_id)
            arp_clients = backend.hotspot_clients(private_id)
            dhcp_clients = DHCP_FALLBACK.leases_snapshot()
            merged_clients: dict[str, dict[str, Any]] = {}
            for c in dhcp_clients:
                ip = str(c.get("ip", "")).strip()
                if ip:
                    merged_clients[ip] = dict(c)
            for c in arp_clients:
                ip = str(c.get("ip", "")).strip()
                if not ip:
                    continue
                if ip in merged_clients:
                    merged_clients[ip]["state"] = c.get("state", "")
                    if not merged_clients[ip].get("mac") and c.get("mac"):
                        merged_clients[ip]["mac"] = c.get("mac")
                else:
                    merged_clients[ip] = dict(c)
            clients = list(merged_clients.values())
            clients.sort(key=lambda x: str(x.get("ip", "")))
            data = {
                "phase": phase,
                "capturedAtUtc": utc_now_iso(),
                "hotspotState": hs["state"],
                "clientCount": hs["clientCount"],
                "clientsObserved": len(clients),
                "clients": clients,
                "publicAdapterId": public_id,
                "privateAdapterId": private_id,
                "privateHas192_168_137_1": backend.private_has_137_ip(private_id),
                "privateIpv4": None if cfg is None else cfg.get("ip"),
                "privateSubnetCidr": None if cfg is None else cfg.get("cidr"),
                "services": backend.services_probe(),
                "recentWlanEvents": backend.recent_event("Microsoft-Windows-WLAN-AutoConfig/Operational"),
                "recentIcsEvents": backend.recent_event("Microsoft-Windows-SharedAccess_NAT/Operational"),
                "notes": [],
            }
            if not data["privateHas192_168_137_1"]:
                data["notes"].append("Private adapter does not have a valid hotspot IPv4 address")
            return PipeResponse(True, version=SERVICE_VERSION, state=hs["state"], message=f"Probe collected: {phase}", data=data)

        if cmd == "diag_snapshot":
            public_id = _arg(args, "publicAdapterId")
            private_id = _arg(args, "privateAdapterId")
            ok, adapters, err = backend.get_adapters()
            if not ok:
                return PipeResponse(False, error=f"Adapter inventory failed: {err}")
            hs = backend.hotspot_status()
            data = {
                "collectedAtUtc": utc_now_iso(),
                "publicAdapterId": public_id,
                "privateAdapterId": private_id,
                "hotspot": hs,
                "services": backend.services_probe(),
                "adapters": adapters,
                "wlanEvents": backend.recent_event("Microsoft-Windows-WLAN-AutoConfig/Operational", 10),
                "icsEvents": backend.recent_event("Microsoft-Windows-SharedAccess_NAT/Operational", 10),
            }
            return PipeResponse(True, version=SERVICE_VERSION, state="Ready", message="Diagnostic snapshot collected", data=data)

        return PipeResponse(False, error=f"Unknown command: {cmd}")


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Thread-per-request server to keep GUI responsive during long commands."""

    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    """Service entrypoint."""
    logging.basicConfig(
        level=logging.DEBUG if DEBUG_MODE else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        with ThreadedTCPServer((SERVICE_HOST, SERVICE_PORT), RequestHandler) as server:
            LOG.info("Service started on %s:%s", SERVICE_HOST, SERVICE_PORT)
            server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOG.info("Service stopped by user")
        return 0
    except Exception:
        LOG.error("Fatal server error:\n%s", traceback.format_exc())
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
