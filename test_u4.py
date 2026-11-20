"""
AeroTurn - round-2 requirement tests (U-4.1 to U-4.5). No AI involved.
Each runs the live test from livetests.py (its own scratch database) and fails with the
unmet expectations and the evidence, so a failure says exactly what broke.

  pytest test_u4.py
"""
import pytest

import livetests


@pytest.mark.parametrize("test", livetests.ALL, ids=lambda t: t.__name__)
def test_round2_requirement(test):
    res = test()
    failed = [c["expect"] for c in res["checks"] if not c["ok"]]
    evidence = "\n".join(f"  {what}: {ev}" for what, ev in res["steps"])
    assert not failed, f"{res['id']} failed: {failed}\n{evidence}"
