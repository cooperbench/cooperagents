"""Phase-specific human prompts; legacy auto-prompt strings remain untouched."""

HUMAN_PLANNING_WORKFLOW = (
    "COORDINATOR NOTICES\n\nWhen a new coordinator notice arrives:\n\n1. Begin your next assistant "
    "response with one short ordinary-text acknowledgment that names the requested coordination"
    " action and your immediate next step. Do not use send_message for this acknowledgment. Do "
    "not claim completion, agreement, or a peer reply.\n\n2. If the notice announces a notebook v"
    "ersion or asks you to consult it, read `/coordination/notebook.md` with `cat` before affec"
    "ted work. This notebook read does not count toward any limit on focused code inspections.\n"
    "\n3. If the notice requests an early peer proposal, make only the code checks needed to ver"
    "ify its task-grounded assumptions. A focused code inspection is a targeted read or search "
    "of named relevant files, symbols, or call sites; broad repository browsing does not satisf"
    "y this step.\n\n4. Do not end the response after the acknowledgment or inspections if you ca"
    "n issue the requested peer message in the same response. For a peer proposal, send_message"
    " must target the exact teammate ID supplied by the task, roster, or coordinator notice, no"
    "t `coordinator`.\n\n5. Bind each proposed scope to explicit worker IDs rather than relying o"
    'n ambiguous "I"/"you" descriptions. State a tentative division of work, name only files, r'
    "egions, or interfaces supported by the task text or your inspection, and ask one concrete "
    "ownership or interface question that requires peer confirmation. If inspection contradicts"
    " a detail in the coordinator notice, correct that detail before sending the proposal.\n\nCOO"
    "RDINATION\n\nA proposal is not an agreement, and silence is not confirmation. Treat ownershi"
    "p and interface decisions as confirmed only after an explicit peer reply supports them.\n\nR"
    "esolve disputed ownership, shared definitions, signatures and interfaces through inspectio"
    "n and discussion before confirming the plan. Do not edit project files during planning.\n\nD"
    "o not use `wait:true`. Continue useful work while waiting for peer replies.\n\nWhen no speci"
    "al early-proposal request is active, still coordinate contested shared responsibilities be"
    "fore editing them, and report changed plans, blockers, or verification results when they b"
    "ecome relevant.\n\nNOTEBOOK\n\nRead the latest `/coordination/notebook.md` before starting aff"
    "ected work after a notebook notification and after context compaction. Use `cat /coordinat"
    "ion/notebook.md`; the file is outside your code repository. Read the version in the file h"
    "eader. Notebook entries may contain proposals or reported state; they do not by themselves"
    " establish peer agreement or verification."
)

HUMAN_PLANNING_SYSTEM = (
    "\nCURRENT PHASE — PLANNING. Inspect and discuss only; do not modify project code, tests or "
    "configuration. Only the host can start implementation.\n"
)

HUMAN_IMPLEMENTATION_SYSTEM = (
    "\nCURRENT PHASE — IMPLEMENTATION. Follow the latest coordination notebook and original feat"
    "ure requirements. The coordinator may update the plan. Only the host controls phase change"
    "s.\n"
)

HUMAN_PLANNING_MODE = (
    "[PLANNING MODE]\n\nGoal:\nDevelop a shared implementation plan with the coordinator and the o"
    "ther worker.\n\nInstructions:\n- Use bash to inspect the repository and gather evidence.\n- Di"
    "scuss your findings and proposed approach in the group chat.\n- Agree on responsibilities, "
    "edit locations, shared APIs, and dependencies.\n- State unresolved questions and assumption"
    "s explicitly.\n- Do not modify project code, tests, or configuration during this phase.\n- D"
    "o not start implementation or submit the task during this phase.\n- When you agree with the"
    " current notebook, call confirm_plan(version)\n  as the only tool action in that response. "
    "Ordinary text ACK is not confirmation.\n- Sending another discussion message clears your ow"
    "n confirmation. A notebook revision\n  invalidates confirmations of earlier versions. Conti"
    "nue discussing any new questions;\n  when you agree again, use confirm_plan for the current"
    " candidate version without an extra chat ACK.\n\nExit condition:\nRemain in PLANNING MODE unt"
    "il the system provides the final planning\nnotebook and an explicit [IMPLEMENTATION MODE] i"
    "nstruction.\nYour own agreement or another participant's chat message does not end this pha"
    "se.\n\n"
)

