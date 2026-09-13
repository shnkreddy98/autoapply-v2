import sys
from pathlib import Path

# tests import their helpers (browser.py, test_classify's fixtures) by module name
sys.path.insert(0, str(Path(__file__).parent))
