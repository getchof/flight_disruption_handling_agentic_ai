"""
Airport disruption-recovery agent - Stage 2 (Streamlit UI)

RUN (PowerShell)
  pip install streamlit
  python -m streamlit run app.py

All the logic lives in aviation_agent.py. This file only draws the screen.
If the UI ever fails, the agent still works in the terminal:
  python aviation_agent.py
"""
import html

import streamlit as st

import aviation_agent as agent

st.set_page_config(page_title="Disruption Control", page_icon="✈️", layout="wide")

# One icon per tool, used in the timeline.
ICONS = {
    "get_flight_status": "🛫", "get_affected_passengers": "👥", "find_alternative_flights": "🔎",
    "rebook_passenger": "🎫", "get_baggage_status": "🧳", "reroute_baggage": "🔀",
    "check_crew_duty": "⏱️", "reassign_crew": "👨‍✈️", "notify_passengers": "📨",
    "escalate_to_duty_manager": "🚨",
}

# Extra styling on top of the theme in .streamlit/config.toml
st.markdown("""
<style>
  .banner { background: linear-gradient(90deg, #13294b, #0b1d33); border: 1px solid #24406b;
            border-left: 6px solid #f5a623; border-radius: 12px; padding: 18px 24px; margin-bottom: 18px; }
  .banner h1 { margin: 0; font-size: 1.9rem; color: #e8eef7; }
  .banner p  { margin: 4px 0 0; color: #8fa3bf; }
  .chip { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: .8rem;
          font-weight: 600; margin-right: 6px; }
  .chip.delay { background: #f5a62333; color: #f5a623; }
  .chip.ok    { background: #2ecc7133; color: #2ecc71; }
  .step { background: #13294b; border: 1px solid #24406b; border-left: 4px solid #4a90d9;
          border-radius: 10px; padding: 10px 14px; margin-bottom: 8px; }
  .step.blocked { border-left-color: #e74c3c; background: #3a1c2a; }
  .step.done    { border-left-color: #2ecc71; }
  .step .head { font-weight: 600; color: #e8eef7; }
  .step .time { float: right; color: #8fa3bf; font-size: .8rem; }
  .step .body { color: #b8c6da; font-size: .88rem; margin-top: 4px; }
  .card { background: #13294b; border: 1px solid #24406b; border-radius: 12px; padding: 14px 16px;
          margin-bottom: 10px; }
  .card .label { color: #8fa3bf; font-size: .85rem; }
  .card .value { color: #f5a623; font-size: 2rem; font-weight: 700; line-height: 1.1; }
  .badge { background: #e74c3c; color: white; border-radius: 10px; padding: 10px 14px; margin-bottom: 8px;
           font-size: .88rem; }
  .badge b { display: block; margin-bottom: 2px; }
  .final { background: #13294b; border: 1px solid #f5a623; border-radius: 12px; padding: 14px 18px; }
  .muted { color: #8fa3bf; }
</style>
""", unsafe_allow_html=True)


# ---------- Small helpers that turn log entries into HTML ----------
def esc(x):
    return html.escape(str(x))


def describe(entry):
    """Turn one log entry into (title, detail) in plain words."""
    tool, args, out = entry["tool"], entry["inputs"], entry["result"]
    title = tool.replace("_", " ").capitalize()
    pid = args.get("passenger_id")
    if pid in agent.PASSENGERS:
        title += f" · {agent.PASSENGERS[pid]['name']}"
    elif "bag_tag" in args:
        title += f" · {args['bag_tag']}"
    elif "flight_number" in args:
        title += f" · {args['flight_number']}"

    if entry["blocked"]:
        detail = f"⛔ Blocked by rule: {out['rule']}"
    elif "error" in out:
        detail = f"⚠️ {out['error']}"
    elif tool == "rebook_passenger":
        detail = f"Rebooked onto {out['new_flight']} (departs {out['departure']})"
    elif tool == "reroute_baggage":
        detail = f"Bag sent to {out['new_flight']}"
    elif tool == "reassign_crew":
        detail = f"{out['removed']} replaced by {out['added']}"
    elif tool == "notify_passengers":
        detail = f"Sent to {out['count']} passenger(s): “{args.get('message', '')}”"
    elif tool == "escalate_to_duty_manager":
        detail = f"{args.get('subject', '')} — {args.get('details', '')}"
    elif tool == "get_affected_passengers":
        missed = sum(p["misses_connection"] for p in out["passengers"])
        detail = f"{len(out['passengers'])} passengers, {missed} miss their connection"
    elif tool == "check_crew_duty":
        illegal = [c["name"] for c in out["crew"] if not c["legal"]]
        detail = f"Needs {out['hours_needed']}h. " + (f"Over limit: {', '.join(illegal)}" if illegal else "All crew legal")
    elif tool == "find_alternative_flights":
        detail = "; ".join(f"{o['flight']}: {o['connection']}, {o['seats_left']} seats" for o in out["options"])
    elif tool == "get_flight_status":
        detail = f"{out['origin']} → {out['destination']}, delay {out['delay_min']} min, lands {out['estimated_arrival']}"
    else:
        detail = out.get("status", "done")
    return title, detail


