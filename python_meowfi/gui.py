from __future__ import annotations

"""Desktop GUI for MeowFi.

Contains layout, user actions, service auto-start logic, and live diagnostics view.
"""


import json
import os
import subprocess
import sys
import threading
import time
import importlib
import customtkinter as ctk
import webbrowser

ServiceClient = None
PipeResponse = None
for _client_mod, _common_mod in (
    (".client", ".common"),
    ("python_meowfi.client", "python_meowfi.common"),
    ("client", "common"),
):
    try:
        if _client_mod.startswith("."):
            ServiceClient = importlib.import_module(_client_mod, package=__package__).ServiceClient
            PipeResponse = importlib.import_module(_common_mod, package=__package__).PipeResponse
        else:
            ServiceClient = importlib.import_module(_client_mod).ServiceClient
            PipeResponse = importlib.import_module(_common_mod).PipeResponse
        break
    except Exception:
        ServiceClient = None
        PipeResponse = None

if ServiceClient is None or PipeResponse is None:
    # Заглушки для тестирования UI без бэкенда
    print("WARNING: Client modules not found. Running in UI-only mode.")

    class ServiceClient:  # type: ignore[no-redef]
        def send(self, cmd, args=None, timeout_sec=10):
            return type(
                "obj",
                (object,),
                {
                    "success": True,
                    "message": "Simulated OK",
                    "data": [],
                    "error": None,
                    "version": "1.0",
                    "state": "Idle",
                },
            )

    class PipeResponse:  # type: ignore[no-redef]
        def __init__(self, s, error=None):
            self.success = s
            self.error = error


# --- CYBERPUNK PALETTE ---
COLOR_BG = "#050505"
COLOR_PANEL = "#111111"
COLOR_ACCENT_CYAN = "#00fff6"
COLOR_ACCENT_PURPLE = "#bd00ff"
COLOR_TEXT_MAIN = "#ffffff"
COLOR_TEXT_DIM = "#888888"
COLOR_TERMINAL_BG = "#0f0f0f"
COLOR_TERMINAL_TEXT = "#00ff41"
DEFAULT_GITHUB_URL = "https://github.com/shigeon90-ops"


class CyberButton(ctk.CTkButton):
    """Кнопка с неоновой обводкой (второстепенная)"""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(
            fg_color="transparent",
            border_width=2,
            border_color=kwargs.get("border_color", COLOR_ACCENT_CYAN),
            text_color=kwargs.get("text_color", COLOR_ACCENT_CYAN),
            hover_color=COLOR_PANEL,
            corner_radius=6,
            font=("Segoe UI", 12, "bold"),
            height=32,
        )


class CyberFrame(ctk.CTkFrame):
    """Панель с рамкой"""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(
            fg_color=COLOR_PANEL,
            border_width=1,
            border_color="#333333",
            corner_radius=10,
        )


