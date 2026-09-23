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

This service is a part of the deployment, not a generic ASR box bolted onto it:
it sits on the same network as the life store and reads the deployment's own
dictation vocabulary from there (scripts/dictation_vocabulary.py), so every
client is biased towards the names its speakers use — dashboard dictation and
the messenger gateways' voice notes alike — without any of them having to send
that list along with each clip.

Configuration (environment)
  WHISPER_MODEL           Whisper model id (default "base").
  STT_DEVICE              faster-whisper device (default "cpu").
  STT_COMPUTE_TYPE        faster-whisper compute type (default "int8").
  STT_HTTP_PORT           Listen port (default 8100).
  STT_TOKEN               When set, /transcribe requires a matching Bearer token.
  STT_SUPPORTED_LANGUAGES Comma-separated ISO 639-1 codes the speakers use. When
                          set, a detected language outside the set triggers a
                          re-decode forcing the most probable allowed language,
                          so a mis-detection never yields unintelligible text.
                          Unset, it is the union of the languages the messenger
                          channels declare (SIGNAL_/WHATSAPP_/TELEGRAM_
                          SUPPORTED_LANGUAGES) — this service transcribes for
                          all of them, so no single channel's setting can stand
                          in for the deployment's.
  STT_MAX_BODY_BYTES      Upload cap (default 25 MiB).
  STT_VAD_FILTER          Skip non-speech with Silero VAD (default on, "0" off).
                          Silence is what makes Whisper invent sentences, and
                          decoding less audio is also faster.
  STT_HOTWORDS            Bias the decode with the life store's names (default
                          on, "0" off). Budget and endpoint: see
                          scripts/dictation_vocabulary.py.
"""
import hmac
import inspect
import json
import os
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from faster_whisper import WhisperModel

try:
    import dictation_vocabulary
except ImportError:  # the module is optional — without it, decoding is unbiased
    dictation_vocabulary = None

MODEL_NAME = os.environ.get("WHISPER_MODEL", "base").strip() or "base"
DEVICE = os.environ.get("STT_DEVICE", "cpu").strip() or "cpu"
COMPUTE_TYPE = os.environ.get("STT_COMPUTE_TYPE", "int8").strip() or "int8"
HTTP_PORT = int(os.environ.get("STT_HTTP_PORT", "8100"))
TOKEN = os.environ.get("STT_TOKEN", "").strip()
MAX_BODY_BYTES = int(os.environ.get("STT_MAX_BODY_BYTES", str(25 * 1024 * 1024)))
VAD_FILTER = os.environ.get("STT_VAD_FILTER", "1").strip().lower() not in ("0", "false", "no", "off")
HOTWORDS = os.environ.get("STT_HOTWORDS", "1").strip().lower() not in ("0", "false", "no", "off")


def _languages() -> list[str]:
    """The languages this service decodes for.

    An explicit STT_SUPPORTED_LANGUAGES wins. Otherwise the set is the union of
    what the messenger channels declare, in the order they declare it: this one
    model serves the dashboard and every gateway, so borrowing a single
    channel's setting would silently drop the languages the others speak.
    """
    sources = [os.environ.get("STT_SUPPORTED_LANGUAGES", "")]
    if not sources[0].strip():
        sources = [os.environ.get(f"{channel}_SUPPORTED_LANGUAGES", "")
                   for channel in ("SIGNAL", "WHATSAPP", "TELEGRAM")]
    codes: list[str] = []
    for value in sources:
        for code in value.split(","):
            code = code.strip().lower()
            if code and code not in codes:
                codes.append(code)
    return codes


SUPPORTED_LANGUAGES = _languages()
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

# faster-whisper is installed unpinned, and its decode knobs arrived in different
# releases: `hotwords` only exists from 1.0.x on, where older builds can only be
# biased through `initial_prompt`. Probe the signature instead of assuming, so an
# older wheel degrades rather than erroring on every request.
_TRANSCRIBE_PARAMS = frozenset(inspect.signature(MODEL.transcribe).parameters)
_HOTWORD_PARAM = "hotwords" if "hotwords" in _TRANSCRIBE_PARAMS else "initial_prompt"
# Silero VAD loads a model of its own on first use; if that is unavailable at
# runtime, disable it once for the process instead of failing every transcription.
_VAD_ENABLED = VAD_FILTER and "vad_filter" in _TRANSCRIBE_PARAMS


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


def _decode(audio_path: Path, hotwords: str | None, **overrides) -> tuple[str, object]:
    """Run one decode pass and materialize its text (callers hold MODEL_LOCK).

    Keeps every decode of a request on identical settings — the forced-language
    re-decode below included — and consumes the segment generator inside the try,
    since faster-whisper defers part of the work into the iteration.
    """
    global _VAD_ENABLED
    kwargs: dict = {
        "beam_size": 5,
        # Each window conditioned on the previous one's text is how Whisper gets
        # into a repetition loop; short dictations have nothing to gain from it.
        "condition_on_previous_text": False,
    }
    if _VAD_ENABLED:
        kwargs["vad_filter"] = True
    if hotwords:
        kwargs[_HOTWORD_PARAM] = hotwords
    kwargs.update(overrides)
    try:
        segments, info = MODEL.transcribe(str(audio_path), **kwargs)
        return "".join(segment.text for segment in segments).strip(), info
    except Exception as exc:  # noqa: BLE001 - retry once without the optional VAD
        if not kwargs.pop("vad_filter", False):
            raise
        print(f"[stt] VAD filter unusable ({exc}) — disabled for this process", flush=True)
        _VAD_ENABLED = False
        segments, info = MODEL.transcribe(str(audio_path), **kwargs)
        return "".join(segment.text for segment in segments).strip(), info


def _hotwords() -> str | None:
    """The deployment's names, as Whisper hotwords — cached by the module and
    fail-open there, so an unreachable store costs the bias, not the decode."""
    if not HOTWORDS or dictation_vocabulary is None:
        return None
    return dictation_vocabulary.vocabulary("stt")[1] or None


def transcribe(audio_path: Path, forced_lang: str | None = None) -> tuple[str, str]:
    hotwords = _hotwords()
    lang_kwargs: dict = {}
    # An explicit request language wins; otherwise, with a single supported
    # language force it outright so Whisper never mis-detects, and with several
    # detect first and constrain below.
    if forced_lang:
        lang_kwargs["language"] = forced_lang
    elif len(SUPPORTED_LANGUAGES) == 1:
        lang_kwargs["language"] = SUPPORTED_LANGUAGES[0]
    with MODEL_LOCK:
        text, info = _decode(audio_path, hotwords, **lang_kwargs)
        lang = (info.language or DEFAULT_LANGUAGE).strip().lower()
        if not forced_lang and SUPPORTED_LANGUAGES and lang not in SUPPORTED_LANGUAGES:
            # Detected language is outside the allowed set: re-decode forcing the
            # most probable supported language so the text is decoded correctly.
            forced = _best_supported_language(info)
            if forced:
                text, info = _decode(audio_path, hotwords, language=forced)
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
        params = parse_qs(parsed.query)
        forced_lang = (params.get("lang", [""])[0] or "").strip().lower() or None
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
        # Which knobs this wheel supports and which languages are in force, so a
        # degraded install or an empty language set is visible at a glance.
        + f", vad={'on' if _VAD_ENABLED else 'off'}"
        + (f", hotwords via {_HOTWORD_PARAM}" if HOTWORDS and dictation_vocabulary else ", no hotwords")
        + f", languages={','.join(SUPPORTED_LANGUAGES) or 'any'}"
        + (" (token required)" if TOKEN else ""),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
