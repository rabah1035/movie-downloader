"""
سيرفر إنتاجي للموقع — يعمل كموقع انترنت دائم مع حماية بكلمة مرور.
"""
import json
import os
import socket
import sys
import webbrowser
from pathlib import Path

from waitress import serve

import web_downloader


def _parse_auth(auth_header: str):
    """يفك تشفير ترويسة Authorization البسيطة."""
    if not auth_header or not auth_header.startswith("Basic "):
        return None, None
    try:
        import base64
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        return decoded.split(":", 1)
    except Exception:
        return None, None

CONFIG_FILE = Path(__file__).parent / "site_config.json"

DEFAULT_CONFIG = {
    "port": 80,
    "username": "admin",
    "password": "moviedl2024",
    "duckdns_token": "",
    "duckdns_domain": "",
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_public_ip() -> str:
    try:
        import requests
        return requests.get("https://api.ipify.org", timeout=8).text.strip()
    except Exception:
        return ""


class AuthMiddleware:
    """طبقة حماية بكلمة مرور — لا تلمس كود الموقع الأصلي."""

    def __init__(self, app, username: str, password: str):
        self.app = app
        self.username = username
        self.password = password

    def __call__(self, environ, start_response):
        username, password = _parse_auth(environ.get("HTTP_AUTHORIZATION", ""))
        if username == self.username and password == self.password:
            return self.app(environ, start_response)

        start_response("401 Unauthorized", [
            ("WWW-Authenticate", 'Basic realm="Movie Downloader"'),
            ("Content-Type", "text/plain; charset=utf-8"),
        ])
        return [b"401 Unauthorized"]


def setup_firewall(port: int):
    """يفتح البورت في جدار حماية ويندوز."""
    rule_name = f"Movie Downloader Web {port}"
    cmd = (
        f'netsh advfirewall firewall show rule name="{rule_name}" >nul 2>&1 '
        f'&& echo EXISTS || '
        f'netsh advfirewall firewall add rule name="{rule_name}" '
        f"dir=in action=allow protocol=tcp localport={port}"
    )
    os.system(cmd)


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def main():
    cfg = load_config()
    port = cfg["port"]

    print("=" * 65)
    print(" 🎬 MOVIE DOWNLOADER — PRODUCTION SERVER")
    print("=" * 65)

    # فتح البورت
    setup_firewall(port)

    # حماية الموقع بكلمة مرور
    protected_app = AuthMiddleware(web_downloader.app, cfg["username"], cfg["password"])

    local_ip = get_local_ip()
    public_ip = get_public_ip()

    print(f"\n 📍 LOCAL:  http://{local_ip}:{port}")
    if public_ip:
        print(f" 🌐 PUBLIC: http://{public_ip}:{port}")

    ddns = cfg.get("duckdns_domain", "")
    if ddns:
        print(f" 🔗 DOMAIN: https://{ddns}.duckdns.org")

    print(f"\n 🔑 Login: {cfg['username']} / {cfg['password']}")
    print(f" 📄 Config: {CONFIG_FILE.name}")
    print(f"\n Press CTRL+C to stop\n")

    if ddns:
        update_url = f"https://www.duckdns.org/update?domains={ddns}&token={cfg['duckdns_token']}&ip={public_ip}"
        try:
            import requests
            requests.get(update_url, timeout=10)
            print(f" ✅ DuckDNS updated: {ddns}.duckdns.org -> {public_ip}")
        except Exception:
            print(f" ⚠️ Could not update DuckDNS (check token/domain)")

    try:
        serve(protected_app, host="0.0.0.0", port=port, threads=8)
    except OSError as e:
        print(f"\n ❌ Port {port} busy: {e}")
        print(f" Trying port {port + 1}...")
        cfg["port"] = port + 1
        save_config(cfg)
        serve(protected_app, host="0.0.0.0", port=port + 1, threads=8)


if __name__ == "__main__":
    main()
