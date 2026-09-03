"""Make the agent package importable for the unit tests (no livekit needed)."""

import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"
sys.path.insert(0, str(AGENT_DIR))
