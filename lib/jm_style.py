"""画风分析：从图里算出一组可比的数值特征。

为什么不用首尾页：
  第一页常常是封面，最后一页常常是广告 / 汉化组招募 / 版权页。
  实测第一本「子宮脫手淫直播」首尾抓到的 sat=0.000、gray=180 —— 那是白底黑字，
  根本不是漫画内容。拿它算画风，算出来的是"这个作者喜欢留白"。

所以采样规则是：**跳过首 3 页和尾 3 页，剩下从中间均匀取**。
页数太少（<=8）的本子就全取，那种本子本来也没什么广告。

另外再加一道过滤：算完发现一页既没饱和、边缘又极低的（白底 / 纯色底），
直接丢掉，不参与统计。两道防线总比一道强。
"""

import random
from pathlib import Path

IMG_SUFFIX = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# 采样张数。多了没用 —— 同一本画风高度一致，8 张的中位数已经稳得像 80 张。
SAMPLE_PER_BOOK = 8
HEAD_SKIP = 3
TAIL_SKIP = 3


def _load_np():
    import numpy as np

    return np


def pick_pages(files, count=SAMPLE_PER_BOOK):
    """
    选要分析哪几页。

    规则（按优先级）：
      1. 页数 <= 8：全要，没什么可挑的；
      2. 否则砍掉首 3 尾 3，在剩下的里**等距**取 count 张。

    为什么等距而不是随机：同样的目录跑两次要给出同样的结论，
    随机会让报告每次都不一样（用户会以为出 bug 了）。
    """
    items = sorted(files)
    n = len(items)
    if n == 0:
        return []
    if n <= HEAD_SKIP + TAIL_SKIP + 2:
        return items

    body = items[HEAD_SKIP:n - TAIL_SKIP]
    if len(body) <= count:
        return body
    step = len(body) / float(count)
    picked = []
    for i in range(count):
        idx = int(round(i * step + step / 2.0))
        if idx >= len(body):
            idx = len(body) - 1
        picked.append(body[idx])
    # 去重但保序
    seen = set()
    out = []
    for p in picked:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def features_of(path: Path) -> dict:
    """
    单张图的画风指标。全都在 0~1 或小整数区间，方便直接比较。

    每个指标对应什么观感：
      sat    饱和度   —— 全彩 vs 黑白/淡彩
      light  亮度     —— 明亮清新 vs 暗调阴郁
      contrast 对比度 —— 硬光硬影 vs 平涂
      edge   边缘密度 —— 线稿繁复程度（越高越细密）
      skin   肤色占比 —— 肉体特写画面占比
      warm   暖色倾向 —— 粉/橙 vs 冷蓝
    """
    from PIL import Image

    np = _load_np()
    with Image.open(path) as im:
        im = im.convert("RGB")
        # 统一缩到 480 以内：算特征不需要原分辨率，缩了快十倍，结果几乎不变
        im.thumbnail((480, 480))
        a = np.asarray(im, dtype=np.float32)

    r = a[:, :, 0]
    g = a[:, :, 1]
    b = a[:, :, 2]
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1.0), 0.0)
    gray = a @ np.array([0.299, 0.587, 0.114], dtype=np.float32)

    gx = np.abs(np.diff(gray, axis=1)).mean() if gray.shape[1] > 1 else 0.0
    gy = np.abs(np.diff(gray, axis=0)).mean() if gray.shape[0] > 1 else 0.0

    # 经典皮肤色判定（Kovac 那套简化版），够用且不依赖任何模型
    skin_mask = (r > 95) & (g > 40) & (b > 20) & (r > g) & (r > b) & ((r - g) > 15)

    return {
        "sat": float(sat.mean()),
        "light": float(gray.mean() / 255.0),
        "contrast": float(gray.std() / 128.0),
        "edge": float((gx + gy) / 2.0 / 64.0),
        "skin": float(skin_mask.mean()),
        "warm": float((r.mean() - b.mean()) / 255.0),
    }


