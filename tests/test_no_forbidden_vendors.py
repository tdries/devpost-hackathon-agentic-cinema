import pathlib, re
FORBIDDEN = re.compile(r"\b(anthropic|openai|boto3|botocore|azure|claude|gpt-\d)\b", re.I)
ALLOW = {"tests/test_no_forbidden_vendors.py"}

def test_no_forbidden_vendors_in_source():
    root = pathlib.Path(__file__).resolve().parents[1]
    hits = []
    for p in list(root.glob("src/**/*.py")) + [root / "requirements.txt"]:
        rel = str(p.relative_to(root))
        if rel in ALLOW:
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if FORBIDDEN.search(line):
                hits.append(f"{rel}:{i}: {line.strip()}")
    assert not hits, "Forbidden vendor reference:\n" + "\n".join(hits)
