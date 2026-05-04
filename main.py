import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stt_app.local_model_scan import LOCAL_MODEL_SCAN_WORKER_ARG  # noqa: E402

if __name__ == "__main__" and LOCAL_MODEL_SCAN_WORKER_ARG in sys.argv[1:]:
    from stt_app.local_model_scan_worker import main as run_local_model_scan_worker

    worker_args = [
        arg for arg in sys.argv[1:] if arg != LOCAL_MODEL_SCAN_WORKER_ARG
    ]
    raise SystemExit(run_local_model_scan_worker(worker_args))

from stt_app.main import run  # noqa: E402 - import requires adjusted sys.path


if __name__ == "__main__":
    raise SystemExit(run())
