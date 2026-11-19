# Flight disruption agent (hackathon)

An airport disruption-recovery agent built on the Gemini SDK (`google-genai`), with local Ollama as a fallback.
Main file: `aviation_agent.py`. Streamlit UI: `app.py`. Offline tests: `python test_scenarios.py`.

- Business and safety rules are enforced in code inside the action tools (`rebook_passenger`, `reroute_baggage`, `reassign_crew`, `notify_passengers`), not by the LLM. Put rule changes there, and keep the `SYSTEM` prompt consistent with them.
- Never read, edit, or print `.env`, and never print or log API keys.
