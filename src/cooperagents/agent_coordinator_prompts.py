"""Versioned prompt texts shared by the gate and coordinator."""
from hashlib import sha256
import json

GATE_INSTRUCTIONS = "Assess whether a coordinator intervention would help the target make progress. Use only the supplied execution state."
DECISION_QUESTION_INSTRUCTIONS = "Choose whether the coordinator should intervene now."
WAKE_CRITERION = "Wake when timely coordination is likely to help resolve a problem or coordinate workers."
SKIP_CRITERION = "Skip when the worker can reasonably continue without an intervention."
OPENING_PLAN_PROMPT = "Inspect the base checkout and propose ownership, interfaces and risks for the workers. Submit a plan grounded in evidence; ask workers to verify and correct it."
WAKE_PROMPT = "Review the team state and inspect committed snapshots as needed. Send concise, actionable coordination messages when useful, then end the wake."
SPEC_CHECK_PROMPT = "Compare the frozen diff with the assigned feature and any teammate features. Reject only concrete missing requirements, API mismatches, unrelated edits or lost teammate changes; cite the feature text."

def prompt_id(name: str, text: str, version: int = 1) -> str:
    return f"{name}-v{version}:{sha256(text.encode('utf-8')).hexdigest()[:12]}"

def gate_prompt_id(instructions: str = GATE_INSTRUCTIONS, question: str = DECISION_QUESTION_INSTRUCTIONS, wake: str = WAKE_CRITERION, skip: str = SKIP_CRITERION) -> str:
    return prompt_id("gate", json.dumps([instructions, question, wake, skip], ensure_ascii=False, separators=(",", ":")))

GATE_PROMPT_ID = gate_prompt_id()
OPENING_PLAN_PROMPT_ID = prompt_id("opening-plan", OPENING_PLAN_PROMPT)
WAKE_PROMPT_ID = prompt_id("wake", WAKE_PROMPT)
SPEC_CHECK_PROMPT_ID = prompt_id("spec-check", SPEC_CHECK_PROMPT)
