## Architecture Narrative

text → [API Gateway] → [Rate Limiter] → [Detection Pipeline] → [Signal A]+[Signal B]
→ [Scorer: fuse → confidence] → [Label Mapper] → [Audit Log] → response → user

1. The API Gateway/Submission Endpoint:
   1. The text arrives as an HTTP request (POST/submit with content in the body). This endpoint's job is to accept text-based content, validate it (check if it's text, within size limits) and hand it down the pipeline. At the end, it's also this component that will package the finalized response, return to caller.
2. Rate Limiter:
   1. Before text is allowed any further, request is passed through rate limiting, meant to count number of submissions the caller has made in a time window.
   2. If too many, return 429 too many requests error.
   3. This protects the expensive detection work downstream from abuse/being overwhelmed.
3. Detection pipeline:
   1. Fan the text out to each signal, collect their individual results, pass to the scorer. Is a coordinator who oversees each signal will run.
4. Signal Analyzers: Text will be examined independently by at least 2 distinct signals. Each looks at a different text property, outputs their own opinion (with a subscore indicating leaning towards AI or human).
   1. Signal A (numeric/style): Measures attributes like sentence-length variance and diversity of vocabulary (AI text tends to be smoother and more uniform).
   2. Signal B (LLM judge via Groq): Asks a model how machine-like the text reads, judging meaning and register rather than statistics.
   3. Signal B is the primary detector and Signal A is a secondary corroborator. We learned during development that sentence structure alone isn't a reliable AI tell (fluent AI writes with human-like rhythm), so the semantic judge has to carry the decision while the stylometric stats only nudge it.
5. The scorer: The judge who weights the evidence. Take the individual signal results, come up with a concluding single confidence score (0-1 where 1 = definitely AI). Weight Signal B heavily (0.8) and Signal A lightly (0.2), then shrink toward the uncertain middle when the text is too short to trust.
6. Label mapper:
   1. high score → "Written by AI"
   2. low score → "A human wrote it"
   3. middle → "Uncertain"
7. Audit log: Permanent record before response goes out. The full decision is written to the audit log: Classirifcation, confidence score, which signals fired and what said, timestamp, and ID for this conetent.
8. Response: Returns the structured result to the caller: Attribution result, confidence score, transparency label text. Is displayed to user.
9. (IF the creator disagrees): The appeals endpoint/complaint desk. Capture the creator's reasoning for contesting a classification, attach appeal to original logged decision, flip content's status to be under review. Do not run detection.

## Architecture Diagram

Two flows. Each arrow is labeled with what passes between components.

![alt text](<Screenshot 2026-06-24 at 2.45.47 PM.jpg>)

Note the Audit Log is touched by **both** flows — the submission flow writes the original decision, and the appeal flow reads it back, mutates it, and writes it again. That shared node is why the data model is one record per piece of content rather than two separate logs.

## Spec Questions Addressed

### 1. Detection signals

Two signals that fail in **different** ways, so their agreement is meaningful (see the false-positive section on why correlated signals must not fake confidence).

**Signal A — Stylometric (heuristic, deterministic, no API).** Measures the statistical "texture" of the writing. Three sub-features, each normalized to 0–1:

- **Burstiness** — variance of sentence length. Humans vary a lot (a 3-word sentence next to a 40-word one); AI is smoother. Low variance → leans AI.
- **Lexical diversity** — type-token ratio (unique words ÷ total words). Very uniform or oddly "balanced" vocabulary leans AI.
- **Repetition** — rate of repeated bigrams/trigrams. Excessive n-gram repetition leans AI.

Output: a single `score_A ∈ [0,1]` (1 = looks AI), computed as a weighted blend of the three sub-features, plus a `detail` string naming the dominant feature.

**Signal B — LLM judge (Groq).** Sends the text to a Groq-hosted LLM with a rubric prompt asking it to assess how machine-generated the writing reads. Captures holistic / semantic cues the heuristics can't (clichés, hedging patterns, formulaic transitions, abstract filler, absence of lived detail). Degrades gracefully: on any API failure it returns a neutral 0.5 flagged `available: false`, and fusion falls back to Signal A alone.

Output: `score_B ∈ [0,1]` (1 = looks AI) plus a one-sentence `rationale`.

**Combining them into one confidence score** (the Scorer). **Signal B is the primary detector; Signal A is a secondary corroborator** — see the design-change note below for why we abandoned equal-peer fusion. Two steps:

