import os
from pathlib import Path

TEST_ROOT = Path(__file__).parent / ".runtime"
os.environ["FINREPORT_DATABASE_URL"] = f"sqlite:///{(TEST_ROOT / 'finreport.db').as_posix()}"
os.environ["FINREPORT_STORAGE_DIR"] = str(TEST_ROOT / "storage")
os.environ["FINREPORT_AV_MODE"] = "mock"
