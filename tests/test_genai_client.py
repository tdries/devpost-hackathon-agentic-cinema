from customs import genai_client

class FakeResp:
    def __init__(self, text): self.text = text

def test_generate_json_repairs_once(monkeypatch):
    calls = []
    def fake_call(model, contents, config):
        calls.append(1)
        return FakeResp('not json' if len(calls) == 1 else '{"a": 1}')
    monkeypatch.setattr(genai_client, "_generate", fake_call)
    out = genai_client.generate_json("m", ["p"], {"type": "object"})
    assert out == {"a": 1} and len(calls) == 2
