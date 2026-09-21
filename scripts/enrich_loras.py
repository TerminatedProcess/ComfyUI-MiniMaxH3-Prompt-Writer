#!/usr/bin/env python3
"""
enrich_loras.py

Distil metadata for all MiniMax-H3 LoRAs in the hub DB into a local sidecar
SQLite (.lab/lora_meta.db), using a local LLM to summarise Civitai
descriptions/trained-words into a compact, prompt-writer-friendly form.

Read-only against the hub DB. Idempotent / resumable: rows already present
in the sidecar are skipped unless --force is passed. Content is immutable
per hash_sha256, so distillation happens at most once per LoRA (ever).

Usage:
    python3 scripts/enrich_loras.py [--force] [--limit N] [--db PATH]
"""

import argparse
import html
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

HUB_DB = "/mnt/llm/hub/hubmodels/hubrootv3.db"
SIDECAR_DB_DEFAULT = ".lab/lora_meta.db"
LLM_URL = "http://127.0.0.1:8890/v1/chat/completions"
LLM_MODEL = "qwen3-vl-8b-abliterated"
LLM_TIMEOUT = 60

SYSTEM_PROMPT = (
    "You distil raw metadata about a LoRA (a small fine-tune adapter for a video "
    "generation model, MiniMax H3) into a compact structured summary for a prompt-"
    "writing tool. The content may be adult/NSFW; describe it factually and "
    "directly, the same as any other visual concept. Do not refuse, moralise, or "
    "sanitise.\n\n"
    "You will be given the LoRA's file/display name, its known activation trigger "
    "words, and (optionally) a cleaned Civitai description. Some of the description "
    "text is noise: Discord invite links, changelog entries, donation asks, strength/"
    "sampler/workflow settings, unrelated announcements. Ignore that noise.\n\n"
    "Output STRICT JSON only, no markdown fences, no commentary, matching exactly "
    "this shape:\n"
    '{"concept": "<ONE short plain sentence, under 200 characters, describing the '
    'visual/behavioural EFFECT this LoRA has on a video, or null if there is no real '
    'signal>", '
    '"vocabulary": ["<short phrase>", ...up to 8 short phrases that would naturally '
    'appear in a video prompt invoking this LoRA, empty list if concept is null>"], '
    '"mode_fit": "<one of \\"t2v\\", \\"i2v\\", \\"any\\">"}\n\n'
    "Critical grounding rule: you are SUMMARISING, not WRITING. Only state details that "
    "are explicitly present in the supplied name/description. Do NOT invent scene "
    "elements, settings, clothing, props, camera work, character actions, or narrative "
    "detail that is not literally stated in the source text. If the source describes a "
    "mechanism or effect generically (e.g. \"causes penetration instead of external "
    "rubbing\"), describe it that generically - do not dress it up into an invented scene. "
    "A vague-but-accurate sentence is correct; a vivid-but-fabricated one is a failure.\n\n"
    "Other rules:\n"
    "- concept MUST be null if the provided text gives no real signal about what the "
    "LoRA visually does (e.g. only changelog/community noise, settings/workflow tips, "
    "or empty). A wrong or invented concept is worse than none - do not guess.\n"
    "- concept must be ONE sentence, no more than ~200 characters. Do not write a "
    "paragraph or a narrative scene.\n"
    "- vocabulary phrases must also be grounded in the source text or be generic "
    "prompt-writing terms for the described effect - not invented specifics (no "
    "invented clothing, settings, or props).\n"
    "- vocabulary must be empty when concept is null.\n"
    "- mode_fit: infer from the name/description if \"T2V\" or \"I2V\" is mentioned; "
    "default to \"any\" if unclear.\n"
    "- Never invent trigger words; those are supplied separately and are not your job.\n"
)


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def connect_hub_readonly(path):
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def connect_sidecar(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lora_meta (
            hash_sha256 TEXT PRIMARY KEY,
            filename TEXT,
            name TEXT,
            preview_url TEXT,
            triggers TEXT,
            concept TEXT,
            vocabulary TEXT,
            mode_fit TEXT,
            signal_source TEXT,
            distilled_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def fetch_rows(hub_conn):
    query = """
        SELECT
            m.hash_sha256 AS hash_sha256,
            m.filename AS filename,
            m.name AS name,
            m.preview_url AS preview_url,
            m.triggers AS hub_triggers,
            c.response_json AS cache_json
        FROM models m
        LEFT JOIN api_cache c ON c.cache_key = m.hash_sha256
        WHERE m.model_type = 'lora' AND m.base_model = 'minimax h3'
        ORDER BY m.filename
    """
    return hub_conn.execute(query).fetchall()


def parse_hub_triggers(raw):
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x) for x in val if str(x).strip()]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def extract_cache_signal(cache_json_raw):
    """Return (trained_words, description_clean, base_model) from cached Civitai JSON."""
    if not cache_json_raw:
        return [], "", None
    try:
        data = json.loads(cache_json_raw)
    except (json.JSONDecodeError, TypeError):
        return [], "", None
    trained_words = data.get("trainedWords") or []
    trained_words = [str(w) for w in trained_words if str(w).strip()]
    description = strip_html(data.get("description") or "")
    base_model = data.get("baseModel")
    return trained_words, description, base_model


