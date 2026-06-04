# Ensures the repository root is on sys.path so `import app` / `import db`
# work when the suite is run via the bare `pytest` console script (as CI does),
# not just `python -m pytest`.
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