1. **Weighted base (B-primary):** `raw = 0.2·score_A + 0.8·score_B`. The semantic judge dominates; the stylometric score only nudges.
2. **Length cap:** let `length_conf = min(1, word_count / 40)`. Short text can't earn confidence:
   `confidence = 0.5 + (raw − 0.5)·length_conf`. If Signal B is unavailable, fall back to `raw = score_A` and halve the confidence strength, since the primary detector is gone.

The result `confidence ∈ [0,1]` is what flows to the Label Mapper.

> **Design change made during development.** The original plan fused the two signals as equal peers (`0.4·A + 0.6·B`) with a _disagreement penalty_ that pulled the score toward 0.5 whenever they conflicted. Testing exposed a fatal flaw: a clearly AI-generated paragraph scored **0.247 (human)** on Signal A — because stylometric features like sentence structure are **not a reliable indicator of AI authorship** (fluent modern AI writes with varied, human-like rhythm). The disagreement penalty then let that _blind_ Signal A veto a correct Signal B (0.90), collapsing the verdict to "uncertain." Fix: make Signal B the primary signal (weight 0.8), drop the disagreement penalty, and lower the AI threshold to 0.75. After the change the same text fused to **0.769 → AI**, while a genuine human poem (Mara) still landed in "uncertain" rather than being accused — protection now comes from Signal B's quality (it reliably recognizes real humans too), not from an extreme threshold.

### 2. Uncertainty representation

