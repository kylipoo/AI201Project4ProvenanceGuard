"""Verify the rate limiter: the 11th /submit within a minute must 429.

Stubs the two signal functions so no real Groq call is made, and redirects the
audit log to a scratch file so the real log isn't polluted.
"""

import warnings

import app as app_module
import audit

SCRATCH = "/private/tmp/claude-501/-Users-matthew-Dev-AI201Project4ProvenanceGuard/c72a3ee6-a69b-4bf7-823b-ea6165832fe0/scratchpad/verify_audit.jsonl"

_failures = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


def main():
    audit._LOG_PATH = SCRATCH
    app_module.stylometric_score = lambda t: {"score": 0.5, "detail": "stub"}
    app_module.llm_judge_score = lambda t: {"score": 0.5, "rationale": "stub", "available": True}

    client = app_module.app.test_client()

    print("Rate limiting on POST /submit (limit: 10/minute)")
    codes = []
    for _ in range(11):
        r = client.post("/submit", json={"text": "word " * 30, "creator_id": "flooder"})
        codes.append(r.status_code)

    check("first 10 requests allowed (200)", codes[:10] == [200] * 10, str(codes[:10]))
    check("11th request blocked (429)", codes[10] == 429, str(codes[10]))

    # The 429 body should be JSON in our error shape, not Flask's default HTML.
    r = client.post("/submit", json={"text": "word " * 30, "creator_id": "flooder"})
    body = r.get_json(silent=True)
    check("429 body is JSON with our error shape",
          isinstance(body, dict) and body.get("error") == "rate limit exceeded",
          str(body))


if __name__ == "__main__":
    # Surface the "no storage configured" warning as a failure if it fires.
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        try:
            main()
        except UserWarning as w:
            check("no Flask-Limiter storage warning on startup", False, str(w))
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {_failures}")
        raise SystemExit(1)
    print("All rate-limit checks passed.")