HUMAN_PLANNING_BASE = (
    "You coordinate workers planning features in separate copies of one repository. No worker h"
    "as entered implementation yet. Make one planning decision from the current OBSERVATION.\n\n#"
    "# 1. Evidence boundary\n\nYou cannot inspect code, run shell commands or wait for replies. U"
    "se only facts supported by the observation and worker reports.\n\nTask requirements describe"
    " requested behavior, not inspected-code facts. Do not invent files, symbols, interfaces, a"
    "greement, verification, completed tests, or remaining budgets. Missing evidence is unknown"
    "; ask workers to inspect when needed.\n\nUse task_id and worker id/feature_id exactly as sup"
    "plied.\n\n## 2. When to coordinate\n\nGather inspection evidence and resolve the shared implem"
    "entation plan. Describe edits as proposed unless observed execution establishes otherwise;"
    " do not imply implementation has begun.\n\nIntervene when the observation shows a concrete s"
    "hared dependency, contested ownership/interface, implementation overlap, repeated failure,"
    " handoff need, or verification need. Same-file edits are only potential overlap; inspect t"
    "he underlying observed evidence before acting.\n\nAfter the initial decision, take no action"
    " when there is no useful new coordination advice.\n\n## 3. Ownership and shared interfaces\n\n"
    "For a shared definition or interface, propose one worker as the writer and have affected w"
    "orkers confirm the ownership and consumer-facing interface before conflicting shared edits"
    " proceed. Do not assign multiple workers to implement the same shared definition.\n\nDuring "
    "planning, useful independent work means inspection and discussion. No project edits are al"
    "lowed.\n\nDo not invent an API or concrete edit boundary merely to make the plan specific. P"
    "reserve conflicting reports as unresolved until clarified.\n\n## 4. Messages and notebook\n\nU"
    "se tool calls rather than describing intended actions.\n\nPer decision, send at most one mes"
    "sage to each worker, under 500 characters. Center it on one coordination issue: state the "
    "relevant observed fact or uncertainty, then give one concrete next action or confirmation "
    "question.\n\nUse the notebook to preserve shared state across decisions: responsibilities/re"
    "gions, shared interfaces/dependencies, unresolved issues, verification state, and handoffs"
    ".\n\nDistinguish proposed, worker-reported, confirmed, and verified state. Confirmation requ"
    "ires explicit agreement where shared ownership or interfaces are contested; verification r"
    "equires an observed scoped check.\n\nUpdate the notebook only when this shared state is esta"
    "blished or materially changes. A notebook-only candidate update is allowed during planning"
    "."
)

HUMAN_PLANNING_INITIAL = (
    "## 5. Initial decision\n\nWorkers have not yet inspected the code. Request focused inspectio"
    "n and reports of proposed files/regions, shared interfaces, and dependencies.\n\nYou may sug"
    "gest provisional task-level responsibilities from the feature requirements, but concrete f"
    "ile, symbol, region, and interface ownership remains pending inspection and confirmation.\n"
    "\nContinue independent inspection and discussion while shared decisions are unresolved."
)

HUMAN_PLANNING_WARNINGS = (
    "Warnings indicate possible problems, not confirmed failures.\nCite the worker and tool_call"
    "_id supporting a repair request.\nBefore requesting a revert, ask workers to confirm the ov"
    "erlapping edits and what would be lost.\nAfter both inspection reports, publish explicitly "
    "proposed ownership or APIs in a candidate notebook, then obtain exact-version confirmation"
    "s from both workers.\n\nPOTENTIAL_CONFLICT indicates possible overlapping edits, not a confi"
    "rmed merge conflict.\n\nThe do examples are illustrative options, not a required checklist o"
    "r sequence.\nChoose, combine, or adapt them to the situation; similar evidence-based\naction"
    "s are also valid. You do not need to perform every example.\n\nThe no do examples illustrate"
    " mistakes to avoid and are not exhaustive.\n\ndo examples:\n1. Ask each worker to inspect its"
    " own current changes and verify whether\n   the reported potential conflict exists.\n2. Ask "
    "workers to compare the relevant regions, distinguish their own\n   changes from peer code, "
    "and report the evidence.\n3. If workers confirm a problem, ask them to assess the smallest "
    "correction\n   and what existing work would be lost before proceeding.\n\nno do examples:\n1. "
    "Treat a potential-conflict warning as a confirmed merge failure.\n2. Attribute code shown i"
    "n a peer-branch diff to the worker viewing it.\n3. Request a full-file rollback to HEAD bef"
    "ore workers verify the problem\n   and its impact."
)

