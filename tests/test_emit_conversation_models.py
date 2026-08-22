#!/usr/bin/env python3
"""Focused checks for the conversation-model boot emitter.

`scripts/emit-conversation-models.py` derives the offered per-conversation model
list (hand-edited JSON-LD in config/) into deterministic N-Triples under
chambers/_generated/, so the life store indexes it without any deployment
declaring a converter. This exercises source precedence, determinism,
write-if-changed, and idempotent directory creation.

Standalone, no third-party deps:

    python3 tests/test_emit_conversation_models.py
"""
import importlib.util
import json
import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "emit-conversation-models.py"


def _load_module():
    """Load the emitter script as a module (hyphenated filename → importlib)."""
    spec = importlib.util.spec_from_file_location("emit_conversation_models", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(tmp, *, env_inline=None, file_content=None, file_missing=False):
    """Run the emitter with a given source config, return (module, output text).

    Sets the emitter's env knobs, points OUTPUT at a fresh nested path (so we can
    check mkdir), reloads the module so it picks up the env, and calls main().
    """
    out_path = Path(tmp) / "gen" / "nested" / "conversation-models.nt"
    os.environ.pop("RETINUE_CONVERSATION_MODELS", None)
    os.environ.pop("RETINUE_CONVERSATION_MODELS_FILE", None)
    os.environ["CONVERSATION_MODELS_NT_PATH"] = str(out_path)

    if env_inline is not None:
        os.environ["RETINUE_CONVERSATION_MODELS"] = env_inline
    if file_content is not None:
        src = Path(tmp) / "models.jsonld"
        src.write_text(file_content, encoding="utf-8")
        os.environ["RETINUE_CONVERSATION_MODELS_FILE"] = str(src)
    elif file_missing:
        os.environ["RETINUE_CONVERSATION_MODELS_FILE"] = str(Path(tmp) / "nope.jsonld")

    mod = _load_module()
    mod.OUTPUT = out_path  # re-read after module load to be explicit
    rc = mod.main()
    assert rc == 0, f"main() returned {rc}"
    text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    return mod, out_path, text


JSONLD_DOC = json.dumps({
    "@context": {
        "rn": "https://retinue-os.github.io/ns/conversation#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "models": {"@id": "rn:offersModel", "@container": "@set"},
        "id": "rn:modelId",
        "label": "rdfs:label",
    },
    "@id": "rn:conversationModelList",
    "@type": "rn:ConversationModelList",
    "models": [
        {"@type": "rn:ConversationModel", "id": "", "label": "Default"},
        {"@type": "rn:ConversationModel", "id": "opus", "label": "Opus"},
    ],
})


def test_file_jsonld_doc():
    with tempfile.TemporaryDirectory() as tmp:
        _, out_path, text = _run(tmp, file_content=JSONLD_DOC)
        # Directory was created on demand (idempotent mkdir).
        assert out_path.parent.is_dir(), "nested output dir not created"
        # The list node and both models are present.
        assert "<https://retinue-os.github.io/ns/conversation#conversationModelList>" in text
        assert '<https://retinue-os.github.io/ns/conversation#modelId> ""' in text
        assert '"Opus"' in text
        # Deterministic: N-Triples, sorted, one triple per line, no blank nodes.
        lines = [l for l in text.splitlines() if l]
        assert lines == sorted(lines), "output not sorted"
        assert "_:" not in text, "blank node leaked into output"
        assert all(l.endswith(" .") for l in lines), "not well-formed N-Triples"
    print("PASS test_file_jsonld_doc")


def test_env_inline_array_wins_over_file():
    with tempfile.TemporaryDirectory() as tmp:
        inline = json.dumps([{"id": "haiku", "label": "Haiku"}])
        _, _, text = _run(tmp, env_inline=inline, file_content=JSONLD_DOC)
        # Env array wins: haiku present, the file's opus absent.
        assert '"Haiku"' in text
        assert '"Opus"' not in text
        assert '<https://retinue-os.github.io/ns/conversation#modelId> "haiku"' in text
    print("PASS test_env_inline_array_wins_over_file")


def test_missing_file_falls_back_to_default():
    with tempfile.TemporaryDirectory() as tmp:
        mod, _, text = _run(tmp, file_missing=True)
        # The built-in default list (4 models) is emitted.
        for label in ("Default", "Opus", "Sonnet", "Haiku"):
            assert label in text, f"default label {label} missing"
        # Empty-id default option gets a slug-less (but legal) node, not the
        # literal string "default" -- that would collide with an explicit
        # id of "default" (see #28).
        assert "#model->" in text
    print("PASS test_missing_file_falls_back_to_default")


def test_invalid_env_then_file():
    with tempfile.TemporaryDirectory() as tmp:
        # Broken inline JSON → ignored, file used instead.
        _, _, text = _run(tmp, env_inline="{not json", file_content=JSONLD_DOC)
        assert '"Opus"' in text
    print("PASS test_invalid_env_then_file")


def test_write_if_changed():
    with tempfile.TemporaryDirectory() as tmp:
        mod, out_path, text1 = _run(tmp, file_content=JSONLD_DOC)
        first_mtime = out_path.stat().st_mtime_ns
        # Re-render identical content: write_if_changed must report no rewrite.
        content = mod.render(mod.load_models())
        rewrote = mod.write_if_changed(content, out_path)
        assert rewrote is False, "identical content triggered a rewrite"
        assert out_path.stat().st_mtime_ns == first_mtime, "file was touched"
        # Different content: rewrite happens.
        rewrote2 = mod.write_if_changed(content + "\n# x\n", out_path)
        assert rewrote2 is True, "changed content did not rewrite"
    print("PASS test_write_if_changed")


def test_slug_sanitises_ids():
    with tempfile.TemporaryDirectory() as tmp:
        inline = json.dumps([{"id": "vendor/model:v1", "label": "Weird"}])
        mod, _, text = _run(tmp, env_inline=inline)
        # IRI-unsafe chars percent-encoded deterministically; id literal kept
        # verbatim in the modelId triple.
        assert "#model-vendor%2Fmodel%3Av1>" in text
        assert '<https://retinue-os.github.io/ns/conversation#modelId> "vendor/model:v1"' in text
    print("PASS test_slug_sanitises_ids")


def test_slug_is_injective_for_former_collisions():
    """Regression for #28: the old slug (default-fallback + `[^A-Za-z0-9._-]`
    -> `_`) merged distinct ids onto one node. Both collision shapes from the
    issue must now stay distinct."""
    with tempfile.TemporaryDirectory() as tmp:
        inline = json.dumps([
            {"id": "", "label": "Gateway default"},
            {"id": "default", "label": "Explicit default"},
            {"id": "anthropic/claude-opus-4", "label": "Slash form"},
            {"id": "anthropic:claude-opus-4", "label": "Colon form"},
        ])
        mod, _, text = _run(tmp, env_inline=inline)

        # '' vs 'default': previously both slugged to "model-default".
        assert "#model->" in text, "empty id lost its slug-less node"
        assert "#model-default>" in text, "explicit id 'default' lost its own node"

        # '/' vs ':': previously both folded to "model-anthropic_claude-opus-4".
        assert "#model-anthropic%2Fclaude-opus-4>" in text
        assert "#model-anthropic%3Aclaude-opus-4>" in text

        # Four distinct ids -> four distinct model subjects, four offersModel
        # edges (render() sorts but does not dedupe, so a collision would
        # have shown as a repeated node/edge rather than a missing one).
        type_suffix = "<https://retinue-os.github.io/ns/conversation#ConversationModel> ."
        subj_lines = [l for l in text.splitlines() if l.endswith(type_suffix)]
        assert len(subj_lines) == 4, f"expected 4 distinct model nodes, got {len(subj_lines)}"
        assert len(set(subj_lines)) == 4, "two ids rendered the identical type triple"
    print("PASS test_slug_is_injective_for_former_collisions")


if __name__ == "__main__":
    test_file_jsonld_doc()
    test_env_inline_array_wins_over_file()
    test_missing_file_falls_back_to_default()
    test_invalid_env_then_file()
    test_write_if_changed()
    test_slug_sanitises_ids()
    test_slug_is_injective_for_former_collisions()
    print("all emit-conversation-models tests passed")
