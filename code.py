"""
Refund agent - Gemini version (free tier) - TechX Dubai 2026 practice

SETUP (PowerShell)
  pip install google-genai python-dotenv
  Put GEMINI_API_KEY=your-key in a .env file in the same folder
  python refund_agent_gemini.py

Get a free key at aistudio.google.com. If MODEL errors, copy the current
free model name from AI Studio.
"""
import functools
from datetime import date

import time

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()  # loads GEMINI_API_KEY from the .env file

client = genai.Client()  # reads GEMINI_API_KEY from the environment
# Tried in order. If one is overloaded (503) or not found (404), the next is used.
# Copy exact IDs from the model cards in AI Studio if any name gives a 404.
MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
]
TODAY = date(2026, 11, 20)

# ---------- Business rules (constants so they are easy to change) ----------
REFUND_WINDOW_DAYS = 14
AUTO_LIMIT = 500
VIP_LIMIT = 1000

# ---------- Mock "APIs" (replace with real ones later) ----------
ORDERS = {
    "A123": {"customer_id": "C1", "amount": 300, "delivered": "2026-11-15"},
    "B456": {"customer_id": "C2", "amount": 800, "delivered": "2026-11-18"},
    "C789": {"customer_id": "C1", "amount": 200, "delivered": "2026-10-01"},
    "D111": {"customer_id": "C3", "amount": 900, "delivered": "2026-11-17"},
}
CUSTOMERS = {
    "C1": {"name": "Sara", "email": "sara@example.com", "vip": False},
    "C2": {"name": "Omar", "email": "omar@example.com", "vip": False},
    "C3": {"name": "Lina", "email": "lina@example.com", "vip": True},
}
REFUNDED = set()  # order IDs already refunded, so a retry can't refund twice
EMAILED_THIS_REQUEST = set()  # addresses emailed during the current request (reset per request)


# ---------- Rule enforcement in CODE: the LLM cannot bypass this ----------
def enforce_rules(order_id, amount):
    """Return (allowed, reason)."""
    order = ORDERS.get(order_id)
    if not order:
        return False, f"order {order_id} not found"

    if order_id in REFUNDED:
        return False, f"order {order_id} was already refunded"

    customer = CUSTOMERS.get(order["customer_id"])
    if not customer:
        return False, "customer not found"

    if amount > order["amount"]:
        return False, f"refund {amount} is more than the order amount {order['amount']}"

    days = (TODAY - date.fromisoformat(order["delivered"])).days
    if days > REFUND_WINDOW_DAYS:
        return False, f"delivered {days} days ago, window is {REFUND_WINDOW_DAYS} days"

    limit = VIP_LIMIT if customer["vip"] else AUTO_LIMIT
    if amount > limit:
        return False, f"AED {amount} is above the AED {limit} auto-approval limit; escalate to a human"

    return True, "ok"


# ---------- Tools. Type hints + docstrings are what Gemini reads ----------
def get_order(order_id: str) -> dict:
    """Get order details (customer_id, amount, delivered date) by order ID."""
    o = ORDERS.get(order_id)
    return o if o else {"error": f"Order {order_id} not found"}


def get_customer(customer_id: str) -> dict:
    """Get customer details (name, email, vip) by customer ID."""
    return CUSTOMERS.get(customer_id, {"error": "Customer not found"})


def issue_refund(order_id: str, amount: float) -> dict:
    """Refund an order. Company policy is enforced by the system and may block it."""
    ok, reason = enforce_rules(order_id, amount)
    if not ok:
        return {"error": f"Refund blocked by policy: {reason}"}
    REFUNDED.add(order_id)
    return {"status": "refunded", "order_id": order_id, "amount": amount}


def escalate_to_human(order_id: str, summary: str) -> dict:
    """Send a case to a human agent with a short summary of the situation."""
    return {"status": "escalated", "order_id": order_id, "summary": summary}


def send_email(to: str, subject: str, body: str) -> dict:
    """Email the customer with the outcome of their request."""
    if to in EMAILED_THIS_REQUEST:  # a retry re-ran the request: don't email twice
        return {"status": "already sent", "note": "customer was already emailed for this request"}
    body = body.replace("\\n", "\n")  # some models send a literal \n instead of a line break
    print(f"\n  EMAIL to {to}\n  Subject: {subject}\n  {body}\n")
    EMAILED_THIS_REQUEST.add(to)
    return {"status": "sent"}


