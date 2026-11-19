# ✈️ AeroTurn — Operations Control Centre agent

A late wide-body (EK-812, +75 min) threatens 45 connections, its crew is 20 minutes from the legal duty limit
and a hydraulic inspection is still open. AeroTurn recovers passengers, bags and crew **atomically**, enforces
crew and maintenance floors **in code**, and needs a **human controller** for every schedule, gate or crew change.

> 🤖 **AI plans** (analyses, prioritises, drafts, recommends) · ⚙️ **Code decides** (rules, maths, writes, rollback) · 👤 **Humans approve**

## Run it

```powershell
cd aeroturn
python -m venv .venv ; .venv\Scripts\activate      # optional
pip install -r requirements.txt
copy ..\.env.example ..\.env                         # then paste your own keys into ..\.env
python -m streamlit run app.py                       # console at http://localhost:8501
```

| Command | What it does | Needs AI? |
|---|---|---|
| `python test_guards.py` | 21 rule + database tests | No |
| `python test_tools.py` | 19 service, rollback and approval tests | No |
| `python redteam.py` | 17 attacks against the guards | No (Prompt Guard optional) |
| `python agent.py` | Full recovery in the terminal, full fallback chain | Yes |
| `python agent.py --local` | Same, offline on Ollama qwen3:4b | Local only |
| `python agent.py --delay 150 --crew-left 600` | What-if: a different delay / crew situation | Yes |
| `python test_whatif.py` | 10 tests: rules, costs and rollback adapt to other scenarios | No |
| `python agent.py --flight EK-812` | Recover a specific late inbound flight (default: code finds the late one) | Yes |
| `python agent.py --list-providers` | Which keys are set (never prints them) | No |

## 90-second demo script

1. **(10 s) Situation.** Point at the banner: EK-812 +75 min, crew 20 min from limit, M-1001 open. *"An unconstrained AI would rebook mid-way and double-book, or fly a tired crew."*
2. **(20 s) Run AeroTurn.** Watch the timeline: every step shows 🤖 which model chose it → ⚙️ what code decided. Vulnerable passengers are rebooked first (10/10). Changes go to the **approval queue**, not the database.
3. **(10 s) Blocked departure.** Click **🛫 Propose departure now** → red: crew duty (−70 min vs 495 needed) + M-1001 sign-off missing.
4. **(15 s) Atomic rollback.** Click **💥 Rebook batch with seat conflict** → purple ROLLED BACK, *"seats & bags identical to before: YES"*, double bookings 0, orphan bags 0.
5. **(15 s) Human approval.** Type the controller name → approve crew swap, **gate change B12→B14**, delay; engineer signs off; propose departure → now passes → approve → RELEASED.
6. **(10 s) Decision support.** Tab 💶: code-priced options — A €1,500 vs B €403,750 vs C €450,250; AI recommends A, controller clicks *Choose*.
7. **(10 s) Proof.** Tab 🛡️ → **17/17 attacks stopped**; tab 🔗 → simulate insider edit → *TAMPERING DETECTED*. Pick **Reworded injection** and Run → blocked by both layers (regex + Prompt Guard model) before any AI sees it.

8. **(bonus) What-if.** Open **🔮 What-if scenario builder** → *Long delay +150 min* → Build: 115 connections now break (Mumbai too), departure moves to 13:00, costs change. Or let a judge pick the delay.

*No internet?* Steps 3, 4, 5, 6, 7 and 8 need no AI. Step 2 runs on `Local only (Ollama)`.

## Likely judge questions

- **Where is the backend?** Python modules imported by the Streamlit app (no separate server): `tools.py` services + `write_gateway`, `guards.py` rules, SQLite via `db.py`. External calls are only the AI providers.
- **Can the AI bypass a rule?** No. Rules live in `guards.py` (frozen, no network, no LLM), run inside the tools and again on approval. The AI has no approve/sign-off tool. Proven by `redteam.py`.
- **What if a rebooking fails half-way?** `write_gateway` wraps it in `BEGIN IMMEDIATE … COMMIT/ROLLBACK`, checks integrity before commit, and the DB `UNIQUE` index refuses a second seat even for raw SQL.
- **What if the AI is wrong in its brief?** The console shows *Verified from the database* next to the AI brief; notifications use facts written by code.
- **Does it only work for this one case?** No. The what-if builder rebuilds the scenario from 4 settings (delay, crew duty left, maintenance, seat supply). Missed connections, crew legality, critical path, gates and costs are all *calculated*, so they adapt; `test_whatif.py` proves it.
- **What if every provider is down?** Fallback ends at local Ollama; the guard demos need no AI; after 3 replans or the step limit the case is handed to human review.

## Manual checklist (TechX p.33)

- [ ] Runs end to end on local Ollama (`--local`): first attempt timed out on 8 GB RAM; re-test with compact state pending · [x] Inputs validated, injections blocked (regex + Prompt Guard)
- [x] Guards in code · [x] Risky actions wait for a human · [x] One write gateway with rollback
- [x] Keys in `.env`, `.env` in `.gitignore` · [x] Multiple providers ending with Ollama
- [x] Steps, replans, retries, answer length capped · [x] Every input, tool call, decision and approver logged (hash-chained)
- [x] Timeouts + low retries; replan cap → human review · [x] Broken JSON reported back to the model
- [x] Clear tool names/descriptions, inputs checked · [x] Synthetic data with edge cases and attacks
- [x] PLAN.md · [x] Demo shows a safe case, an approval and blocked attacks
