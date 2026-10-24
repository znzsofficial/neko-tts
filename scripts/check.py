"""CI gate: real SDK, non-empty discovery, and no silently skipped tests."""

import importlib.metadata
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
print("maibot-plugin-sdk", importlib.metadata.version("maibot-plugin-sdk"), flush=True)
suite = unittest.defaultTestLoader.discover(str(ROOT), pattern="test_*.py")


def collected(group):
    for test in group:
        if isinstance(test, unittest.TestSuite):
            yield from collected(test)
        else:
            yield test.id().rsplit(".", 2)[0]


groups = set(collected(suite))
required = {
    "test_audio",
    "test_deferred_speech",
    "test_lifecycle",
    "test_focus_scope",
    "test_history_visibility",
    "test_package",
    "test_plugin",
}
if not required <= groups:
    raise SystemExit(
        "Required audio/lifecycle/package test groups missing: "
        + ", ".join(sorted(required - groups))
    )
result = unittest.TextTestRunner(verbosity=2).run(suite)
if not result.testsRun or result.skipped or not result.wasSuccessful():
    raise SystemExit("CI gate failed: empty discovery, skipped tests, or failures")
