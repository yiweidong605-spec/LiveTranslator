import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAGS = ROOT / "infra" / "platform" / "airflow" / "dags"
for p in (ROOT, DAGS):  # repo root (app/, scripts/) and the DAG folder (lt_lib/), like Airflow does
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
