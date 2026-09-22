"""Exercise the real HTTP clients, team harness and official scorer without a model."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from cooperagents.eval.apptainer import ApptainerEvalBackend
from cooperagents.harness import _Coordinator


def main():
    run = Path(sys.argv[1]).resolve()
    run.mkdir(parents=True, exist_ok=True)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            message = {"role": "assistant", "content": "Check your own changes before completing."}
            finish = "stop"
            if body.get("tools"):
                history = body["messages"]
                prompt = "\n".join(str(m.get("content", "")) for m in history if m["role"] == "user")
                own, mate = ("agent1", "agent2") if "TEAMMATES: agent2" in prompt else ("agent2", "agent1")
                step = sum(m["role"] == "assistant" for m in history)
                if step == 0:
                    name, args = "send_message", {"recipient": mate, "content": "Dummy smoke: I own my separate marker file."}
                elif step == 1:
                    name, args = (
                        "bash",
                        {"command": f"printf 'invalid go syntax\\n' > dummy_{own}.go; echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"},
                    )
                else:
                    name, args = (
                        "bash",
                        {
                            "command": (
                                f"rm -f dummy_{own}.go; printf 'dummy smoke\\n' > dummy_{own}.txt; "
                                f"git add -A && git -c user.name=dummy -c user.email=dummy@local commit -q -m smoke && "
                                f"git push -q shared HEAD:refs/heads/{own} && git fetch -q shared {mate} && "
                                "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
                            )
                        },
                    )
                message = {
                    "role": "assistant",
                    "content": "Execute the deterministic smoke step.",
                    "tool_calls": [
                        {"id": f"call_{own}_{step}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                    ],
                }
                finish = "tool_calls"
            result = {
                "id": "dummy",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    os.environ.update(
        OPENAI_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1",
        OPENAI_API_KEY="dummy-local-only",
        AZURE_OPENAI_BASE_URL="",
        AZURE_OPENAI_API_KEY="",
        AZURE_OPENAI_KEY="",
        AZURE_OPENAI_DEPLOYMENT="qwen/qwen3.5-9b",
        ENV_FILE="/dev/null",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        COOPER_TEMPERATURE_FORCE="1.0",
        COOPER_TOP_P="0.95",
        COOPER_TOP_K="20",
        COOPER_MIN_P="0.0",
        COOPER_PRESENCE_PENALTY="1.5",
        COOPER_REPETITION_PENALTY="1.0",
        COOPER_REASONING_ENABLED="false",
        COOPER_REQUIRE_PARAMETERS="true",
    )
    try:
        # Short workers may finish before the periodic monitor fires. Probe its actual composer explicitly.
        nudge = _Coordinator({}, "qwen/qwen3.5-9b")._compose("LOOP", SimpleNamespace(messages=[]))
        assert nudge == "Check your own changes before completing.", nudge
        command = [
            sys.executable,
            "scripts/bench_compare.py",
            "--pairs",
            "go_chi_task:27:3,4",
            "--team-only",
            "--max-agents",
            "2",
            "--no-seed",
            "--coop-tools",
            "--git-share",
            "--coordinator",
            "--completion-gate",
            "--step-limit",
            "8",
            "--eval-concurrency",
            "1",
            "--team-name",
            "dummy",
            "--log-dir",
            str(run / "logs"),
        ]
        (run / "training-args.txt").write_text("\n".join(command) + "\n")
        subprocess.run(command, check=True)
        assert any(b.get("tools") for b in requests), "No worker HTTP requests"
        assert any(not b.get("tools") for b in requests), "No coordinator HTTP requests"
        expected = dict(
            temperature=1.0,
            top_p=0.95,
            top_k=20,
            min_p=0.0,
            presence_penalty=1.5,
            repetition_penalty=1.0,
            reasoning={"enabled": False},
            provider={"require_parameters": True},
        )
        for body in requests:
            for key, value in expected.items():
                assert body.get(key) == value, (key, body.get(key))
        assert any("SUBMISSION REJECTED" in json.dumps(b["messages"]) for b in requests), "Gate rejection not exercised"
        result_paths = list((run / "logs").rglob("result.json"))
        assert len(result_paths) == 1
        result = json.loads(result_paths[0].read_text())
        assert set(result["agents"]) == {"agent1", "agent2"} and not result["helpers"]
        assert all(a["status"] == "submitted" and a["steps"] == 3 for a in result["agents"].values()), result
        shared = Path(os.environ["COOPER_SCRATCH"]) / "shared"
        repositories = list(shared.glob("*/repo.git"))
        assert len(repositories) == 1, "Missing shared Git repository"
        for agent in ("agent1", "agent2"):
            files = subprocess.check_output(["git", f"--git-dir={repositories[0]}", "ls-tree", "--name-only", agent], text=True)
            assert f"dummy_{agent}.txt" in files, "Worker changes were not pushed to shared Git"
        patches = list((run / "logs").rglob("*.patch"))
        assert any("dummy_agent1.txt" in p.read_text() and "dummy_agent2.txt" in p.read_text() for p in patches), (
            "Missing merged worker markers"
        )
        from cooperbench.eval.sandbox import run_patch_test

        backend = ApptainerEvalBackend(json.loads(Path(os.environ["COOPER_IMAGE_MANIFEST"]).read_text()), os.environ["COOPER_SCRATCH"])
        gold = {
            str(f): run_patch_test("go_chi_task", 27, f, backend=backend, dataset_dir=Path(os.environ["COOPERBENCH_DIR"]) / "dataset")
            for f in (3, 4)
        }
        (run / "gold-eval.json").write_text(json.dumps(gold, indent=2))
        assert all(r["passed"] and not r["error"] for r in gold.values()), gold
        (run / "smoke.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "http_requests": len(requests),
                    "sampling": expected,
                    "gold_features": [3, 4],
                    "dummy_score_is_not_model_quality": True,
                },
                indent=2,
            )
        )
    finally:
        # Requests contain only synthetic task context; never store authorization headers.
        (run / "dummy-requests.json").write_text(json.dumps(requests, indent=2))
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
