"""
AeroTurn - the LLM brain.                                                    [AI]

The AI analyses the disruption, decides the order of recovery actions, drafts
notes for passengers and writes the executive brief. It can ONLY act through the
4 tools in tools.py, where code enforces every rule and a human approves changes.

AI providers are tried ONE AT A TIME, in the order of PROVIDERS below. If one is
busy, out of quota, has no key, or times out (30 s), the next one takes over and
continues the same conversation (nothing is redone).

RUN (from the aeroturn folder)
  python agent.py                       # full fallback chain
  python agent.py --provider groq       # only providers whose name contains "groq"
  python agent.py --local               # only local Ollama models (offline, free)
  python agent.py --list-providers      # which providers have a key / are reachable
  python agent.py --message "..."       # custom disruption text (checked for injection first)

Keys go in the .env file in the agent folder (see .env.example). Never print them.
"""
import inspect
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import costs
import db
import guards as g
import tools as T

load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # agent/.env
load_dotenv()                                                    # or a .env next to this file

TIMEOUT_S = 30           # cloud call timeout (workshop advice: never let one call freeze the agent)
LOCAL_TIMEOUT_S = 600    # local CPU models are slow
MAX_STEPS = 30           # cap on loop steps
MAX_STEPS_LOCAL = 60
MAX_REPLANS = 3          # checklist-guard send-backs; after that the case goes to human review
MAX_OUTPUT_TOKENS = 4096 # cap on answer length (reasoning models count their thinking too)
OLLAMA_CONTEXT = 8192
PROMPT_GUARD_MODEL = "meta-llama/llama-prompt-guard-2-86m"   # Groq guard model (stage 2, layer 2)
PROMPT_GUARD_THRESHOLD = 0.5

