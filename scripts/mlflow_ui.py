"""打开 MLflow 看板：优先打开平台（小主机）上的 MLflow；连不上就在本机临时启动一个看本地 mlflow.db。"""
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.tracking import LOCAL_URI, configured_uri, remote_ok  # noqa: E402

VIEW = "/#/experiments?workflowType=machine_learning"
HINT = "提示：左上角选「Model training」→ 点实验 translation-eval → 勾选多个 run → Compare 可以画图对比。"

uri = configured_uri()
if uri and uri.startswith("http") and remote_ok(uri):
    print(f"打开平台 MLflow：{uri}")
    print(HINT)
    webbrowser.open(uri.rstrip("/") + VIEW)
    sys.exit(0)

if uri and uri.startswith("http"):
    print(f"平台 MLflow（{uri}）连不上，改为打开本地记录。")
proc = subprocess.Popen([sys.executable, "-m", "mlflow", "ui", "--backend-store-uri", LOCAL_URI,
                         "--port", "5050"], cwd=ROOT)
print("本地 MLflow 启动中 …（关闭这个窗口即可退出看板）")
for _ in range(90):
    try:
        urllib.request.urlopen("http://127.0.0.1:5050", timeout=1)
        break
    except Exception:
        time.sleep(1)
print(HINT)
webbrowser.open("http://127.0.0.1:5050" + VIEW)
proc.wait()
