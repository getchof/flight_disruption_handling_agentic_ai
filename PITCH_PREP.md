# AeroTurn — Pitch & Demo Preparation Guide

> Read this on the way. It covers the story, the numbers, how the system works, the exact demo clicks, the pitch script, and answers to likely judge questions.
>
> **Slogan:** 🤖 **AI plans** · ⚙️ **Code enforces** · 👤 **Humans approve**

---

## 0. The 60-second summary (memorise this)

- **Track:** 4 — Aviation & Airport Operations. Project: **AeroTurn**, an airline Operations Control Centre (OCC) agent.
- **Problem:** a late plane at Dubai causes four problems at once — passengers miss connections, the crew is about to exceed legal duty hours, a mandatory inspection is open, and the gate is double-booked.
- **Risk of a plain AI agent:** it could rebook halfway and fail (double bookings, lost bags) or bend safety rules to save time.
- **Our design:** the AI chooses actions; **code** enforces every rule and makes every change in all-or-nothing transactions; a **human controller** approves every schedule, gate and crew change.
- **Result in the demo:** 38 passengers rebooked, **10/10 vulnerable protected**, 0 double bookings, 0 orphan bags, departure **11:45**, cheapest option **€1,500** vs **€450,250** to cancel, **19/19** attacks blocked.

---

## 1. The scenario (know it cold)

**Time:** 20 Nov 2026, **10:15** Dubai time (fixed scenario clock).

### The four problems

| Problem | Facts | Where on screen |
|---|---|---|
| **Late plane** | **EK-812** Bangkok → Dubai, due 09:00, landed **10:15 (+75 min)** | Banner (amber chip) |
| **Passengers** | 280 transfer passengers; **45 miss their connection** (18 London, 12 New York, 15 Paris); **10 vulnerable** | Banner, score boxes, 🧳 Passengers tab |
| **Crew** | EK-813 crew has **20 min** of legal duty left; the flight needs **495 min** | Banner (red), EK-813 card "crew OVER DUTY LIMIT" |
| **Maintenance** | Hydraulic inspection **M-1001** is OPEN, mandatory sign-off | Banner (red), EK-813 card |
| **Gate** | EK-813's gate **B12** is booked by **EK-555** from 11:00 | Approval queue (GATE_CHANGE), Critical path |

### Inbound vs outbound
- **Inbound** = arriving at Dubai: **EK-812** (BKK → DXB).
- **Outbound** = leaving Dubai: **EK-813** (DXB → MAN, due out 11:00).
- **Same aircraft:** Boeing 777 **A6-EQX**. Late inbound ⇒ late outbound.
- **Turnaround** = everything on the ground between landing and take-off (deboard, unload bags, clean, cater, fuel, crew briefing, load bags, board). Minimum for a wide-body: **90 min**.

### Vulnerable passenger tiers (protected first)
1. **MEDA** — medical (e.g. oxygen)
2. **UM** — unaccompanied minor
3. **WCHR** — wheelchair
4. **INF** — travelling with an infant
9. STANDARD

---

## 2. Every flight in the scenario

| Group | Flights | Role |
|---|---|---|
| **The disrupted plane (A6-EQX)** | **EK-812** BKK→DXB (late) · **EK-813** DXB→MAN 11:00 | Start of everything / the flight to recover |
| **Missed connections** | EK-201 → New York 10:30 (12 pax) · EK-001 → London 10:45 (18) · EK-073 → Paris 11:00 (15) | Leave before 10:15 + 60 min = 11:15 → missed |
| **Rebooking options** | EK-005 → London 14:30 (8 free) · EK-007 → London 16:00 (6) · EK-203 → New York 15:00 (9) · EK-075 → Paris 15:30 (16) | Where the 45 go |
| **Trap** | **EK-003** → London 11:10 (20 free) | Only 55 min to connect → code **blocks** it |
| **Connections that still work** | EK-511 Mumbai · EK-384 Hong Kong · EK-412 Sydney · EK-061 Hamburg | The other 235 passengers; unaffected |
| **Gate conflict** | **EK-555** → Doha, gate B12 from 11:00 | Forces EK-813 to move to **B14** |
| **What-if extras** | EK-513 Mumbai 17:00 · EK-386 Hong Kong 18:30 | Used only in long-delay scenarios |
| **Option E** | **EK-017** → Manchester 14:25 (45 free) | Where EK-813's own passengers could go if cancelled |
| **Knock-on (cost only)** | **EK-814** MAN→DXB, 350 pax | Return flight; delayed or cancelled by options B/C/E |