def timeline_html(log):
    if not log:
        return "<p class='muted'>Pick a scenario and press <b>Run agent</b>. Steps will appear here live.</p>"
    rows = []
    for e in reversed(log):  # newest first, so the latest step is always visible
        title, detail = describe(e)
        is_action = e["tool"] in ("rebook_passenger", "reroute_baggage", "reassign_crew",
                                  "notify_passengers", "escalate_to_duty_manager")
        cls = "blocked" if e["blocked"] else ("done" if is_action and "error" not in e["result"] else "")
        rows.append(f"<div class='step {cls}'><span class='time'>{esc(e['time'])}</span>"
                    f"<div class='head'>{ICONS.get(e['tool'], '•')} {esc(title)}</div>"
                    f"<div class='body'>{esc(detail)}</div></div>")
    return "".join(rows)


def status_html(summary):
    models = summary.get("models_used") or []
    model_line = ""
    if models:
        chips = " → ".join(f"<span class='chip {'delay' if m.startswith('local') else 'ok'}'>"
                           f"{'💻' if m.startswith('local') else '☁️'} {esc(m)}</span>" for m in models)
        model_line = f"<div class='card'><div class='label'>🤖 Model</div><div style='margin-top:6px'>{chips}</div></div>"
    cards = [("🎫 Passengers rebooked", summary["passengers_rebooked"]),
             ("🧳 Bags rerouted", summary["bags_rerouted"]),
             ("👨‍✈️ Crew changes", summary["crew_changes"]),
             ("🚨 Escalations", summary["escalations"])]
    out = model_line + "".join(f"<div class='card'><div class='label'>{label}</div><div class='value'>{n}</div></div>"
                               for label, n in cards)
    for e in summary["blocked"]:
        title, _ = describe(e)
        out += f"<div class='badge'><b>⛔ BLOCKED BY RULE</b>{esc(title)}: {esc(e['result']['rule'])}</div>"
    return out


EMPTY_SUMMARY = {"passengers_rebooked": 0, "bags_rerouted": 0, "crew_changes": 0,
                 "escalations": 0, "blocked": [], "models_used": []}

# ---------- Page layout ----------
f = agent.BASE_FLIGHTS["EK-101"]
st.markdown(f"""
<div class="banner">
  <h1>✈️ Disruption Recovery Control</h1>
  <p>AI agent for airport operations · rebooking, baggage, crew duty, notifications, escalation</p>
  <div style="margin-top:10px">
    <span class="chip delay">EK-101 {f['origin']} → {f['destination']} · DELAYED +{f['delay_min']} min</span>
    <span class="chip ok">Rules enforced in code</span>
  </div>
</div>""", unsafe_allow_html=True)

left, center, right = st.columns([1, 2.2, 1.1], gap="large")

with left:
    st.subheader("🛠️ Disruption")
    names = list(agent.SCENARIOS)
    scenario = st.selectbox("Preset scenario", names, format_func=lambda n: agent.SCENARIOS[n]["title"])
    message = st.text_area("Message to the agent (edit for a custom case)",
                           value=agent.SCENARIOS[scenario]["message"], height=140, key=f"msg_{scenario}")
    st.info(f"**Expected outcome:** {agent.SCENARIOS[scenario]['expected']}")
    local_only = st.toggle(f"💻 Local model only ({agent.OLLAMA_MODEL})",
                           help="Runs on this PC with Ollama: offline, no API quota, but slower.")
    run = st.button("▶ Run agent", type="primary", width="stretch")
    st.caption("Each run resets the mock data for the chosen scenario. "
               f"If all Gemini models fail, the local {agent.OLLAMA_MODEL} model takes over.")

with center:
    st.subheader("🕒 Live timeline")
    final_box = st.empty()
    timeline_box = st.empty()

with right:
    st.subheader("📊 Status")
    status_box = st.empty()


def draw(log, summary, final=None):
    timeline_box.markdown(timeline_html(log), unsafe_allow_html=True)
    status_box.markdown(status_html(summary), unsafe_allow_html=True)
    if final:
        final_box.markdown(f"<div class='final'><b>🧾 Agent summary</b><br>"
                           f"{esc(final).replace(chr(10), '<br>')}</div><br>", unsafe_allow_html=True)


if run:
    agent.load_scenario(scenario)
    draw([], EMPTY_SUMMARY)

    def on_step(entry):  # called by the agent after every tool call -> live update
        draw(agent.ACTION_LOG, agent.summarize_log())

    with st.spinner("Agent is working… (the local model can take a few minutes)" if local_only
                    else "Agent is working…"):
        final = agent.run_agent(message, on_step=on_step, local_only=local_only)
    st.session_state.last = {"log": list(agent.ACTION_LOG), "summary": agent.summarize_log(), "final": final}

last = st.session_state.get("last")
if last:
    draw(last["log"], last["summary"], last["final"])
else:
    draw([], EMPTY_SUMMARY)
