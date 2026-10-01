"""Deterministic handling for Comate's structured GRILL questions.

The interaction itself happens in Comate.  This module keeps the boundary
auditable once answers come back: validate the question packet, reject malformed
or incomplete answers, and turn selected options into the existing decision-log
shape.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def validate_questions(questions: Any) -> list[str]:
    """Return stable validation codes for a choice-question packet."""
    if not isinstance(questions, list) or not questions:
        return ["CHOICE_QUESTIONS_REQUIRED"]
    issues: list[str] = []
    seen: set[str] = set()
    for index, question in enumerate(questions):
        prefix = f"choice_questions[{index}]"
        if not isinstance(question, dict):
            issues.append(f"{prefix}:OBJECT_REQUIRED")
            continue
        question_id = question.get("id")
        if not isinstance(question_id, str) or not question_id.strip():
            issues.append(f"{prefix}.id:REQUIRED")
        elif question_id in seen:
            issues.append(f"{prefix}.id:DUPLICATE")
        else:
            seen.add(question_id)
        for field in ("title", "prompt", "impact"):
            if not isinstance(question.get(field), str) or not question[field].strip():
                issues.append(f"{prefix}.{field}:REQUIRED")
        options = question.get("options")
        if not isinstance(options, list) or len(options) < 2:
            issues.append(f"{prefix}.options:AT_LEAST_TWO")
            continue
        option_ids: set[str] = set()
        for option_index, option in enumerate(options):
            option_prefix = f"{prefix}.options[{option_index}]"
            if not isinstance(option, dict):
                issues.append(f"{option_prefix}:OBJECT_REQUIRED")
                continue
            option_id, label = option.get("id"), option.get("label")
            if not isinstance(option_id, str) or not option_id.strip():
                issues.append(f"{option_prefix}.id:REQUIRED")
            elif option_id in option_ids:
                issues.append(f"{option_prefix}.id:DUPLICATE")
            else:
                option_ids.add(option_id)
            if not isinstance(label, str) or not label.strip():
                issues.append(f"{option_prefix}.label:REQUIRED")
        for field in ("allow_multiple", "required", "other_allowed"):
            if not isinstance(question.get(field), bool):
                issues.append(f"{prefix}.{field}:BOOLEAN_REQUIRED")
        if not isinstance(question.get("evidence"), list) or not question["evidence"]:
            issues.append(f"{prefix}.evidence:REQUIRED")
    return issues


def apply_answers(
    decision_log: dict[str, Any],
    questions: list[dict[str, Any]],
    answers: dict[str, Any],
    *,
    decided_by: str,
    answer_evidence: list[str],
) -> dict[str, Any]:
    """Apply one Comate answer round without inventing unanswered decisions.

    `answers` maps a question id to either an option id, a list of option ids, or
    an object with `choices`, optional `other_text`, and optional `rationale`.
    """
    issues = validate_questions(questions)
    if issues:
        raise ValueError(";".join(issues))
    if not isinstance(decision_log, dict) or not isinstance(answers, dict):
        raise ValueError("ANSWER_PACKET_INVALID")
    if not isinstance(decided_by, str) or not decided_by.strip():
        raise ValueError("DECIDED_BY_REQUIRED")
    if not isinstance(answer_evidence, list) or not answer_evidence:
        raise ValueError("ANSWER_EVIDENCE_REQUIRED")

    result = deepcopy(decision_log)
    result.setdefault("decisions", [])
    result.setdefault("unresolved_frontier", [])
    result.setdefault("acceptance_delta", [])
    result.setdefault("glossary_delta", {})
    result.setdefault("adr_candidates", [])
    result.setdefault("source_evidence", [])
    result["source_evidence"] = list(dict.fromkeys(
        [*result["source_evidence"], *answer_evidence]
    ))

    existing = {
        item.get("id"): item for item in result["decisions"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    unresolved: list[dict[str, Any]] = []
    for question in questions:
        question_id = question["id"]
        raw = answers.get(question_id)
        if raw is None:
            if question.get("required", True):
                unresolved.append(question)
            continue
        choices, other_text, rationale = _answer_parts(raw)
        option_map = {option["id"]: option["label"] for option in question["options"]}
        if not choices:
            raise ValueError(f"{question_id}:CHOICE_REQUIRED")
        if not question["allow_multiple"] and len(choices) != 1:
            raise ValueError(f"{question_id}:SINGLE_CHOICE_ONLY")
        unknown = [choice for choice in choices if choice not in option_map]
        if unknown:
            raise ValueError(f"{question_id}:UNKNOWN_OPTION:{','.join(unknown)}")
        if other_text and not question["other_allowed"]:
            raise ValueError(f"{question_id}:OTHER_NOT_ALLOWED")
        other_option_ids = {
            option["id"] for option in question["options"]
            if isinstance(option, dict)
            and (
                str(option.get("id", "")).lower() in {"other", "x"}
                or "其他" in str(option.get("label", ""))
            )
        }
        if question["other_allowed"] and any(
            choice in other_option_ids and not other_text for choice in choices
        ):
            raise ValueError(f"{question_id}:OTHER_TEXT_REQUIRED")
        selected = [f"{choice} {option_map[choice]}" for choice in choices]
        if other_text:
            selected.append(f"其他：{other_text.strip()}")
        decision = {
            "id": question_id,
            "question": question["prompt"],
            "options": [f"{option['id']} {option['label']}" for option in question["options"]],
            "choice": "；".join(selected),
            "rationale": rationale or f"{decided_by} 在 Comate 中选择了上述选项。",
            "evidence": list(dict.fromkeys([*question["evidence"], *answer_evidence])),
        }
        existing[question_id] = decision

    result["decisions"] = list(existing.values())
    result["choice_questions"] = unresolved
    result["unresolved_frontier"] = [
        f"{question['id']}：{question['title']}" for question in unresolved
    ]
    if unresolved:
        result["decision_result"] = "UNRESOLVED"
        result["status"] = "INCOMPLETE"
    else:
        result["decision_result"] = "DECISIONS_RECORDED" if result["decisions"] else "NO_OPEN_DECISIONS"
        result["status"] = "COMPLETE"
    return result


def _answer_parts(raw: Any) -> tuple[list[str], str, str]:
    if isinstance(raw, str):
        return [raw], "", ""
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)], "", ""
    if not isinstance(raw, dict):
        raise ValueError("ANSWER_VALUE_INVALID")
    choices = raw.get("choices", raw.get("choice"))
    if isinstance(choices, str):
        choices = [choices]
    if not isinstance(choices, list) or not all(isinstance(item, str) for item in choices):
        raise ValueError("ANSWER_CHOICES_INVALID")
    other = raw.get("other_text", "")
    rationale = raw.get("rationale", "")
    if not isinstance(other, str) or not isinstance(rationale, str):
        raise ValueError("ANSWER_TEXT_INVALID")
    return choices, other, rationale
