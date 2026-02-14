from __future__ import annotations

"""Minimal DHCP server used as fallback when ICS DHCP is unavailable.

Scope: binds only to selected hotspot gateway IP and serves addresses for that subnet.
"""


import ipaddress
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass


LOG = logging.getLogger("meowfi.dhcp")

MAGIC_COOKIE = b"\x63\x82\x53\x63"
OPT_MSG_TYPE = 53
OPT_REQ_IP = 50
OPT_SERVER_ID = 54
OPT_LEASE_TIME = 51
OPT_SUBNET_MASK = 1
OPT_ROUTER = 3
OPT_DNS = 6
OPT_END = 255

DHCP_DISCOVER = 1
DHCP_OFFER = 2
DHCP_REQUEST = 3
DHCP_ACK = 5


@dataclass(slots=True)
class Lease:
    ip: str
    expires_at: float


class MiniDhcpServer:
    """Small DHCP daemon for hotspot clients."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._bind_ip: str | None = None
        self._pool_start = 20
        self._pool_end = 120
        self._leases: dict[str, Lease] = {}
        self._running = False
        self._dns = ["1.1.1.1", "8.8.8.8"]
        self._lease_seconds = 3600
        self._network: ipaddress.IPv4Network = ipaddress.ip_network("192.168.137.0/24")
        self._server_ip: str = "192.168.137.1"
        self._prefix_len: int = 24

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def ensure_running(self, bind_ip: str, prefix_len: int = 24) -> tuple[bool, str]:
        """Start (or reuse) DHCP server for a specific private subnet."""
        try:
            bind_addr = ipaddress.ip_address(bind_ip)
            if not bind_addr.is_private:
                return False, f"Refusing DHCP bind on non-private IP: {bind_ip}"
            if prefix_len < 16 or prefix_len > 30:
                return False, f"Unsupported prefix length: /{prefix_len}"
            network = ipaddress.ip_network(f"{bind_ip}/{prefix_len}", strict=False)
        except Exception as ex:
            return False, f"Invalid DHCP network config: {ex}"

        with self._lock:
            if self._running and self._bind_ip == bind_ip and self._prefix_len == prefix_len:
                return True, f"DHCP already running on {bind_ip}/{prefix_len}"

        self.stop()
        self._stop.clear()
        self._bind_ip = bind_ip
        self._server_ip = bind_ip
        self._prefix_len = prefix_len
        self._network = network
        self._leases.clear()
        self._thread = threading.Thread(target=self._run, name="MeowFiDHCP", daemon=True)
        self._thread.start()

        for _ in range(20):
            time.sleep(0.05)
            with self._lock:
                if self._running:
                    return True, f"DHCP started on {bind_ip}:67 ({self._network.network_address}/{self._network.prefixlen})"
        return False, "DHCP thread did not start"

    def stop(self) -> tuple[bool, str]:
        """Stop DHCP daemon and close UDP socket."""
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        th = self._thread
        if th and th.is_alive():
            th.join(timeout=1.0)
        with self._lock:
            self._running = False
        return True, "DHCP stopped"

    def leases_snapshot(self) -> list[dict[str, str | int]]:
        """Expose active leases for diagnostics/GUI."""
        now = time.time()
        out: list[dict[str, str | int]] = []
        with self._lock:
            for mac, lease in list(self._leases.items()):
                if lease.expires_at < now:
                    del self._leases[mac]
                    continue
                ttl = int(max(0, lease.expires_at - now))
                out.append({"mac": mac, "ip": lease.ip, "ttlSec": ttl, "source": "dhcp"})
        out.sort(key=lambda x: str(x.get("ip", "")))
        return out

    def _run(self) -> None:
        """Background loop: receive DHCP packets and answer discover/request."""
        bind_ip = self._bind_ip or "0.0.0.0"
        sock: socket.socket | None = None
        last_error: Exception | None = None
        for _ in range(20):
            if self._stop.is_set():
                return
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                # Binding to hotspot gateway IP limits scope and avoids polluting other networks.
                sock.bind((bind_ip, 67))
                sock.settimeout(0.5)
                self._sock = sock
                with self._lock:
                    self._running = True
                LOG.info("DHCP fallback listening on %s:67", bind_ip)
                break
            except Exception as ex:
                last_error = ex
                try:
                    if sock is not None:
                        sock.close()
                except Exception:
                    pass
                sock = None
                time.sleep(0.25)

        if sock is None:
            with self._lock:
                self._running = False
            LOG.error("DHCP fallback failed to start on %s:67: %s", bind_ip, last_error)
            return

        while not self._stop.is_set():
            try:
                payload, _addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                continue

            try:
                self._handle_packet(sock, payload)
            except Exception:
                continue

        try:
            sock.close()
        except Exception:
            pass
        with self._lock:
            self._running = False
        LOG.info("DHCP fallback stopped")

    def _handle_packet(self, sock: socket.socket, packet: bytes) -> None:
        if len(packet) < 240:
            return
        if packet[0] != 1:  # BOOTREQUEST
            return
        if packet[236:240] != MAGIC_COOKIE:
            return

        hlen = packet[2]
        if hlen < 6:
            return
        xid = packet[4:8]
        flags = packet[10:12]
        chaddr_raw = packet[28 : 28 + hlen]
        mac = ":".join(f"{b:02x}" for b in chaddr_raw[:6])
        options = self._parse_options(packet[240:])
        msg_type = options.get(OPT_MSG_TYPE, b"")
        if not msg_type:
            return

        req_ip = None
        if OPT_REQ_IP in options and len(options[OPT_REQ_IP]) == 4:
            req_ip = socket.inet_ntoa(options[OPT_REQ_IP])

        offered_ip = self._allocate_ip(mac, requested=req_ip)
        if not offered_ip:
            return

        if msg_type == bytes([DHCP_DISCOVER]):
            reply = self._build_reply(
                msg_type=DHCP_OFFER,
                xid=xid,
                flags=flags,
                chaddr=chaddr_raw[:6],
                yiaddr=offered_ip,
            )
            sock.sendto(reply, ("255.255.255.255", 68))
            return

        if msg_type == bytes([DHCP_REQUEST]):
            reply = self._build_reply(
                msg_type=DHCP_ACK,
                xid=xid,
                flags=flags,
                chaddr=chaddr_raw[:6],
                yiaddr=offered_ip,
            )
            sock.sendto(reply, ("255.255.255.255", 68))
            return

    def _parse_options(self, raw: bytes) -> dict[int, bytes]:
        opts: dict[int, bytes] = {}
        i = 0
        while i < len(raw):
            code = raw[i]
            i += 1
            if code == 0:
                continue
            if code == OPT_END:
                break
            if i >= len(raw):
                break
            ln = raw[i]
            i += 1
            if i + ln > len(raw):
                break
            opts[code] = raw[i : i + ln]
            i += ln
        return opts

    def _allocate_ip(self, mac: str, requested: str | None = None) -> str | None:
        now = time.time()
        for key in list(self._leases.keys()):
            if self._leases[key].expires_at < now:
                del self._leases[key]

        if mac in self._leases:
            return self._leases[mac].ip

        requested_ip = None
        if requested:
            try:
                ip = ipaddress.ip_address(requested)
                if ip in self._network:
                    requested_ip = requested
            except Exception:
                requested_ip = None

        used = {lease.ip for lease in self._leases.values()}
        if requested_ip and requested_ip not in used and requested_ip != self._server_ip:
            self._leases[mac] = Lease(requested_ip, now + self._lease_seconds)
            return requested_ip

        host_count = max(0, int(self._network.num_addresses) - 2)
        if host_count < 3:
            return None

        preferred: list[str]
        if self._network.prefixlen == 24:
            base = str(self._network.network_address).rsplit(".", 1)[0]
            preferred = []
            for last_octet in range(self._pool_start, self._pool_end + 1):
                preferred.append(f"{base}.{last_octet}")
        else:
            preferred = [str(h) for h in self._network.hosts()]

        for candidate in preferred:
            if candidate == self._server_ip:
                continue
            if candidate in used:
                continue
            try:
                if ipaddress.ip_address(candidate) not in self._network:
                    continue
            except Exception:
                continue
            self._leases[mac] = Lease(candidate, now + self._lease_seconds)
            return candidate
        return None

    def _build_reply(self, msg_type: int, xid: bytes, flags: bytes, chaddr: bytes, yiaddr: str) -> bytes:
        op = 2
        htype = 1
        hlen = 6
        hops = 0
        secs = 0
        ciaddr = b"\x00\x00\x00\x00"
        yiaddr_raw = socket.inet_aton(yiaddr)
        siaddr = socket.inet_aton(self._server_ip)
        giaddr = b"\x00\x00\x00\x00"
        chaddr_padded = chaddr + b"\x00" * (16 - len(chaddr))
        sname = b"\x00" * 64
        boot_file = b"\x00" * 128

        bootp = struct.pack(
            "!BBBB4sHH4s4s4s4s16s64s128s",
            op,
            htype,
            hlen,
            hops,
            xid,
            secs,
            int.from_bytes(flags, "big"),
            ciaddr,
            yiaddr_raw,
            siaddr,
            giaddr,
            chaddr_padded,
            sname,
            boot_file,
        )

        opts = bytearray()
        opts += MAGIC_COOKIE
        opts += bytes([OPT_MSG_TYPE, 1, msg_type])
        opts += bytes([OPT_SERVER_ID, 4]) + socket.inet_aton(self._server_ip)
        opts += bytes([OPT_LEASE_TIME, 4]) + struct.pack("!I", self._lease_seconds)
        opts += bytes([OPT_SUBNET_MASK, 4]) + socket.inet_aton(str(self._network.netmask))
        opts += bytes([OPT_ROUTER, 4]) + socket.inet_aton(self._server_ip)
        dns_raw = b"".join(socket.inet_aton(x) for x in self._dns)
        opts += bytes([OPT_DNS, len(dns_raw)]) + dns_raw
        opts += bytes([OPT_END])
        return bootp + bytes(opts)