def infer_mode_fit(name, description):
    hay = f"{name or ''} {description or ''}".upper()
    has_t2v = "T2V" in hay
    has_i2v = "I2V" in hay
    if has_t2v and not has_i2v:
        return "t2v"
    if has_i2v and not has_t2v:
        return "i2v"
    return "any"


# Few-shot examples anchoring the two failure modes seen in testing:
# (1) trivial/version-only "description" text must yield concept=null, not a
#     hallucinated guess; (2) a real description that includes an illustrative
#     example scene/prompt must be summarised as a general mechanism, not
#     copied as if the example's wardrobe/setting/props were inherent to the LoRA.
FEWSHOT = [
    {
        "role": "user",
        "content": (
            "Name: AfterMidnight_ref2va_h3_sexytime_rank64-v1.2\n\n"
            "Description:\nv1.2"
        ),
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {"concept": None, "vocabulary": [], "mode_fit": "any"}
        ),
    },
    {
        "role": "user",
        "content": (
            "Name: BEANFLK_H3_V1\n\n"
            "Known trigger words: BEANFLK\n\n"
            "Description:\nThis is the version that actually goes in. Most video "
            "models will rub around on the outside all day and never insert. This "
            "build was made to fix that - fingers pushing in deep until her "
            "knuckles press against her, working inside her, and coming back out "
            "wet. It will also bring her hands down and start the whole thing from "
            "a frame where she isn't touching herself at all.\n\n"
            "Example prompt showing the trigger in use:\n"
            "BEANFLK. A woman in a red velvet dress sits back on a bar stool in a "
            "dim bar, legs spread wide... She slides two fingers between her labia "
            "and pushes them in deep..."
        ),
    },
    {
        "role": "assistant",
        "content": json.dumps(
            {
                "concept": (
                    "Causes fingers to insert deep into the vulva during the video "
                    "rather than only rubbing externally, and can initiate the "
                    "motion itself without the hand pre-positioned."
                ),
                "vocabulary": [
                    "fingers push in deep",
                    "knuckles press against her",
                    "working inside her",
                    "hips lifting to meet her hand",
                    "fingers drawing out wet",
                ],
                "mode_fit": "any",
            }
        ),
    },
]