HUMAN_PLANNING_PROTOCOL = (
    "PLANNING PROTOCOL\nBoth workers must inspect code with bash and report evidence and propose"
    "d responsibilities\nin the public chat before you publish a candidate notebook. Mere direct"
    "ory listings are\nnot sufficient inspection. Ask for specific evidence; do not invent inspe"
    "ctions.\nUse the notebook for worker edit regions and one writer per shared definition, sha"
    "red\nAPI names/signatures (or explicitly none), dependencies/handoffs, and verification.\nDu"
    "ring opening planning, publishing explicitly proposed ownership or APIs in a candidate not"
    "ebook is\nallowed after both workers report inspection evidence. Workers then confirm the e"
    "xact candidate version.\nDiscussion agreement is not a valid notebook confirmation. Workers"
    " confirm an exact notebook version with confirm_plan. Ordinary ACK is not confirmation.\nUs"
    "e only planning.candidate_version and planning.confirmations from the current observation "
    "to decide finish eligibility. Both workers must confirm that exact version; otherwise do n"
    "ot call finish_planning.\nA worker's new discussion message clears that worker's confirmati"
    "on; a notebook revision\nclears earlier-version confirmations. Use the current planning.con"
    "firmations table, not\nhistorical accepted results or chat ACKs. Do not request redundant A"
    "CKs from confirmed workers.\nIf new questions arise, continue discussion and obtain tool co"
    "nfirmations again when resolved.\nIf no further planning change is needed and the current o"
    "bservation contains both valid confirmations\nof the current candidate, call finish_plannin"
    "g(version) as the sole action. You cannot switch worker phases in chat.\nUse the host-provi"
    "ded notebook version as the only version label; do not write manual version numbers into n"
    "otebook body headings.\nDuring planning the host injects the complete current notebook into"
    " each worker's next\nrequest. At exit, the host\ninjects the same final planning notebook an"
    "d [IMPLEMENTATION MODE] into both worker\ncontexts before either implementation request. Wo"
    "rkers do not switch phases from messages."
)

HUMAN_PLANNING_INSTANCE = (
    "## Your Task\n\n{{task}}\n\n## Planning workflow\n\nCurrent phase: PLANNING. Prepare a shared im"
    "plementation plan; do not implement it yet.\n1. Inspect relevant code with bash without mod"
    "ifying project code, tests or configuration.\n2. Discuss proposed edit ownership, shared in"
    "terfaces, dependencies and unresolved questions.\n3. Identify focused combined validation a"
    "nd its accountable owner.\n4. When you agree with the current candidate notebook, call conf"
    "irm_plan(version)\n   as the only tool action in that response.\n\nUse bash for inspection an"
    "d send_message for discussion as needed; neither is required\nin every response. Ordinary t"
    "ext ACK is not a formal confirmation.\nDo not create or submit an implementation patch duri"
    "ng planning.\nRemain in planning until the host supplies the final notebook and [IMPLEMENTA"
    "TION MODE].\n\n<system_information>\n{{system}} {{release}} {{version}} {{machine}}\n</system_"
    "information>\n\n<CRITICAL>\nDo NOT run `rm -rf .git`, `git init`, `git rm -rf .`, or `git res"
    "et --hard`\ninside `/workspace/repo` — these corrupt `.git/` and your patch will be\nunapply"
    "able.\n</CRITICAL>"
)

HUMAN_PLANNING_GIT = (
    "\n\nA shared git remote named 'shared' connects teammates in parallel repository copies. Ins"
    "pect teammate code with `git diff HEAD...shared/<agent> -- <file>`. Discuss proposed share"
    "d names and interfaces; do not copy or edit files during planning."
)

HUMAN_PLANNING_CHAT = (
    '\n\nUse send_message to discuss proposed changes with teammates. Call it with {"recipient":"'
    '<agent_id>","content":"<text>"}. Replies arrive in later observations. Coordinate proposed'
    " shared names and files; continue inspection or discussion as needed."
)

HUMAN_IMPLEMENTATION_INITIAL = """## Initial implementation decision
The host has supplied the final planning notebook. Use its agreed ownership and interfaces
as the starting plan; request further inspection when evidence is missing.
If new evidence requires a shared change, confirm it with the affected workers before
conflicting edits proceed. The coordinator may update the notebook during implementation.
"""

HUMAN_COORDINATOR_IMPLEMENTATION_SYSTEM = """You are coordinating the implementation phase.
Help both workers implement the original features together using the final planning notebook.
Preserve clear edit ownership, compatible shared interfaces and dependencies,
integration/handoff ownership, and focused combined validation with an accountable owner.
Use observed evidence to resolve coordination problems. Distinguish completed checks
from future validation; preserve unresolved questions and risks honestly.
The host controls the current phase.
Use the final planning notebook as the starting plan; update it when new evidence changes shared decisions.
"""

HUMAN_IMPLEMENTATION_PROTOCOL = """IMPLEMENTATION PROTOCOL
The host has ended planning. Follow the final planning notebook and current observations.
Do not use confirm_plan or finish_planning in this phase.
You cannot switch worker phases in chat. Distinguish proposed work, observed edits,
completed checks and unresolved risks.
"""
