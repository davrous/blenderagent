"""WaveSpeed video-edit transport. Submission is deliberately never retried."""

import ipaddress
import logging
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from media_analysis import MAX_MEDIA_BYTES

RATES = {"480p": 0.11, "720p": 0.22, "1080p": 0.55, "4k": 1.10}
TERMINAL_FAILURES = {"failed", "cancelled", "timeout", "deleted"}


class _NoSignedUrls(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(r"(https?://[^\s?\"']+)\?[^\s\"']+", r"\1?[redacted]", record.getMessage())
        record.args = ()
        return True


logging.getLogger("httpx").addFilter(_NoSignedUrls())
logging.getLogger("httpcore").setLevel(logging.WARNING)


def public_https(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Provider media requires a public HTTPS URL.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError("Provider media hostname could not be resolved.") from None
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise ValueError("Private provider media addresses are not allowed.")
    return url


class WaveSpeedClient:
    def __init__(self, *, client=None):
        self.key = os.getenv("WAVESPEED_API_KEY", "")
        if not self.key:
            raise ValueError("WAVESPEED_API_KEY is not configured.")
        self.base = "https://api.wavespeed.ai/api/v3"
        self.client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False)

    def close(self):
        self.client.close()

    def api(self, method: str, endpoint: str, payload: dict | None = None) -> dict:
        try:
            response = self.client.request(method, self.base + endpoint,
                                           headers={"Authorization": f"Bearer {self.key}"}, json=payload)
            if not response.is_success:
                raise ValueError(f"WaveSpeed returned HTTP {response.status_code}.")
            envelope = response.json()
            if envelope.get("code") != 200 or not isinstance(envelope.get("data"), dict):
                raise ValueError("WaveSpeed returned an invalid result envelope.")
            return envelope["data"]
        except (httpx.HTTPError, TypeError, KeyError):
            raise ValueError("WaveSpeed transport failed; credentials and URLs are redacted.") from None

    def upload(self, path: Path) -> str:
        size = path.stat().st_size
        if not 0 < size <= MAX_MEDIA_BYTES:
            raise ValueError("WaveSpeed input exceeds the 200 MiB limit.")
        ticket = self.api("POST", "/media/uploads", {"filename": path.name, "size": size, "content_type": "video/mp4"})
        upload = ticket.get("upload", {})
        method = upload.get("method", "")
        if method not in ("PUT", "POST"):
            raise ValueError("WaveSpeed returned an unsupported upload method.")
        url = public_https(upload.get("url", ""))
        headers = upload.get("headers", {})
        if not isinstance(headers, dict) or any(name.lower() in ("authorization", "cookie", "host") for name in headers):
            raise ValueError("WaveSpeed returned unsafe upload headers.")
        download_url = public_https(ticket.get("download_url", ""))
        try:
            with path.open("rb") as source:
                response = self.client.request(method, url, headers=headers, content=source,
                                               timeout=180)
            if not response.is_success:
                raise ValueError(f"WaveSpeed upload returned HTTP {response.status_code}.")
        except httpx.HTTPError:
            raise ValueError("WaveSpeed upload failed.") from None
        return download_url

    def submit(self, video: str, prompt: str, resolution: str, generate_audio: bool) -> str:
        if resolution not in RATES or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 8000 or type(generate_audio) is not bool:
            raise ValueError("Invalid Seedance finishing settings.")
        data = self.api("POST", "/bytedance/seedance-2.5/video-edit", {
            "video": video, "prompt": prompt, "resolution": resolution, "generate_audio": generate_audio,
        })
        prediction = data.get("id", "")
        if not isinstance(prediction, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", prediction):
            raise ValueError("WaveSpeed did not return a prediction ID.")
        return prediction

    def poll(self, prediction: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", prediction):
            raise ValueError("Invalid prediction ID.")
        return self.api("GET", f"/predictions/{prediction}/result")

    def download(self, url: str, path: Path):
        public_https(url)
        try:
            with self.client.stream("GET", url, timeout=180) as response:
                if not response.is_success:
                    raise ValueError(f"WaveSpeed output download returned HTTP {response.status_code}.")
                size = 0
                with path.open("wb") as target:
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_MEDIA_BYTES:
                            raise ValueError("WaveSpeed output exceeds 200 MiB.")
                        target.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise