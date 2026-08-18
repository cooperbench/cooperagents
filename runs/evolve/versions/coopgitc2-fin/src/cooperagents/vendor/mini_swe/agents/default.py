"""Basic agent class. See https://mini-swe-agent.com/latest/advanced/control_flow/ for visual explanation
or https://minimal-agent.com for a tutorial on the basic building principles.
"""

import json
import logging
import re
import traceback
import typing
from pathlib import Path

from jinja2 import StrictUndefined, Template
from pydantic import BaseModel

from cooperagents.vendor.mini_swe import Environment, Model, __version__
from cooperagents.vendor.mini_swe.exceptions import InterruptAgentFlow, LimitsExceeded
from cooperagents.vendor.mini_swe.utils.serialize import recursive_merge

# Messaging is wired by CooperAgents' bus, not mini-swe's own connector.
MessagingConnector = object  # type: ignore[assignment,misc]


class AgentConfig(BaseModel):
    """Check the config files in config/ for example settings."""

    system_template: str
    """Template for the system message (the first message)."""
    instance_template: str
    """Template for the first user message specifying the task (the second message overall)."""
    step_limit: int = 0
    """Maximum number of steps the agent can take."""
    cost_limit: float = 3.0
    """Stop agent after exceeding (!) this cost."""
    output_path: Path | None = None
    """Save the trajectory to this path."""
    compaction_enabled: bool = True
    """Enable context compaction (summarization of old messages)."""
    compaction_token_trigger: int = 20000
    """Compact when prompt token count exceeds this threshold."""
    compaction_keep_recent_turns: int = 2
    """Number of recent assistant turns to keep verbatim after compaction."""
    wall_deadline: float | None = None
    """Unix timestamp after which the run must stop. Checked at query()
    entry as well as at action execution: an agent whose model calls hang or
    retry for long stretches never reaches execute(), so an execute-only cap
    cannot bound it (observed: 8h runs in connection-error retry loops
    against a saturated endpoint)."""
    compaction_summary_prompt: str = (
        "You are summarizing the transcript below so the agent can continue in a "
        "fresh context without re-running commands. You are an outside observer "
        "of the conversation, not its next participant — output the summary as a "
        "single text response.\n"
        "\n"
        "The system prompt and the original task are preserved separately — DO NOT "
        "restate them. Focus on what would otherwise be LOST when the prior turns "
        "are discarded.\n"
        "\n"
        "Cite only what actually appears in the transcript. Do NOT invent line "
        "counts, file sizes, line numbers, or file contents you did not see. "
        "If you don't know a value, omit the field rather than guessing.\n"
        "\n"
        "Output ONLY the summary, using these exact section headings. Quote "
        "verbatim where stated; paraphrasing forces the agent to re-run commands.\n"
        "\n"
        "## FILE MAP\n"
        "One line per file the agent has touched (read, edited, or referenced):\n"
        "    `<path>`: <one-phrase description of what it contains>. Read so far: "
        '<line ranges or "all" or "first N lines" — only if known from `wc`, '
        "`head`, `tail`, or `sed -n` output above>. Modified: <yes/no>.\n"
        "Include `<line_count> lines` ONLY if a `wc -l` was actually run on it.\n"
        "\n"
        "## RELEVANT CODE READ\n"
        "For each file the agent read, quote the lines that matter for the task, "
        "verbatim, with citations:\n"
        "\n"
        "    `<path>` lines `<a>-<b>`: <one-line note on why this region matters>\n"
        "    ```\n"
        "    <verbatim snippet — exact indentation>\n"
        "    ```\n"
        "\n"
        "Include the parts likely needed again (definitions, key call sites, "
        "similar patterns nearby, struct shapes). Skip true boilerplate (license "
        "headers, unused imports). If a file was read and nothing in it matters, "
        "write one line: `<path>: read, nothing relevant`.\n"
        "\n"
        "## KEY SYMBOLS / IDENTIFIERS\n"
        "Flat list of important names the agent has discovered, with locations:\n"
        "`<name>` -> `<path>:<line>` — <one-phrase role>.\n"
        "Only include locations actually seen in the transcript.\n"
        "\n"
        "## SEARCH RESULTS WORTH KEEPING\n"
        "For each grep/find/ls/git-log: command + only the matches that matter:\n"
        "    `<cmd>` -> `<file>:<line>: <verbatim match>`.\n"
        "Also note negative results: `<cmd>` -> no matches.\n"
        "\n"
        "## EDITS ALREADY APPLIED\n"
        "For each edit (sed/echo>/cat-heredoc/python-write):\n"
        "    `<path>` <one-line description>:\n"
        "    ```diff\n"
        "    - <before>\n"
        "    + <after>\n"
        "    ```\n"
        "\n"
        "## BUILD / TEST OUTPUT\n"
        "For each test/build run: command, exit code, verbatim error lines "
        "(with `<file>:<line>:` refs). Skip pass noise.\n"
        "\n"
        "## COLLEAGUE MESSAGES\n"
        "Every send_message sent and `[Message from …]` received, verbatim, "
        "chronological.\n"
        "\n"
        "## OPEN QUESTIONS / UNREAD REGIONS\n"
        "What still needs investigation. Be specific — list file paths + line "
        "ranges + what to look for, so the next read is targeted not exploratory.\n"
        "\n"
        "## CURRENT PLAN\n"
        "The most recent stated plan and the immediate next intended step.\n"
        "\n"
        "Sections with no content: write `(none)`. Begin with `## FILE MAP` — "
        "no preamble."
    )
    """Prompt appended to conversation history when requesting a summary."""


class DefaultAgent:
    # Optional team-mode hooks, assigned by the cooperagents adapter after
    # construction; absent in solo runs (read via ``getattr`` with a fallback),
    # so these are annotations only — no class-level value is set.
    team_poller: "typing.Any"
    tool_handlers: "dict[str, typing.Callable[[dict], str]]"

    def __init__(
        self,
        model: Model,
        env: Environment,
        *,
        comm: MessagingConnector | None = None,
        agent_id: str = "agent",
        config_class: type = AgentConfig,
        **kwargs,
    ):
        """See the `AgentConfig` class for permitted keyword arguments."""
        self.config = config_class(**kwargs)
        self.messages: list[dict] = []
        self.model = model
        self.env = env
        self.comm = comm
        self.agent_id = agent_id
        self.extra_template_vars = {}
        self.logger = logging.getLogger("agent")
        self.cost = 0.0
        self.n_calls = 0
        self.sent_messages: list[dict] = []
        # Compaction state
        self._last_prompt_tokens: int = 0
        self._compaction_count: int = 0
        self._segments: list[dict] = []
        self._current_segment_messages: list[dict] = []

    def log(self, msg: str):
        """Log message with agent prefix."""
        self.logger.debug(f"[{self.agent_id}] {msg}")

    def get_template_vars(self, **kwargs) -> dict:
        return recursive_merge(
            self.config.model_dump(),
            self.env.get_template_vars(),
            self.model.get_template_vars(),
            {"n_model_calls": self.n_calls, "model_cost": self.cost},
            self.extra_template_vars,
            kwargs,
        )

    def _render_template(self, template: str) -> str:
        return Template(template, undefined=StrictUndefined).render(**self.get_template_vars())

    def add_messages(self, *messages: dict) -> list[dict]:
        self.logger.debug(messages)  # set log level to debug to see
        self.messages.extend(messages)
        return list(messages)

    def handle_uncaught_exception(self, e: Exception) -> list[dict]:
        return self.add_messages(
            self.model.format_message(
                role="exit",
                content=str(e),
                extra={
                    "exit_status": type(e).__name__,
                    "submission": "",
                    "exception_str": str(e),
                    "traceback": traceback.format_exc(),
                },
            )
        )

    def run(self, task: str = "", **kwargs) -> dict:
        """Run step() until agent is finished. Returns dictionary with exit_status, submission keys."""
        self.extra_template_vars |= {"task": task, **kwargs}
        self.messages = []
        self.add_messages(
            self.model.format_message(role="system", content=self._render_template(self.config.system_template)),
            self.model.format_message(role="user", content=self._render_template(self.config.instance_template)),
        )
        while True:
            try:
                self.step()
            except InterruptAgentFlow as e:
                self.add_messages(*e.messages)
            except Exception as e:
                self.handle_uncaught_exception(e)
                raise
            finally:
                self.save(self.config.output_path)
            if self.messages[-1].get("role") == "exit":
                break
        return self.messages[-1].get("extra", {})

    def step(self) -> list[dict]:
        """Query the LM, execute actions. Polls for inter-agent messages
        and (in team mode) the shared task list before querying."""
        # Check for inter-agent messages before querying LLM
        if self.comm:
            messages = self.comm.receive()
            for msg in messages:
                ts = msg.get("timestamp", "")[:19].replace("T", " ")
                self.log(f"INBOX: [{msg['from']} @ {ts}] {msg['content']}")
                self.add_messages(
                    self.model.format_message(
                        role="user",
                        content=f"[Message from {msg['from']}]: {msg['content']}",
                    )
                )
        # In team mode, also refresh the shared task list so the LLM
        # sees the live state of who's working on what before its next
        # response.  ``team_poller`` is set by the adapter when team
        # kwargs are present; absent for solo/coop.
        poller = getattr(self, "team_poller", None)
        if poller is not None:
            summary = poller.poll()
            if summary:
                self.add_messages(self.model.format_message(role="user", content=summary))
        return self.execute_actions(self.query())

    def _get_prompt_tokens(self, message: dict) -> int:
        return message.get("extra", {}).get("response", {}).get("usage", {}).get("prompt_tokens", 0)

    def _should_compact(self) -> bool:
        return self.config.compaction_enabled and self._last_prompt_tokens >= self.config.compaction_token_trigger

    @staticmethod
    def _find_turn_boundary(messages: list[dict], n_turns: int) -> int:
        """Return the index where the last n_turns complete assistant turns start."""
        assistant_indices = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
        if not assistant_indices or n_turns <= 0:
            return len(messages)
        start = max(0, len(assistant_indices) - n_turns)
        return assistant_indices[start]

    def _close_current_segment(self, kind: str = "solver") -> None:
        """Append accumulated messages as a named segment and reset the buffer."""
        msgs = self._current_segment_messages or self.messages
        if msgs:
            self._segments.append({"kind": kind, "messages": list(msgs)})
            self._current_segment_messages = []

    def _emergency_truncate(self) -> None:
        """Mechanical history truncation after a context-window overflow.

        No model call (a summarizer request would overflow too): keep
        system + task + the last complete assistant turns, clip any oversized
        message bodies/tool arguments, and insert a re-orientation stub."""
        prefix = self.messages[:2]  # system + task
        conversation = self.messages[2:]
        boundary = self._find_turn_boundary(conversation, self.config.compaction_keep_recent_turns)
        recent = conversation[boundary:]

        def clip(m: dict) -> dict:
            m = dict(m)
            c = m.get("content")
            if isinstance(c, str) and len(c) > 20000:
                m["content"] = c[:8000] + "\n...[clipped after context overflow]...\n" + c[-4000:]
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                a = fn.get("arguments")
                if isinstance(a, str) and len(a) > 20000:
                    # arguments is parsed as JSON server-side: a raw character
                    # clip can split an escape sequence and every later request
                    # then 400s ("Invalid \escape" at the clip boundary —
                    # killed both coopgitc2-zoxide-i6 agents at step ~116).
                    # Truncate INSIDE the parsed values and re-dump so the
                    # string stays valid JSON.
                    import json as _json

                    def _shrink(v):
                        if isinstance(v, str) and len(v) > 12000:
                            return v[:8000] + "...[clipped]..." + v[-4000:]
                        if isinstance(v, dict):
                            return {k: _shrink(x) for k, x in v.items()}
                        if isinstance(v, list):
                            return [_shrink(x) for x in v]
                        return v

                    try:
                        fn["arguments"] = _json.dumps(_shrink(_json.loads(a)))
                    except Exception:
                        # unparseable arguments: dump the head as a JSON string
                        # (always valid) rather than concatenating raw halves.
                        fn["arguments"] = _json.dumps({"clipped": a[:8000]})
            return m

        stub = {
            "role": "user",
            "content": (
                "[Context overflow: earlier steps were dropped from your history. "
                "Your work so far is intact ON DISK — re-orient with `git status`, "
                "`git diff --stat`, and `ls` instead of re-doing it.]"
            ),
        }
        self._close_current_segment("solver")
        self.messages = prefix + [stub] + [clip(m) for m in recent]
        self._compaction_count += 1
        self.log(
            f"Emergency truncation #{self._compaction_count}: kept system+task+{len(recent)} recent messages"
        )

    def _compact_messages(self) -> None:
        """Summarize old messages and replace history, keeping recent turns verbatim."""
        summarize_fn = getattr(self.model, "summarize_context", None)
        if not callable(summarize_fn):
            self.log("Model does not support summarize_context, skipping compaction")
            return

        prefix = self.messages[:2]  # system + task
        conversation = self.messages[2:]
        boundary = self._find_turn_boundary(conversation, self.config.compaction_keep_recent_turns)
        old_turns = conversation[:boundary]
        recent_turns = conversation[boundary:]

        if not old_turns:
            return

        self._close_current_segment("solver")

        summarizer_input = prefix + old_turns
        summary_msg = summarize_fn(
            summarizer_input,
            summary_prompt=self.config.compaction_summary_prompt,
        )
        self._segments.append(
            {
                "kind": "summarizer",
                "messages": [
                    *[{k: v for k, v in m.items() if k != "extra"} for m in summarizer_input],
                    {"role": "user", "content": self.config.compaction_summary_prompt},
                    summary_msg,
                ],
            }
        )

        self.messages = prefix + [summary_msg] + recent_turns
        self._compaction_count += 1
        self.log(
            f"Compaction #{self._compaction_count}: {self._last_prompt_tokens} prompt tokens -> compacted "
            f"({len(old_turns)} messages summarized, {len(recent_turns)} kept)"
        )

    def query(self) -> dict:
        """Query the model and return model messages. Override to add hooks."""
        import time as _time
        if self.config.wall_deadline is not None and _time.time() > self.config.wall_deadline:
            raise LimitsExceeded(
                {"role": "exit", "content": "LimitsExceeded",
                 "extra": {"exit_status": "LimitsExceeded", "submission": ""}}
            )
        if 0 < self.config.step_limit <= self.n_calls or 0 < self.config.cost_limit <= self.cost:
            raise LimitsExceeded(
                {
                    "role": "exit",
                    "content": "LimitsExceeded",
                    "extra": {"exit_status": "LimitsExceeded", "submission": ""},
                }
            )
        if self._should_compact():
            try:
                self._compact_messages()
            except Exception as e:  # noqa: BLE001
                # The summarizer call sends prefix + all old turns; when a
                # single giant observation has already blown past the window,
                # the SUMMARIZE call itself overflows (terminal-killed the
                # fx/zoxide team agents). Fall back to mechanical truncation.
                es = str(e).lower()
                if ("ContextWindow" not in type(e).__name__
                        and "context length" not in es and "contextwindow" not in es):
                    raise
                self._emergency_truncate()
        self.n_calls += 1
        try:
            message = self.model.query(self.messages)
        except Exception as e:  # noqa: BLE001 - reactive compaction for overflow only
            # Proactive compaction can miss a single-step jump (one tool call
            # writing a whole file adds >10k tokens at once). On a context
            # overflow, mechanically truncate history — a summarize call would
            # itself overflow — and retry once.
            if "ContextWindow" not in type(e).__name__ and "context length" not in str(e).lower():
                raise
            # One truncation can be insufficient when the kept recent turns are
            # themselves huge (observed: solo-i3style-i6c died at step 324 on
            # the retry's own overflow). Escalate: retry up to 3 times, keeping
            # fewer recent turns each time (2 -> 1 -> 0).
            keep = self.config.compaction_keep_recent_turns
            last_err: Exception = e
            for attempt in range(3):
                self.config.compaction_keep_recent_turns = max(0, keep - attempt)
                self._emergency_truncate()
                try:
                    message = self.model.query(self.messages)
                    break
                except Exception as e2:  # noqa: BLE001
                    last_err = e2
                    if "ContextWindow" not in type(e2).__name__ and "context length" not in str(e2).lower():
                        self.config.compaction_keep_recent_turns = keep
                        raise
            else:
                self.config.compaction_keep_recent_turns = keep
                raise last_err
            self.config.compaction_keep_recent_turns = keep
        self.cost += message.get("extra", {}).get("cost", 0.0)
        self._last_prompt_tokens = self._get_prompt_tokens(message)
        self.add_messages(message)
        self._current_segment_messages = list(self.messages)
        return message

    def execute_actions(self, message: dict) -> list[dict]:
        """Execute actions in message, add observation messages, return them.

        Only the ``bash`` tool is registered with the model (see adapter.py) —
        ``send_message`` is invoked by the agent embedding a shell command
        like ``send_message <recipient> <<'MSG' ... MSG`` inside the bash
        command string.  We parse any such calls out of the command, run
        them through the messaging connector, and execute the remainder (if
        any) against the docker env.  Single-tool registration is much
        more reliable for smaller models than exposing two tools.
        """
        actions = message.get("extra", {}).get("actions", [])
        outputs = []
        for action in actions:
            tool_name = action.get("tool_name", "bash")
            handler = getattr(self, "tool_handlers", {}).get(tool_name)
            if handler is not None:
                # Generic registered-tool dispatch (TK4 task board etc.): the
                # harness wires host-side handlers; output goes back as a
                # normal observation.
                try:
                    outputs.append(handler(action))
                except Exception as e:  # noqa: BLE001 - a tool bug must not kill the agent
                    outputs.append({"output": f"tool {tool_name} failed: {e}", "returncode": 1, "exception_info": ""})
                continue
            if tool_name == "send_message" and self.comm:
                # Defensive: supported for legacy callers that still
                # register send_message as a tool.
                outputs.append(self._handle_send_message(action))
                continue

            cmd = action.get("command", "")
            if self.comm:
                sm_matches = _parse_send_messages(cmd)
                if sm_matches:
                    sm_outputs = []
                    for recipient, content, wait in sm_matches:
                        r = self._handle_send_message({"recipient": recipient, "content": content, "wait": wait})
                        sm_outputs.append(r["output"])
                    remaining = _strip_send_message(cmd)
                    combined = "\n".join(sm_outputs)
                    if not remaining.strip():
                        outputs.append({"output": combined, "returncode": 0, "exception_info": ""})
                        continue
                    env_out = self.env.execute({**action, "command": remaining})
                    env_out["output"] = combined + "\n" + env_out.get("output", "")
                    outputs.append(env_out)
                    continue

            outputs.append(self.env.execute(action))
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))

    def _handle_send_message(self, action: dict) -> dict:
        """Handle a send_message call via the messaging connector.

        ``wait=True`` (when the agent wrote ``send_message --wait ...`` in
        bash) uses ``send_and_wait`` so the peer's reply comes back in the
        same tool output.
        """
        recipient = action.get("recipient", "")
        content = action.get("content", "")
        wait = action.get("wait", False)

        if wait and hasattr(self.comm, "send_and_wait"):
            replies = self.comm.send_and_wait(recipient, content, timeout=60)
            self.log(f"SENT (blocking) to {recipient}: {content[:80]}...")
            self.sent_messages.append({"to": recipient, "content": content})
            output = f"Message sent to {recipient}"
            for r in replies or []:
                output += f"\n\n[Reply from {r['from']}]: {r['content']}"
            return {"output": output, "returncode": 0, "exception_info": ""}

        self.comm.send(recipient, content)
        self.log(f"SENT to {recipient}: {content[:80]}...")
        self.sent_messages.append({"to": recipient, "content": content})
        return {"output": f"Message sent to {recipient}", "returncode": 0, "exception_info": ""}

    def serialize(self, *extra_dicts) -> dict:
        """Serialize agent state to a json-compatible nested dictionary for saving."""
        last_message = self.messages[-1] if self.messages else {}
        last_extra = last_message.get("extra", {})
        agent_data = {
            "info": {
                "model_stats": {
                    "instance_cost": self.cost,
                    "api_calls": self.n_calls,
                },
                "config": {
                    "agent": self.config.model_dump(mode="json"),
                    "agent_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                },
                "mini_version": __version__,
                "exit_status": last_extra.get("exit_status", ""),
                "submission": last_extra.get("submission", ""),
            },
            "messages": self.messages,
            "trajectory_format": "mini-swe-agent-1.1",
        }
        if self._compaction_count > 0:
            segments = list(self._segments)
            current = self._current_segment_messages or self.messages
            if current:
                segments.append({"kind": "solver", "messages": list(current)})
            agent_data["segments"] = segments
        return recursive_merge(agent_data, self.model.serialize(), self.env.serialize(), *extra_dicts)

    def save(self, path: Path | None, *extra_dicts) -> dict:
        """Save the trajectory of the agent to a file if path is given. Returns full serialized data.
        You can pass additional dictionaries with extra data to be (recursively) merged into the output data.
        """
        data = self.serialize(*extra_dicts)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2))
        return data


