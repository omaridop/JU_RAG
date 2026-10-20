"""Keep the local Waitress origin running for kasitbot.app (Windows)."""
from pathlib import Path
import msvcrt
import os
import socket
import subprocess
import time

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main():
    logs = ROOT / 'logs'
    logs.mkdir(exist_ok=True)
    # One supervisor, including when the login shortcut is launched twice.
    with (logs / 'public-server.lock').open('a+b') as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        while True:
            # Never open a second Chroma client while another app owns the port.
            with socket.socket() as probe:
                probe.settimeout(1)
                occupied = probe.connect_ex(('127.0.0.1', 5000)) == 0
            if occupied:
                time.sleep(5)
                continue
            env = os.environ.copy()
            env.update({k: v for k, v in dotenv_values(ROOT / '.env').items() if v is not None})
            env.update(DEBUG='false', COOKIE_SECURE='true', TRUST_PROXY='true', PYTHONUTF8='1')
            output = logs / 'public-server.log'
            if output.exists() and output.stat().st_size > 5_000_000:
                output.replace(logs / 'public-server.previous.log')
            with output.open('a', encoding='utf-8') as stream:
                process = subprocess.Popen(
                    [str(ROOT / '.venv/Scripts/python.exe'), '-m', 'waitress',
                     '--listen=127.0.0.1:5000', '--threads=8', 'run:app'],
                    cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                    stdout=stream, stderr=stream,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                process.wait()
            time.sleep(5)


if __name__ == '__main__':
    main()
