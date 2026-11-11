"""安装向导：检测显卡 → 安装 GPU 支持 → 下载离线模型 → 配置 Ollama 翻译模型。
由 setup.bat 调用；可以重复运行（已下载的会跳过）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MODELS = ROOT / "models"
CONFIG = ROOT / "config.yaml"


def title(s):
    print("\n" + "=" * 60 + f"\n  {s}\n" + "=" * 60, flush=True)


def ask(prompt: str, default: str = "") -> str:
    try:
        ans = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        ans = ""
    return ans or default


def pip(*args):
    subprocess.check_call([sys.executable, "-m", "pip", "install", *args])


def set_config(key_path: str, value: str):
    """只替换 config.yaml 里对应的一行，保留注释。"""
    section, key = key_path.split(".")
    lines = CONFIG.read_text(encoding="utf-8").splitlines(keepends=True)
    in_sec = False
    for i, line in enumerate(lines):
        if re.match(rf"^{section}:\s*(#.*)?$", line):
            in_sec = True
            continue
        if in_sec and re.match(r"^\S", line):
            break
        m = re.match(rf"^(\s+{key}:\s*)(\S+)(.*)$", line.rstrip("\n"))
        if in_sec and m:
            lines[i] = f"{m.group(1)}{value}{m.group(3)}\n"
            break
    CONFIG.write_text("".join(lines), encoding="utf-8")


# ----------------------------------------------------------------------------
def step_gpu():
    title("第 1 步：检测显卡")
    from app.asr import gpu_info

    gi = gpu_info()
    if not gi:
        print("没有检测到 NVIDIA 显卡，将使用 CPU 模式（能用，但识别和翻译会慢一些）。")
        return None
    name, mem = gi
    print(f"检测到显卡：{name}，显存 {mem/1024:.1f} GB")
    print("安装 GPU 加速所需的 CUDA 运行库（约 1GB，只需一次）…")
    try:
        pip("nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*")
    except subprocess.CalledProcessError:
        print("⚠ CUDA 运行库安装失败，程序会自动退回 CPU 模式。")
    return gi


def step_whisper(gi):
    title("第 2 步：下载语音识别模型（faster-whisper）")
    from faster_whisper import download_model

    names = ["large-v3-turbo", "small"] if gi else ["small"]
    for name in names:
        out = MODELS / "whisper" / name
        if (out / "model.bin").exists():
            print(f"✓ {name} 已存在，跳过")
            continue
        size = {"large-v3-turbo": "1.6GB", "small": "480MB"}.get(name, "")
        print(f"下载 {name}（{size}）…")
        for attempt in range(3):
            try:
                download_model(name, output_dir=str(out))
                print(f"✓ {name} 下载完成")
                break
            except Exception as e:
                print(f"  下载失败（{e}），重试 {attempt + 1}/3 …")
                time.sleep(3)
        else:
            print(f"⚠ {name} 下载失败。检查网络后重新运行 setup.bat。")


def step_piper():
    title("第 3 步：下载朗读语音（Piper）")
    import yaml

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    voices = cfg.get("tts", {}).get("voices", {})
    out = MODELS / "piper"
    out.mkdir(parents=True, exist_ok=True)
    try:
        from piper.download_voices import download_voice
    except Exception as e:
        print(f"⚠ Piper 不可用（{e}），将使用 Windows 自带语音。")
        return
    for name in voices.values():
        if (out / f"{name}.onnx").exists():
            print(f"✓ {name} 已存在，跳过")
            continue
        print(f"下载 {name} …")
        try:
            download_voice(name, out)
            print(f"✓ {name}")
        except Exception as e:
            print(f"⚠ {name} 下载失败：{e}")


from app.paths import CACHE, OLLAMA_DIR, OLLAMA_MODELS, find_ollama, ollama_env  # noqa: E402

OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"


def install_ollama() -> str | None:
    """下载 Ollama 安装包，并安装到本文件夹的 ollama 子目录（不装到 C 盘）。"""
    import urllib.request

    CACHE.mkdir(parents=True, exist_ok=True)
    setup = CACHE / "OllamaSetup.exe"
    if not setup.exists() or setup.stat().st_size < 10_000_000:
        print(f"下载 Ollama 安装包（约 1GB）→ {setup}")
        try:
            def hook(n, bs, total):
                if total > 0 and n % 200 == 0:
                    print(f"  {min(100, n * bs * 100 // total)}%", flush=True)
            urllib.request.urlretrieve(OLLAMA_SETUP_URL, setup, hook)
        except Exception as e:
            print(f"⚠ 自动下载失败（{e}）")
            return None
    print(f"运行安装程序，安装到 {OLLAMA_DIR}（安装窗口里一路点下一步即可）…")
    subprocess.call([str(setup), f"/DIR={OLLAMA_DIR}"])
    return find_ollama()


def ollama_running() -> bool:
    import requests

    try:
        requests.get("http://127.0.0.1:11434/api/tags", timeout=2)
        return True
    except Exception:
        return False


def step_ollama(gi):
    title("第 4 步：本地翻译大模型（Ollama）")
    # 让 Ollama 把模型存到本文件夹，而不是 C:\Users\你\.ollama
    OLLAMA_MODELS.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":  # setx 写入用户环境变量，托盘里的 Ollama 重启后也会用这个目录
        subprocess.call(["setx", "OLLAMA_MODELS", str(OLLAMA_MODELS)], stdout=subprocess.DEVNULL)
        print(f"已设置环境变量 OLLAMA_MODELS = {OLLAMA_MODELS}")
    os.environ["OLLAMA_MODELS"] = str(OLLAMA_MODELS)

    exe = find_ollama()
    if not exe:
        exe = install_ollama()
    elif ollama_running():
        print("检测到 Ollama 已经在运行。为了让它使用新的模型目录，")
        print("请在屏幕右下角托盘里右键 Ollama 图标 → Quit Ollama，然后按回车。")
        ask("退出后按回车", "")
    while not exe:
        print("还没有安装 Ollama（运行本地大模型的免费软件）。")
        print("我帮你打开下载页面：下载 Windows 版 → 安装 → 安装完回到这里按回车。")
        webbrowser.open("https://ollama.com/download/windows")
        a = ask("装好了按回车（输入 s 跳过这一步）", "")
        if a.lower() == "s":
            print("已跳过。之后可以重新运行 setup.bat 完成这一步。")
            return
        exe = find_ollama()

    if not ollama_running():
        print("启动 Ollama 服务…")
        subprocess.Popen([exe, "serve"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         env=ollama_env())
        for _ in range(20):
            if ollama_running():
                break
            time.sleep(1)

    mem = gi[1] if gi else 0
    if mem >= 7500:
        rec, why = "qwen2.5:7b", "显存充足，用 7B 模型，翻译质量更好（约 4.7GB）"
    elif mem >= 4000:
        rec, why = "qwen2.5:3b", "显存中等，用 3B 模型，速度快（约 1.9GB）"
    else:
        rec, why = "qwen2.5:3b", "CPU 模式，用 3B 模型（每句约 2~5 秒）"
    print(f"\n推荐：{rec}  — {why}")
    print("  1) 用推荐模型")
    print("  2) 腾讯混元翻译专用模型 HY-MT1.5-7B（专门做翻译，约 4.6GB，需要 8GB 以上显存）")
    print("  3) 自己输入 Ollama 模型名")
    c = ask("请选择", "1")
    model = rec
    if c == "2":
        model = "hf.co/tencent/HY-MT1.5-7B-GGUF:Q4_K_M"
    elif c == "3":
        model = ask("模型名", rec)
    print(f"下载 {model}（只需一次，之后离线可用）…")
    r = subprocess.call([exe, "pull", model], env=ollama_env())
    if r != 0:
        print(f"⚠ 下载失败，可以稍后在命令行运行：ollama pull {model}")
        return
    set_config("translator.model", model)
    print(f"✓ 已把 config.yaml 的翻译模型设为 {model}")


def main():
    print("\n  实时翻译助手 · 安装向导\n  所有模型下载到本地后，使用时完全不需要联网。")
    gi = step_gpu()
    step_whisper(gi)
    step_piper()
    step_ollama(gi)
    title("安装完成 🎉")
    print("双击 run.bat 启动。第一次启动加载模型需要 10~30 秒。")


if __name__ == "__main__":
    main()
