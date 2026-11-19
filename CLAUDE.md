# Refund agent (hackathon practice)

A customer-support refund agent built on the Gemini SDK (`google-genai`). Main file: `code.py`.

- Business rules are enforced in code in `enforce_rules()`, not by the LLM. Put policy changes there, and keep the `SYSTEM` prompt consistent with it.
- Never read, edit, or print `.env`, and never print or log API keys.
