import pathlib
import sys

# Make the repo-root modules (qscreen_ingest, qscreen_app) importable from tests/.
sys.path.insert(0, str(pathlib.Path(__file__).parent.resolve()))