def _parse_send_messages(cmd: str) -> list[tuple[str, str, bool]]:
    """Extract (recipient, content, wait) tuples from send_message calls.

    ``--wait`` may appear before or after the recipient.  Supports three
    formats: heredoc (``<<'MSG'``), double-quoted, single-quoted.
    """
    matches: list[tuple[str, str, bool]] = []
    for m in re.finditer(
        r"send_message\s+(--wait\s+)?(\w+)(\s+--wait)?\s+<<'?(\w+)'?\s*\n(.*?)\n\4",
        cmd,
        re.DOTALL,
    ):
        wait = bool(m.group(1) or m.group(3))
        matches.append((m.group(2), m.group(5), wait))
    if not matches:
        for m in re.finditer(r'send_message\s+(--wait\s+)?(\w+)(\s+--wait)?\s+"([^"]*)"', cmd):
            wait = bool(m.group(1) or m.group(3))
            matches.append((m.group(2), m.group(4), wait))
        for m in re.finditer(r"send_message\s+(--wait\s+)?(\w+)(\s+--wait)?\s+'([^']*)'", cmd):
            wait = bool(m.group(1) or m.group(3))
            matches.append((m.group(2), m.group(4), wait))
    return matches


def _strip_send_message(cmd: str) -> str:
    """Remove send_message calls from a compound bash command."""
    cmd = re.sub(
        r"send_message\s+(--wait\s+)?\w+(\s+--wait)?\s+<<'?(\w+)'?\s*\n.*?\n\3",
        "",
        cmd,
        flags=re.DOTALL,
    )
    cmd = re.sub(r'send_message\s+(--wait\s+)?\w+(\s+--wait)?\s+"[^"]*"', "", cmd)
    cmd = re.sub(r"send_message\s+(--wait\s+)?\w+(\s+--wait)?\s+'[^']*'", "", cmd)
    cmd = re.sub(r"^\s*&&\s*", "", cmd)
    cmd = re.sub(r"\s*&&\s*$", "", cmd)
    cmd = re.sub(r"&&\s*&&", "&&", cmd)
    cmd = re.sub(r"\|\|\s*\|\|", "||", cmd)
    return cmd.strip()
