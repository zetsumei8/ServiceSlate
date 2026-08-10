from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from serviceslate.db import DATA_DIR

LOCAL_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LAN_CONFIG = DATA_DIR / ".lan-config.json"
PORT_FILE = DATA_DIR / ".serviceslate-port"
LOCK_FILE = DATA_DIR / ".serviceslate-running"


def _probe(port: int, host: str = LOCAL_HOST) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/setup/status", timeout=0.6) as response:
            return 200 <= response.status < 500
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _read_existing_port() -> int | None:
    try:
        port = int(PORT_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return port if _probe(port) else None


def _choose_port() -> int:
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 25):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((LOCAL_HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError("ServiceSlate could not find an available local connection. Close another copy and try again.")


def _open_browser(port: int) -> None:
    url = f"http://{LOCAL_HOST}:{port}"
    for _ in range(40):
        if _probe(port):
            webbrowser.open(url)
            return
        time.sleep(0.15)


def _lan_config() -> dict:
    try:
        data = json.loads(LAN_CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lan = _lan_config()
    role = os.environ.get("SERVICESLATE_LAN_ROLE", str(lan.get("role") or "local")).lower()
    if role == "client":
        host_url = str(os.environ.get("SERVICESLATE_LAN_HOST") or lan.get("host_url") or "").rstrip("/")
        if not host_url:
            raise RuntimeError("Office client mode needs an Office Host address. Run CONFIGURE_OFFICE_NETWORK.cmd.")
        try:
            with urllib.request.urlopen(host_url + "/api/setup/status", timeout=2.0) as response:
                if response.status >= 500:
                    raise RuntimeError("Office Host is not ready")
        except Exception as exc:
            raise RuntimeError(f"Office Host is unreachable at {host_url}. Company data was not opened locally to avoid creating a competing copy.") from exc
        webbrowser.open(host_url)
        return

    bind_host = "0.0.0.0" if role == "host" else LOCAL_HOST
    os.environ["SERVICESLATE_LAN_ROLE"] = role
    existing = _read_existing_port()
    if existing:
        print("ServiceSlate is already open. Opening it in your browser…")
        webbrowser.open(f"http://{LOCAL_HOST}:{existing}")
        return

    # A stale marker can remain after a crash or forced shutdown. It is safe to replace
    # only after confirming there is no responding ServiceSlate instance.
    PORT_FILE.unlink(missing_ok=True)
    LOCK_FILE.unlink(missing_ok=True)
    port = int(lan.get("port") or DEFAULT_PORT) if role == "host" else _choose_port()
    if role == "host":
        # Fail plainly if the standard office port is already occupied. A stable port keeps join addresses durable.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
            try:
                check.bind((bind_host, port))
            except OSError as exc:
                raise RuntimeError(f"Office Host port {port} is already in use") from exc
    os.environ["SERVICESLATE_PORT"] = str(port)
    PORT_FILE.write_text(str(port), encoding="utf-8")
    LOCK_FILE.write_text(json.dumps({"port": port, "started": time.time()}), encoding="utf-8")
    threading.Thread(target=_open_browser, args=(port,), daemon=True).start()
    try:
        uvicorn.run("serviceslate.app:app", host=bind_host, port=port, reload=False, log_level="warning")
    finally:
        try:
            if PORT_FILE.exists() and PORT_FILE.read_text(encoding="utf-8").strip() == str(port):
                PORT_FILE.unlink(missing_ok=True)
        finally:
            LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
