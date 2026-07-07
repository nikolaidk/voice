#!/usr/bin/env python
"""One-time YouTube connection for Fluent Agents Studio.

Usage:  .venv/bin/python scripts/youtube_auth.py

Opens a browser for Google consent and stores a refresh token under
data/_youtube/. See app/youtube_out.py for the Google Cloud setup steps.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.youtube_out import run_auth_flow  # noqa: E402

if __name__ == "__main__":
    data_dir = Path(__file__).resolve().parent.parent / "data"
    channel = run_auth_flow(data_dir)
    print(f"\n✓ YouTube connected: {channel}")
    print("The studio can now publish videos (default privacy: private).")
