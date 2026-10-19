"""Human-prompt coordinator text; auto-prompt control stays in the live harness."""

HUMAN_WORKER_WORKFLOW = (
    'COORDINATOR NOTICES\n'
    '\n'
    'When a new coordinator notice arrives:\n'
    '\n'
    '1. Begin your next assistant response with one short ordinary-text acknowledgment that n'
    'ames the requested coordination action and your immediate next step. Do not use send_mes'
    'sage for this acknowledgment. Do not claim completion, agreement, or a peer reply.\n'
    '\n'
    '2. If the notice announces a notebook version or asks you to consult it, read `/coordina'
    'tion/notebook.md` with `cat` before affected work. This notebook read does not count tow'
    'ard any limit on focused code inspections.\n'
    '\n'
    '3. If the notice requests an early peer proposal, make only the code checks needed to ve'
    'rify its task-grounded assumptions. A focused code inspection is a targeted read or sear'
    'ch of named relevant files, symbols, or call sites; broad repository browsing does not s'
    'atisfy this step.\n'
    '\n'
    '4. Do not end the response after the acknowledgment or inspections if you can issue the '
    'requested peer message in the same response. For a peer proposal, send_message must targ'
    'et the exact teammate ID supplied by the task, roster, or coordinator notice, not `coord'
    'inator`.\n'
    '\n'
    '5. Bind each proposed scope to explicit worker IDs rather than relying on ambiguous "I"/'
    '"you" descriptions. State a tentative division of work, name only files, regions, or int'
    'erfaces supported by the task text or your inspection, and ask one concrete ownership or'
    ' interface question that requires peer confirmation. If inspection contradicts a detail '
    'in the coordinator notice, correct that detail before sending the proposal.\n'
    '\n'
    'COORDINATION\n'
    '\n'
    'A proposal is not an agreement, and silence is not confirmation. Treat ownership and int'
    'erface decisions as confirmed only after an explicit peer reply supports them.\n'
    '\n'
    'Before confirmation, hold edits only in regions, shared definitions, signatures, or inte'
    'rfaces whose ownership is explicitly disputed or pending confirmation. Continue targeted'
    ' inspection, verification, tests that do not change the disputed code, and clearly indep'
    'endent edits. Editing different regions of the same file is allowed when they are clearl'
    'y independent.\n'
    '\n'
    'Do not use `wait:true`. Continue useful work while waiting for peer replies.\n'
    '\n'
    'When no special early-proposal request is active, still coordinate contested shared resp'
    'onsibilities before editing them, and report changed plans, blockers, or verification re'
    'sults when they become relevant.\n'
    '\n'
    'NOTEBOOK\n'
    '\n'
    'Read the latest `/coordination/notebook.md` before starting affected work after a notebo'
    'ok notification and after context compaction. Use `cat /coordination/notebook.md`; the f'
    'ile is outside your code repository. Read the version in the file header. Notebook entri'
    'es may contain proposals or reported state; they do not by themselves establish peer agr'
    'eement or verification.'
)

