from __future__ import annotations

import base64
import time
from typing import Any, Callable

from .context import (
    CHAT_TEMPLATE_OVERHEAD_TOKENS,
    CONTEXT_SAFETY_TOKENS,
    estimate_visual_tokens,
    non_thinking_output_tokens,
)
from . import goals as goal_ledger
from .media import STORE, MediaError
from .models.contract import ModelError, final_message_text
from .targets import TargetError, mode_limits, target_for_mode

def _asset_data_uri(session_id: str, asset_id: str, representation: str) -> str:
    try:
        media_type, payload = STORE.read_model_visual(session_id, asset_id, representation)
    except MediaError as error:
        raise ModelError("MEDIA_PREPARATION_FAILED", error.message, {"media_code": error.code}) from error
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _messages(
    assembled: dict[str, Any],
    model_info: dict[str, Any],
    session_id: str,
    runtime_plan: dict[str, Any],
    count_text_tokens: Callable[[str], int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    system = "\n\n".join(
        message["content"] for message in assembled["messages"] if message["role"] == "system"
    )
    user_text = next(message["content"] for message in assembled["messages"] if message["role"] == "user")
    content: list[dict[str, Any]] = []
    debug_user_parts: list[dict[str, str]] = []
    media_inputs = sorted(
        assembled["media_inputs"],
        key=lambda item: {"image": 0, "video": 1, "audio": 2}[item["type"]],
    )
    image_count = len([item for item in media_inputs if item["type"] == "image"])
    video_frame_count = 0
    video_sheet_count = 0
    for item in media_inputs:
        asset = item.get("snapshot_asset") or STORE.get(session_id, item["asset_id"])
        if item["type"] == "image":
            binding = f"{item['reference']}: image reference."
            content.append({"type": "text", "text": binding})
            content.append({
                "type": "image_url",
                "image_url": {"url": item.get("snapshot_uri") or _asset_data_uri(session_id, item["asset_id"], "image")},
            })
            debug_user_parts.extend([
                {"type": "text", "text": binding},
                {"type": "image", "source": item["reference"], "representation": "prepared image"},
            ])
        elif item["type"] == "video":
            frames = asset.get("_frames", [])
            video_frame_count += len(frames)
            video_sheet_count += 1
            binding = (
                f"{item['reference']}: one ordered contact sheet sampled from this same video. "
                "Read frames left-to-right, then top-to-bottom, using the displayed order and the accompanying manifest timestamps to infer motion. "
                f"This sheet is only the internal visual representation of {item['reference']}; it is not a <Picture N> "
                "and must never change or renumber the external reference labels."
            )
            content.append({"type": "text", "text": binding})
            content.append({
                "type": "image_url",
                "image_url": {"url": item.get("snapshot_uri") or _asset_data_uri(session_id, item["asset_id"], "contact_sheet")},
            })
            debug_user_parts.extend([
                {"type": "text", "text": binding},
                {"type": "image", "source": item["reference"], "representation": "ordered video contact sheet"},
            ])
    content.append({"type": "text", "text": user_text})
    debug_user_parts.append({"type": "text", "text": user_text})
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    visual_input_count = image_count + video_sheet_count
    text_tokens = count_text_tokens(system + "\n\n" + user_text)
    fallback_visual_tokens, visual_token_details, vision_budget_applied = estimate_visual_tokens(
        assembled,
        model_info,
    )
    visual_tokens = int(runtime_plan.get("estimated_visual_tokens", fallback_visual_tokens))
    if assembled.get("completion_policy") == "single_call":
        # A stable batch plan must not undercount a later chunk's actual media.
        visual_tokens = max(visual_tokens, fallback_visual_tokens)
    estimated_input_tokens = (
        text_tokens
        + visual_tokens
        + CHAT_TEMPLATE_OVERHEAD_TOKENS
    )
    output_limit = runtime_plan.get("max_output_tokens")
    reserved_output_tokens = (
        int(output_limit) if isinstance(output_limit, int) and output_limit > 0 else 0
    ) + CONTEXT_SAFETY_TOKENS
    if estimated_input_tokens + reserved_output_tokens > runtime_plan["context_tokens"]:
        raise ModelError(
            "CONTEXT_BUDGET_EXCEEDED",
            "The selected references and guide leave too little context for a complete prompt.",
            {
                "estimated_input_tokens": estimated_input_tokens,
                "reserved_output_tokens": reserved_output_tokens,
                "context_tokens": runtime_plan["context_tokens"],
                "suggestion": "Choose a larger Context profile or shorten the creative brief.",
            },
        )
    return messages, {
        "visual_input_count": visual_input_count,
        "video_frame_count": video_frame_count,
        "video_sheet_count": video_sheet_count,
        "vision_budget_applied": runtime_plan.get("vision_budget_applied", vision_budget_applied),
        "estimated_visual_tokens": visual_tokens,
        "visual_token_details": runtime_plan.get("visual_token_details", visual_token_details),
        "estimated_input_tokens": estimated_input_tokens,
        "reserved_output_tokens": reserved_output_tokens,
        "debug_input_sequence": [
            {"role": "system", "parts": [
                {
                    "type": "text",
                    "source": message.get("name", "system"),
                    "text": message["content"],
                }
                for message in assembled["messages"] if message["role"] == "system"
            ]},
            {"role": "user", "parts": debug_user_parts},
        ],
    }


def _target(assembled: dict[str, Any]):
    mode = assembled.get("input", {}).get("mode")
    try:
        return target_for_mode(mode)
    except TargetError as error:
        raise ModelError("INVALID_MODE", "The selected generation mode is not supported.", {"mode": mode}) from error


def _audit(prompt: str, assembled: dict[str, Any]) -> dict[str, Any]:
    """Audit the draft the way its own target is audited.

    H3 checks official sections and reference inventory; Krea 2 checks that the
    result is one paragraph of prose; Anima checks tag order and the variant's
    score-tag rule. All of them additionally check that facts established in the
    generic prompt survived and that every standing goal still holds.
    """
    return _target(assembled).audit(prompt, assembled)


def _text_only_modes() -> list[str]:
    return sorted(mode for mode, limits in mode_limits().items() if not limits)


def validate_media_capabilities(model_info: dict[str, Any], assembled: dict[str, Any]) -> None:
    mode = assembled.get("input", {}).get("mode")
    has_visual_request = any(
        item.get("type") in {"image", "video"} for item in assembled.get("media_inputs", [])
    )
    if (
        model_info.get("family") == "gguf"
        and model_info.get("capabilities", {}).get("images") is False
        and has_visual_request
    ):
        # Gated on the actual attachments rather than on the mode: a text-only
        # model can write any target's prompt, it just cannot look at pictures.
        supported = _text_only_modes()
        raise ModelError(
            "DIRECT_VISION_REQUIRED",
            "This Direct GGUF model is running without a compatible vision projector. "
            "Modes that take no reference media are available: " + ", ".join(supported) + ".",
            {
                "mode": mode,
                "supported_modes": supported,
                "suggestion": "Remove the attached references, or add the matching mmproj GGUF beside the model.",
            },
        )
    required = {item["requires_capability"] for item in assembled["media_inputs"]}
    unsupported = sorted(name for name in required if model_info["capabilities"].get(name) is not True)
    if unsupported:
        if model_info.get("family") == "external" and {"images", "video_frames"}.intersection(unsupported):
            raise ModelError(
                "EXTERNAL_VISION_REQUIRED",
                "The External llama.cpp model is running in text-only mode and cannot analyze the attached images or video.",
                {
                    "capabilities": unsupported,
                    "suggestion": "Restart llama-server with the matching mmproj, remove visual references, or select a vision-capable prompt model.",
                },
            )
        raise ModelError(
            "UNSUPPORTED_MEDIA",
            "The selected prompt model cannot analyze all media in the current manifest.",
            {"capabilities": unsupported},
        )


def run_h3_pipeline(
    model_info: dict[str, Any],
    assembled: dict[str, Any],
    session_id: str,
    runtime_plan: dict[str, Any],
    *,
    complete: Callable[..., dict[str, Any]],
    count_text_tokens: Callable[[str], int],
    is_cancelled: Callable[[], bool],
    thinking: bool,
    seed: int | None,
    on_phase: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    validate_media_capabilities(model_info, assembled)
    standard_output_tokens = non_thinking_output_tokens(assembled)

    if on_phase:
        on_phase("processing_media")
    media_started = time.perf_counter()
    messages, media_metrics = _messages(assembled, model_info, session_id, runtime_plan, count_text_tokens)
    media_processing_seconds = time.perf_counter() - media_started
    if is_cancelled():
        raise ModelError("GENERATION_CANCELLED", "Generation was cancelled after media preparation.")

    if on_phase:
        on_phase("generating")
    generation_started = time.perf_counter()
    # Prompt writing is creative sampling; a JSON contract is not. The generic
    # stages ask for lower temperature because a document that stops mid-string
    # is a total loss, and the creativity belongs in the field values, not in
    # whether the object closes.
    sampling = {"temperature": 1.0, "top_p": 0.95, "top_k": 64, **(assembled.get("sampling") or {})}
    response = complete(
        messages=messages,
        temperature=sampling["temperature"],
        top_p=sampling["top_p"],
        top_k=sampling["top_k"],
        # A Direct GGUF model policy owns sampling for prompt writing, but not
        # for a JSON contract: `structured` tells the backend this request chose
        # its own values on purpose.
        structured=bool(assembled.get("sampling")),
        max_tokens=runtime_plan["max_output_tokens"],
        seed=seed,
        thinking=thinking,
        purpose="generation",
    )
    message = response["choices"][0]["message"]
    usage = response.get("usage", {})
    primary_finish_reason = response["choices"][0].get("finish_reason")
    thinking_attempt_tokens = int(usage.get("completion_tokens", 0)) if thinking else 0
    qwen_reasoning_contract = model_info.get("architecture_adapter") in {"qwen35", "qwen35moe"}
    reasoning_content: str | None = None
    if thinking and primary_finish_reason == "length":
        text = ""
    else:
        text, reasoning_content = final_message_text(
            message,
            thinking=thinking,
            qwen_reasoning_contract=qwen_reasoning_contract,
        )
    single_call = assembled.get("completion_policy") == "single_call"
    if single_call and primary_finish_reason not in {"stop", "eos", "end_turn"}:
        if assembled.get("sequence_stage") == "plan":
            raise ModelError("INVALID_SEQUENCE_PLAN", "The model stopped before completing its plan. No new chunks were written. Run Generate Sequence again.",
                             {"stage": "planning", "reason": "incomplete_response", "finish_reason": primary_finish_reason})
        raise ModelError("GENERATION_INCOMPLETE", "The model did not complete the chunk. Increase the generation budget if it reached its limit.")
    if is_cancelled():
        raise ModelError("GENERATION_CANCELLED", "Generation was cancelled.")
    thinking_fallback = not single_call and thinking and (
        not text.strip() or primary_finish_reason == "length"
    )
    if thinking_fallback:
        fallback_output_tokens = standard_output_tokens
        if runtime_plan.get("generation_budget_manual"):
            fallback_output_tokens = min(
                fallback_output_tokens,
                int(runtime_plan["max_output_tokens"]),
            )
        response = complete(
            messages=messages,
            temperature=1.0,
            top_p=0.95,
            top_k=64,
            max_tokens=fallback_output_tokens,
            seed=seed,
            thinking=False,
            purpose="generation",
        )
        message = response["choices"][0]["message"]
        text, _fallback_reasoning = final_message_text(
            message,
            thinking=False,
            qwen_reasoning_contract=qwen_reasoning_contract,
        )
        fallback_usage = response.get("usage", {})
        usage = {
            "prompt_tokens": fallback_usage.get("prompt_tokens", usage.get("prompt_tokens", 0)),
            "completion_tokens": thinking_attempt_tokens + int(fallback_usage.get("completion_tokens", 0)),
        }
    final_finish_reason = response["choices"][0].get("finish_reason")
    if final_finish_reason == "length":
        final_output_limit = fallback_output_tokens if thinking_fallback else runtime_plan["max_output_tokens"]
        raise ModelError(
            "GENERATION_TRUNCATED",
            "The model reached the available output limit before completing the prompt. Try again, shorten the requested detail, or enable Thinking when available.",
            {"max_output_tokens": final_output_limit},
        )
    if not text.strip():
        raise ModelError("EMPTY_GENERATION", "The model did not produce a final prompt.")

    prompt = text
    reasoning_tokens = count_text_tokens(reasoning_content) if reasoning_content else 0
    if single_call:
        seconds = time.perf_counter() - generation_started
        return {"prompt": text, "input_tokens": int(usage.get("prompt_tokens", 0)),
                "output_tokens": int(usage.get("completion_tokens", 0)), "generation_seconds": round(seconds, 3),
                "media_processing_seconds": round(media_processing_seconds, 3), **media_metrics,
                "thinking_fallback": False, "format_repair_attempted": False,
                "primary_finish_reason": primary_finish_reason, "seed": seed}
    target = _target(assembled)
    initial_audit = _audit(prompt, assembled)
    # A judged goal cannot be checked by inspection, so it gets a verifier pass.
    # Unverifiable is NOT the same as met: a goal whose verdict never arrives
    # stays pending and is reported as such.
    def verify_pending_goals(audit: dict[str, Any], text: str) -> int:
        """Resolve the audit's pending judged goals against `text`. Returns tokens."""
        pending_ids = set(audit.get("goals_pending") or [])
        if not pending_ids or is_cancelled():
            return 0
        pending = [goal for goal in (audit.get("goals") or []) if goal["id"] in pending_ids]
        output_limit = runtime_plan.get("max_output_tokens")
        verify_response = complete(
            messages=goal_ledger.verify_messages(pending, text),
            temperature=0.0,
            top_p=0.9,
            top_k=40,
            # The external llama.cpp backend leaves the output limit to the
            # server, so this is None there; a verdict is two lines either way.
            max_tokens=min(768, int(output_limit)) if isinstance(output_limit, int) and output_limit > 0 else 768,
            seed=seed,
            thinking=False,
            purpose="verify",
        )
        verify_usage = verify_response.get("usage", {})
        spent = int(verify_usage.get("completion_tokens", 0))
        usage["prompt_tokens"] = int(usage.get("prompt_tokens", 0)) + int(verify_usage.get("prompt_tokens", 0))
        usage["completion_tokens"] = int(usage.get("completion_tokens", 0)) + spent
        verdict_text, _verify_reasoning = final_message_text(
            verify_response["choices"][0]["message"],
            thinking=False,
            qwen_reasoning_contract=qwen_reasoning_contract,
        )
        verdicts = goal_ledger.parse_verification(verdict_text, pending)
        updated_goals, goal_violations = goal_ledger.apply_verdicts(audit.get("goals") or [], verdicts)
        audit["goals"] = updated_goals
        audit["goals_pending"] = [goal["id"] for goal in pending if goal["id"] not in verdicts]
        if goal_violations:
            audit["goal_violations"] = list(audit.get("goal_violations") or []) + goal_violations
            audit["shared_failures"] = list(audit.get("shared_failures") or []) + goal_violations
            audit["repair_required"] = True
        return spent

    goal_verification_tokens = verify_pending_goals(initial_audit, prompt)

    format_repair_attempted = False
    format_repair_applied = False
    format_repair_tokens = 0
    format_repair_reason = None
    format_repair_failure = None
    format_repair_method = None
    # The gate is the audit's own verdict, whatever the target. Every audit
    # reports repair_required, so an image target's format failure, a dropped
    # locked fact and an unmet goal all reach the same single repair pass.
    repair_needed = initial_audit.get("repair_required") is True
    repair_plan: dict[str, Any] = {}
    if repair_needed:
        format_repair_attempted = True
        repair_plan = target.repair_plan(assembled, messages, prompt, initial_audit)
        repair_plan["original"] = prompt
        repair_messages = repair_plan["messages"]
        format_repair_method = repair_plan["method"]
        format_repair_reason = repair_plan.get("reason") or "format audit"
        if is_cancelled():
            raise ModelError("GENERATION_CANCELLED", "Generation was cancelled before prompt correction.")
        repair_output_tokens = (
            min(standard_output_tokens, int(runtime_plan["max_output_tokens"]))
            if runtime_plan.get("generation_budget_manual")
            else standard_output_tokens
        )
        repair_response = complete(
            messages=repair_messages,
            temperature=0.3,
            top_p=0.9,
            top_k=40,
            max_tokens=repair_output_tokens,
            seed=seed,
            thinking=False,
            purpose="repair",
        )
        repair_usage = repair_response.get("usage", {})
        format_repair_tokens = int(repair_usage.get("completion_tokens", 0))
        repair_finish_reason = repair_response["choices"][0].get("finish_reason")
        repaired, _repair_reasoning = final_message_text(
            repair_response["choices"][0]["message"],
            thinking=False,
            qwen_reasoning_contract=qwen_reasoning_contract,
        )
        # A repair is never accepted unchecked: each target re-audits its own
        # correction and reports why it refused one, so a failed repair cannot
        # look identical to a successful one.
        if not repaired:
            format_repair_failure = "empty repair"
        elif repair_finish_reason == "length":
            format_repair_failure = "repair reached its output limit"
        else:
            accepted, repaired_audit, failure = target.accept_repair(assembled, prompt, repaired, repair_plan)
            if accepted:
                prompt = repaired
                format_repair_applied = True
                initial_audit = repaired_audit
                # Re-auditing reset every judged goal to pending, so a repair
                # driven by an unmet goal was accepted without anyone checking
                # it fixed that goal, and the user saw "could not be verified".
                goal_verification_tokens += verify_pending_goals(initial_audit, prompt)
                if initial_audit.get("repair_required") is True:
                    format_repair_applied = False
                    prompt = repair_plan.get("original", prompt)
                    format_repair_failure = (
                        "repaired draft still failed: "
                        + ", ".join(initial_audit.get("shared_failures") or ["an unmet goal"])
                    )
            else:
                format_repair_failure = failure
        usage["prompt_tokens"] = int(usage.get("prompt_tokens", 0)) + int(repair_usage.get("prompt_tokens", 0))
        usage["completion_tokens"] = int(usage.get("completion_tokens", 0)) + format_repair_tokens

    generation_seconds = time.perf_counter() - generation_started
    output_tokens = int(usage.get("completion_tokens", 0))
    # Anima answers with a positive and a negative prompt; splitting them here
    # means the UI gets two fields it can copy separately instead of a blob the
    # user has to divide by hand at paste time.
    parsed = target.parse_output(prompt) if target.parse_output else {}
    return {
        "prompt": parsed.get("prompt", prompt) or prompt,
        "negative_prompt": parsed.get("negative_prompt", ""),
        "media_warnings": assembled["input"].get("media_warnings") or [],
        "goals": initial_audit.get("goals") or [],
        "goal_verification_tokens": goal_verification_tokens,
        "prompt_audit": initial_audit,
        "input_tokens": int(usage.get("prompt_tokens", 0)),
        "output_tokens": output_tokens,
        "generation_seconds": round(generation_seconds, 3),
        "media_processing_seconds": round(media_processing_seconds, 3),
        **media_metrics,
        "thinking_fallback": thinking_fallback,
        "thinking_attempt_tokens": thinking_attempt_tokens,
        "reasoning_tokens": reasoning_tokens,
        "primary_finish_reason": primary_finish_reason,
        "format_repair_attempted": format_repair_attempted,
        "format_repair_applied": format_repair_applied,
        "format_repair_reason": format_repair_reason,
        "format_repair_failure": format_repair_failure,
        "format_repair_method": format_repair_method,
        "format_repair_multimodal": format_repair_method == "multimodal reference correction",
        "format_repair_tokens": format_repair_tokens,
        "seed": seed,
        "tokens_per_second": round(output_tokens / generation_seconds, 2) if generation_seconds > 0 else 0,
    }
