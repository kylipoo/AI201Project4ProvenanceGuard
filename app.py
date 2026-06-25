"""Provenance Guard — content attribution API.

POST /submit runs two signals (stylometric + Groq LLM judge), fuses them into a
confidence score, maps that to a transparency label, and writes an audit entry.
GET /log returns recent audit entries. Still to come: rate limiting and appeals.
"""

import uuid

from flask import Flask, jsonify, request

import audit
from scoring import fuse, to_label
from signals import llm_judge_score, stylometric_score

app = Flask(__name__)

# Reject anything longer than this outright (validation, not detection).
MAX_CONTENT_CHARS = 10_000


@app.route("/")
def home():
    return "Provenance Guard is running."


@app.route("/submit", methods=["POST"])
def submit():
    """Accept a piece of text for attribution analysis.

    Request body (JSON):
        { "text": "<the content to analyze>", "creator_id": "<optional>" }

    Returns 200 with a structured stub. 400 on invalid input.
    NOTE: the response shape is intentionally partial for M3 — only `signals`
    is real; classification/confidence/label arrive in M4/M5.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be JSON"}), 400

    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "text is required and must be a non-empty string"}), 400

    if len(text) > MAX_CONTENT_CHARS:
        return jsonify(
            {"error": f"text exceeds maximum length of {MAX_CONTENT_CHARS} characters"}
        ), 400

    # Required: identifies who submitted, so appeals + rate limiting can key on it.
    creator_id = data.get("creator_id")
    if not isinstance(creator_id, str) or not creator_id.strip():
        return jsonify({"error": "creator_id is required and must be a non-empty string"}), 400

    content_id = "c_" + uuid.uuid4().hex[:10]

    # --- Multi-signal detection -----------------------------------------
    signal_a = stylometric_score(text)          # stylometric (statistics)
    signal_b = llm_judge_score(text)            # semantic LLM judge (Groq)

    # --- Fusion + label mapping (see scoring.py / planning.md) -----------
    word_count = len(text.split())
    confidence = fuse(
        signal_a["score"], signal_b["score"], word_count, signal_b["available"]
    )
    verdict = to_label(confidence)
    attribution = verdict["classification"]
    label = verdict["label"]

    # Write a structured audit entry for this decision. The appeal workflow
    # (M5) will look this record up by content_id and mutate its status.
    audit.append_entry(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "attribution": attribution,
            "confidence": confidence,
            "stylometric_score": signal_a["score"],
            "llm_score": signal_b["score"],
            "llm_available": signal_b["available"],
            "status": "classified",
        }
    )

    return jsonify(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "attribution": attribution,
            "confidence": confidence,
            "label": label,
            "received_chars": len(text),
            "signals": {"stylometric": signal_a, "llm_judge": signal_b},
            "status": "classified",
        }
    ), 200


@app.route("/log", methods=["GET"])
def log():
    """Return recent audit-log entries as JSON, most recent first.

    Optional ?limit=N caps the number returned. In a real system this would
    require auth; here it exists for documentation and grading visibility.
    """
    limit = request.args.get("limit", type=int)
    return jsonify({"entries": audit.get_log(limit=limit)})


if __name__ == "__main__":
    app.run(port=5001, debug=True)
