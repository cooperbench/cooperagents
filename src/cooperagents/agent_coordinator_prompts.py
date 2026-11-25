"""Versioned prompt texts shared by the gate and coordinator."""

import json
from hashlib import sha256

GATE_INSTRUCTIONS = "Assess whether a coordinator intervention would help the target make progress. Use only the supplied execution state."
DECISION_QUESTION_INSTRUCTIONS = "Choose whether the coordinator should intervene now."
WAKE_CRITERION = (
    "Wake when timely coordination can help: e.g. a worker sent a peer_message to coordinator, "
    "hit NO_PROGRESS_FAILING, HUNK_CHURN, PEER_OVERLAP, or SUBMIT_REJECTED, "
    "or recent steps show repeated errors or stagnation."
)
SKIP_CRITERION = "Skip when the worker can reasonably continue without an intervention."
TOOL_RULES = (
    'Tool rules: paths are relative to the repository root; use "." for the root. '
    "search_code matches one fixed string (git grep -F); regular-expression syntax such as |, .* or \\b "
    "is matched literally, so search one identifier or phrase per call. "
    "rev accepts only the base_sha or a worker snapshot sha listed in git_evidence; read tools default to base_sha."
)
OPENING_PLAN_PROMPT = (
    "Inspect the base checkout and propose ownership, interfaces and risks for the workers. "
    "Submit a plan grounded in evidence; ask workers to verify and correct it.\n"
    + TOOL_RULES + "\n"
    "The user message gives tool_call_budget and reserved_tool_calls. Calls use one unit each, "
    "except reads beyond the read budget in an earlier batch. The tool list stays the same. "
    "When the remaining budget is at or below reserved_tool_calls, only submit_plan is accepted. "
    "Each request after the first ends with `[remaining_tool_calls: N]`. "
    "When the evidence is incomplete, submit the plan with the evidence collected so far and record the gaps in risks."
)
WAKE_PROMPT = (
    "Review the team state and inspect committed snapshots as needed. "
    "Send concise, actionable coordination messages when useful, then end the wake.\n"
    + TOOL_RULES + "\n"
    "The second message gives tool_call_budget and reserved_tool_calls. At most 2 send_message calls per wake. "
    "The tool list stays the same. When the remaining budget is at or below reserved_tool_calls, "
    "only send_message and end_wake are accepted. "
    "Each request after the first ends with `[remaining_tool_calls: N]`."
)
OPENING_BUDGET_STATUS = (
    "[remaining_tool_calls: {remaining}] {reads} read calls remain. "
    "When {reserve} or fewer calls remain, only submit_plan is accepted."
)
OPENING_BUDGET_WARNING = "[remaining_tool_calls: {remaining}] One read call remains. After that, only submit_plan is accepted."
OPENING_FINAL_TURN = (
    "[remaining_tool_calls: {remaining}] Final turn: only submit_plan is accepted. "
    "Call submit_plan now with the evidence collected so far."
)
WAKE_BUDGET_STATUS = (
    "[remaining_tool_calls: {remaining}] {reads} read calls remain. "
    "When {reserve} or fewer calls remain, only send_message and end_wake are accepted."
)
WAKE_BUDGET_WARNING = "[remaining_tool_calls: {remaining}] One read call remains. After that, only send_message and end_wake are accepted."
WAKE_FINAL_TURN = (
    "[remaining_tool_calls: {remaining}] Final turn: only send_message and end_wake are accepted. "
    "Send any needed coordination message with send_message, then call end_wake if a call remains."
)
SPEC_CHECK_PROMPT = (
    "Compare the frozen diff with the assigned feature and any teammate features. Reject only concrete missing requirements, "
    "API mismatches, unrelated edits or lost teammate changes; cite the feature text."
)


def prompt_id(name: str, text: str, version: int = 1) -> str:
    return f"{name}-v{version}:{sha256(text.encode('utf-8')).hexdigest()[:12]}"


def gate_prompt_id(
    instructions: str = GATE_INSTRUCTIONS,
    question: str = DECISION_QUESTION_INSTRUCTIONS,
    wake: str = WAKE_CRITERION,
    skip: str = SKIP_CRITERION,
) -> str:
    return prompt_id("gate", json.dumps([instructions, question, wake, skip], ensure_ascii=False, separators=(",", ":")))


GATE_PROMPT_ID = gate_prompt_id()
OPENING_PLAN_PROMPT_ID = prompt_id("opening-plan", OPENING_PLAN_PROMPT)
WAKE_PROMPT_ID = prompt_id("wake", WAKE_PROMPT)
SPEC_CHECK_PROMPT_ID = prompt_id("spec-check", SPEC_CHECK_PROMPT)
BUDGET_NOTES_PROMPT_ID = prompt_id("budget-notes", json.dumps(
    [OPENING_BUDGET_STATUS, OPENING_BUDGET_WARNING, OPENING_FINAL_TURN,
     WAKE_BUDGET_STATUS, WAKE_BUDGET_WARNING, WAKE_FINAL_TURN],
    ensure_ascii=False, separators=(",", ":"),
))
