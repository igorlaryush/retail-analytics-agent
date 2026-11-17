import os
import tempfile

# Must run before agent.config is imported: keep test artefacts out of ./data
os.environ["AGENT_DATA_DIR"] = tempfile.mkdtemp(prefix="agent-test-")
