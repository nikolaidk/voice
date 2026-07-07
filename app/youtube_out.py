"""Publish production videos to YouTube.

Auth model: YouTube uploads require OAuth2 user consent — an API key is not
enough. The one-time flow is run from a terminal with
`python scripts/youtube_auth.py` (opens a browser, saves a refresh token);
after that the server publishes on demand using the stored credentials.

Files (under the data directory, never committed):
  _youtube/client_secret.json  — OAuth client from Google Cloud Console
  _youtube/credentials.json    — stored token, written by the auth script
"""

import json
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

PRIVACY_VALUES = ("private", "unlisted", "public")


def yt_dir(data_dir: Path) -> Path:
    return data_dir / "_youtube"


def client_secret_path(data_dir: Path) -> Path:
    return yt_dir(data_dir) / "client_secret.json"


def credentials_path(data_dir: Path) -> Path:
    return yt_dir(data_dir) / "credentials.json"


def load_credentials(data_dir: Path):
    """Stored credentials, refreshed if expired; None when not connected."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = credentials_path(data_dir)
    if not path.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json())
    return creds if creds.valid else None


def connected(data_dir: Path) -> bool:
    try:
        return load_credentials(data_dir) is not None
    except Exception:
        return False


def channel_title(data_dir: Path) -> str | None:
    """Name of the connected channel, for display."""
    creds = load_credentials(data_dir)
    if creds is None:
        return None
    from googleapiclient.discovery import build

    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    resp = yt.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    return items[0]["snippet"]["title"] if items else None


def upload(
    data_dir: Path,
    video_path: Path,
    title: str,
    description: str,
    privacy: str = "private",
    tags: list[str] | None = None,
) -> dict:
    """Resumable upload; returns {"video_id", "url"}. Raises on failure."""
    creds = load_credentials(data_dir)
    if creds is None:
        raise RuntimeError(
            "YouTube is not connected — run `python scripts/youtube_auth.py` once."
        )
    if privacy not in PRIVACY_VALUES:
        raise ValueError(f"privacy must be one of {PRIVACY_VALUES}")

    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "title": title[:100],  # YouTube's hard limit
            "description": description[:4900],
            "tags": (tags or [])[:30],
            "categoryId": "28",  # Science & Technology
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(
        str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024,
        resumable=True,
    )
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    video_id = response["id"]
    return {"video_id": video_id, "url": f"https://youtu.be/{video_id}"}


def run_auth_flow(data_dir: Path) -> str:
    """Interactive one-time consent flow (terminal use). Returns channel name."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = client_secret_path(data_dir)
    if not secret.exists():
        raise SystemExit(
            f"Missing {secret}\n\n"
            "One-time Google setup:\n"
            "  1. console.cloud.google.com → create (or pick) a project\n"
            "  2. Enable the 'YouTube Data API v3'\n"
            "  3. OAuth consent screen → External → add yourself as a test user\n"
            "  4. Credentials → Create credentials → OAuth client ID → Desktop app\n"
            f"  5. Download the JSON and save it as {secret}\n"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    yt_dir_ = yt_dir(data_dir)
    yt_dir_.mkdir(parents=True, exist_ok=True)
    credentials_path(data_dir).write_text(creds.to_json())
    name = channel_title(data_dir) or "connected"
    return name