def logged(fn):
    """Print every tool call and result, and turn exceptions into error results."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        print(f"-> {fn.__name__}({kwargs})")
        try:
            out = fn(*args, **kwargs)
        except Exception as e:
            out = {"error": str(e)}
        print(f"<- {out}")
        return out
    return wrapper


TOOL_FUNCS = [
    logged(f)
    for f in (get_order, get_customer, issue_refund, escalate_to_human, send_email)
]

SYSTEM = f"""You are a customer support agent for an online store. Today is {TODAY}.
Handle refund requests: look up the order and customer, then refund or escalate,
and always email the customer the outcome.
Policy: refund within {REFUND_WINDOW_DAYS} days of delivery; up to AED {AUTO_LIMIT} auto-approve
(VIP up to AED {VIP_LIMIT}); above that, escalate to a human instead of refunding.
If a refund is blocked for being over the limit, escalate to a human. If it is blocked
for being outside the window, politely decline. If it is blocked because the order was
already refunded, do not refund again; email the customer that it was already refunded. If the order is not found, email the
customer asking for the correct order ID (if you don't have their email, say so in your reply).
Only use the tools provided. Never invent order data."""


TOOLS_BY_NAME = {f.__name__: f for f in TOOL_FUNCS}
MAX_STEPS = 10  # max model calls per request, so a confused model can't loop forever

CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM,
    tools=TOOL_FUNCS,
    # We run the tool loop ourselves (below), so the conversation history survives
    # a failed API call and the next model continues instead of starting over.
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
)


def generate_with_fallback(contents, state, retries_per_model=2):
    """One model call. On errors, retry, then move on to the next model.
    state["model_index"] remembers which model works, so later steps start there."""
    while state["model_index"] < len(MODELS):
        model = MODELS[state["model_index"]]
        for attempt in range(1, retries_per_model + 1):
            try:
                return client.models.generate_content(model=model, contents=contents, config=CONFIG)
            except Exception as e:
                msg = str(e).replace("\n", " ")[:150]
                print(f"  ({model} attempt {attempt}: {type(e).__name__}: {msg})")
                # 404 = wrong/unavailable model name, 429 = quota used up.
                # Retrying the same model won't help, so skip to the next one.
                if "404" in msg or "429" in msg:
                    break
                time.sleep(3 * attempt)
        state["model_index"] += 1
    return None


def run_agent(customer_message):
    EMAILED_THIS_REQUEST.clear()  # new customer request: allow one email again
    contents = [types.Content(role="user", parts=[types.Part(text=customer_message)])]
    state = {"model_index": 0}

    for _ in range(MAX_STEPS):
        resp = generate_with_fallback(contents, state)
        if resp is None:
            return "Failed: all models unavailable. Wait a few minutes and try again."

        contents.append(resp.candidates[0].content)  # keep the model's turn in the history
        calls = resp.function_calls
        if not calls:
            return resp.text  # no more tools to call: this is the final answer

        # Run each requested tool and add its result to the history.
        results = []
        for call in calls:
            fn = TOOLS_BY_NAME.get(call.name)
            out = fn(**dict(call.args or {})) if fn else {"error": f"unknown tool {call.name}"}
            results.append(types.Part.from_function_response(name=call.name, response=out))
        contents.append(types.Content(role="user", parts=results))

    return "Stopped: too many steps without a final answer."


if __name__ == "__main__":
    tests = [
        "Hi, order A123 arrived broken. Refund please.",       # refund 300, email
        "Order B456 is faulty, I want my 800 back.",           # escalate (over 500, not VIP)
        "Please refund order C789, I don't like it.",          # decline (older than 14 days)
        "Order D111 is the wrong size, refund me. - Lina",     # refund 900 (VIP)
        "Refund order Z999 please.",                           # not found, ask for ID
    ]
    for t in tests:
        print("=" * 60, "\nCUSTOMER:", t)
        print("AGENT:", run_agent(t))