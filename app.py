"""Provenance Guard — content attribution API.

POST /submit runs two signals (stylometric + Groq LLM judge), fuses them into a
confidence score, maps that to a transparency label, and writes an audit entry.
POST /appeal lets a creator contest a verdict (attaches their reason, flips the
record to under_review, runs no re-classification). GET /content/{id} returns a
single record; GET /log returns recent audit entries (optionally filtered by
status). A per-IP rate limiter (Flask-Limiter) protects the write endpoints —
especially the Groq-backed detection pipeline — from flooding.
"""

import uuid

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import audit
from scoring import fuse, to_label
from signals import llm_judge_score, stylometric_score

app = Flask(__name__)

# Reject anything longer than this outright (validation, not detection).
MAX_CONTENT_CHARS = 10_000

# --- Rate limiting ------------------------------------------------------------
# Key by client IP (no auth yet, so IP is the best abuse signal we have). Limits
# are declared per-route below rather than globally, so read endpoints stay free
# while the expensive write paths are protected. in-memory storage is fine for a
# single-process dev server; a real deployment would point storage_uri at Redis
# so limits are shared across workers and survive restarts. See README "Rate
# limiting" for why each number was chosen.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)


@app.errorhandler(429)
def ratelimit_exceeded(e):
    """Return the 429 as JSON so it matches the rest of the API's error shape."""
    return jsonify({"error": "rate limit exceeded", "detail": str(e.description)}), 429


@app.route("/")
def home():
    return "Provenance Guard is running."


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
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


@app.route("/appeal", methods=["POST"])
@limiter.limit("20 per hour")
def appeal():
    """Contest a classification. Request body (JSON):

        { "content_id": "<the decision being contested>",
          "creator_reasoning": "<free-text: why the classification is wrong>" }

    Looks up the original decision and attaches the creator's reasoning, then
    flips status to "under_review" for a human to take over. The original
    attribution/confidence/signals are PRESERVED, never overwritten, and NO
    re-classification runs. Returns a confirmation plus the updated record.
    404 if content_id is unknown; 400 on invalid input.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be JSON"}), 400

    content_id = data.get("content_id")
    if not isinstance(content_id, str) or not content_id.strip():
        return jsonify({"error": "content_id is required and must be a non-empty string"}), 400

    # `creator_reasoning` is the canonical field; `reason` is accepted as an alias.
    creator_reasoning = data.get("creator_reasoning") or data.get("reason")
    if not isinstance(creator_reasoning, str) or not creator_reasoning.strip():
        return jsonify(
            {"error": "creator_reasoning is required and must be a non-empty string"}
        ), 400

    record = audit.attach_appeal(content_id, creator_reasoning)
    if record is None:
        return jsonify({"error": f"no content found with id {content_id}"}), 404

    # Explicit confirmation that the appeal was received, plus the updated record
    # so the caller can see the new status and the preserved original decision.
    return jsonify(
        {
            "message": "Appeal received. This content is now under review by a human.",
            "content_id": content_id,
            "status": record["status"],
            "record": record,
        }
    ), 200


@app.route("/content/<content_id>", methods=["GET"])
def get_content(content_id):
    """Return one piece of content's audit record so its creator can see the
    verdict (and decide whether to appeal). 404 if the id is unknown."""
    record = audit.find_entry(content_id)
    if record is None:
        return jsonify({"error": f"no content found with id {content_id}"}), 404
    return jsonify(record), 200


@app.route("/log", methods=["GET"])
def log():
    """Return recent audit-log entries as JSON, most recent first.

    Optional ?limit=N caps the number returned and ?status=under_review filters
    to the appeal queue a human reviewer works. In a real system this would
    require auth; here it exists for documentation and grading visibility.
    """
    limit = request.args.get("limit", type=int)
    status = request.args.get("status")
    return jsonify({"entries": audit.get_log(limit=limit, status=status)})


if __name__ == "__main__":
    app.run(port=5001, debug=True)