def _is_blank_or_ad(f: dict) -> bool:
    """
    白底 / 纯色底（广告页、扉页、空白页）。

    判据来自实测：真正的漫画页边缘密度普遍 > 0.03；
    白底黑字的广告页 sat 接近 0 且 edge 很低。两个都低才算，避免误杀线稿页。
    """
    return f["sat"] < 0.04 and f["edge"] < 0.03


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return float((vals[mid - 1] + vals[mid]) / 2.0)


def book_style(folder: Path, count: int = SAMPLE_PER_BOOK) -> dict:
    """
    一本的画风 = 每个指标取**中位数**。

    为什么是中位数不是平均：广告页、跨页大图、纯黑页都是离群值，
    平均会被它们拽跑；中位数只看"典型的那张长什么样"。
    """
    if not folder.is_dir():
        return {}
    files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in IMG_SUFFIX]
    if not files:
        return {}

    picked = pick_pages(files, count)
    rows = []
    skipped = 0
    for f in picked:
        try:
            feat = features_of(f)
        except Exception:
            skipped += 1
            continue
        if _is_blank_or_ad(feat):
            skipped += 1
            continue
        rows.append(feat)

    if not rows:
        return {}

    keys = ("sat", "light", "contrast", "edge", "skin", "warm")
    out = {k: round(_median([r[k] for r in rows]), 4) for k in keys}
    out["sampled"] = len(rows)
    out["skipped"] = skipped
    out["total"] = len(files)
    return out


def describe(style: dict) -> list:
    """把数值翻成人话。报告里直接读这几句就够，不用自己看小数。"""
    if not style:
        return []
    out = []
    sat = style.get("sat", 0)
    if sat < 0.08:
        out.append("黑白为主（几乎没有彩色）")
    elif sat < 0.2:
        out.append("淡彩 / 低饱和")
    elif sat < 0.4:
        out.append("中等饱和，偏写实上色")
    else:
        out.append("高饱和浓彩")

    light = style.get("light", 0)
    if light < 0.35:
        out.append("整体偏暗调")
    elif light < 0.55:
        out.append("明暗适中")
    else:
        out.append("明亮通透")

    edge = style.get("edge", 0)
    if edge < 0.05:
        out.append("线条极简 / 大色块")
    elif edge < 0.12:
        out.append("线条干净")
    elif edge < 0.25:
        out.append("线条较密")
    else:
        out.append("线条非常繁复")

    skin = style.get("skin", 0)
    if skin > 0.3:
        out.append("肉体特写占比很高")
    elif skin > 0.15:
        out.append("常见肌肤描写")
    else:
        out.append("肌肤画面较少 / 偏剧情")

    warm = style.get("warm", 0)
    if warm > 0.06:
        out.append("暖色调（粉、橙）")
    elif warm < -0.03:
        out.append("冷色调（蓝、青）")
    return out


def distance(a: dict, b: dict) -> float:
    """
    两本画风的距离（0 = 完全一样）。

    各指标量纲差很多（sat 0~1，edge 通常 0~0.3），
    所以先按各自的经验尺度归一，再等权求和 —— 权重要靠数据调，现在样本太少不猜。
    """
    if not a or not b:
        return 99.0
    scale = {"sat": 0.3, "light": 0.3, "contrast": 0.4, "edge": 0.2, "skin": 0.3, "warm": 0.15}
    total = 0.0
    for k, s in scale.items():
        total += abs(float(a.get(k, 0)) - float(b.get(k, 0))) / s
    return round(total, 3)


def aggregate(styles: list) -> dict:
    """把一个目录里所有本的画风合成一个基准画像。"""
    styles = [s for s in styles if s]
    if not styles:
        return {}
    keys = ("sat", "light", "contrast", "edge", "skin", "warm")
    out = {k: round(_median([s.get(k, 0) for s in styles]), 4) for k in keys}
    out["books"] = len(styles)
    return out