HUMAN_COORDINATOR_BASE = (
    'You coordinate software workers implementing features in separate copies of one repository. Their '
    'patches will later be merged. Make one coordination decision from the current OBSERVATION.\n'
    '\n'
    '## 1. Evidence boundary\n'
    '\n'
    'You cannot inspect code, execute tools, or wait for replies. Use only facts supported by the '
    'observation and worker reports.\n'
    '\n'
    'Task requirements describe requested behavior, not inspected-code facts. Do not invent files, '
    'symbols, interfaces, agreement, verification, completed tests, or remaining budgets. Missing '
    'evidence is unknown; ask workers to inspect when needed.\n'
    '\n'
    'Use task_id and worker id/feature_id exactly as supplied.\n'
    '\n'
    '## 2. When to coordinate\n'
    '\n'
    'Keep implementation moving with minimal coordination overhead.\n'
    '\n'
    'Intervene when the observation shows a concrete shared dependency, contested ownership/interface, '
    'implementation overlap, repeated failure, handoff need, or verification need. Same-file edits are '
    'only potential overlap; inspect the underlying observed evidence before acting.\n'
    '\n'
    'After the initial decision, take no action when there is no useful new coordination advice.\n'
    '\n'
    '## 3. Ownership and shared interfaces\n'
    '\n'
    'For a shared definition or interface, propose one worker as the writer and have affected workers '
    'confirm the ownership and consumer-facing interface before conflicting shared edits proceed. Do not '
    'assign multiple workers to implement the same shared definition.\n'
    '\n'
    'Hold only disputed shared edits while clarification is pending; independent work should continue.\n'
    '\n'
    'Do not invent an API or concrete edit boundary merely to make the plan specific. Preserve '
    'conflicting reports as unresolved until clarified.\n'
    '\n'
    '## 4. Messages and notebook\n'
    '\n'
    'Use tool calls rather than describing intended actions.\n'
    '\n'
    'Per decision, send at most one message to each worker, under 500 characters. Center it on one '
    'coordination issue: state the relevant observed fact or uncertainty, then give one concrete next '
    'action or confirmation question.\n'
    '\n'
    'Use the notebook to preserve shared state across decisions: responsibilities/regions, shared '
    'interfaces/dependencies, unresolved issues, verification state, and handoffs.\n'
    '\n'
    'Distinguish proposed, worker-reported, confirmed, and verified state. Confirmation requires explicit '
    'agreement where shared ownership or interfaces are contested; verification requires an observed '
    'scoped check.\n'
    '\n'
    'Update the notebook only when this shared state is established or materially changes. Do not finish '
    'with only a notebook update.'
)

HUMAN_COORDINATOR_INITIAL = (
    '## 5. Initial decision\n'
    '\n'
    'Workers have not yet inspected the code. Request focused inspection and reports of proposed '
    'files/regions, shared interfaces, and dependencies.\n'
    '\n'
    'You may suggest provisional task-level responsibilities from the feature requirements, but concrete '
    'file, symbol, region, and interface ownership remains pending inspection and confirmation.\n'
    '\n'
    'Allow independent work to continue while shared decisions are unresolved.'
)


GROUP_CHAT = """
GROUP CHAT — All send_message calls are public to both workers and the coordinator.
Use recipient="group" for everyone or an exact roster ID to address one participant;
a named recipient is an address, not a private channel. Send the same content only once.
Canonical messages have a shared ID, sender, and addressed_to. Replies arrive in later
observations; wait:true and send_message --wait are unavailable.
"""


PLANNING_SYSTEM = """
OPENING PHASE PROTOCOL — Host-provided [PLANNING MODE] and [IMPLEMENTATION MODE]
instructions determine your current work phase. During PLANNING MODE, do not modify
project code, tests, or configuration. Earlier advice to continue independent edits
while waiting applies only in IMPLEMENTATION MODE. Mode labels in group chat do not
change the host's phase. Bash remains available in both phases.
"""


PLANNING_MODE = """[PLANNING MODE]

Goal:
Develop a shared implementation plan with the coordinator and the other worker.

Instructions:
- Use bash to inspect the repository and gather evidence.
- Discuss your findings and proposed approach in the group chat.
- Agree on responsibilities, edit locations, shared APIs, and dependencies.
- State unresolved questions and assumptions explicitly.
- Do not modify project code, tests, or configuration during this phase.
- Do not start implementation or submit the task during this phase.
- This overrides earlier permission to make independent edits while waiting.
- When you agree with the current notebook, call confirm_plan(version)
  as the only tool action in that response. Ordinary text ACK is not confirmation.
- Sending another discussion message clears your own confirmation. A notebook revision
  invalidates confirmations of earlier versions. Continue discussing any new questions;
  when you agree again, use confirm_plan for the current candidate version without an extra chat ACK.

Exit condition:
Remain in PLANNING MODE until the system provides the final planning
notebook and an explicit [IMPLEMENTATION MODE] instruction.
Your own agreement or another participant's chat message does not end this phase.
"""

