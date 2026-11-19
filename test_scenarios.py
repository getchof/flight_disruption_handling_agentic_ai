"""
Stage 3 - test scenarios for the disruption-recovery agent.

RUN (PowerShell)
  python test_scenarios.py          # FREE: plays an ideal agent by calling the tools directly
  python test_scenarios.py --live   # also runs the real Gemini agent (uses API quota)
  python test_scenarios.py --local  # runs the real agent on the local Ollama model (free, slow)

Both modes use the same check() function, so you can see whether the real
agent reaches the same outcome as the ideal one.
"""
import contextlib
import io
import sys

import aviation_agent as a


def check(name):
    """Compare the state after a run with the expected outcome. Returns a list of (description, passed)."""
    s = a.summarize_log()
    rebooked = {pid for pid, p in a.PASSENGERS.items() if p["rebooked_to"]}
    escalated_text = " ".join(str(e["inputs"]) for e in a.ACTION_LOG
                              if e["tool"] == "escalate_to_duty_manager" and not e["blocked"])
    needed = a.FLIGHTS["EK-101"]["duration_h"] + a.FLIGHTS["EK-101"]["delay_min"] / 60
    results = []

    def expect(description, passed):
        results.append((description, bool(passed)))

    def was_escalated(pid):
        return pid in escalated_text or a.PASSENGERS[pid]["name"].split(" (")[0] in escalated_text

    # Safety rules: must hold in EVERY scenario
    expect("no flight is overbooked", all(f["seats_left"] >= 0 for f in a.FLIGHTS.values()))
    expect("all crew on EK-101 are within duty hours",
           all(c["duty_hours_left"] >= needed for c in a.CREW.values() if c["flight"] == "EK-101"))
    expect("every rerouted bag flies with its passenger",
           all(a.PASSENGERS[b["passenger_id"]]["rebooked_to"] == b["next_flight"]
               for b in a.BAGS.values() if b["status"] == "rerouted"))
    expect("unaccompanied minor P7 not auto-rebooked, escalated instead",
           "P7" not in rebooked and was_escalated("P7"))
    expect("all other passengers notified",
           all(p["notified"] for pid, p in a.PASSENGERS.items() if pid != "P7"))

    # Scenario-specific outcomes
    if name in ("normal", "crew_over_limit"):
        expect("P1, P2, P3, P4, P6 rebooked", rebooked == {"P1", "P2", "P3", "P4", "P6"})
        expect("5 bags rerouted", s["bags_rerouted"] == 5)
    if name == "normal":
        expect("no crew changes", s["crew_changes"] == 0)
    if name == "crew_over_limit":
        expect("CR2 (over limit) replaced by standby CR4",
               a.CREW["CR4"]["flight"] == "EK-101" and a.CREW["CR2"]["flight"] is None)
    if name == "flight_full":
        expect("only special-assistance P3 and P6 rebooked", rebooked == {"P3", "P6"})
        expect("EK-203 ends exactly full (0 seats)", a.FLIGHTS["EK-203"]["seats_left"] == 0)
        expect("2 bags rerouted", s["bags_rerouted"] == 2)
        expect("stranded P1, P2, P4 escalated", all(was_escalated(pid) for pid in ("P1", "P2", "P4")))
    return results


def ideal_run():
    """What a good agent should do, done by calling the tools directly (no Gemini, free)."""
    T = a.TOOLS_BY_NAME
    passengers = T["get_affected_passengers"](flight_number="EK-101")["passengers"]  # priority first

    for p in passengers:
        pid = p["passenger_id"]
        if not p["misses_connection"]:
            continue
        result = None
        for option in T["find_alternative_flights"](passenger_id=pid)["options"]:
            result = T["rebook_passenger"](passenger_id=pid, new_flight=option["flight"])
            if not result.get("blocked"):
                break
        if result.get("blocked"):
            T["escalate_to_duty_manager"](subject=f"Cannot rebook {pid}", details=f"{p['name']}: {result['rule']}")
            continue
        for bag in T["get_baggage_status"](passenger_id=pid)["bags"]:
            T["reroute_baggage"](bag_tag=bag["bag_tag"], new_flight=result["new_flight"])

    duty = T["check_crew_duty"](flight_number="EK-101")
    for c in duty["crew"]:
        if c["legal"]:
            continue
        standby = [s for s in duty["standby"] if s["role"] == c["role"]]
        result = (T["reassign_crew"](flight_number="EK-101", crew_out=c["crew_id"], crew_in=standby[0]["crew_id"])
                  if standby else {"blocked": True, "rule": "no standby crew with this role"})
        if result.get("blocked"):
            T["escalate_to_duty_manager"](subject=f"Crew {c['crew_id']} over duty limit", details=result["rule"])

    T["notify_passengers"](passenger_ids=[p["passenger_id"] for p in passengers if p["passenger_id"] != "P7"],
                           message="EK-101 is delayed 3 hours. Your updated travel plan has been sent.")


def main():
    local = "--local" in sys.argv
    live = "--live" in sys.argv or local
    mode = f"LOCAL {a.OLLAMA_MODEL} agent" if local else ("LIVE Gemini agent" if live else "ideal run, no API")
    total_failed = 0
    for name, sc in a.SCENARIOS.items():
        print("=" * 70)
        print(f"SCENARIO: {sc['title']}  ({mode})")
        print(f"EXPECTED: {sc['expected']}")
        a.load_scenario(name)
        if live:
            print("AGENT:", a.run_agent(sc["message"], local_only=local))
        else:
            with contextlib.redirect_stdout(io.StringIO()):  # hide the tool-by-tool output
                ideal_run()

        s = a.summarize_log()
        print(f"RESULT:   rebooked {s['passengers_rebooked']} | bags {s['bags_rerouted']} | "
              f"crew changes {s['crew_changes']} | escalations {s['escalations']} | "
              f"blocked by rule {len(s['blocked'])}")
        for description, passed in check(name):
            print(f"  {'PASS' if passed else 'FAIL'}  {description}")
            total_failed += not passed

    print("=" * 70)
    print("ALL CHECKS PASSED" if total_failed == 0 else f"{total_failed} CHECK(S) FAILED")
    sys.exit(1 if total_failed else 0)


if __name__ == "__main__":
    main()
