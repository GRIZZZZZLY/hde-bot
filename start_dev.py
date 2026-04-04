"""
Dev launcher: starts ngrok, updates .env with the public URL, then starts the bot.

Usage:
    python start_dev.py

Requirements:
    - ngrok installed and on PATH (or path set in NGROK_PATH below)
    - .env file filled in (except WEBHOOK_HOST, this script sets it)
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from dotenv import dotenv_values


NGROK_PATH = os.getenv("NGROK_PATH", "ngrok")
APP_PORT = int(os.getenv("APP_PORT", "8080"))
NGROK_API = "http://127.0.0.1:4040/api/tunnels"
ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")


def get_ngrok_url(retries: int = 10, delay: float = 1.0) -> str:
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(NGROK_API, timeout=3) as resp:
                data = json.loads(resp.read())
            for tunnel in data.get("tunnels", []):
                if tunnel.get("proto") == "https":
                    return tunnel["public_url"].rstrip("/")
        except (urllib.error.URLError, KeyError, json.JSONDecodeError):
            pass
        print(f"  waiting for ngrok... ({attempt + 1}/{retries})")
        time.sleep(delay)
    raise RuntimeError("Could not get ngrok public URL. Is ngrok running?")


def is_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def update_env(key: str, value: str) -> None:
    if not os.path.exists(ENV_FILE):
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")
        return

    with open(ENV_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = rf"^{re.escape(key)}=.*$"
    replacement = f"{key}={value}"

    if re.search(pattern, content, flags=re.MULTILINE):
        content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n{replacement}\n"

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def read_env_value(key: str) -> str:
    if not os.path.exists(ENV_FILE):
        return ""
    values = dotenv_values(ENV_FILE)
    return str(values.get(key) or "").strip()


def main() -> None:
    procs: list[subprocess.Popen] = []

    def shutdown(sig=None, frame=None):
        print("\nShutting down...")
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    static_url = os.getenv("NGROK_STATIC_URL", "").strip() or read_env_value("NGROK_STATIC_URL")

    if not is_port_available(APP_PORT):
        print(
            f"ERROR: port {APP_PORT} is already in use.\n"
            "Stop the existing process using this port or change APP_PORT in .env,\n"
            "then run start_dev.py again."
        )
        sys.exit(1)

    print(f"[1/3] Starting ngrok on port {APP_PORT}...")
    try:
        ngrok_cmd = [NGROK_PATH, "http", str(APP_PORT)]
        if static_url:
            ngrok_cmd.extend(["--url", static_url])

        ngrok_proc = subprocess.Popen(
            ngrok_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print(
            f"ERROR: ngrok not found at '{NGROK_PATH}'.\n"
            "Install from https://ngrok.com/download and add to PATH,\n"
            "or set NGROK_PATH=C:\\path\\to\\ngrok.exe"
        )
        sys.exit(1)

    procs.append(ngrok_proc)
    time.sleep(1.5)

    if ngrok_proc.poll() is not None:
        print("ERROR: ngrok exited immediately. Check your ngrok auth token or static domain.")
        sys.exit(1)

    print("[2/3] Fetching public URL from ngrok...")
    try:
        public_url = get_ngrok_url()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        shutdown()

    print(f"      Public URL: {public_url}")
    update_env("WEBHOOK_HOST", public_url)
    print(f"      .env updated: WEBHOOK_HOST={public_url}")

    print("[3/3] Starting bot (python -m bot.main)...")
    bot_proc = subprocess.Popen(
        [sys.executable, "-m", "bot.main"],
        cwd=os.path.dirname(__file__),
    )
    procs.append(bot_proc)

    print("\n" + "=" * 60)
    print("  Bot is running!")
    print(f"  Telegram webhook : {public_url}/webhook/telegram")
    print(f"  HDE webhook URL  : {public_url}/webhook/hde")
    if static_url:
        print(f"  Static ngrok URL : {static_url}")
    print("  Press Ctrl+C to stop.")
    print("=" * 60 + "\n")

    while True:
        time.sleep(2)
        if ngrok_proc.poll() is not None:
            print("ERROR: ngrok exited unexpectedly.")
            shutdown()
        if bot_proc.poll() is not None:
            print("ERROR: bot exited unexpectedly.")
            shutdown()


if __name__ == "__main__":
    main()
