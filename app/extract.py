"""Extract readable text and images from a URL or a PDF file."""

import io
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from PIL import Image
from pypdf import PdfReader

# Keep prompts to Claude bounded; beyond this we refuse rather than silently truncate.
MAX_SOURCE_CHARS = 400_000

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


class ExtractionError(Exception):
    pass


MAX_IMAGES = 8
_MIN_W, _MIN_H = 280, 180


def _process_image(data: bytes) -> bytes | None:
    """Normalize a candidate image: filter small ones, cap size, emit JPEG."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    if img.width < _MIN_W or img.height < _MIN_H:
        return None
    if getattr(img, "is_animated", False):
        img.seek(0)
    img = img.convert("RGB")
    if img.width > 1400:
        img = img.resize((1400, round(img.height * 1400 / img.width)))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82)
    return out.getvalue()


async def extract_from_url(url: str) -> tuple[str, str, list[tuple[str, bytes]]]:
    """Fetch a URL and return (title, text, images)."""
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=30.0, headers=_HEADERS
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ExtractionError(f"Could not fetch URL: {e}") from e

    content_type = resp.headers.get("content-type", "")
    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        return extract_from_pdf(resp.content, fallback_title=url)

    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else url

    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()

    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = "\n".join(
        line for line in (l.strip() for l in main.get_text("\n").splitlines()) if line
    )

    if len(text) < 200:
        raise ExtractionError(
            "Could not extract meaningful text from this URL "
            "(the page may be JavaScript-rendered or behind a paywall)."
        )
    _check_size(text)

    images = await _collect_url_images(url, main)
    return title, text, images


async def _collect_url_images(base_url: str, main) -> list[tuple[str, bytes]]:
    candidates: list[str] = []
    seen = set()
    for img in main.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        absolute = urljoin(base_url, src)
        if absolute not in seen and absolute.lower().split("?")[0].endswith(
            (".jpg", ".jpeg", ".png", ".webp", ".gif")
        ):
            seen.add(absolute)
            candidates.append(absolute)

    images: list[tuple[str, bytes]] = []
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=15.0, headers=_HEADERS
    ) as client:
        for src in candidates[:24]:
            if len(images) >= MAX_IMAGES:
                break
            try:
                r = await client.get(src)
                r.raise_for_status()
            except httpx.HTTPError:
                continue
            processed = _process_image(r.content)
            if processed:
                images.append((f"img_{len(images) + 1:02d}.jpg", processed))
    return images


def extract_from_pdf(
    data: bytes, fallback_title: str = "Uploaded PDF"
) -> tuple[str, str, list[tuple[str, bytes]]]:
    """Extract (title, text, images) from PDF bytes."""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as e:
        raise ExtractionError(f"Could not read PDF: {e}") from e

    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if len(text) < 200:
        raise ExtractionError(
            "Could not extract text from this PDF (it may be a scanned image without a text layer)."
        )
    _check_size(text)

    title = fallback_title
    if reader.metadata and reader.metadata.title:
        title = reader.metadata.title

    images: list[tuple[str, bytes]] = []
    for page in reader.pages:
        if len(images) >= MAX_IMAGES:
            break
        try:
            page_images = page.images
        except Exception:
            continue
        for im in page_images:
            if len(images) >= MAX_IMAGES:
                break
            processed = _process_image(im.data)
            if processed:
                images.append((f"img_{len(images) + 1:02d}.jpg", processed))
    return title, text, images


def _check_size(text: str) -> None:
    if len(text) > MAX_SOURCE_CHARS:
        raise ExtractionError(
            f"Source is too large ({len(text):,} characters; limit is {MAX_SOURCE_CHARS:,}). "
            "Split the document and convert it in parts."
        )