class App(ctk.CTk):
    """Main application window and all user interaction handlers."""

    def __init__(self) -> None:
        super().__init__()

        # Настройка окна
        self.title("MeowFi [NEON_EDITION]")
        self.geometry("1260x860")
        self.minsize(1120, 760)
        self.configure(fg_color=COLOR_BG)
        ctk.set_appearance_mode("Dark")

        # Клиентская логика
        self.client = ServiceClient()
        self.public_options: list[dict] = []
        self.private_options: list[dict] = []
        self.trace_running = False
        self.last_clients: list[dict] = []
        self._owned_service_proc: subprocess.Popen | None = None
        self._service_starting = False
        self._service_restarted_once = False
        self.github_url = os.getenv("MEOWFI_GITHUB_URL", DEFAULT_GITHUB_URL).strip() or DEFAULT_GITHUB_URL

        # --- GRID LAYOUT ---
        # 0 колонку (меню) не растягиваем, 1 (контент) растягиваем
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main_area()
        self._build_console()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # По умолчанию показываем Dashboard
        self.show_dashboard()
        self.after(100, self.ensure_service_running)

    def _build_sidebar(self):
        """Left navigation and persistent quick actions."""
        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0, fg_color="#0a0a0a")
        self.sidebar.grid(row=0, column=0, rowspan=2, sticky="nsew")

        # Лого
        self.logo = ctk.CTkLabel(self.sidebar, text="MEOW_FI", font=("Segoe UI", 24, "bold"), text_color=COLOR_ACCENT_PURPLE)
        self.logo.pack(pady=(40, 40))
        ctk.CTkLabel(self.sidebar, text=r"/\_/\  NEON CAT", font=("Consolas", 11, "bold"), text_color=COLOR_ACCENT_CYAN).pack(pady=(0, 18))

        # Кнопки навигации
        self.btn_nav_dash = CyberButton(self.sidebar, text="DASHBOARD", command=self.show_dashboard, border_color=COLOR_ACCENT_CYAN, text_color=COLOR_ACCENT_CYAN)
        self.btn_nav_dash.pack(fill="x", padx=20, pady=10)

        self.btn_nav_diag = CyberButton(self.sidebar, text="DIAGNOSTICS", command=self.show_diagnostics, border_color=COLOR_ACCENT_PURPLE, text_color=COLOR_ACCENT_PURPLE)
        self.btn_nav_diag.pack(fill="x", padx=20, pady=10)

        # Статус соединения
        ctk.CTkLabel(self.sidebar, text=r"=^.^=  stay purrsistent", font=("Consolas", 10), text_color=COLOR_TEXT_DIM).pack(side="bottom", pady=(0, 8))
        CyberButton(
            self.sidebar,
            text="GITHUB",
            command=self.open_github,
            border_color="#7efcff",
            text_color="#7efcff",
        ).pack(side="bottom", fill="x", padx=20, pady=(0, 8))
        self.lbl_ipc_status = ctk.CTkLabel(self.sidebar, text="IPC: Disconnected", text_color="gray")
        self.lbl_ipc_status.pack(side="bottom", pady=20)

    def _build_main_area(self):
        """Container for page content (dashboard / diagnostics)."""
        self.content_area = ctk.CTkFrame(self, fg_color="transparent")
        self.content_area.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)

        # Создаем два фрейма (страницы), будем их скрывать/показывать
        self.frame_dashboard = ctk.CTkScrollableFrame(self.content_area, fg_color="transparent")
        self.frame_diagnostics = ctk.CTkScrollableFrame(self.content_area, fg_color="transparent")

        self._setup_dashboard_ui()
        self._setup_diagnostics_ui()

    def _build_console(self):
        """Bottom log panel."""
        self.console_frame = ctk.CTkFrame(self, height=160, fg_color="transparent")
        self.console_frame.grid(row=1, column=1, sticky="ew", padx=20, pady=(0, 20))

        ctk.CTkLabel(self.console_frame, text="> SYSTEM_LOGS", font=("Consolas", 12), text_color="#555").pack(anchor="w")

        self.status_text = ctk.CTkTextbox(
            self.console_frame,
            font=("Consolas", 12),
            fg_color=COLOR_TERMINAL_BG,
            text_color=COLOR_TERMINAL_TEXT,
            border_width=1,
            border_color="#333",
            activate_scrollbars=True,
        )
        self.status_text.pack(fill="both", expand=True)
        self.log("System initialized. Waiting for Neural Link...")

    # --- UI SETUP: DASHBOARD ---
    def _setup_dashboard_ui(self):
        """Main control page: adapters, hotspot settings, start/stop actions, clients."""
        # Верхняя панель управления
        top_bar = ctk.CTkFrame(self.frame_dashboard, fg_color="transparent")
        top_bar.pack(fill="x", pady=(0, 20))
        top_bar.grid_columnconfigure(99, weight=1)

        CyberButton(top_bar, text="REFRESH ADAPTERS", command=self.on_load_adapters).pack(side="left", padx=(0, 10))
        CyberButton(top_bar, text="CONNECT SERVICE", command=self.on_connect).pack(side="left")
        CyberButton(top_bar, text="REFRESH CLIENTS", command=lambda: self.on_probe("manual_clients_refresh")).pack(side="left", padx=(10, 0))
        self.lbl_busy = ctk.CTkLabel(top_bar, text="Ready", text_color=COLOR_TEXT_DIM, font=("Consolas", 11))
        self.lbl_busy.pack(side="right", padx=(10, 0))

        # Карточка настроек (Source & Target)
        settings_card = CyberFrame(self.frame_dashboard)
        settings_card.pack(fill="x", pady=10)

        # Source
        ctk.CTkLabel(settings_card, text="/// PUBLIC INTERNET SOURCE", text_color=COLOR_ACCENT_CYAN, font=("Arial", 11, "bold")).pack(anchor="w", padx=20, pady=(15, 5))
        self.public_combo = ctk.CTkComboBox(settings_card, values=[], width=400, state="readonly")
        self.public_combo.pack(fill="x", padx=20, pady=(0, 15))

        # Target
        ctk.CTkLabel(settings_card, text="/// HOTSPOT PRIVATE ADAPTER", text_color=COLOR_ACCENT_PURPLE, font=("Arial", 11, "bold")).pack(anchor="w", padx=20, pady=(5, 5))
        self.private_combo = ctk.CTkComboBox(settings_card, values=[], width=400, state="readonly")
        self.private_combo.pack(fill="x", padx=20, pady=(0, 15))

        # SSID / Pass / Band Grid
        grid_frame = ctk.CTkFrame(settings_card, fg_color="transparent")
        grid_frame.pack(fill="x", padx=20, pady=(0, 20))

        self.entry_ssid = ctk.CTkEntry(grid_frame, placeholder_text="SSID", width=200)
        self.entry_ssid.insert(0, "MeowFi")
        self.entry_ssid.pack(side="left", padx=(0, 10), fill="x", expand=True)

        self.entry_pass = ctk.CTkEntry(grid_frame, placeholder_text="Password", show="*", width=200)
        self.entry_pass.insert(0, "MeowFi12345")
        self.entry_pass.pack(side="left", padx=(0, 10), fill="x", expand=True)

        self.combo_band = ctk.CTkComboBox(grid_frame, values=["Auto", "2.4 GHz", "5 GHz"], width=100, state="readonly")
        self.combo_band.set("Auto")
        self.combo_band.pack(side="left")

        # ACTION + CLIENT WIDGET ROW
        action_row = ctk.CTkFrame(self.frame_dashboard, fg_color="transparent")
        action_row.pack(fill="x", pady=(12, 8))
        action_row.grid_columnconfigure(0, weight=3)
        action_row.grid_columnconfigure(1, weight=2)

        btn_frame = ctk.CTkFrame(action_row, fg_color="transparent")
        btn_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.btn_start = ctk.CTkButton(
            btn_frame,
            text="INITIATE VPN SHARING",
            command=self.on_start_vpn,
            font=("Segoe UI", 16, "bold"),
            height=44,
            fg_color=COLOR_ACCENT_CYAN,
            text_color="black",
            hover_color="#00ccbf",
        )
        self.btn_start.pack(fill="x", pady=(0, 8))

        self.btn_stop = ctk.CTkButton(
            btn_frame,
            text="TERMINATE SHARING",
            command=self.on_stop_vpn,
            font=("Segoe UI", 14, "bold"),
            height=36,
            fg_color="#333",
            hover_color="#444",
            border_width=1,
            border_color="#555",
        )
        self.btn_stop.pack(fill="x")

        clients_frame = CyberFrame(action_row)
        clients_frame.grid(row=0, column=1, sticky="nsew")
        ctk.CTkLabel(clients_frame, text="/// CONNECTED CLIENTS", text_color=COLOR_ACCENT_CYAN, font=("Arial", 11, "bold")).pack(anchor="w", padx=14, pady=(12, 4))
        self.lbl_clients = ctk.CTkLabel(clients_frame, text="Clients: 0", text_color=COLOR_TEXT_DIM, font=("Consolas", 12))
        self.lbl_clients.pack(anchor="w", padx=14, pady=(0, 6))
        self.clients_text = ctk.CTkTextbox(
            clients_frame,
            height=88,
            font=("Consolas", 11),
            fg_color=COLOR_TERMINAL_BG,
            text_color=COLOR_TEXT_MAIN,
            border_width=1,
            border_color="#333",
            activate_scrollbars=True,
        )
        self.clients_text.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self._render_clients([])

    # --- UI SETUP: DIAGNOSTICS ---
    def _setup_diagnostics_ui(self):
        """Diagnostics page with probe/trace/manual controls."""
        lbl = ctk.CTkLabel(self.frame_diagnostics, text="ADVANCED CONTROL & DIAGNOSTICS", font=("Arial", 16, "bold"), text_color="gray")
        lbl.pack(anchor="w", pady=(0, 20))

        # Блок диагностики
        diag_frame = CyberFrame(self.frame_diagnostics)
        diag_frame.pack(fill="x", pady=10)
        ctk.CTkLabel(diag_frame, text="DIAGNOSTIC TOOLS", font=("Arial", 12, "bold")).pack(anchor="w", padx=15, pady=10)

        row1 = ctk.CTkFrame(diag_frame, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=10)
        CyberButton(row1, text="RUN FULL DIAG", command=self.on_diag, border_color="#fff", text_color="#fff").pack(side="left", padx=5, expand=True, fill="x")
        CyberButton(row1, text="STEP PROBE", command=lambda: self.on_probe("manual_probe"), border_color="#fff", text_color="#fff").pack(side="left", padx=5, expand=True, fill="x")
        CyberButton(row1, text="COPY LOG", command=self.copy_log, border_color="#fff", text_color="#fff").pack(side="left", padx=5, expand=True, fill="x")

        row2 = ctk.CTkFrame(diag_frame, fg_color="transparent")
        row2.pack(fill="x", padx=10, pady=(0, 15))
        CyberButton(row2, text="START TRACE", command=self.start_trace, border_color="#0f0", text_color="#0f0").pack(side="left", padx=5, expand=True, fill="x")
        CyberButton(row2, text="STOP TRACE", command=self.stop_trace, border_color="#f00", text_color="#f00").pack(side="left", padx=5, expand=True, fill="x")

        # Блок ручного управления
        man_frame = CyberFrame(self.frame_diagnostics)
        man_frame.pack(fill="x", pady=20)
        ctk.CTkLabel(man_frame, text="MANUAL OVERRIDE (DANGER ZONE)", font=("Arial", 12, "bold"), text_color="#ff5555").pack(anchor="w", padx=15, pady=10)

        row3 = ctk.CTkFrame(man_frame, fg_color="transparent")
        row3.pack(fill="x", padx=10, pady=10)
        CyberButton(row3, text="START HOTSPOT", command=self.on_start_hotspot).pack(side="left", padx=5, expand=True, fill="x")
        CyberButton(row3, text="STOP HOTSPOT", command=self.on_stop_hotspot).pack(side="left", padx=5, expand=True, fill="x")

    # --- NAV LOGIC ---
    def show_dashboard(self):
        self.frame_diagnostics.pack_forget()
        self.frame_dashboard.pack(fill="both", expand=True)
        self.btn_nav_dash.configure(fg_color="#222")
        self.btn_nav_diag.configure(fg_color="transparent")

    def show_diagnostics(self):
        self.frame_dashboard.pack_forget()
        self.frame_diagnostics.pack(fill="both", expand=True)
        self.btn_nav_dash.configure(fg_color="transparent")
        self.btn_nav_diag.configure(fg_color="#222")

    # --- LOGIC IMPL (Ported from original) ---

    def log(self, text: str) -> None:
        self.status_text.insert("end", text + "\n")
        self.status_text.see("end")

    def copy_log(self) -> None:
        text = self.status_text.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.log(">> Log copied to clipboard.")

    def call(self, command: str, args: dict | None = None, timeout: float = 10.0):
        """Safe wrapper over service client call."""
        try:
            return self.client.send(command, args=args, timeout_sec=timeout)
        except Exception as ex:
            return PipeResponse(False, error=str(ex))

    def _set_busy(self, text: str, color: str = "#f3d36b") -> None:
        if hasattr(self, "lbl_busy"):
            self.lbl_busy.configure(text=text, text_color=color)
            self.update_idletasks()

    def _clear_busy(self) -> None:
        if hasattr(self, "lbl_busy"):
            self.lbl_busy.configure(text="Ready", text_color=COLOR_TEXT_DIM)
            self.update_idletasks()

    def on_connect(self) -> None:
        self._set_busy("Working... Please wait")
        r = self.call("ping")
        if r.success:
            self.log(f">> Connected. Ver={r.version} Msg={r.message}")
            self.lbl_ipc_status.configure(text="IPC: CONNECTED", text_color=COLOR_ACCENT_CYAN)
        else:
            self.log(f">> Connect failed: {r.error}")
            self.lbl_ipc_status.configure(text="IPC: ERROR", text_color="red")
            self.ensure_service_running()
        self._clear_busy()

    def ensure_service_running(self) -> None:
        if self._service_starting:
            return
        self._service_starting = True
        threading.Thread(target=self._ensure_service_running_worker, daemon=True).start()

    def _ensure_service_running_worker(self) -> None:
        """Ensure local service is reachable; auto-start if needed."""
        if not self._service_restarted_once:
            self._service_restarted_once = True
            self._force_restart_service_listener()
        r = self.call("ping")
        if r.success:
            self.after(0, lambda: self.lbl_ipc_status.configure(text="IPC: CONNECTED", text_color=COLOR_ACCENT_CYAN))
            self._service_starting = False
            return
        if self._owned_service_proc and self._owned_service_proc.poll() is None:
            self._service_starting = False
            return
        self.after(0, lambda: self.log(">> Service not reachable. Attempting auto-start..."))
        try:
            flags = 0
            startupinfo = None
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--service"]
            else:
                cmd = [sys.executable, "-m", "python_meowfi.service"]
            self._owned_service_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                startupinfo=startupinfo,
            )
            for _ in range(25):
                time.sleep(0.2)
                r2 = self.call("ping")
                if r2.success:
                    self.after(0, lambda: self.lbl_ipc_status.configure(text="IPC: CONNECTED (AUTO)", text_color=COLOR_ACCENT_CYAN))
                    self.after(0, lambda: self.log(">> Service auto-started."))
                    self._service_starting = False
                    return
            self.after(0, lambda: self.lbl_ipc_status.configure(text="IPC: ERROR", text_color="red"))
            self.after(0, lambda: self.log(">> Auto-start did not respond in time."))
        except Exception as ex:
            self.after(0, lambda: self.lbl_ipc_status.configure(text="IPC: ERROR", text_color="red"))
            self.after(0, lambda: self.log(f">> Auto-start failed: {ex}"))
        self._service_starting = False

    def _force_restart_service_listener(self) -> None:
        try:
            flags = 0
            startupinfo = None
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
            script = (
                "$p=Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 38777 -State Listen -ErrorAction SilentlyContinue | "
                "Select-Object -First 1 -ExpandProperty OwningProcess; "
                "if($p){ Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=flags,
                startupinfo=startupinfo,
                timeout=6,
            )
            time.sleep(0.2)
        except Exception:
            pass

    def on_load_adapters(self) -> None:
        self._set_busy("Loading adapters... Please wait")
        r = self.call("adapters", timeout=20)
        if not r.success:
            self.log(f">> Load adapters failed: {r.error}")
            self._clear_busy()
            return

        adapters = r.data or []
        self.public_options = [a for a in adapters if a.get("isLikelyInternetSource")]
        self.private_options = [a for a in adapters if a.get("isLikelyHotspotPrivate")]

        # CTkComboBox takes strings. We map strings back to IDs later.
        pub_values = [self._label(a) for a in self.public_options]
        priv_values = [self._label(a) for a in self.private_options]

        self.public_combo.configure(values=pub_values)
        self.private_combo.configure(values=priv_values)

        if pub_values:
            self.public_combo.set(pub_values[0])
        if priv_values:
            self.private_combo.set(priv_values[0])

        self.log(f">> {r.message}")
        for a in adapters:
            self.log(f"   - {self._label(a)}")
        self._clear_busy()

    @staticmethod
    def _label(a: dict) -> str:
        vpn = "[VPN] " if a.get("isLikelyVpn") else ""
        return f"{vpn}{a.get('name')} ({a.get('type')})"

    # Helper to find ID by the string displayed in ComboBox
    def _find_id_by_label(self, label: str, options: list[dict]) -> str | None:
        if not label:
            return None
        for a in options:
            if self._label(a) == label:
                return a["id"]
        return None

    def selected_public_id(self) -> str | None:
        return self._find_id_by_label(self.public_combo.get(), self.public_options)

    def selected_private_id(self) -> str | None:
        return self._find_id_by_label(self.private_combo.get(), self.private_options)

    def _selected_band_arg(self) -> str:
        text = (self.combo_band.get() or "Auto").strip().lower()
        if text.startswith("5"):
            return "5ghz"
        if text.startswith("2.4") or text.startswith("2"):
            return "2.4ghz"
        return "auto"

    def on_start_hotspot(self) -> None:
        self._set_busy("Starting hotspot... Please wait")
        ssid = self.entry_ssid.get().strip()
        pwd = self.entry_pass.get().strip()
        band = self._selected_band_arg()
        pub_id = self.selected_public_id()
        self.log(">> Requesting Hotspot Start...")
        r = self.call("hotspot_start", {"adapterId": pub_id, "ssid": ssid, "passphrase": pwd, "band": band}, timeout=35)
        self.log(f">> Result: {r.message}" if r.success else f">> Error: {r.error}")
        self.on_probe("after_hotspot_start")
        self._clear_busy()

    def on_stop_hotspot(self) -> None:
        self._set_busy("Stopping hotspot... Please wait")
        r = self.call("hotspot_stop", timeout=15)
        self.log(f">> Stopped: {r.message}" if r.success else f">> Error: {r.error}")
        self.on_probe("after_hotspot_stop")
        self._clear_busy()

    def on_start_vpn(self) -> None:
        """High-level action: hotspot + NAT + DHCP flow."""
        self._set_busy("Starting VPN sharing... Please wait")
        pub_id = self.selected_public_id()
        prv_id = self.selected_private_id()
        ssid = self.entry_ssid.get().strip()
        pwd = self.entry_pass.get().strip()
        band = self._selected_band_arg()

        self.log(">> INITIATING VPN SHARING...")
        self.log(f"   SSID: {ssid} | Source: {self.public_combo.get()}")

        r = self.call(
            "start_vpn_sharing",
            {"publicAdapterId": pub_id, "privateAdapterId": prv_id, "ssid": ssid, "passphrase": pwd, "band": band},
            timeout=90,
        )
        if r.success:
            self.log(f">> SUCCESS: {r.message}")
        else:
            self.log(f">> FAILURE: {r.error}")
        self.on_probe("after_start_vpn_sharing")
        self._clear_busy()

    def on_stop_vpn(self) -> None:
        self._set_busy("Stopping VPN sharing... Please wait")
        pub_id = self.selected_public_id()
        r = self.call("stop_vpn_sharing", {"publicAdapterId": pub_id}, timeout=25)
        self.log(f">> VPN Sharing Stopped: {r.message}" if r.success else f">> Error: {r.error}")
        self.on_probe("after_stop_vpn_sharing")
        self._clear_busy()

    def _render_clients(self, clients: list[dict]) -> None:
        self.clients_text.delete("1.0", "end")
        if not clients:
            self.lbl_clients.configure(text="Clients: 0")
            self.clients_text.insert("end", "No active clients detected yet.\n")
            return
        self.lbl_clients.configure(text=f"Clients: {len(clients)}")
        for c in clients:
            ip = str(c.get("ip", "-"))
            mac = str(c.get("mac", "-"))
            ttl = c.get("ttlSec")
            state = str(c.get("state", ""))
            source = str(c.get("source", ""))
            suffix = []
            if state:
                suffix.append(state)
            if isinstance(ttl, int):
                suffix.append(f"ttl={ttl}s")
            if source:
                suffix.append(source)
            tail = f" [{' | '.join(suffix)}]" if suffix else ""
            self.clients_text.insert("end", f"{ip:15}  {mac}{tail}\n")

    def on_probe(self, phase: str) -> None:
        """Collect and render one diagnostics snapshot."""
        if phase != "live_trace":
            self._set_busy("Running probe... Please wait")
        r = self.call(
            "probe_step",
            {
                "phase": phase,
                "publicAdapterId": self.selected_public_id(),
                "privateAdapterId": self.selected_private_id(),
            },
            timeout=20,
        )
        if not r.success:
            self.log(f"PROBE ERROR: {r.error}")
            if phase != "live_trace":
                self._clear_busy()
            return
        p = r.data or {}
        clients = p.get("clients") or []
        if isinstance(clients, list):
            self.last_clients = clients
            self._render_clients(clients)
        self.log(f"[PROBE] Phase: {p.get('phase')}")
        self.log(f"   Hotspot: {p.get('hotspotState')} | Clients(winrt): {p.get('clientCount')} | Clients(observed): {p.get('clientsObserved')}")
        self.log(f"   PrivateIP: {p.get('privateHas192_168_137_1')}")
        if p.get("privateSubnetCidr"):
            self.log(f"   PrivateSubnet: {p.get('privateSubnetCidr')}")
        if phase != "live_trace":
            self._clear_busy()

    def on_diag(self) -> None:
        self._set_busy("Collecting diagnostics... Please wait")
        r = self.call(
            "diag_snapshot",
            {"publicAdapterId": self.selected_public_id(), "privateAdapterId": self.selected_private_id()},
            timeout=30,
        )
        if not r.success:
            self.log(f"Diag failed: {r.error}")
            self._clear_busy()
            return
        payload = json.dumps(r.data, ensure_ascii=False, indent=2)
        self.log(">> Diagnostic Snapshot:")
        self.log(payload)
        self._clear_busy()

    def start_trace(self) -> None:
        if self.trace_running:
            self.log(">> Trace already running.")
            return
        self.trace_running = True
        self.log(">> LIVE TRACE STARTED.")
        self._trace_tick()

    def stop_trace(self) -> None:
        self.trace_running = False
        self.log(">> LIVE TRACE STOPPED.")

    def _trace_tick(self) -> None:
        if not self.trace_running:
            return

        def worker() -> None:
            # Используем after, чтобы вернуться в главный поток UI для вызова
            self.after(0, lambda: self.on_probe("live_trace"))
            self.after(2000, self._trace_tick)

        threading.Thread(target=worker, daemon=True).start()

    def on_close(self) -> None:
        try:
            self.trace_running = False
            if self._owned_service_proc and self._owned_service_proc.poll() is None:
                self._owned_service_proc.terminate()
        except Exception:
            pass
        self.destroy()

    def open_github(self) -> None:
        try:
            webbrowser.open(self.github_url, new=2)
            self.log(f">> Opened GitHub: {self.github_url}")
        except Exception as ex:
            self.log(f">> Failed to open GitHub URL: {ex}")


def main() -> int:
    if "--service" in sys.argv:
        _service = None
        for mod_name in ("python_meowfi.service", "service"):
            try:
                import importlib
                _service = importlib.import_module(mod_name)
                break
            except Exception:
                _service = None
        if _service is None:
            raise RuntimeError("Service module import failed in --service mode")
        return int(_service.main())
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