**Seat maths:** London 18 pax vs 8 + 6 = 14 seats → 4 left over. New York 12 vs 9 → 3 left over. Paris 15 vs 16 → all fit. **Total left over: 7** → transfer desk overnight, **bags held in Dubai**.

---

## 3. The crew problem

- **Max duty:** 13 h (780 min). Crew started **21:35 yesterday** → limit at **10:35**.
- **Needed after take-off:** 465 min flight + 30 min post-flight = **495 min**.
- At the earliest departure 11:45 they would already be **−70 min**. → Illegal.
- **Fix:** standby crew. **Crew matcher** (code) picks the **closest legal** person per role:

| Role | Chosen | Rejected |
|---|---|---|
| Captain | **Capt. Hassan Ali** (8 min) | Capt. Grace Lee — 45 min away, can't brief before boarding |
| First Officer | **FO Nadia Rahman** (8 min) | FO Ken Ito — already worked 9 h, not legal |
| Purser | **Purser Diego Costa** (15 min) | — |
| Cabin Crew | **CC Sara Mensah** (8 min) | — |

---

## 4. Departure time — the critical path

- Critical path: **Deboard 25 → Catering 35 → Boarding 30 = 90 min** → 10:15 + 90 = **11:45**.
- Bags (unload 30 + load 25 = 55) have spare time; they are not critical.
- Crew task = minutes away + 30 min briefing (15 + 30 = 45 for Diego Costa, the farthest).
- **11:45 is calculated by code** (`turnaround.py`, Critical Path Method), not guessed by the AI.

---

## 5. The six recovery options (💶 tab)

Prices: EU261-style **indicative** — €600 per long-haul passenger if ≥3 h late (half if rerouted and ≤4 h late), meals €25 (≥2 h), hotel €150 (overnight), standby call-out €375 per crew member. EK-813 load 310 passengers; EK-814 load 350.

| | Option | Total | Departs | Key point |
|---|---|---|---|---|
| **A** | Standby crew swap + gate change | **€1,500** ✅ lowest cost, earliest departure | 11:45 | Needs M-1001 sign-off |
| **B** | Wait for current crew to rest (12 h) | €403,750 | 23:35 | EK-814 ~12 h late (+€210,000) |
| **C** | Cancel, everyone tomorrow | €450,250 | — | EK-814 cancelled |
| **D** | Swap to spare aircraft **A6-EQS** + standby crew | €5,500 | 11:30 | **Needs nothing else** — no sign-off, own stand |
| **E** | Cancel, rebook 105 today (45 on EK-017, 60 via partner) + 205 tomorrow | €442,000 | — | Cheaper than C |
| **F** | Fallback crew (Grace Lee, Ola Novak) + gate change | €1,500 (same as A) | 12:00 | Only if closest crew unavailable |

**How A's €1,500 is made:** 45 min delay → no compensation, no meals; 4 crew × €375 = €1,500.
**How B's €403,750 is made:** 310 × €600 = €186,000 + 310 × €25 = €7,750 + EK-814 350 × €600 = €210,000.
**How C's €450,250 is made:** €186,000 + 310 × (€25 + €150) = €54,250 + €210,000.

**Why A over F at the same price:** same money, A leaves 15 min earlier; F is only a fallback. (Code: tie broken by earliest departure.)

