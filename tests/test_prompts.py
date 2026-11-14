"""Prompt versions (A/B tests) and the fixed sampling seed used by evaluations."""
import pytest

from app import translate as tr


def make(prompt_version=None, seed=None):
    t = tr.Translator.__new__(tr.Translator)  # no Ollama needed to build prompts
    t.style, t.scenario, t.context_turns, t.history = "chat", "test", 4, []
    t.glossary = [("实习", {"fr": "stage", "en": "internship"})]
    t.backend, t.url, t.model = "ollama", "http://ollama.invalid", "m"
    t.prompt_version = prompt_version or tr.PROMPT_VERSION
    t.seed = seed
    return t


def system_prompt(t, text="C'est grave bien !", src="fr", tgt="zh", context=None):
    return t.build_messages(text, src, tgt, context=context or [])[0]["content"]


def test_v2_adds_the_idiom_rule_and_examples_v1_does_not():
    v1, v2 = system_prompt(make("v1")), system_prompt(make("v2"))
    assert "俚语" not in v1 and "俚语" in v2
    assert "J'ai le cafard" in v2


def test_rules_are_numbered_without_gaps():
    for src, tgt in (("fr", "zh"), ("zh", "fr"), ("zh", "en")):
        lines = [ln for ln in system_prompt(make("v2"), "x", src, tgt).splitlines() if ln[:1].isdigit()]
        assert [int(ln.split(".")[0]) for ln in lines] == list(range(1, len(lines) + 1))
        assert all(ln.split(". ", 1)[1].strip() for ln in lines)  # no empty rule


def test_glossary_and_context_survive_in_both_versions():
    for v in tr.PROMPT_VERSIONS:
        p = system_prompt(make(v), "我在找实习", "zh", "fr", context=[("你好", "Bonjour")])
        assert "实习 → stage" in p and "你好 ⇒ Bonjour" in p


def test_examples_are_not_copied_from_the_test_set():
    import json
    from pathlib import Path

    testset = (Path(__file__).resolve().parent.parent / "data/eval/mt_testset.jsonl").read_text(encoding="utf-8")
    srcs = [json.loads(line)["src"] for line in testset.splitlines() if line.strip()]
    for examples in tr.IDIOM_EXAMPLES.values():
        for e in examples:
            for side in e.split(" → "):  # neither the source nor the answer may appear in a test sentence
                core = side.strip(" .!。！")
                assert not any(core in s for s in srcs), f"prompt example leaks a test sentence: {side}"


def test_unknown_prompt_version_is_rejected(monkeypatch):
    monkeypatch.setattr(tr, "ensure_ollama", lambda url: None)
    with pytest.raises(ValueError):
        tr.Translator({"prompt_version": "v99"}, log=lambda *a: None)


@pytest.mark.parametrize("seed", [None, 42])
def test_seed_goes_into_the_ollama_request_only_when_set(monkeypatch, seed):
    sent = {}

    class Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self):
            yield b'{"message": {"content": "ok"}, "done": true}'

    def fake_post(url, json=None, **kw):
        sent.update(json)
        return Resp()

    monkeypatch.setattr(tr.requests, "post", fake_post)
    list(make(seed=seed).stream("x", "fr", "zh", context=[]))
    assert sent["options"].get("seed") == seed
