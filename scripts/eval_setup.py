"""评测环境准备：装评测依赖 → （可选）装 COMET → 下载候选翻译模型。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app.paths import find_ollama, ollama_env  # noqa: E402

SIZES = {  # Ollama 默认量化（Q4_K_M 左右）的大致体积
    "qwen2.5:7b": "4.7GB", "qwen2.5:14b": "9.0GB", "qwen3:8b": "5.2GB", "qwen3:14b": "9.3GB",
    "gemma3:12b": "8.1GB", "translategemma:12b": "8GB 左右", "mistral-nemo:12b": "7.1GB",
    "hf.co/tencent/HY-MT1.5-7B-GGUF:Q4_K_M": "4.6GB",
}


def ask(prompt, default=""):
    try:
        a = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        a = ""
    return a or default


def pip(*a):
    return subprocess.call([sys.executable, "-m", "pip", "install", *a])


def main():
    print("\n== 1. 安装评测工具（sacrebleu 打分、jiwer 词错误率、MLflow 实验记录）==")
    pip("-r", str(ROOT / "requirements-eval.txt"))

    print("\n== 2. COMET 神经网络打分（可选）==")
    print("COMET 比 chrF 更接近人工判断，但要装 PyTorch（约 3GB）+ 下载 2.3GB 模型。")
    if ask("现在安装 COMET 吗？(y/n)", "n").lower().startswith("y"):
        pip("torch", "--index-url", "https://download.pytorch.org/whl/cu124")
        pip("unbabel-comet")

    print("\n== 3. 下载候选翻译模型 ==")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cands = cfg.get("eval", {}).get("candidates", [])
    for i, m in enumerate(cands, 1):
        print(f"  {i}) {m:42s} {SIZES.get(m, '')}")
    sel = ask("要下载哪些？输入编号用逗号隔开，all = 全部，回车跳过", "all")
    if not sel or sel.lower() == "skip":
        return
    picked = cands if sel.lower() == "all" else [cands[int(x) - 1] for x in sel.split(",") if x.strip().isdigit()]
    exe = find_ollama()
    if not exe:
        print("⚠ 找不到 Ollama，请先运行 setup.bat")
        return
    for m in picked:
        print(f"\n下载 {m} …")
        if subprocess.call([exe, "pull", m], env=ollama_env()) != 0:
            print(f"⚠ {m} 下载失败（可能模型名在 Ollama 上不存在或网络问题），跳过")
    print("\n完成。回到菜单选 2 开始评测。")


if __name__ == "__main__":
    main()
