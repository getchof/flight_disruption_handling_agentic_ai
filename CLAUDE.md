# AeroTurn (hackathon project, Track 4 Aviation & Airport Operations) - folder `aeroturn/`

Airline OCC disruption-recovery agent: the LLM plans, code decides, a human Operations Controller approves.

- Rules in `guards.py` (pure Python, frozen limits), SQLite data in `db.py`, services + human actions in `tools.py`,
  pricing in `costs.py`, LLM loop + provider fallback chain in `agent.py`, console in `app.py`.
- Every write goes through `tools.write_gateway()` (BEGIN / integrity check / COMMIT or ROLLBACK, audited).
- Works for any late inbound flight: `tools.disruptions()` finds them; the next flight comes from the aircraft registration.
- Run from `aeroturn/`: `python test_guards.py`, `python test_tools.py`, `python test_whatif.py`, `python redteam.py`
  (all free, no AI), `python agent.py [--provider X | --local | --flight ID]`, `python -m streamlit run app.py`.
- The AI must never get approve/reject/sign-off/select-plan as tools (human-only actions).
- Never read, edit, or print `.env`, and never print or log API keys.
