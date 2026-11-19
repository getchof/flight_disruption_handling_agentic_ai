# Refund agent (hackathon practice)

A customer-support refund agent built on the Gemini SDK (`google-genai`). Main file: `code.py`.

- Business rules are enforced in code in `enforce_rules()`, not by the LLM. Put policy changes there, and keep the `SYSTEM` prompt consistent with it.
- Never read, edit, or print `.env`, and never print or log API keys.

## AeroTurn (hackathon project, Track 4 Aviation) - folder `aeroturn/`
- Rules in `guards.py` (pure Python), SQLite data in `db.py`, services + human actions in `tools.py`,
  LLM loop + provider fallback chain in `agent.py`, console in `app.py`.
- Run from `aeroturn/`: `python test_guards.py`, `python test_tools.py`, `python redteam.py` (all free, no AI),
  `python agent.py [--provider X | --local]`, `python -m streamlit run app.py`.
- The AI must never get approve/reject/sign-off/select-plan as tools (human-only actions).