def call_llm(name, triggers, description):
    user_parts = [f"Name: {name or '(unknown)'}"]
    if triggers:
        user_parts.append(f"Known trigger words: {', '.join(triggers)}")
    if description:
        user_parts.append(f"Description:\n{description}")
    user_content = "\n\n".join(user_parts)

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *FEWSHOT,
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": 500,
    }
    resp = requests.post(LLM_URL, json=payload, timeout=LLM_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    return body["choices"][0]["message"]["content"]


def parse_llm_json(raw_text):
    text = raw_text.strip()
    # Strip markdown code fences if present.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Try to locate the first {...} block if there's stray text around it.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    data = json.loads(text)
    concept = data.get("concept")
    if concept is not None:
        concept = str(concept).strip() or None
    if concept is not None:
        # Defensive guard: keep only the first sentence, cap length. A model that
        # drifts into a multi-sentence narrative is a grounding failure, not
        # something we want to store verbatim.
        first_sentence = re.split(r"(?<=[.!?])\s+", concept, maxsplit=1)[0]
        concept = first_sentence[:220].rstrip()
    vocabulary = data.get("vocabulary") or []
    if not isinstance(vocabulary, list):
        vocabulary = []
    vocabulary = [str(v).strip() for v in vocabulary if str(v).strip()]
    mode_fit = data.get("mode_fit") or "any"
    if mode_fit not in ("t2v", "i2v", "any"):
        mode_fit = "any"
    if concept is None:
        vocabulary = []
    return concept, vocabulary, mode_fit


def distill_with_retry(name, triggers, description):
    for attempt in range(2):
        try:
            raw = call_llm(name, triggers, description)
            return parse_llm_json(raw)
        except Exception as exc:  # noqa: BLE001 - defensive, one bad row must not abort run
            last_err = exc
            if attempt == 0:
                continue
    print(f"    [warn] LLM distill failed for {name!r}: {last_err}", file=sys.stderr)
    return None, [], "any"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Recompute rows that already exist")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N rows (testing)")
    parser.add_argument("--db", default=SIDECAR_DB_DEFAULT, help="Path to sidecar sqlite db")
    args = parser.parse_args()

    hub_conn = connect_hub_readonly(HUB_DB)
    side_conn = connect_sidecar(args.db)

    rows = fetch_rows(hub_conn)
    hub_conn.close()
    print(f"Fetched {len(rows)} MiniMax-H3 LoRA rows from hub DB.")

    existing = set()
    if not args.force:
        existing = {
            r[0] for r in side_conn.execute("SELECT hash_sha256 FROM lora_meta").fetchall()
        }
        print(f"{len(existing)} rows already present in sidecar; will skip those.")

    processed = 0
    skipped = 0
    llm_calls = 0
    failed = []
    start = time.time()

    for row in rows:
        h = row["hash_sha256"]
        if not args.force and h in existing:
            skipped += 1
            continue
        if args.limit is not None and processed >= args.limit:
            break

        hub_triggers = parse_hub_triggers(row["hub_triggers"])
        trained_words, description, _cache_base_model = extract_cache_signal(row["cache_json"])

        triggers = list(dict.fromkeys(hub_triggers + trained_words))  # dedupe, preserve order

        has_trained_words = bool(trained_words)
        has_description = bool(description.strip())

        if has_trained_words and has_description:
            signal_source = "both"
        elif has_trained_words:
            signal_source = "trained_words"
        elif has_description:
            signal_source = "description"
        else:
            signal_source = "none"

        if signal_source == "none":
            concept, vocabulary, mode_fit = None, [], infer_mode_fit(row["name"], description)
        else:
            try:
                concept, vocabulary, mode_fit = distill_with_retry(
                    row["name"] or row["filename"], triggers, description
                )
                llm_calls += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    [error] unexpected failure on {row['filename']}: {exc}", file=sys.stderr)
                failed.append(row["filename"])
                concept, vocabulary, mode_fit = None, [], "any"
            if mode_fit == "any":
                inferred = infer_mode_fit(row["name"], description)
                if inferred != "any":
                    mode_fit = inferred

        side_conn.execute(
            """
            INSERT INTO lora_meta
                (hash_sha256, filename, name, preview_url, triggers, concept, vocabulary, mode_fit, signal_source, distilled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hash_sha256) DO UPDATE SET
                filename=excluded.filename,
                name=excluded.name,
                preview_url=excluded.preview_url,
                triggers=excluded.triggers,
                concept=excluded.concept,
                vocabulary=excluded.vocabulary,
                mode_fit=excluded.mode_fit,
                signal_source=excluded.signal_source,
                distilled_at=excluded.distilled_at
            """,
            (
                h,
                row["filename"],
                row["name"],
                row["preview_url"],
                json.dumps(triggers),
                concept,
                json.dumps(vocabulary),
                mode_fit,
                signal_source,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        side_conn.commit()
        processed += 1

        if processed % 10 == 0:
            elapsed = time.time() - start
            print(f"  ...{processed} processed, {skipped} skipped, {elapsed:.1f}s elapsed")

    elapsed = time.time() - start
    print(
        f"Done. processed={processed} skipped={skipped} llm_calls={llm_calls} "
        f"failed={len(failed)} elapsed={elapsed:.1f}s"
    )
    if failed:
        print("Failed filenames:", failed)

    side_conn.close()


if __name__ == "__main__":
    main()
