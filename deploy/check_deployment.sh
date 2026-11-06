#!/usr/bin/env bash
# Check the SAP AI Core deployment health behind sap/gemini-2.5-flash.
# Reads the service key (aicore.json) itself — the secret is never printed; only
# deployment ids/status/scaling and a timed single-call probe are shown.
#
# Usage:  bash deploy/check_deployment.sh
# Env:    AICORE_JSON (path to the service key; default: ./aicore.json then ../aicore.json)
#         AICORE_RESOURCE_GROUP (default: default)
#         COOPER_MODEL (probe model; default: sap/gemini-2.5-flash)
set -uo pipefail
cd "$(dirname "$0")/.."

AICORE_JSON="${AICORE_JSON:-}"
[ -z "$AICORE_JSON" ] && { [ -f aicore.json ] && AICORE_JSON=aicore.json || AICORE_JSON=../aicore.json; }
RG="${AICORE_RESOURCE_GROUP:-default}"

echo "=== deployment status (resource-group: $RG) ==="
uv run python - "$AICORE_JSON" "$RG" <<'PY'
import json, sys, ssl, urllib.request, urllib.parse, urllib.error
try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _CTX = ssl.create_default_context()
path, rg = sys.argv[1], sys.argv[2]
try:
    k = json.load(open(path))
except Exception as e:
    print(f"could not read service key at {path}: {e}"); sys.exit(1)
# This project's aicore.json uses AICORE_* keys (fall back to env of same name).
import os
def pick(name): return k.get(name) or os.getenv(name) or ""
auth = pick("AICORE_AUTH_URL")
cid = pick("AICORE_CLIENT_ID")
csec = pick("AICORE_CLIENT_SECRET")
base = pick("AICORE_BASE_URL")
rg = k.get("AICORE_RESOURCE_GROUP") or os.getenv("AICORE_RESOURCE_GROUP") or rg
if not (auth and cid and csec and base):
    print("service key missing AICORE_AUTH_URL/CLIENT_ID/CLIENT_SECRET/BASE_URL"); sys.exit(1)

# OAuth2 client-credentials token (never printed)
data = urllib.parse.urlencode({"grant_type": "client_credentials",
                               "client_id": cid, "client_secret": csec}).encode()
try:
    with urllib.request.urlopen(urllib.request.Request(auth.rstrip("/") + "/oauth/token", data=data), timeout=30, context=_CTX) as r:
        tok = json.load(r)["access_token"]
    print("token: acquired OK")
except Exception as e:
    print(f"token: FAILED ({type(e).__name__}: {str(e)[:150]}) — auth/key issue")
    sys.exit(1)

req = urllib.request.Request(base.rstrip("/") + "/v2/lm/deployments",
                             headers={"Authorization": "Bearer " + tok, "AI-Resource-Group": rg})
try:
    with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
        body = json.load(r)
except urllib.error.HTTPError as e:
    print(f"list deployments HTTP {e.code}: {e.read()[:200].decode('utf-8','replace')}"); sys.exit(1)
except Exception as e:
    print(f"list deployments FAILED: {type(e).__name__}: {str(e)[:150]}"); sys.exit(1)

deps = body.get("resources", body if isinstance(body, list) else [])
print(f"deployments: {len(deps)}")
for d in deps:
    did = d.get("id", "?")
    status = d.get("status", "?")
    tstatus = d.get("targetStatus", "")
    model = ""
    det = d.get("details", {}) or {}
    res = (det.get("resources", {}) or {}).get("backend_details", {}) or {}
    model = (res.get("model", {}) or {}).get("name", "") or det.get("scaling", "")
    scaling = det.get("scaling", {}) or {}
    print(f"  {did}  status={status} target={tstatus} model={model} scaling={scaling} "
          f"msg={(d.get('statusMessage') or d.get('statusDetails') or '')}"[:300])
PY

echo
echo "=== timed single-call probe (one authed completion; capacity vs down) ==="
MODEL="${COOPER_MODEL:-sap/gemini-2.5-flash}"
if command -v uv >/dev/null 2>&1 && [ -d .venv ]; then
  uv run python - "$MODEL" <<'PY' 2>&1 | grep -viE "credential|warning" | tail -4
import sys, time
sys.path.insert(0, "src")
try:
    from dotenv import load_dotenv; load_dotenv(".env", override=False)
except Exception:
    pass
model = sys.argv[1]
from cooperagents.llm import LiteLLMClient
t0 = time.time()
try:
    a = LiteLLMClient(model, system_prompt="reply with one word").decide(
        agent_id="probe", role="solo", messages=[{"role": "user", "content": "Say OK"}], tools=[])
    print(f"PROBE_OK {time.time()-t0:.1f}s tool={a.tool}  (a lone call succeeds -> endpoint is up; stalls are concurrency/capacity)")
except Exception as e:
    print(f"PROBE_FAIL {time.time()-t0:.1f}s {type(e).__name__}: {str(e)[:160]}  (even one call fails -> deployment degraded/down)")
PY
else
  echo "(skipped probe: need uv + .venv)"
fi
