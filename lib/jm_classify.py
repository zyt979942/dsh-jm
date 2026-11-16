"""分层画风分类：技法（互斥）+ 内容（可叠加）。

为什么单独一个文件、而且必须分两层：

  最初我把「黑白线稿」「高密线稿」「局部特写」平铺在一条 if 链里，
  结果 44% 的样本被贴上 2~3 个标签，其中一半是自相矛盾的
  （黑白线稿 x 高密线稿），另一半是本该并存的（淡彩 x 局部特写）。
  两类错误混在一起，用户看到的就是"分类乱了"。

  拆开之后：
    · 技法 = 画法（线条 / 用色 / 明暗），一批只能有一个，或者标成"混合"
    · 内容 = 画面有什么（特写 / 全景），和画法无关，可以没有也可以并列

  再加一层混合检测：离第一名和第二名都很近（差 < 25%）时不硬塞，
  直接标 "A+B"。诚实说"它两边都沾"比强行归类有用。
"""

TECH_CENTERS = {
    "黑白线稿":   dict(sat=0.02, light=0.72, contrast=0.55, edge=0.22, skin=0.02, warm=0.00),
    "淡彩赛璐璐": dict(sat=0.14, light=0.68, contrast=0.35, edge=0.10, skin=0.25, warm=0.06),
    "浓彩厚涂":   dict(sat=0.35, light=0.55, contrast=0.50, edge=0.12, skin=0.30, warm=0.10),
    "暗调写实":   dict(sat=0.18, light=0.35, contrast=0.55, edge=0.15, skin=0.25, warm=0.02),
    "高密线稿":   dict(sat=0.12, light=0.65, contrast=0.45, edge=0.30, skin=0.15, warm=0.03),
    # ⚠️「手办写实/3D」**故意不在这张表里**。
    #
    #   我一开始给它也写了中心值（edge=0.05, skin=0.38），结果它把所有
    #   "线细 + 肤色占比高"的样本都吸走了 —— 实测 14 本里有 6 本被判成手办，
    #   连《鈴蘭的催眠調教》(edge 0.106) 这种明显是 AI 绘图的也算进来。
    #   根因是 0.38 的肤色中心太有吸引力，而手办真正的特征是"没有描边"这**一个**，
    #   不该拿其他维度去拉票。
    #
    #   所以它只走下面的硬规则：线密度 < 0.075 且有色且明亮。
    #   宁可漏判（也不过是归到淡彩里），不要误判 —— 误判会让人觉得分类很蠢。
}

_AXIS_SCALE = dict(sat=0.3, light=0.3, contrast=0.4, edge=0.2, skin=0.3, warm=0.15)
MIX_RATIO = 1.25


def classify_tech(style: dict) -> tuple:
    """技法分类，返回 (label, is_mixed)。label 可能是 A，也可能是 A+B。"""
    if not style:
        return "未知", False

    # 硬规则：手办 / 3D 渲染。
    #
    # 判据是「几乎没有描边」（edge < 0.075 = 实测手办 0.057，而最细的漫画
    # 也要 0.084）+ 有色 + 明亮。三个条件一起才成立：
    #   · 只有 edge 低 → 黑白线稿的空白页也会中招
    #   · 只有 sat 高 → 彩色漫画全是，等于没筛
    # 这是唯一不参与距离竞争的类，理由见 TECH_CENTERS 上的注释。
    if (style.get("edge", 1) < 0.075
            and style.get("sat", 0) > 0.15
            and style.get("light", 0) > 0.5):
        return "手办写实/3D", False

    scored = []
    for name, center in TECH_CENTERS.items():
        d = sum(abs(style.get(k, 0) - center[k]) / _AXIS_SCALE[k] for k in _AXIS_SCALE)
        scored.append((d, name))
    scored.sort()

    d1, n1 = scored[0]
    d2, n2 = scored[1]
    if d1 > 1e-6 and (d2 / d1) < MIX_RATIO:
        return n1 + "+" + n2, True
    return n1, False


def classify_content(style: dict) -> list:
    """
    内容层，可叠加、也可以为空。

    只放和画法无关、纯描述画面构成的东西。以后加「跨页大图」「对白密集」
    之类也挂这里，别塞进技法。
    """
    if not style:
        return []
    out = []
    skin = style.get("skin", 0)
    if skin > 0.35:
        out.append("局部特写")
    elif skin < 0.10:
        out.append("剧情/全景")
    return out


def label_of(style: dict) -> dict:
    """一次给出完整标签：技法 + 内容 + 是否混合。"""
    tech, mixed = classify_tech(style)
    return {"tech": tech, "mixed": mixed, "content": classify_content(style)}


def format_label(lab: dict) -> str:
    """拼成一行给人看，例如：手办写实/3D ｜ 局部特写。"""
    parts = [lab.get("tech") or "未知"]
    if lab.get("content"):
        parts.append(" / ".join(lab["content"]))
    return " ｜ ".join(parts)


def rarity_note(tech: str, all_techs: list) -> str:
    """
    这种风格在你库里占多少。

    ⚠️ 别把"稀有"当噪声丢掉 —— 只收过一本的风格，恰恰说明那是新口味。
       报告里明确写出来，比混在一堆"淡彩赛璐璐"里有信息量得多。
    """
    if not all_techs or not tech:
        return ""
    n = sum(1 for t in all_techs if t == tech)
    total = len(all_techs)
    if total < 8:
        return ""
    if n == 1:
        return "（全库仅此一本，属于新出现的小众风格）"
    if n <= 2:
        return "（全库 " + str(n) + " 本，偏小众）"
    return ""


def summarize(styles: list) -> dict:
    """
    把一批画风聚合成分布：技法各占多少、内容各占多少。

    输入是 features 列表（每项含 sat/light/contrast/edge/skin/warm），
    返回 {tech: {count, ratio, mixed}, content: {name: count}, labels: [...]}
    """
    tech_count = {}
    content_count = {}
    labels = []
    mixed_n = 0

    for s in styles:
        if not s:
            continue
        lab = label_of(s)
        labels.append(lab)
        if lab["mixed"]:
            mixed_n += 1
        tech_count[lab["tech"]] = tech_count.get(lab["tech"], 0) + 1
        for c in lab["content"]:
            content_count[c] = content_count.get(c, 0) + 1

    total = len(labels) or 1
    techs = [
        {"tech": k, "count": v, "ratio": round(v / float(total), 3)}
        for k, v in sorted(tech_count.items(), key=lambda kv: -kv[1])
    ]
    contents = [
        {"content": k, "count": v, "ratio": round(v / float(total), 3)}
        for k, v in sorted(content_count.items(), key=lambda kv: -kv[1])
    ]
    return {
        "total": len(labels),
        "mixed": mixed_n,
        "tech": techs,
        "content": contents,
        "labels": labels,
    }