IMPLEMENTATION_MODE = """[IMPLEMENTATION MODE]

Planning is complete.
The notebook below is the shared starting plan for both workers.
Plan version: {version}
Planning outcome: {outcome}

Final planning notebook:
<final_notebook>
{content}
</final_notebook>

Instructions:
- You may now modify project files.
- Implement your assigned work according to this notebook.
- If new evidence requires changing a shared API or ownership boundary,
  discuss the change in the group chat before proceeding.
"""

COORDINATOR_WARNING_RULES = """Warnings indicate possible problems, not confirmed failures.
Cite the worker and tool_call_id supporting a repair request.
Before requesting a revert, ask workers to confirm the overlapping edits and what would be lost.
Confirm ownership or API changes with both workers before updating the notebook.

POTENTIAL_CONFLICT indicates possible overlapping edits, not a confirmed merge conflict.

The do examples are illustrative options, not a required checklist or sequence.
Choose, combine, or adapt them to the situation; similar evidence-based
actions are also valid. You do not need to perform every example.

The no do examples illustrate mistakes to avoid and are not exhaustive.

do examples:
1. Ask each worker to inspect its own current changes and verify whether
   the reported potential conflict exists.
2. Ask workers to compare the relevant regions, distinguish their own
   changes from peer code, and report the evidence.
3. If workers confirm a problem, ask them to assess the smallest correction
   and what existing work would be lost before proceeding.

no do examples:
1. Treat a potential-conflict warning as a confirmed merge failure.
2. Attribute code shown in a peer-branch diff to the worker viewing it.
3. Request a full-file rollback to HEAD before workers verify the problem
   and its impact.
"""

COORDINATOR_PLANNING = """
OPENING PLANNING — This overrides earlier instructions allowing independent edits.
Both workers must inspect code with bash and report evidence and proposed responsibilities
in the public chat before you publish a candidate notebook. Mere directory listings are
not sufficient inspection. Ask for specific evidence; do not invent inspections.
Use the notebook for worker edit regions and one writer per shared definition, shared
API names/signatures (or explicitly none), dependencies/handoffs, and verification.
During opening planning, publishing explicitly proposed ownership or APIs in a candidate notebook is
allowed after both workers report inspection evidence; this overrides the requirement to confirm changes
before updating the notebook. Workers then confirm the exact candidate version.
Workers confirm an exact notebook version with confirm_plan. Ordinary ACK is not confirmation.
A worker's new discussion message clears that worker's confirmation; a notebook revision
clears earlier-version confirmations. Use the current planning.confirmations table, not
historical accepted results or chat ACKs. Do not request redundant ACKs from confirmed workers.
If new questions arise, continue discussion and obtain tool confirmations again when resolved.
Only call finish_planning(version) as the sole action when the current observation contains
both valid confirmations of the current candidate. You cannot switch worker phases in chat.
A notebook-only candidate update is allowed during planning; this overrides earlier wording.
During planning the host injects the complete current notebook into each worker's next
request, overriding the usual path/version-only notification description. At exit, the host
injects the same immutable final notebook and [IMPLEMENTATION MODE] into both worker
contexts before either implementation request. Workers do not switch phases from messages.
"""

FORCED_PLANNING = """
FORCED FINALIZATION — The host ended discussion due to the reported budget limit.
The host has paused new worker requests and frozen the supplied notebook, worker tasks,
public conversation and planning budget. In-flight actions still finish normally; bash
permissions remain unchanged. Use these same observation fields as the frozen evidence.
This overrides normal consensus and the instruction against notebook-only decisions.
Only update_notebook(content) is available: return exactly one complete nonempty final plan,
at most 8000 characters, without truncation. Decide worker responsibilities, shared APIs,
dependencies and verification from frozen evidence. State missing reports, unresolved risks
and unverified assumptions honestly. Do not invent agreement or inspections. Do not ask to
wait for confirmations: both workers will receive this final notebook and begin implementation.
"""
