#!/usr/bin/env python3
"""Checks the dictation vocabulary and how it reaches the Whisper decoder.

Voice input used to get its name hints from a regex over one chamber's
`contacts/*.ttl`, which saw contacts and nothing else — no doctor in a
care-provider list, no medication — and only after the fact, in the
transcript-repair pass. The vocabulary now comes from the life store (every
chamber's RDF) through scripts/dictation_vocabulary.py, which both processes
that need it import: the STT service biases its decode with the names' words,
the web gateway hands the whole names to the repair model.

Covers the module (query-to-vocabulary path, caching, fail-open, word
extraction, per-rank budget), the STT service's decode kwargs (hotwords on
whichever parameter the installed wheel has, VAD filter with its one-time
fallback, no conditioning on previous text), and the language set the service
decodes for.

    python3 tests/test_dictation_hotwords.py
"""
import importlib.util
import os
import sys
import tempfile
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


# ── The vocabulary module ─────────────────────────────────────────────────────

def _load_vocabulary(env: dict[str, str] | None = None):
    """Load scripts/dictation_vocabulary.py fresh (its cache is module state)."""
    for var in ("DICTATION_NAME_LIMIT", "DICTATION_HOTWORD_CHARS", "QLEVER_LIFE_URL"):
        os.environ.pop(var, None)
    os.environ.update(env or {})
    spec = importlib.util.spec_from_file_location(
        "dictation_vocabulary_under_test", SCRIPTS_DIR / "dictation_vocabulary.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bindings(*names, rank: int = 1) -> list[dict]:
    """Result rows as QLever returns them: a ?rank and a ?name per row. A name may
    be given as `(rank, name)` to mix ranks in one result set."""
    rows = []
    for entry in names:
        r, name = entry if isinstance(entry, tuple) else (rank, entry)
        rows.append({"rank": {"type": "literal", "value": str(r)},
                     "name": {"type": "literal", "value": name}})
    return rows


def test_vocabulary_from_the_store_and_its_cache():
    dv = _load_vocabulary()
    calls = []

    def fake_query(query):
        calls.append(query)
        return _bindings("Dr. med. Mira Halloran", "Zolabin", "Northgate Laboratories AG")

    dv._bindings = fake_query
    names, hotwords = dv.vocabulary()
    assert names == ["Dr. med. Mira Halloran", "Zolabin", "Northgate Laboratories AG"], names
    # "Dr." has fewer than three letters and drops out; the rest survive once.
    assert hotwords == "med Mira Halloran Zolabin Northgate Laboratories", hotwords
    assert len(calls) == 1, calls
    # Second call inside the TTL is served from the cache.
    assert dv.vocabulary() == (names, hotwords)
    assert len(calls) == 1, calls


def test_names_are_deduplicated_and_normalized():
    dv = _load_vocabulary()
    dv._bindings = lambda q: _bindings(
        "Mira  Halloran\n", "mira halloran", "Zolabin", "", "Zolabin 10 mg")
    names, hotwords = dv.vocabulary()
    assert names == ["Mira Halloran", "Zolabin", "Zolabin 10 mg"], names
    # A digit-only token is not a dictatable word, and "Zolabin" repeats.
    assert hotwords == "Mira Halloran Zolabin", hotwords


def test_unreachable_store_fails_open():
    dv = _load_vocabulary()

    def boom(query):
        raise OSError("qlever-life: connection refused")

    dv._bindings = boom
    assert dv.vocabulary() == ([], ""), "must not raise or invent"


def test_hotword_budget_is_respected():
    dv = _load_vocabulary()
    letters = "abcdefghijklmnopqrstuvwxyz"
    ranked = [(1, f"Aa{a}{b}") for a in letters for b in letters]
    hotwords = dv.hotword_string(ranked, 100)
    assert len(hotwords) <= 100, len(hotwords)
    # Truncation happens at a word boundary, never mid-word.
    assert all(len(w) == 4 for w in hotwords.split()), hotwords
    assert hotwords.split()[0] == "Aaaa", hotwords
    assert dv.hotword_string(ranked, 0) == ""


def test_every_rank_gets_a_share_of_the_budget():
    """A store full of people must not crowd medications out of the window: a
    medication sits at rank 3, behind every person the store knows."""
    dv = _load_vocabulary()
    letters = "abcdefghijklmnopqrstuvwxyz"
    ranked = ([(1, f"Aa{a}{b}") for a in letters for b in letters]
              + [(3, "Zolabin"), (3, "Trevanid")])
    hotwords = dv.hotword_string(ranked, 100).split()
    assert "Zolabin" in hotwords and "Trevanid" in hotwords, hotwords
    assert hotwords[0] == "Aaaa", hotwords


def test_name_limit_caps_the_query_result():
    dv = _load_vocabulary({"DICTATION_NAME_LIMIT": "3"})
    dv._bindings = lambda q: _bindings(*[f"Name{i}" for i in range(50)])
    names, _ = dv.vocabulary()
    assert names == ["Name0", "Name1", "Name2"], names


def test_endpoint_comes_from_the_environment():
    dv = _load_vocabulary({"QLEVER_LIFE_URL": "http://elsewhere:7001/"})
    assert dv.LIFE_URL == "http://elsewhere:7001", dv.LIFE_URL


# ── STT service: what reaches MODEL.transcribe ────────────────────────────────

class _Segment:
    def __init__(self, text):
        self.text = text


class _Info:
    def __init__(self, language, probs=None):
        self.language = language
        self.all_language_probs = probs


class _FakeModel:
    """A WhisperModel whose transcribe() records the kwargs it was given.

    Its signature is what the service introspects to decide between `hotwords`
    and `initial_prompt`, so each variant below declares its own.
    """
    fail_on_vad = False

    def __init__(self, name, device=None, compute_type=None):
        self.name = name
        self.calls: list[dict] = []
        self.info = _Info("de")

    def _record(self, kwargs):
        if self.fail_on_vad and kwargs.get("vad_filter"):
            raise RuntimeError("could not load Silero VAD model")
        self.calls.append(kwargs)
        return iter([_Segment(" hallo ")]), self.info

    def transcribe(self, path, beam_size=5, language=None, vad_filter=False,
                   condition_on_previous_text=True, hotwords=None):
        return self._record({
            "beam_size": beam_size, "language": language, "vad_filter": vad_filter,
            "condition_on_previous_text": condition_on_previous_text,
            "hotwords": hotwords,
        })


class _LegacyModel(_FakeModel):
    """An older faster-whisper: no `hotwords`, no `vad_filter`."""

    def transcribe(self, path, beam_size=5, language=None,
                   condition_on_previous_text=True, initial_prompt=None):
        return self._record({
            "beam_size": beam_size, "language": language,
            "condition_on_previous_text": condition_on_previous_text,
            "initial_prompt": initial_prompt,
        })


_LANGUAGE_VARS = ("STT_SUPPORTED_LANGUAGES", "SIGNAL_SUPPORTED_LANGUAGES",
                  "WHATSAPP_SUPPORTED_LANGUAGES", "TELEGRAM_SUPPORTED_LANGUAGES")


def _load_stt(model_cls, env: dict[str, str] | None = None, store_names=("Halloran", "Zolabin")):
    """Load scripts/stt-service.py against a stubbed faster_whisper.

    The service reads the vocabulary itself, so the store stands in as a
    pre-warmed cache on the real module — that is the path a request takes.
    """
    stub = types.ModuleType("faster_whisper")
    stub.WhisperModel = model_cls
    sys.modules["faster_whisper"] = stub
    for var in _LANGUAGE_VARS + ("STT_VAD_FILTER", "STT_HOTWORDS"):
        os.environ.pop(var, None)
    os.environ.update(env or {})
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "stt_service_under_test", SCRIPTS_DIR / "stt-service.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if mod.dictation_vocabulary is not None:
        names = list(store_names)
        mod.dictation_vocabulary._cache = (
            time.monotonic() + 3600, names, " ".join(names))
    return mod


def test_decode_uses_the_stores_hotwords_vad_and_no_conditioning():
    stt = _load_stt(_FakeModel)
    text, lang = stt.transcribe(Path("/tmp/x.webm"))
    assert text == "hallo", text
    assert lang == "de", lang
    call = stt.MODEL.calls[0]
    # Nothing in the request carries them: the service read them from the store.
    assert call["hotwords"] == "Halloran Zolabin", call
    assert call["vad_filter"] is True, call
    assert call["condition_on_previous_text"] is False, call


def test_hotwords_can_be_switched_off():
    stt = _load_stt(_FakeModel, {"STT_HOTWORDS": "0"})
    stt.transcribe(Path("/tmp/x.webm"))
    assert stt.MODEL.calls[0]["hotwords"] is None, stt.MODEL.calls[0]


def test_unreachable_store_still_transcribes():
    stt = _load_stt(_FakeModel)
    stt.dictation_vocabulary._cache = None
    stt.dictation_vocabulary._bindings = lambda q: (_ for _ in ()).throw(
        OSError("qlever-life: connection refused"))
    text, _ = stt.transcribe(Path("/tmp/x.webm"))
    assert text == "hallo", "the bias is optional; the transcription is not"
    assert stt.MODEL.calls[0]["hotwords"] is None, stt.MODEL.calls[0]


def test_legacy_wheel_falls_back_to_initial_prompt():
    stt = _load_stt(_LegacyModel)
    assert stt._HOTWORD_PARAM == "initial_prompt", stt._HOTWORD_PARAM
    assert stt._VAD_ENABLED is False, "vad_filter must not be passed to a wheel without it"
    stt.transcribe(Path("/tmp/x.webm"))
    assert stt.MODEL.calls[0]["initial_prompt"] == "Halloran Zolabin", stt.MODEL.calls[0]


def test_vad_failure_disables_it_for_the_process():
    class _VadBroken(_FakeModel):
        fail_on_vad = True

    stt = _load_stt(_VadBroken)
    text, _ = stt.transcribe(Path("/tmp/x.webm"))
    assert text == "hallo", "a broken VAD must degrade, not fail the request"
    assert stt._VAD_ENABLED is False, "the failure must not be retried per request"
    assert "vad_filter" not in stt.MODEL.calls[0] or not stt.MODEL.calls[0]["vad_filter"]
    stt.transcribe(Path("/tmp/x.webm"))
    assert len(stt.MODEL.calls) == 2, "second request decodes in one pass"


def test_vad_filter_can_be_switched_off():
    stt = _load_stt(_FakeModel, {"STT_VAD_FILTER": "0"})
    stt.transcribe(Path("/tmp/x.webm"))
    assert stt.MODEL.calls[0]["vad_filter"] is False, stt.MODEL.calls[0]


def test_forced_language_redecode_keeps_the_hotwords():
    stt = _load_stt(_FakeModel, {"STT_SUPPORTED_LANGUAGES": "de,en"})
    stt.MODEL.info = _Info("la", [("la", 0.7), ("de", 0.2), ("en", 0.1)])
    text, lang = stt.transcribe(Path("/tmp/x.webm"))
    assert lang == "de", lang
    assert text == "hallo", text
    assert len(stt.MODEL.calls) == 2, stt.MODEL.calls
    second = stt.MODEL.calls[1]
    assert second["language"] == "de", second
    assert second["hotwords"] == "Halloran Zolabin", second
    assert second["vad_filter"] is True and second["condition_on_previous_text"] is False, second


# ── STT service: which languages it decodes for ───────────────────────────────

def test_languages_are_the_union_of_the_channels():
    """One model serves the dashboard and every gateway, so borrowing a single
    channel's setting would drop the languages the other channels speak."""
    stt = _load_stt(_FakeModel, {
        "SIGNAL_SUPPORTED_LANGUAGES": "de,en",
        "WHATSAPP_SUPPORTED_LANGUAGES": "es",
        "TELEGRAM_SUPPORTED_LANGUAGES": " en , fr ",
    })
    assert stt.SUPPORTED_LANGUAGES == ["de", "en", "es", "fr"], stt.SUPPORTED_LANGUAGES
    assert stt.DEFAULT_LANGUAGE == "de", stt.DEFAULT_LANGUAGE


def test_explicit_setting_wins_over_the_channels():
    stt = _load_stt(_FakeModel, {
        "STT_SUPPORTED_LANGUAGES": "it",
        "SIGNAL_SUPPORTED_LANGUAGES": "de,en",
    })
    assert stt.SUPPORTED_LANGUAGES == ["it"], stt.SUPPORTED_LANGUAGES


def test_no_setting_anywhere_leaves_detection_unconstrained():
    stt = _load_stt(_FakeModel)
    assert stt.SUPPORTED_LANGUAGES == [], stt.SUPPORTED_LANGUAGES
    assert stt.DEFAULT_LANGUAGE == "en", stt.DEFAULT_LANGUAGE


# ── Web gateway: the repair pass reads the same module ────────────────────────

def test_gateway_hints_come_from_the_same_module():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
        os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
        os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
        os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
        (tmp / "chambers").mkdir(parents=True, exist_ok=True)
        if "markdown_it" not in sys.modules:
            try:
                import markdown_it  # noqa: F401
            except ImportError:
                stub = types.ModuleType("markdown_it")
                stub.MarkdownIt = object
                sys.modules["markdown_it"] = stub
        sys.path.insert(0, str(SCRIPTS_DIR))
        spec = importlib.util.spec_from_file_location(
            "web_gateway_dictation_under_test", SCRIPTS_DIR / "web-gateway.py")
        wg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wg)
        wg.dictation_vocabulary._cache = (
            time.monotonic() + 3600, ["Mira Halloran"], "Mira Halloran")
        # Whole names for the repair model -- the decoder gets the words, there.
        assert wg._dictation_names() == ["Mira Halloran"]
        # And nothing about the vocabulary is appended to the STT request.
        assert not hasattr(wg, "_stt_url"), "the hop carries audio only"


def main():
    test_vocabulary_from_the_store_and_its_cache()
    test_names_are_deduplicated_and_normalized()
    test_unreachable_store_fails_open()
    test_hotword_budget_is_respected()
    test_every_rank_gets_a_share_of_the_budget()
    test_name_limit_caps_the_query_result()
    test_endpoint_comes_from_the_environment()
    test_decode_uses_the_stores_hotwords_vad_and_no_conditioning()
    test_hotwords_can_be_switched_off()
    test_unreachable_store_still_transcribes()
    test_legacy_wheel_falls_back_to_initial_prompt()
    test_vad_failure_disables_it_for_the_process()
    test_vad_filter_can_be_switched_off()
    test_forced_language_redecode_keeps_the_hotwords()
    test_languages_are_the_union_of_the_channels()
    test_explicit_setting_wins_over_the_channels()
    test_no_setting_anywhere_leaves_detection_unconstrained()
    test_gateway_hints_come_from_the_same_module()
    print("all dictation-hotword tests passed")


if __name__ == "__main__":
    main()