# ---------- AI providers, tried in this order. Edit freely. ----------
# kind: "openai" = OpenAI-compatible API, "gemini" = Google SDK, "ollama" = local
# Model names change often: run  python agent.py --list-providers  and fix them here.
PROVIDERS = [
    {"name": "Groq gpt-oss-120b",        "kind": "openai", "model": "openai/gpt-oss-120b",
     "base_url": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY"},
    {"name": "Groq qwen3.8-27b",         "kind": "openai", "model": "qwen/qwen3.8-27b",
     "base_url": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY"},
    {"name": "Groq gpt-oss-20b",         "kind": "openai", "model": "openai/gpt-oss-20b",
     "base_url": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY"},
    {"name": "Cerebras gpt-oss-120b",    "kind": "openai", "model": "gpt-oss-120b",
     "base_url": "https://api.cerebras.ai/v1", "key": "CEREBRAS_API_KEY"},
    {"name": "OpenRouter Nemotron Ultra (free)", "kind": "openai", "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
     "base_url": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY"},
    {"name": "OpenRouter Gemma 4 31B (free)", "kind": "openai", "model": "google/gemma-4-31b-it:free",
     "base_url": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY"},
    {"name": "Mistral small",            "kind": "openai", "model": "mistral-small-latest",
     "base_url": "https://api.mistral.ai/v1", "key": "MISTRAL_API_KEY"},
    {"name": "GitHub Models gpt-4.1-mini", "kind": "openai", "model": "openai/gpt-4.1-mini",
     "base_url": "https://models.github.ai/inference", "key": "GITHUB_TOKEN"},
    {"name": "Hugging Face gpt-oss-120b", "kind": "openai", "model": "openai/gpt-oss-120b",
     "base_url": "https://router.huggingface.co/v1", "key": "HF_TOKEN"},
    {"name": "Gemini 4 Argon",           "kind": "gemini", "model": "gemini-4-argon",        "key": "GEMINI_API_KEY"},
    {"name": "Gemini 4 Hydrogen",        "kind": "gemini", "model": "gemini-4-hydrogen",     "key": "GEMINI_API_KEY"},
    {"name": "Gemini 4 Carbon",          "kind": "gemini", "model": "gemini-4-carbon",       "key": "GEMINI_API_KEY"},
    {"name": "Gemini 3.8 Flash",         "kind": "gemini", "model": "gemini-3.8-flash",      "key": "GEMINI_API_KEY"},
    {"name": "Gemini 3.6 Flash",         "kind": "gemini", "model": "gemini-3.6-flash",      "key": "GEMINI_API_KEY"},
    {"name": "Gemini 3.5 Flash-Lite",    "kind": "gemini", "model": "gemini-3.5-flash-lite", "key": "GEMINI_API_KEY"},
    {"name": "Gemini 3.1 Flash-Lite",    "kind": "gemini", "model": "gemini-3.1-flash-lite", "key": "GEMINI_API_KEY"},
    {"name": "Ollama qwen3:4b (local)",  "kind": "ollama", "model": "qwen3:4b"},
    {"name": "Ollama qwen2.5:3b (local)", "kind": "ollama", "model": "qwen2.5:3b"},
]

SYSTEM = """You are AeroTurn, the AI assistant in an airline Operations Control Centre (OCC) in Dubai (DXB).
Times are Dubai local. You analyse and plan. The system's CODE enforces every safety rule, and a HUMAN
Operations Controller approves schedule, gate and crew changes. You cannot override a rule, sign off
maintenance, or approve anything yourself.

Workflow:
1. Call get_turnaround_and_crew_state.
2. Passengers: for each destination, rebook missed passengers with execute_atomic_passenger_rebook onto
   flights with bookable=true. Vulnerable passengers (MEDA, UM, WCHR, INF) FIRST, in their own batch.
   Never put more passengers in a batch than the flight's free_seats. Passengers left without a seat
   stay with the transfer desk.
3. Aircraft EK-813: call find_legal_standby_crew, then stage_controller_schedule_change:
   - CREW_SWAP with crew_in = recommended_crew_in from the matcher.
   - GATE_CHANGE: if gate_problem is not empty, pick a free wide-body gate, new_time = projected_departure.
   - DELAY: new_time = projected_departure.
   - DEPARTURE: propose it once, to show the controller what still blocks it.
4. send_passenger_notification to every passenger who missed a connection (one call can list many IDs).
5. Call get_recovery_options. The system has priced each option; compare them (cost, passengers
   affected, knock-on, blockers) and recommend ONE. The controller decides, not you.
6. If a tool returns blocked or rolled_back, read the reason, fix the request (smaller batch, another
   flight, other crew, another gate) and continue. Never try to work around a rule.

Finish with an EXECUTIVE RECOVERY BRIEF for the Operations Controller (under 220 words):
Situation | Turnaround critical path | Passengers (rebooked per flight, vulnerable protected, still
unresolved, compensation saved) | Crew & aircraft (crew swap, maintenance status) |
Recommended recovery option (and why, with the system's EUR figures) | Awaiting your approval
(approval IDs) | Blocked by guards (what and why). Use only facts from tool results."""

def get_turnaround_and_crew_state() -> dict:
    """Get the full disruption picture: the late inbound flight, the outbound flight it must
    operate, crew legal duty left, maintenance items, gate situation, passengers who miss
    their connection grouped by destination (vulnerable passengers first) with their
    rebooking options, and any changes waiting for Operations Controller approval."""
    return compact_state(T.get_turnaround_and_crew_state())


def compact_state(s):
    """Short version of the state for the AI (manual: keep tool results short, every token
    counts; small local models need it). Same facts, about 3x fewer tokens."""
    by_dest = {}
    for p in s["missed_connections"]["passengers"]:          # already sorted vulnerable-first
        d = by_dest.setdefault(p["destination"], {"vulnerable": [], "standard": []})
        if p["tier"] == "STANDARD":
            d["standard"].append(p["passenger_id"])
        else:
            d["vulnerable"].append(f"{p['passenger_id']} {p['tier']}")
    for dest, d in by_dest.items():
        d["options"] = [f"{o['flight_id']} dep {o['departs']} free_seats {o['free_seats']} "
                        f"{'BOOKABLE' if o['bookable'] else 'NOT bookable: ' + o['note']}"
                        for o in s["rebooking_options"].get(dest, [])]
    crew = lambda c: (f"{c['crew_id']} {c['name']} ({c['role']}) legal={c['legal_for_EK-813']} "
                      f"{c['minutes_away']} min away")
    return {"now": s["now"], "inbound": s["inbound"], "outbound": s["outbound"],
            "crew_on_EK-813": [crew(c) for c in s["crew_on_EK-813"]],
            "standby_crew": [crew(c) for c in s["standby_crew"]],
            "maintenance": [f"{m['item_id']} {m['status']} mandatory={bool(m['mandatory'])}: {m['description']}"
                            for m in s["maintenance"]],
            "missed_connections": {"count": s["missed_connections"]["count"],
                                   "vulnerable": s["missed_connections"]["vulnerable"], "by_destination": by_dest},
            "pending_approvals": [f"#{a['approval_id']} {a['kind']}" for a in s["pending_approvals"]],
            "integrity": s["integrity"]}


AGENT_TOOLS = [get_turnaround_and_crew_state] + T.AGENT_TOOLS[1:] + [costs.get_recovery_options]
TOOLS_BY_NAME = {f.__name__: f for f in AGENT_TOOLS}

# ---------- Run state (read by the console) ----------
ACTION_LOG = []    # one entry per tool call
MODELS_USED = []   # providers that answered, in order
_on_step = None


# =====================================================================
# Tool schemas (OpenAI / Ollama format), built from type hints + docstrings
# =====================================================================
def _json_type(annotation):
    if annotation in (int,):
        return {"type": "integer"}
    if annotation in (float,):
        return {"type": "number"}
    if annotation in (bool,):
        return {"type": "boolean"}
    if getattr(annotation, "__origin__", None) is list:
        return {"type": "array", "items": _json_type(annotation.__args__[0])}
    return {"type": "string"}


def tool_schema(fn):
    sig = inspect.signature(fn)
    props = {name: _json_type(p.annotation) for name, p in sig.parameters.items()}
    required = [name for name, p in sig.parameters.items() if p.default is inspect.Parameter.empty]
    return {"type": "function", "function": {
        "name": fn.__name__, "description": inspect.getdoc(fn),
        "parameters": {"type": "object", "properties": props, "required": required}}}


TOOL_SCHEMAS = [tool_schema(f) for f in AGENT_TOOLS]


# =====================================================================
# Provider adapters. All of them take and return the same message format:
#   {"role": "assistant", "content": "...", "tool_calls": [{"id", "type", "function": {"name", "arguments"}}]}
# =====================================================================
def _new_id():
    return uuid.uuid4().hex[:9]   # 9 characters: accepted by every provider (Mistral is strict)


def _call_openai(p, messages):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ[p["key"]], base_url=p["base_url"], timeout=TIMEOUT_S, max_retries=0)
    clean = []
    for m in messages:
        if m["role"] == "assistant":
            x = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                x["tool_calls"] = m["tool_calls"]
            clean.append(x)
        elif m["role"] == "tool":
            clean.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            clean.append({"role": m["role"], "content": m["content"]})
    r = client.chat.completions.create(model=p["model"], messages=clean, tools=TOOL_SCHEMAS,
                                       tool_choice="auto", temperature=0, max_tokens=MAX_OUTPUT_TOKENS)
    msg = r.choices[0].message
    return {"role": "assistant", "content": msg.content or "",
            "tool_calls": [{"id": _new_id(), "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                           for tc in (msg.tool_calls or [])]}


_gemini_client = None


def _call_gemini(p, messages):
    global _gemini_client
    from google import genai
    from google.genai import types
    if _gemini_client is None:
        _gemini_client = genai.Client(http_options=types.HttpOptions(timeout=TIMEOUT_S * 1000))
    contents = []
    for m in messages[1:]:
        if m["role"] == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=m["content"])]))
        elif m["role"] == "assistant":
            if m.get("_gemini") is not None:          # Gemini's own turn: replay it exactly
                contents.append(m["_gemini"])
                continue
            parts = [types.Part(text=m["content"])] if m.get("content") else []
            for tc in m.get("tool_calls") or []:      # turn made by ANOTHER provider
                parts.append(types.Part(
                    function_call=types.FunctionCall(name=tc["function"]["name"],
                                                     args=json.loads(tc["function"]["arguments"] or "{}")),
                    thought_signature=b"skip_thought_signature_validator"))
            contents.append(types.Content(role="model", parts=parts or [types.Part(text=" ")]))
        elif m["role"] == "tool":
            part = types.Part.from_function_response(name=m["name"], response=json.loads(m["content"]))
            last = contents[-1] if contents else None
            if last is not None and last.role == "user" and last.parts and last.parts[0].function_response:
                last.parts.append(part)                # results of one turn go in one message
            else:
                contents.append(types.Content(role="user", parts=[part]))
    config = types.GenerateContentConfig(
        system_instruction=messages[0]["content"], tools=AGENT_TOOLS, temperature=0,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    resp = _gemini_client.models.generate_content(model=p["model"], contents=contents, config=config)
    calls = resp.function_calls or []
    return {"role": "assistant", "content": "" if calls else (resp.text or ""),
            "tool_calls": [{"id": _new_id(), "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(dict(c.args or {}))}} for c in calls],
            "_gemini": resp.candidates[0].content}


def _call_ollama(p, messages):
    import ollama
    msgs = []
    for m in messages:
        if m["role"] == "assistant":
            x = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                x["tool_calls"] = [{"function": {"name": tc["function"]["name"],
                                                 "arguments": json.loads(tc["function"]["arguments"] or "{}")}}
                                   for tc in m["tool_calls"]]
            msgs.append(x)
        elif m["role"] == "tool":
            msgs.append({"role": "tool", "tool_name": m["name"], "content": m["content"]})
        else:
            msgs.append({"role": m["role"], "content": m["content"]})
    extra = {"think": False} if "qwen3" in p["model"] else {}   # thinking is very slow on a CPU
    resp = ollama.Client(timeout=LOCAL_TIMEOUT_S).chat(
        model=p["model"], messages=msgs, tools=TOOL_SCHEMAS,
        options={"temperature": 0, "num_ctx": OLLAMA_CONTEXT, "num_predict": MAX_OUTPUT_TOKENS}, **extra)
    msg = resp.message
    return {"role": "assistant", "content": msg.content or "",
            "tool_calls": [{"id": _new_id(), "type": "function",
                            "function": {"name": tc.function.name, "arguments": json.dumps(dict(tc.function.arguments or {}))}}
                           for tc in (msg.tool_calls or [])]}


ADAPTERS = {"openai": _call_openai, "gemini": _call_gemini, "ollama": _call_ollama}


# =====================================================================
# Fallback: one provider at a time
# =====================================================================
def available_providers(only=None, local_only=False):
    """Providers we can try: key present (cloud) and matching the filter."""
    chain = []
    for p in PROVIDERS:
        if local_only and p["kind"] != "ollama":
            continue
        if only and only.lower() not in p["name"].lower():
            continue
        if p.get("key") and not os.environ.get(p["key"]):
            continue
        chain.append(p)
    return chain


def _skip_now(err):
    """Errors where retrying the same provider is pointless: go to the next one."""
    if isinstance(err, (AttributeError, TypeError, ValueError, KeyError)):   # odd/non-JSON reply: provider broken
        return True
    text = str(err)
    return any(code in text for code in ("400", "401", "402", "403", "404", "413", "422", "429")) \
        or "not found" in text.lower()


def call_with_fallback(messages, chain, state):
    """Ask the current provider; on failure move down the chain. state['i'] remembers who works."""
    while state["i"] < len(chain):
        p = chain[state["i"]]
        for attempt in (1, 2):
            try:
                msg = ADAPTERS[p["kind"]](p, messages)
                if not MODELS_USED or MODELS_USED[-1] != p["name"]:
                    MODELS_USED.append(p["name"])
                return msg, p
            except Exception as e:
                print(f"  ({p['name']} attempt {attempt}: {type(e).__name__}: {str(e).replace(chr(10), ' ')[:140]})")
                if _skip_now(e) or attempt == 2:
                    break
                time.sleep(2)
        state["i"] += 1
        if state["i"] < len(chain):
            print(f"  -> switching to {chain[state['i']]['name']}")
    return None, None


# =====================================================================
# Small-model helpers
# =====================================================================
def tool_calls_from_text(text):
    """Some models WRITE a tool call as JSON text instead of calling it. Turn that into a real call."""
    calls, decoder, i = [], json.JSONDecoder(), 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict) and obj.get("name") in TOOLS_BY_NAME:
            args = obj.get("parameters") or obj.get("arguments") or {}
            calls.append({"id": _new_id(), "type": "function",
                          "function": {"name": obj["name"], "arguments": json.dumps(args if isinstance(args, dict) else {})}})
        i = end
    return calls


def remaining_work():
    """Checklist guard (CODE): what is still undone. The agent may not finish while items remain."""
    s = T.get_turnaround_and_crew_state()
    conn = db.connect(T.DB)
    todo = []
    for dest, opts in s["rebooking_options"].items():
        seats = sum(o["free_seats"] for o in opts if o["bookable"])
        waiting = [p for p in s["missed_connections"]["passengers"] if p["destination"] == dest]
        if waiting and seats:
            vul = sum(p["tier"] != "STANDARD" for p in waiting)
            todo.append(f"{len(waiting)} passenger(s) to {dest} ({vul} vulnerable) still need seats and "
                        f"{seats} bookable seat(s) remain: execute_atomic_passenger_rebook")
    staged = {r[0] for r in conn.execute(
        "SELECT kind FROM pending_approvals WHERE status IN ('PENDING','APPROVED')")}
    if any(not c["legal_for_EK-813"] for c in s["crew_on_EK-813"]) and "CREW_SWAP" not in staged:
        todo.append("crew on EK-813 is not legal: stage a CREW_SWAP with legal standby crew")
    if s["outbound"]["gate_problem"] and "GATE_CHANGE" not in staged:
        todo.append(f"gate problem for EK-813 ({s['outbound']['gate_problem'][0]}): stage a GATE_CHANGE")
    if s["outbound"]["current_departure"] < s["outbound"]["earliest_legal_departure"] and "DELAY" not in staged:
        todo.append(f"stage a DELAY of EK-813 to {s['outbound']['earliest_legal_departure']}")
    affected = {p["passenger_id"] for p in affected_passengers(conn)}
    notified = {r[0] for r in conn.execute("SELECT DISTINCT passenger_id FROM notifications")}
    missing = sorted(affected - notified)
    if missing:
        todo.append(f"send_passenger_notification to {len(missing)} passenger(s) not yet notified: {missing}")
    if not any(e["tool"] == "get_recovery_options" for e in ACTION_LOG):
        todo.append("call get_recovery_options and recommend one option in the brief")
    return todo


def affected_passengers(conn):
    """Everyone whose ORIGINAL connection was missed (even after rebooking)."""
    arrival = db.to_time(conn.execute("SELECT est_arr FROM flights WHERE flight_id='EK-812'").fetchone()[0])
    rows = conn.execute("SELECT p.passenger_id, f.est_dep FROM passengers p JOIN flights f "
                        "ON f.flight_id = p.original_onward").fetchall()
    return [dict(r) for r in rows if g.check_connection(arrival, db.to_time(r["est_dep"]))]


# =====================================================================
# The agent loop
# =====================================================================
def _log(entry):
    ACTION_LOG.append(entry)
    if _on_step:
        _on_step(entry)


def screen_input(message):
    """Stage 2: validate and sanitise BEFORE any AI sees the text. Two independent layers:
      layer 1 (CODE)        regex patterns in guards.py - instant, offline, always on
      layer 2 (GUARD MODEL) Groq Prompt Guard 2 - catches reworded attacks the regex misses
    Returns a list of reasons to block (empty = safe)."""
    reasons = [f"[regex] {v}" for v in g.check_injection(message)]
    if not message.strip() or len(message) > 2000:
        reasons.append("[format] message is empty or longer than 2000 characters")
    if os.environ.get("GROQ_API_KEY"):
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["GROQ_API_KEY"], base_url="https://api.groq.com/openai/v1",
                        timeout=10, max_retries=0)
        for attempt in (1, 2):
            try:
                r = client.chat.completions.create(model=PROMPT_GUARD_MODEL,
                                                   messages=[{"role": "user", "content": message}])
                score = float(r.choices[0].message.content)
                if score >= PROMPT_GUARD_THRESHOLD:
                    reasons.append(f"[guard model] Prompt Guard injection score {score:.3f} >= {PROMPT_GUARD_THRESHOLD}")
                break
            except Exception as e:   # guard model unavailable: the regex layer still protects us
                if attempt == 2:
                    print(f"  (prompt guard unavailable: {type(e).__name__}; regex layer still active)")
                time.sleep(1)
    return reasons


def run_tool(name, arguments, provider):
    fn = TOOLS_BY_NAME.get(name)
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise json.JSONDecodeError("not an object", str(arguments), 0)
        T.audit(db.connect(T.DB), "AI", f"requested:{name}", args, f"by {provider}")   # log the AI's decision
        result = fn(**args) if fn else {"status": "error", "error": f"unknown tool {name}"}
    except json.JSONDecodeError:
        args = {}
        result = {"status": "error", "error": "Your tool arguments were not valid JSON. "
                                              "Call the tool again with a valid JSON object."}
    except TypeError:
        args = {}
        result = {"status": "error", "error": f"Wrong arguments. Correct call: {name}{inspect.signature(fn)}"}
    except Exception as e:
        args = {}
        result = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    status = result.get("status", "ok")
    _log({"time": datetime.now().strftime("%H:%M:%S"), "actor": "AI→CODE", "provider": provider,
          "tool": name, "inputs": args, "result": result, "status": status,
          "blocked": status in ("blocked", "rolled_back")})
    shown = {k: v for k, v in result.items() if k not in ("missed_connections", "rebooking_options", "messages")}
    flag = "   [BLOCKED BY GUARD]" if status == "blocked" else "   [ROLLED BACK]" if status == "rolled_back" else ""
    print(f"-> {name}({args})\n<- {json.dumps(shown)[:300]}{flag}")
    return result


def run_agent(message, on_step=None, only=None, local_only=False):
    """Run AeroTurn on one disruption message. Returns the final text (the executive brief)."""
    global _on_step
    _on_step = on_step
    ACTION_LOG.clear()
    MODELS_USED.clear()

    # Step 2 of the workshop diagram: check the input BEFORE it reaches any AI
    injection = screen_input(message)
    if injection:
        conn = db.connect(T.DB)
        T.audit(conn, "CODE", "input_blocked", message, "; ".join(injection))
        _log({"time": datetime.now().strftime("%H:%M:%S"), "actor": "CODE", "provider": None,
              "tool": "input_filter", "inputs": {"message": message}, "result": {"violations": injection},
              "status": "blocked", "blocked": True})
        print(f"  INPUT BLOCKED: {injection}")
        return "Input refused by the safety filter: it tries to give the agent new orders. Nothing was done."

    chain = available_providers(only, local_only)
    if not chain:
        return "No AI provider available (no API keys found and no local model selected)."
    local = all(p["kind"] == "ollama" for p in chain)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": message}]
    state = {"i": 0}
    nudges_left = MAX_REPLANS

    for _ in range(MAX_STEPS_LOCAL if local else MAX_STEPS):
        msg, provider = call_with_fallback(messages, chain, state)
        if msg is None:
            return hand_to_human("every AI provider in the chain was unavailable")
        if not msg["tool_calls"]:
            msg["tool_calls"] = tool_calls_from_text(msg["content"])
            if msg["tool_calls"]:
                msg["content"] = ""
        messages.append(msg)

        if not msg["tool_calls"]:
            todo = remaining_work()
            if todo and nudges_left > 0:
                nudges_left -= 1
                print(f"  (checklist guard: {len(todo)} item(s) undone, sending the AI back to work)")
                messages.append({"role": "user", "content": "You are not finished. Do not describe actions, "
                                 "CALL the tools. Still to do:\n- " + "\n- ".join(todo)})
                continue
            if todo:   # replan cap reached: a person takes over (manual: cap replans, then human review)
                return msg["content"] + "\n\n" + hand_to_human(f"{len(todo)} item(s) still undone after "
                                                               f"{MAX_REPLANS} replans", todo)
            return msg["content"]

        for tc in msg["tool_calls"]:
            result = run_tool(tc["function"]["name"], tc["function"]["arguments"], provider["name"])
            messages.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["function"]["name"],
                             "content": json.dumps(result)})

    return hand_to_human(f"step limit ({MAX_STEPS}) reached", remaining_work())


def hand_to_human(reason, todo=()):
    """Stop the AI and send the case to the Operations Controller for review (logged)."""
    T.audit(db.connect(T.DB), "CODE", "handed_to_human_review", list(todo), reason)
    items = "".join(f"\n- {t}" for t in todo)
    return f"⚠️ HANDED TO HUMAN REVIEW: {reason}.{items}"


def summarize_log():
    ok = [e for e in ACTION_LOG if not e["blocked"]]
    rebooked = sum(len(e["result"].get("rebooked", [])) for e in ok if e["tool"] == "execute_atomic_passenger_rebook")
    return {
        "passengers_rebooked": rebooked,
        "staged_for_approval": sum(e["status"] == "pending_controller_approval" for e in ACTION_LOG),
        "notifications": sum(e["result"].get("count", 0) for e in ok if e["tool"] == "send_passenger_notification"),
        "blocked": [e for e in ACTION_LOG if e["status"] == "blocked"],
        "rolled_back": [e for e in ACTION_LOG if e["status"] == "rolled_back"],
        "models_used": list(MODELS_USED),
    }


def list_providers():
    """Show which providers are usable, without printing any key."""
    for p in PROVIDERS:
        if p["kind"] == "ollama":
            try:
                import ollama
                names = [m.model for m in ollama.list().models]
                ok = any(n.startswith(p["model"]) for n in names)
                print(f"  {'OK  ' if ok else 'MISS'} {p['name']:<30} {'installed' if ok else 'run: ollama pull ' + p['model']}")
            except Exception:
                print(f"  MISS {p['name']:<30} Ollama is not running")
        else:
            has = bool(os.environ.get(p["key"]))
            print(f"  {'KEY ' if has else '----'} {p['name']:<30} {p['key']} {'set' if has else 'not set'}")


def default_alert():
    """The disruption alert text for the current scenario (delay comes from the database)."""
    delay = db.scenario(db.connect(T.DB))["delay_min"]
    return f"EK-812 from Bangkok landed {delay} minutes late. Recover the turnaround and the missed connections."


def print_verified_facts():
    f = T.verified_facts()
    print("VERIFIED BY CODE (from the database, not the AI)")
    print(f"  Rebooked: {f['rebooked']}")
    print(f"  Unresolved ({len(f['unresolved'])}): {f['unresolved']}")
    for a in f["awaiting_approval"]:
        detail = a.get("gate") or a.get("crew_in") or a.get("new_time")
        print(f"  Awaiting approval #{a['approval_id']}: {a['kind']} {a['flight_id']} {detail}")
    print(f"  Open mandatory maintenance: {f['open_mandatory_maintenance'] or 'none'} | integrity {f['integrity']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console: show dashes and symbols correctly
    args = sys.argv[1:]
    if "--list-providers" in args:
        list_providers()
        raise SystemExit
    only = args[args.index("--provider") + 1] if "--provider" in args else None
    if "--db" in args:   # run on a separate database file (e.g. a long local test)
        T.DB = Path(args[args.index("--db") + 1])
    if "--keep" not in args:
        settings = {}
        if "--delay" in args:          # what-if: python agent.py --delay 150 --crew-left 600
            settings["delay_min"] = int(args[args.index("--delay") + 1])
        if "--crew-left" in args:
            settings["crew_duty_left_min"] = int(args[args.index("--crew-left") + 1])
        db.build(T.DB, **settings)     # start from a clean scenario every time
    text = args[args.index("--message") + 1] if "--message" in args else default_alert()
    print("=" * 70)
    print(f"AeroTurn | providers: {[p['name'] for p in available_providers(only, '--local' in args)]}")
    print(f"MESSAGE: {text}\n")
    brief = run_agent(text, only=only, local_only="--local" in args)
    print("\n" + "=" * 70 + "\nEXECUTIVE RECOVERY BRIEF\n" + brief)
    s = summarize_log()
    print("=" * 70)
    print_verified_facts()
    print("=" * 70)
    print(f"AI used: {' -> '.join(s['models_used']) or 'none'}")
    print(f"Tool calls: blocked {len(s['blocked'])} | rolled back {len(s['rolled_back'])} | "
          f"notified {s['notifications']}")
    print(f"Still to do (checklist): {remaining_work() or 'nothing'}")
