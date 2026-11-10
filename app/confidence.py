"""置信度计算（识别和翻译共用）。"""
from __future__ import annotations

import math


def seq_confidence(probs: list[float]) -> tuple[float | None, float | None, float | None]:
    """把一串概率汇总成一个 0~1 的置信度。

    几何平均反映“整体顺不顺”，最低的那个概率反映“有没有拿不准的地方”。
    一个歧义词就能把分数明显拉低——这正是我们想要的（有歧义 → 置信度低）。
    """
    probs = [min(max(p, 1e-6), 1.0) for p in probs]
    if not probs:
        return None, None, None
    geo = math.exp(sum(math.log(p) for p in probs) / len(probs))
    mn = min(probs)
    # 公式先用经验值，等评测集数据出来后再校准（评测报告里有“置信度 vs 质量”的相关性）
    return geo ** 0.3 * mn ** 0.7, geo, mn
