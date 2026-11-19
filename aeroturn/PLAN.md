# AeroTurn — Master Plan (PLAN.md)

**Track 4 · Aviation & Airport Operations (University)** — Autonomous aircraft turnaround & atomic disruption recovery agent.

**Principle: the LLM plans, code decides, a human approves.** The AI never does maths, never writes to the
database directly and never approves anything. Every change goes through one write gateway with full rollback.

---

## 1. System architecture & tech stack

### Data flow (maps 1:1 to the TechX manual's 10 stages)

| # | Manual stage | AeroTurn | File | Who |
|---|---|---|---|---|
| 1 | Trigger | Disruption alert typed or picked in the OCC console | `app.py`, `agent.py` | Human |
| 2 | Validate & sanitise | Layer 1: regex injection patterns + format check. Layer 2: Groq **Prompt Guard 2** model. Blocked before any AI sees the text | `guards.py`, `agent.py::screen_input` | Code + guard model |
| 3 | Plan | LLM chooses the next tool (ReAct loop), provider fallback chain | `agent.py` | AI |
| 4 | Act | 6 tools over SQLite (state, crew matcher, atomic rebook, stage change, notify, recovery options) | `tools.py`, `costs.py` | Code |
| 5 | Remember state | SQLite: passengers, seats, bags, crew, maintenance, gates, approvals, notifications, audit | `db.py` | Code |
| 6 | Guard check | Crew duty ceiling, crew reachability, maintenance floor, turnaround floor, connection time, seat capacity, vulnerable priority, gate fit/overlap — immutable (`frozen` dataclass) | `guards.py` | Code |
| 7a | Safe → act | In-policy seat + bag rebooking runs automatically (still logged, still atomic) | `tools.py` | Code |
| 7b | Risky → human | Departure release, delay, crew swap, gate change → `pending_approvals` → controller Approve/Reject; engineer sign-off | `tools.py`, `app.py` | Human |
| 8 | One write gateway | `write_gateway()`: re-check guards → `BEGIN IMMEDIATE` → apply → integrity check → `COMMIT` or `ROLLBACK` → audit | `tools.py` | Code |
| 9 | Log & report | SHA-256 hash-chained audit log (AI / CODE / HUMAN actor on every row) + AI executive brief beside code-verified facts | `db.py`, `agent.py` | Code + AI |
| 10 | Demo | OCC console, red-team scorecard (16/16), critical-path Gantt, recovery options | `app.py`, `redteam.py` | You |

### Tech stack
- **Python 3.11+**, **SQLite** (stdlib), **Streamlit** console. Backend = Python modules imported in-process (no web server needed).
- **AI providers (OpenAI-compatible + Google SDK + local)**, tried one at a time:
  Groq gpt-oss-120b → Groq qwen3.8-27b → Groq gpt-oss-20b → Cerebras → OpenRouter (free) → Mistral → GitHub Models →
  Hugging Face → Gemini 4 Argon/Hydrogen/Carbon → Gemini 3.8 Flash / 3.6 Flash / 3.5 Flash-Lite / 3.1 Flash-Lite →
  **Ollama qwen3:4b → qwen2.5:3b (offline)**. Providers without a key are skipped; 4xx/429 skip immediately;
  timeouts/5xx retry once. 30 s timeout, `max_retries=0`, 4096 max output tokens.
- SDKs: `google-genai`, `openai`, `ollama`, `python-dotenv`, `streamlit` (see `requirements.txt`).

---

## 2. File-by-file structure

```
agent/
├── .env                 # API keys (never committed, never read by the coding agent)
├── .env.example         # key names + where to get them
├── .gitignore           # .env, *.sqlite, __pycache__
└── aeroturn/
    ├── PLAN.md          # this file
    ├── README.md        # setup + 90-second demo script + judge Q&A
    ├── requirements.txt
    ├── .streamlit/config.toml   # dark navy/amber theme, Deploy button hidden
    ├── db.py            # schema, seeded scenario, integrity checks, hash-chained audit
    ├── guards.py        # immutable safety rules (pure Python, no DB, no LLM)
    ├── turnaround.py    # critical-path method for the ground turnaround
    ├── tools.py         # agent tools + single write gateway + human actions
    ├── costs.py         # recovery options + EU261-style compensation
    ├── agent.py         # LLM loop, provider fallback, input screening, checklist guard
    ├── redteam.py       # 16 attacks on a fresh copy of the scenario
    ├── app.py           # Streamlit OCC console
    ├── test_guards.py   # 21 tests: rules + database (no AI)
    └── test_tools.py    # 19 tests: services, rollback, approval gate (no AI)
```

