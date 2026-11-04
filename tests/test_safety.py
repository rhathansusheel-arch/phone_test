"""The 7 Phase 7 safety tests (same as safety_check.py), run as part of the test suite."""
from kdp_agent import safety_tests


async def test_all_safety_rules():
    results = await safety_tests.run_all()
    failed = [(n, d) for n, ok, d in results if not ok]
    assert not failed, safety_tests.checklist(results)