**Connecting passengers line (bottom of tab):** 45 affected, 38 rebooked today, 7 overnight → €26,775 now vs €34,875 if nobody were rebooked → **saved €8,100**. Same under every option (those passengers are not on EK-813).

**Order on screen:** the options appear only **after the AI has asked code to price them** in a run (or press **Show options now (no AI)** as a backup).

**When options lock:** once A's steps are approved, B–F are **LOCKED** (can't pick a plan that contradicts what's approved). Choosing another option while A's steps are only pending withdraws them automatically.

---

## 6. How it works technically (file by file)

All in `aeroturn/`. Plain Python, SQLite, Streamlit. No separate server.

```
app.py ─────────── Streamlit console (UI + human buttons)
  │ starts agent in a background thread
agent.py ───────── LLM loop: screen input → provider fallback → tool calls → checklist
  │ calls tools only
tools.py  costs.py ─ services + single write gateway + human actions / option pricing
  │
guards.py  turnaround.py ─ pure rules / critical path (no DB, no AI)
  │
db.py ──────────── SQLite schema, scenario seed, integrity checks, hash-chained audit
```

### `db.py` — data
- SQLite built from a fixed seed (identical every time); what-if settings (delay, crew duty, maintenance, seat factor).
- **`UNIQUE` index on `seats(occupant)`** → the database itself refuses a second seat for a passenger.
- `double_bookings()`, `orphan_bags()` (bag on a flight its owner isn't on, or HELD after its owner was rebooked), `missed_connections()`.
- **Audit log:** each row stores SHA-256(previous hash + row) → `verify_audit_chain()` detects any edit.

### `guards.py` — the rules
- Pure functions, return a list of violations (empty = OK). No DB, no network, no AI.
- Limits in a **`frozen` dataclass** → changing them at runtime raises an error.
- `check_crew_legal`, `check_crew_reachable`, `check_crew_complement`, `check_maintenance`, `check_departure`, `check_connection`, `check_priority`, `check_gate`, `check_injection`.

### `turnaround.py` — critical path
- 10 ground tasks with durations and dependencies; forward + backward pass; zero-slack tasks = critical; returns departure time.

### `tools.py` — services and the write gateway
- **`write_gateway()` — the only function that changes data:**
  1. re-check guards → 2. `BEGIN IMMEDIATE` → 3. apply all writes → 4. integrity check inside the transaction → 5. `COMMIT` or `ROLLBACK` everything → 6. audit (AI / CODE / HUMAN).
- **Agent tools:** `get_turnaround_and_crew_state`, `find_legal_standby_crew`, `execute_atomic_passenger_rebook`, `stage_controller_schedule_change` (only adds to the approval queue), `send_passenger_notification` (code writes the facts; the AI's note is dropped if it states facts).
- **Human actions (never given to the AI):** `approve` (re-checks guards at approval time), `reject`, `engineer_sign_off`.
- `disruptions()` finds any late inbound flight; the outbound is found by aircraft registration → works for any flight, not just EK-812.
- `hold_missed_bags()` — bags of unresolved passengers are HELD at the hub.

### `costs.py` — option pricing
- EU261-style `compensation()` and `care()`; options A–F from the current state; best = lowest cost then earliest departure; `select_recovery_plan()` records the controller's choice.

### `agent.py` — the AI loop
1. **`screen_input()`** — layer 1 regex (`check_injection`) + layer 2 **Prompt Guard** model (Groq). Hit → blocked, no AI call.
2. **`pick_disruption()`** — code decides which late flight the run is about.
3. Loop (max 30 steps): **`call_with_fallback()`** tries providers one at a time (Groq → Cerebras → OpenRouter → Mistral → GitHub → Hugging Face → Gemini → local Ollama). 30 s timeout; rate limits skip immediately.
4. **`run_tool()`** runs the chosen tool, audits "AI requested X", returns the result to the AI.
5. **Checklist guard `remaining_work()`** — if the AI stops early, code sends it back (max 3 times), then hands to human review.
6. **`STOP`** event — the console's ■ Stop agent button.
7. Returns the **executive brief**.

### `app.py` — the console
- Streamlit reruns on every click, so the **agent runs in a background thread**; the timeline refreshes every 2 s; you can approve while the AI works.
- While running: Run, Reset, Build scenario, rollback demos and red-team suite are disabled (they would change data under the agent).
- Brief shown next to **"⚙️ Verified from the database"**.

### Tests (no AI needed)
| File | Count | Covers |
|---|---|---|
| `test_guards.py` | 21 | every rule and DB check |
| `test_tools.py` | 30 | rebooking, rollback (seat + bag), approvals, notifications, options, stop switch, any flight |
| `test_whatif.py` | 11 | other delays, crew, maintenance, seat supply |
| `redteam.py` | 19 attacks | all blocked |

---

## 7. One rebooking, step by step (good for a technical question)

1. AI calls `execute_atomic_passenger_rebook(["P001", …], "EK-005")`.
2. Code checks: missed connection? right destination? ≥60 min to connect? enough seats? vulnerable passengers not skipped?
   - Any violation → **BLOCKED** with the reason; the AI reads it and tries something else.
3. `write_gateway`: `BEGIN` → for each passenger: free old seat → take new seat → route bags → integrity check → `COMMIT`.
4. Any failure (stolen seat, lost bag) → `ROLLBACK`; seats and bags are byte-for-byte as before.
5. Audit row chained; timeline card: *🤖 AI (model) → ⚙️ CODE: COMMITTED*.

---

## 8. Before the demo — checklist

- [ ] Laptop charged; console running: in `aeroturn/` → `python -m streamlit run app.py`
- [ ] Press **↺ Reset scenario**
- [ ] Type the **Operations Controller** name, press Enter
- [ ] Input: **Disruption alert**; AI provider: **Auto**
- [ ] **Do not run the AI beforehand** (uses free quota; quotas ran out after several runs in testing)
- [ ] If you must test, do one run well in advance, then Reset
- [ ] Ollama installed (backup), but it is **too slow for a full run on stage** — the no-AI buttons are the offline backup
- [ ] Agree with Getaneh who clicks and who talks

---

## 9. Demo script with exact clicks (~3 min)

| # | Click | You'll see | Say |
|---|---|---|---|
| 1 Situation (20 s) | — | Banner: +75 min, 45 missed, crew 20 min, M-1001 open | "A late plane, tired crew, open inspection, 45 stranded passengers." |
| 2 Blocked (15 s) | **🛫 Propose EK-813 departure now** | 🔴 BLOCKED: crew −70 min vs 495 + M-1001 missing | "Code blocks it, not the AI. The limit is frozen in Python." |
| 3 Rollback (15 s) | **💥 Rebook batch with seat conflict** | 🟣 ROLLED BACK, "identical to before: YES", 0 / 0 | "Seat stolen mid-batch. All or nothing." (Optional: **🧳 bag-transfer failure**) |
| 4 AI run (1–2 min) | **▶ Run AeroTurn agent** | Timeline fills; vulnerable first; **10/10**; approval queue fills; options appear in 💶 | "The AI plans; code checks every step; nothing changes without me." |
| 5 Approve (30 s) | ✅ #1 CREW_SWAP → ✅ #2 GATE_CHANGE B14 → ✅ #3 DELAY 11:45 | Green cards; crew LEGAL; gate B14 | "Rules are re-checked at approval." |
| 6 Release (20 s) | **🔧 Engineer signs off M-1001** → **🛫 Propose departure** → ✅ #4 | EK-813 **RELEASED 11:45** | "Same request as step 2 — now everything passes." |
| 7 Options (20 s) | 💶 tab → **Choose option A** | A €1,500 vs B/C €400k+; ✓ Option A chosen | "Code prices, the AI recommends in its brief, and the decision is mine." |
| 8 Proof (20 s) | 🛡️ → run suite; 🔗 → simulate insider edit → Verify | **19/19**; **TAMPERING DETECTED** | "Attacked 19 ways, all blocked; history can't be rewritten." |

**Optional:** Input → **Injection attack** → ▶ Run → red *Input safety filter BLOCKED* in a second, no AI called.

**Brief requirements covered:** blocked departure (2) · atomic rollback (3) · vulnerable priority (4) · controller approves gate change (5).

---

## 10. Pitch script (~3 min, matches the demo)

**Hook.** *"10:15 at Dubai. EK-812 lands 75 minutes late. The same plane must fly to Manchester at 11:00. Its crew has 20 minutes of legal duty left. A hydraulic inspection is still open. And 45 passengers, ten of them vulnerable — a child travelling alone, a passenger on oxygen — are about to miss their connections."*

**Problem.** *"An unconstrained AI that rebooks halfway and fails leaves people with two seats or none and bags on the wrong plane. Worse, under pressure it might fly a tired crew. In aviation, 'usually right' is not enough."*

**Answer.** *"AeroTurn splits the work: the AI plans — analyses, prioritises, recommends. Code decides — every rule, every number, every database write. A human approves every schedule, gate and crew change."*

**Demo narration** — see section 9.

**Close.** *"AeroTurn doesn't ask you to trust the AI. It lets the AI do what it's good at and makes it impossible for it to do what it shouldn't. AI plans. Code enforces. Humans approve."*

### 30-second version
*"When a late plane threatens connections, crew limits and maintenance at once, controllers need speed — but an AI agent alone could double-book passengers or fly a tired crew. AeroTurn lets the AI plan while code enforces every safety rule: crew duty and maintenance as frozen Python constraints, rebookings in all-or-nothing transactions, vulnerable passengers first. Every schedule, gate and crew change waits for a human. In our demo: 38 rebooked, 10 of 10 vulnerable protected, departure at 11:45 instead of a €450,000 cancellation, and 19 of 19 attacks blocked."*

---

## 11. Judge questions and answers

**Can the AI break a rule?**
No. Rules live in `guards.py`, outside the AI, frozen, and checked again when a human approves. The AI has no approve, reject, sign-off or choose-option tool. The red team proves it (19/19).

**What if a rebooking fails halfway?**
One write gateway: `BEGIN` → apply → integrity check → `COMMIT` or `ROLLBACK`. Plus the database's own `UNIQUE` index refuses a second seat even for raw SQL. Demo: the seat-conflict and bag-failure buttons.

**What if the AI's brief is wrong?**
Next to it: "Verified from the database". Passenger messages are written by code; the AI can only add an apology (a note with facts is dropped).

**Where exactly is the line between AI and code?**
AI: which tool, which passengers in which batch, which flight, which gate to try, the wording of the brief, the recommendation. Code: legality, times, seats, costs, writes, rollback, approvals. Each timeline card shows 🤖 who chose → ⚙️ what code decided.

**Why does the AI prepare option A's steps before the controller chooses?**
So the controller can act in seconds. Nothing is applied until approved; choosing another option withdraws the pending steps automatically.

**What if the internet goes down?**
The provider chain ends at a local model (Ollama), and every safety demo works with no AI at all. (Be honest: the local model is slow on a laptop CPU.)

**What if an AI provider is rate-limited?**
It moves to the next provider automatically and continues the same conversation. The controller can press ■ Stop agent; everything already done stays done.

**Prompt injection?**
Two layers before the AI sees anything: regex patterns and a Prompt Guard model. And even if one got through, the AI still has no tool to approve anything.

**Does it only work for EK-812?**
No. Code detects any late inbound flight and finds the aircraft's next flight by registration. Tested with a second, different disruption. The what-if builder changes delay, crew, maintenance and seats live.

**How did you get 11:45?**
Critical Path Method over ten ground tasks: deboard → catering → boarding = 90 min after landing at 10:15.

**Why change the crew at all?**
They would be 70 minutes past the 13-hour legal duty limit before take-off. Fresh standby crew costs €1,500; waiting for rest costs about €400,000.

**Is the data real? Is EU261 accurate?**
Synthetic, built to match the brief exactly. Compensation follows EU261-style bands as an indicative benchmark; legal applicability depends on carrier and route.

**Is every decision logged?**
Yes — every input, AI request, code decision and human approval, with the actor. Each row is chained with SHA-256, so tampering is detected (🔗 tab).

**How would this scale to a real airline?**
The rules, gateway and approval flow stay; `db.py` would be replaced by connections to schedule, crew rostering, reservations, baggage and maintenance systems. Harder next step: many simultaneous disruptions competing for the same seats and standby crew, and a production database instead of SQLite.

**What are the limitations?**
Synthetic data, fixed scenario clock, single hub, indicative compensation, disruptions handled one at a time, free AI quotas.

---

## 12. Glossary

| Term | Plain meaning |
|---|---|
| **OCC** | Operations Control Centre — the airline team that manages disruptions |
| **Inbound / outbound** | Flight arriving at / leaving the hub |
| **Turnaround** | Ground time between landing and the next take-off |
| **Critical path** | The chain of tasks that sets the earliest departure |
| **Duty limit** | Maximum legal working hours for crew (13 h here) |
| **Standby crew** | Reserve crew on call at the airport or a hotel |
| **Mandatory sign-off** | An engineer certifies a maintenance item before flight |
| **Missed connection** | Passenger can't reach their next flight in time (needs ≥60 min) |
| **Atomic** | All or nothing: a batch fully succeeds or leaves no trace |
| **Rollback** | Undoing every change of a failed transaction |
| **Double booking** | One passenger holding two seats |
| **Orphan bag** | A bag separated from its passenger |
| **HITL** | Human in the loop — a person approves risky actions |
| **Prompt injection** | Text that tries to give the AI new orders |
| **Hash chain** | Each log row includes a fingerprint of the previous one; edits break the chain |
| **EU261** | European passenger-rights rules for delays/cancellations (used here as a cost benchmark) |
| **Knock-on** | Delay spreading to later flights (EK-814) |
| **Tail swap** | Using a different aircraft for a flight (option D) |

---

## 13. If something goes wrong on stage

| Problem | Do this |
|---|---|
| AI is slow (quota / fallback) | Keep talking through steps 2–3; approve cards as they arrive; press **■ Stop agent** if needed |
| No internet | Steps 2, 3, 6, 7 (**Show options now (no AI)**), 8 work without AI |
| Messy state | **↺ Reset scenario** (disabled while the AI runs — stop it first) |
| Approve says "Enter the Operations Controller name first" | Type the name, press Enter |
| Delay approval says "Approve gate change first" | Approve the gate change, then the delay |
| Options tab is empty | Normal before the AI run — press **Show options now (no AI)** |
| Console shows an old error after code changes | Ctrl+C in the terminal, start Streamlit again |

---

## 14. Numbers card (last-minute review)

- **75** min late · **10:15** landing · **280** transfer pax · **45** missed · **10** vulnerable
- Crew: **20** min left · needs **495** · **−70** at 11:45 · max duty **13 h**
- Turnaround: **90** min minimum · departure **11:45** · gate **B12 → B14**
- Connection minimum **60** min · EK-003 trap **55** min
- Result: **38** rebooked · **7** transfer desk · **10/10** vulnerable · **0** double bookings · **0** orphan bags
- Options: A **€1,500** · B **€403,750** · C **€450,250** · D **€5,500** · E **€442,000** · F **€1,500**
- Saved by rebooking connecting passengers: **€8,100**
- Red team **19/19** · tests **30 + 21 + 11** · **18** AI providers in the fallback chain

**Last line:** *"AI plans. Code enforces. Humans approve."*