| File | Responsibility | Key functions | Connections |
|---|---|---|---|
| `db.py` | Builds `aircraft_turnaround_disruption.sqlite` (seed 812, identical every run); `UNIQUE` index = DB-level no double booking | `build`, `connect`, `missed_connections`, `double_bookings`, `orphan_bags`, `append_audit`, `verify_audit_chain` | used by everything |
| `guards.py` | Immutable floors. `Limits` is a frozen dataclass; priority tiers are a read-only mapping | `check_departure`, `check_crew_legal`, `check_crew_reachable`, `check_crew_complement`, `check_maintenance`, `check_connection`, `check_priority`, `check_gate`, `check_injection` | called by `tools.py`, `agent.py` |
| `turnaround.py` | CPM forward/backward pass over 10 ground tasks; earliest departure is calculated, not guessed | `plan` | `tools.py` |
| `tools.py` | The 3 brief services + matcher + notifications; **`write_gateway`** is the only function that writes; human actions are NOT agent tools | `get_turnaround_and_crew_state`, `execute_atomic_passenger_rebook`, `stage_controller_schedule_change`, `find_legal_standby_crew`, `send_passenger_notification`, `approve`, `reject`, `engineer_sign_off`, `verified_facts`, `turnaround_plan_for` | `db`, `guards`, `turnaround` |
| `costs.py` | Prices options A/B/C (compensation, care, crew call-out, knock-on) — money maths in code only | `get_recovery_options` (agent tool), `select_recovery_plan` (human) | `tools.py` |
| `agent.py` | ReAct loop; fallback chain; `screen_input` (regex + Prompt Guard); checklist guard with `MAX_REPLANS=3` then human review; text-tool-call parser for small models; logs every AI request | `run_agent`, `call_with_fallback`, `screen_input`, `remaining_work`, `hand_to_human` | `tools`, `costs`, `guards` |
| `redteam.py` | Proves the guards: 16 attacks, evidence for each | `run_suite` | everything |
| `app.py` | Console: controls, live timeline (🤖/⚙️/👤), approval queue, KPIs, tabs (options, Gantt, red-team, audit chain, passengers) | `on_agent_step`, `act_*` buttons | `agent`, `tools`, `costs`, `redteam` |

---

## 3. Build order (as built)

| Phase | Must-have (MVP) | Status |
|---|---|---|
| 1. Data & rules | Schema + seed (EK-812 +75 min, 280 pax, 45 missed, 10 vulnerable, crew 20 min from limit, M-1001 open), guards, `test_guards.py` | ✅ 21/21 |
| 2. Tools & gateway | 3 brief services, atomic rollback with simulated mid-way seat conflict, approval queue, `test_tools.py` | ✅ 19/19 |
| 3. AI brain | Agent loop, provider fallback, injection screen, executive brief + verified facts | ✅ cloud run clean |
| 4. Console | Streamlit OCC, demo buttons, approval cards | ✅ headless click-through |
| **Stretch** | Standby crew matcher · turnaround critical path · EU261 options · red-team scorecard · hash-chained audit · Prompt Guard layer | ✅ all built |

---

## 4. Human-in-the-loop checkpoints

**In the product:** departure release, delay, crew swap and gate change wait for the Operations Controller; the
maintenance sign-off is an engineer action; the recovery option is chosen by the controller. Guards are re-checked
at approval time. The AI has no tool to approve, reject, sign off or choose.

**In the build (team checkpoints):**
1. After Phase 1 — check the scenario numbers match the brief (`python db.py`).
2. After Phase 2 — run `python test_guards.py` and `python test_tools.py`; verify duty maths by hand (13 h ceiling, 465 + 30 min needed).
3. After Phase 3 — add keys to `.env` (never paste them in chat); run `python agent.py --list-providers`, then one live run.
4. After Phase 4 — full rehearsal in the console; decide what to cut if time is short.

---

## 5. Coding-agent handoff prompts (for changes during the hackathon)

- *"Read PLAN.md. Add <feature> in <file>. Keep every write inside `write_gateway`, keep guards in `guards.py` pure, never give the AI an approve/sign-off tool, keep `python test_guards.py`, `python test_tools.py` and `python redteam.py` passing, and add a test for the new behaviour."*
- *"A provider returns 404: run `python agent.py --list-providers`, list the provider's models, and update only `PROVIDERS` in agent.py."*
- *"Add a new attack to redteam.py that tries <X>; it must be stopped by an existing guard or you must add the guard in guards.py with a test."*

## Assumptions
- Scenario clock is fixed at 2026-11-20 10:15 Dubai time (demo date lock), all data synthetic.
- Compensation uses EU261-style bands as an **indicative** airline cost benchmark (legal applicability depends on carrier/route).
- Single-user console; SQLite `BEGIN IMMEDIATE` serialises writes.
