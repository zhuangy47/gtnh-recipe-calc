#!/usr/bin/env python3
"""Start the GTNH cost model.

    python run.py                 # opens http://127.0.0.1:5057/ in a browser
    python run.py --port 8080
    python run.py --no-browser

Local by design: the database is a 506 MB file on disk, you are the only client,
and plans are private working documents. There is nothing to serve.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
