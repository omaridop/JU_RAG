from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Windows consoles default to cp1252 and choke on Arabic in logs. Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    

_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_console = logging.StreamHandler()
_console.setFormatter(_fmt)
_root.addHandler(_console)

# Rotating file log (5 MB × 5) for server deployments.
_log_dir = Path(__file__).resolve().parent / "logs"
_log_dir.mkdir(exist_ok=True)
_file = RotatingFileHandler(_log_dir / "app.log", maxBytes=5_000_000,
                            backupCount=5, encoding="utf-8")
_file.setFormatter(_fmt)
_root.addHandler(_file)

from app.config import settings  # noqa: E402
from app.web import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    # Debug (Werkzeug debugger) is OFF unless DEBUG=true in .env — never in prod.
    app.run(host="127.0.0.1", port=5000, debug=settings.DEBUG,
            threaded=True, use_reloader=False)
