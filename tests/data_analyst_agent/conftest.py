import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"

# The agent dir has a top-level `utils` package; evict any cached version
# so this test session imports the data_analyst_agent one (was relevant
# when this lived alongside other agents in the comparison repo).
for mod in [m for m in sys.modules if m == "utils" or m.startswith("utils.")]:
    del sys.modules[mod]

sys.path[:] = [p for p in sys.path if p != str(AGENT_ROOT)]
sys.path.insert(0, str(AGENT_ROOT))
