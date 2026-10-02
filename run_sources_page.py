"""Start the read-only Sources page on this Windows computer only."""
from __future__ import annotations

import argparse
from pathlib import Path

from ai_daily_pipeline.sources_server import serve


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MarieSpace local-only Sources viewer")
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()
    serve(Path(__file__).resolve().parent, arguments.port)