- **What 0.6 means:** "the combined evidence leans AI, but weakly — inside the uncertain band." It is a _confidence-of-AI index_, not a calibrated probability. We deliberately don't claim "60% chance this is AI"; we claim "our signals leaned slightly AI but not enough to say so out loud."
- **Raw → calibrated:** the B-primary fusion above _is_ the calibration. Short length shrinks the score toward 0.5, and Signal A (weighted only 0.2) can nudge but not dominate, so the only way to reach an extreme is a confident Signal B on enough text. (Future work: fit thresholds against a labeled sample of known-AI / known-human texts — noted as a stretch.)
- **Thresholds:**

  | Confidence range | Classification   |
  | ---------------- | ---------------- |
  | `[0.00, 0.25)`   | **likely human** |
  | `[0.25, 0.75)`   | **uncertain**    |
  | `[0.75, 1.00]`   | **likely AI**    |

  Calling something AI requires **≥ 0.75**; calling it human requires **< 0.25**. (The original plan used an asymmetric 0.85 AI bar to guard against false accusations; that was lowered to 0.75 when fusion moved to B-primary — protection now comes from Signal B's reliability rather than an extreme threshold. See the design-change note above.) So `0.51` → uncertain and `0.95` → likely AI produce meaningfully different labels, exactly as the rubric demands.

### 3. Transparency label design

The three exact strings the reader sees. `{pct}` is `round(confidence·100)` for the AI label, `round((1−confidence)·100)` for the human label.

> **High-confidence AI:**
> "⚠️ This content appears to be AI-generated. Our system analyzed its writing patterns and is highly confident (about {pct}%) it was produced by an AI tool rather than written by a person. This is an automated assessment, not a verdict — the creator can contest it."

> **High-confidence human:**
> "✓ This content appears to be human-written. Our system found writing patterns consistent with a human author and is confident (about {pct}%) it was not AI-generated."

> **Uncertain:**
> "❓ We couldn't confidently determine how this content was created. Our system found mixed signals — it may have been written by a person, generated by AI, or a mix of both. We're showing this note instead of a definitive label to avoid making a wrong call."

Note the uncertain label deliberately shows **no percentage** — surfacing "55%" would imply a precision we don't have and invite misreading. Plain language carries the uncertainty instead.

### 4. Appeals workflow

- **Who can appeal:** the content's creator — the author who submitted it (identified by `creator_id`; in a real platform this is the authenticated content owner). For this project, possession of the `content_id` stands in for ownership, with proper auth noted as out of scope.
- **What they provide:** `content_id` (which decision they're contesting) and `creator_reasoning` (free-text — their account of why the classification is wrong). `reason` is accepted as a backward-compatible alias.
- **What the system does on receipt:**
  1. Look up the record by `content_id` → `404` if it doesn't exist.
  2. Append an `appeal` object `{ creator_reasoning, submitted_at }` to that record — the original `classification`, `confidence`, and `signals` are **preserved, never overwritten**.
  3. Flip `status: "classified" → "under_review"`.
  4. The audit log now reflects both the original decision and the appeal on the same record (the log _is_ the record store).
  5. Return the updated record. **No automated re-classification** — a human takes it from here.
- **What a human reviewer sees** (the appeal queue = `GET /log?status=under_review`): per item — `content_id`, the original text (or excerpt), the original `classification` + `confidence`, each signal's score and rationale, the timestamp, and the creator's `reason` + `submitted_at`. Enough context to make a human judgment without re-running anything.

### 5. Anticipated edge cases

Specific scenarios this system will handle poorly, and how the design softens (not solves) each:

1. **Low-entropy human writing — spare poetry, minimalist prose, simple/repetitive vocabulary.** This is the Mara case (worked through in detail below). Short sentences, uniform structure, and controlled vocabulary trip the stylometric heuristic toward AI. _Mitigation:_ Signal A is only weighted 0.2, so it can't accuse on its own; the primary semantic judge (Signal B) recognizes a real human poem and the length cap keeps short poems uncertain — routing most of these into "uncertain" rather than a false accusation. A confident, clean human poem can still be misclassified, which is exactly what the appeal path exists for.
2. **Very short text — a haiku, a one-line caption, a two-sentence excerpt.** Burstiness and lexical-diversity features are statistically meaningless on ~10–25 words, and the LLM judge is reduced to guessing. _Mitigation:_ `length_conf` shrinks the score hard toward 0.5; below a floor (e.g. < 25 words) the system effectively can only return "uncertain."
3. **Hybrid / heavily-edited content — a human rewriting an AI draft, or an AI paraphrasing a human's text.** There is no ground-truth single author. _Mitigation:_ this isn't a failure — the LLM judge itself returns a mid-range score on genuinely mixed text, which fuses to the uncertain band, and "uncertain" is the honest answer. Worth naming so it isn't mistaken for a bug.
4. **Non-English text, code, or structured lists.** The stylometric heuristics are tuned for English prose; they misfire on other languages, source code, or bulleted data. _Mitigation:_ these should land as "uncertain"; a production version would detect and route them separately (noted as out of scope).

## Design Principle: The False Positive Problem

The two errors are not equal. Telling an AI it wrote like a human is harmless. Telling a human writer "you didn't write this" is an accusation. So the guiding rule is: **when in doubt, say "Unknown" — a false "human" is cheap, a false "AI" is an accusation.** Uncertainty is not a weakness to minimize, it is the feature that protects real writers.

### Worked example: Mara the poet

Mara is a human poet whose style is clean, spare, and rhythmically regular — short sentences, consistent structure, controlled vocabulary. These are exactly the properties our **stylometric** signal associates with AI. She submits a genuine poem.

Tracing it through the system (real measured values):

1. Endpoint + Rate Limiter: nothing unusual, text is valid and she's under the limit, passes through.
2. Signal A (stylometric): sees low sentence-length variance and uniform structure, leans AI, **~0.42**.
3. Signal B (LLM judge): reads the poem for meaning and register, not statistics. It is **not** convinced this is AI — a genuine human poem doesn't read like machine output — so it stays lukewarm, **~0.40**.
4. The Scorer (B-primary): `raw = 0.2·0.42 + 0.8·0.40 ≈ 0.41`. Because the primary signal (B, weighted 0.8) isn't convinced, the fused score lands in the **uncertain** band — _not_ an accusation. The stylometric signal was fooled by her spare style; the semantic signal wasn't.

Signal A genuinely mistakes Mara's low-entropy style for AI, and no statistic can know she's human — which is exactly why Signal A is not allowed to decide on its own. The protection isn't a clever uncertainty trick; it's that the **primary** signal reads meaning, and a real human poem doesn't read as machine-generated. We reinforce this three ways:

1. Make the semantic judge primary. Signal A (weighted only 0.2) can nudge but never force an AI verdict by itself, so its known blind spots and false alarms (like Mara) can't accuse anyone.
2. Require ≥ 0.75 to call something AI. Borderline cases route into "Uncertain" instead of an accusation. Trade some true positives for far fewer false accusations — the right trade on a creative platform.
3. Treat short text as inherently uncertain. A 12-line poem doesn't carry enough signal to be confident about anything. Length caps how confident the score is allowed to get, regardless of what the signals say.

With this handling, Mara's score lands around **0.41** in the uncertain band and she sees: _"We couldn't confidently determine how this content was created."_ That is the system doing its job — uncertain, not accusatory.

### If she's still misclassified: the appeal

Suppose the thresholds still land her at confident-AI. The appeal path is her recourse:

1. Mara hits POST /appeal on her content's ID with her reasoning ("I wrote this myself, here are my drafts").
2. The Appeals Endpoint captures her reasoning verbatim.
3. It looks up her content's original audit-log record — the one holding verdict: AI, confidence: 0.80, signals: {A: 0.82, B: 0.78} — and attaches the appeal to it. The original decision is NOT erased; the dispute lives alongside it.
4. It flips status to "under review." No automated re-classification — a human reviewer takes it from here.

This is why the audit log sits at the center of the system and not the end: both the detection flow and the appeal flow write to it. It also argues for the data model being one mutable record per piece of content (ID, verdict, score, signals, status) that the appeal later mutates, rather than two disconnected logs.

## Endpoints needed:

1. POST/submit: Content submission + full detection pipeline.
2. POST/appeal: Appeals workflow, capture creator complaints of inaccuracy.
3. GET/log: Audit Log
4. GET/content/{id}: lets a creator see their verdict so they can appeal it.

## AI Tool Plan

How each implementation milestone will be built with an AI coding tool: what context I feed it, what I ask it to generate, and how I verify the output before moving on. The rule throughout: **provide the relevant spec sections as context, generate one slice at a time, and verify that slice in isolation before wiring it in.**

### M3 — Submission endpoint + first signal

- **Context I'll provide:** the [Detection signals](#1-detection-signals) section (Signal A only), the [Architecture Diagram](#architecture-diagram), and the `POST /submit` contract from [Endpoints needed](#endpoints-needed).
- **What I'll ask it to generate:**
  1. A Flask app skeleton with a single `POST /submit` route that accepts `{ text, creator_id }`, validates input (non-empty, max length), and returns a structured JSON stub.
  2. The **Signal A** function (`stylometric_score(text) -> {score, detail}`) implementing burstiness + lexical diversity + repetition, normalized to 0–1.
- **How I'll verify:** call `stylometric_score` directly on a handful of hand-picked inputs **before** wiring it into the endpoint — a uniform AI-ish paragraph should score high, a bursty human paragraph low. Then hit `/submit` with curl and confirm the response shape and a 400 on empty input. No second signal or scoring yet — the endpoint can return Signal A's raw score as a placeholder.

### M4 — Second signal + confidence scoring

- **Context I'll provide:** the [Detection signals](#1-detection-signals) section (full, including the fusion formula), the [Uncertainty representation](#2-uncertainty-representation) thresholds, and the diagram.
- **What I'll ask it to generate:**
  1. The **Signal B** function (`llm_judge_score(text) -> {score, rationale}`) calling Groq with a rubric prompt, returning 0–1 plus a rationale.
  2. The **Scorer** (`fuse(score_A, score_B, word_count) -> confidence`) implementing the B-primary formula: weighted base (0.2·A + 0.8·B) → length cap.
- **How I'll verify:** the key check is **do scores vary meaningfully** — feed clearly-AI text (expect ≥ 0.75), clearly-human text (expect ≤ 0.25), and a Mara-style low-entropy poem (expect it pulled into the uncertain band). Unit-test the fusion math directly: confirm Signal B dominates (a confident B overrides a wrong A), and that short text shrinks toward 0.5 regardless of signal values.

### M5 — Production layer (labels + appeals)

- **Context I'll provide:** the [Transparency label design](#3-transparency-label-design) variants, the [Appeals workflow](#4-appeals-workflow) section, and the diagram (the appeal flow specifically).
- **What I'll ask it to generate:**
  1. The **Label Mapper** (`to_label(confidence) -> {classification, label_text}`) mapping score → one of the three exact label strings, with `{pct}` interpolation.
  2. The `POST /appeal` endpoint: look up by `content_id` (404 if missing), append the appeal, flip status to `under_review`, preserve the original decision, and log it. Plus `GET /log` and `GET /content/{id}`.
- **How I'll verify:** confirm **all three label variants are reachable** — submit inputs engineered to land in each band and check the exact text comes back. Then test the appeal round-trip: submit → appeal the returned `content_id` → confirm status flips to `under_review`, the original classification/confidence are untouched, and the appeal reason appears in `GET /log`. Finally confirm a 404 when appealing an unknown `content_id`.
