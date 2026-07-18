#!/usr/bin/env python3
"""Speech-to-text microservice.

A single-responsibility service: it owns the Whisper model and turns an audio
clip into text over HTTP. Every component that needs transcription is a client
of it — the web gateway (dashboard voice input) and the Signal gateway (inbound
voice notes) — so exactly one ASR model is loaded system-wide.

Endpoints
  GET  /health      -> {"status": "ok"}
  POST /transcribe   The audio bytes are the raw request body; the caller's
                     container MIME type goes in Content-Type but is only a
                     naming hint (Whisper probes the actual container by
                     content). Optional ``?lang=<iso>`` forces the decode
                     language. Returns {"text": "...", "lang": "<iso>"}.

Configuration (environment)
  WHISPER_MODEL           Whisper model id (default "base").
  STT_DEVICE              faster-whisper device (default "cpu").
  STT_COMPUTE_TYPE        faster-whisper compute type (default "int8").
  STT_HTTP_PORT           Listen port (default 8100).
  STT_TOKEN               When set, /transcribe requires a matching Bearer token.
  STT_SUPPORTED_LANGUAGES Comma-separated ISO 639-1 codes the user speaks. When
                          set, a detected language outside the set triggers a
                          re-decode forcing the most probable allowed language,
                          so a mis-detection never yields unintelligible text.
  STT_MAX_BODY_BYTES      Upload cap (default 25 MiB).
"""
import hmac
import json
import os
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from faster_whisper import WhisperModel

MODEL_NAME = os.environ.get("WHISPER_MODEL", "base").strip() or "base"
DEVICE = os.environ.get("STT_DEVICE", "cpu").strip() or "cpu"
COMPUTE_TYPE = os.environ.get("STT_COMPUTE_TYPE", "int8").strip() or "int8"
HTTP_PORT = int(os.environ.get("STT_HTTP_PORT", "8100"))
TOKEN = os.environ.get("STT_TOKEN", "").strip()
MAX_BODY_BYTES = int(os.environ.get("STT_MAX_BODY_BYTES", str(25 * 1024 * 1024)))
SUPPORTED_LANGUAGES = [
    code.strip().lower()
    for code in os.environ.get("STT_SUPPORTED_LANGUAGES", "").split(",")
    if code.strip()
]
DEFAULT_LANGUAGE = SUPPORTED_LANGUAGES[0] if SUPPORTED_LANGUAGES else "en"

# Container types browsers/Signal emit, mapped to a file extension used only as a
# naming hint for the temp file (Whisper probes the real container by content).
_AUDIO_SUFFIXES = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
}

MODEL = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
# ThreadingHTTPServer serves requests concurrently, but CTranslate2 inference on
# one shared model is not guaranteed thread-safe — serialize all decoding.
MODEL_LOCK = threading.Lock()


def _best_supported_language(info) -> str | None:
    """Pick the most probable language within SUPPORTED_LANGUAGES.

    Uses Whisper's full per-language probabilities (sorted descending) so that
    when the top guess is outside the allowed set we fall back to the best
    candidate the user actually speaks, rather than e.g. Latin.
    """
    if not SUPPORTED_LANGUAGES:
        return None
    probs = getattr(info, "all_language_probs", None)
    if probs:
        for code, _prob in probs:
            if code.lower() in SUPPORTED_LANGUAGES:
                return code.lower()
    return DEFAULT_LANGUAGE


def transcribe(audio_path: Path, forced_lang: str | None = None) -> tuple[str, str]:
    kwargs: dict = {"beam_size": 5}
    # An explicit request language wins; otherwise, with a single supported
    # language force it outright so Whisper never mis-detects, and with several
    # detect first and constrain below.
    if forced_lang:
        kwargs["language"] = forced_lang
    elif len(SUPPORTED_LANGUAGES) == 1:
        kwargs["language"] = SUPPORTED_LANGUAGES[0]
    with MODEL_LOCK:
        segments, info = MODEL.transcribe(str(audio_path), **kwargs)
        text = "".join(segment.text for segment in segments).strip()
        lang = (info.language or DEFAULT_LANGUAGE).strip().lower()
        if not forced_lang and SUPPORTED_LANGUAGES and lang not in SUPPORTED_LANGUAGES:
            # Detected language is outside the allowed set: re-decode forcing the
            # most probable supported language so the text is decoded correctly.
            forced = _best_supported_language(info)
            if forced:
                segments, info = MODEL.transcribe(str(audio_path), beam_size=5, language=forced)
                text = "".join(segment.text for segment in segments).strip()
                lang = forced
    return text, lang


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress default access-log noise
        return

    def _reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        return bool(token) and hmac.compare_digest(token, TOKEN)

    def do_GET(self):
        if urlparse(self.path).path.rstrip("/") in ("", "/health"):
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != "/transcribe":
            self._reply(404, {"error": "not found"})
            return
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            self._reply(400, {"error": "empty body"})
            return
        if length > MAX_BODY_BYTES:
            self._reply(413, {"error": "payload too large"})
            return
        audio = self.rfile.read(length)
        forced_lang = (parse_qs(parsed.query).get("lang", [""])[0] or "").strip().lower() or None
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        suffix = _AUDIO_SUFFIXES.get(ctype, ".bin")
        fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="stt-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(audio)
            text, lang = transcribe(Path(tmp), forced_lang=forced_lang)
        except Exception as exc:  # noqa: BLE001 - report any decode failure to the caller
            print(f"[stt] transcription failed: {exc}\n{traceback.format_exc()}", flush=True)
            self._reply(500, {"error": f"transcription failed: {exc}"})
            return
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        print(f"[stt] transcribed {length} bytes ({lang})", flush=True)
        self._reply(200, {"text": text, "lang": lang})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), _Handler)
    print(
        f"[stt] listening on port {HTTP_PORT} — model '{MODEL_NAME}' on {DEVICE}/{COMPUTE_TYPE}"
        + (" (token required)" if TOKEN else ""),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
