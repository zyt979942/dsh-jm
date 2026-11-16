"""个性化：库路径 / 已下载清单 / 文件夹分析。

这两件事放在一起，是因为它们共用同一份配置文件 prefs.json：
  - 第一个输入框给出库路径，分析就扫那个目录；
  - 第二个大文本框存「已经下过/看过的 jm 号」，推荐时拿来去重。
"""

import json
import re
import sys
import time
from pathlib import Path

IMG_SUFFIX = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}


def _jm():
    import jm

    return jm


def prefs_path() -> Path:
    """配置放 plugin-data 下，跟 last.json / last_pick.json 同一个窝。"""
    return _jm()._state_path().with_name("prefs.json")


DEFAULT_PREFS = {
    "libraryPath": "",
    # 精选目录：用户自己挑出来的"喜欢的那几页"复制到这个文件夹。
    # 这是画风推荐的关键 —— 全库平均出来的只是"你收藏了什么"，
    # 精选才是"你到底喜欢什么"。两者常常不一样（比如库里 70% 是短篇，
    # 但他真正喜欢的是那几张特定的构图）。
    "favoritePath": "",
    "seen": "",
    "updatedAt": "",
}


def load_prefs() -> dict:
    data = dict(DEFAULT_PREFS)
    try:
        raw = json.loads(prefs_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k in DEFAULT_PREFS:
                if k in raw and isinstance(raw[k], str):
                    data[k] = raw[k]
    except Exception:
        pass
    return data


def save_prefs(patch: dict) -> dict:
    """只接受已知字段，避免界面误传把文件写脏。"""
    data = load_prefs()
    for k in DEFAULT_PREFS:
        if k in patch and isinstance(patch[k], str):
            data[k] = patch[k]
    data["updatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    p = prefs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def parse_seen(text: str) -> set:
    """
    把用户粘进来的那一坨拆成 jm 号集合。

    用户会怎么粘都有可能：
        jm123, jm456
        123 456 789
        从别处复制的一列数字
        「[某某] 某本 1445283」这种带标题的
    所以不按行解析，直接全文抓数字，5~8 位的才算 jm 号。
    """
    out = set()
    for m in re.finditer(r"\d{5,8}", str(text or "")):
        out.add(m.group(0))
    return out


def _match_jm_id(jm, client, folder_name, title_hint=""):
    """
    给一个目录名找它的 jm 号。

    三级匹配，从最可靠到最宽松：
      1. 桌面索引（.dsh-jm-index.json）：名字对得上就直接拿；
      2. 站内按作者名搜（目录名里 [xxx] 那段）；
      3. 按标题关键词搜，取第一个高度相似的结果。

    ⚠️ 匹配的结果会**回写索引**。这不是为了好看 —— 没有 jm 号，
       "已看过清单去重"和"按 tag 推荐"都做不了，整个个性化就废了。
    """
    desk = Path.home() / "Desktop"
    try:
        import jm_pick
    except Exception:  # noqa: BLE001
        jm_pick = None

    if jm_pick is not None:
        for aid, e in jm_pick._read_index(desk).items():
            if e.get("folder") == folder_name:
                return str(aid)

    # 目录名形如 "[作者] 标题" / "[汉化组][作者] 标题"
    name = str(folder_name)
    head = re.findall(r"\[([^\]]+)\]", name)
    body = re.sub(r"\[[^\]]*\]", " ", name).strip()

    def _try_ids(ids_titles):
        # 用标题相似度挑，避免同名不同本
        best, score = None, 0.0
        for aid, t in ids_titles:
            s = difflib_ratio(t, name)
            if s > score:
                score, best = s, aid
        return best if score >= 0.55 else None

    # 2) 作者维度
    for author in reversed(head):
        author = author.strip()
        if not author or len(author) < 2:
            continue
        try:
            page = client.search_author(author, page=1)
            got = [(str(a), str(i.get("name") or "")) for a, i in list(page.content)]
        except Exception:  # noqa: BLE001
            continue
        hit = _try_ids(got)
        if hit:
            return hit

    # 3) 标题关键词（取正文前 12 个字，太长了搜不到）
    key = body[:12].strip()
    if key:
        try:
            page = client.search_site(key, page=1)
            got = [(str(a), str(t or "")) for a, t in list(page.content)]
            hit = _try_ids(got)
            if hit:
                return hit
        except Exception:  # noqa: BLE001
            pass
    return ""


def difflib_ratio(a, b):
    import difflib

    return difflib.SequenceMatcher(None, str(a), str(b)).ratio()


def cmd_complete(params: dict) -> dict:
    """
    把某个目录下的每本补全成标准 标签.txt。

    目标格式（缺什么补什么，已有的不覆盖）：
        jm: 1487216
        title: xxx
        author: xxx
        pages: 41
        dims: 1280x1807
        tags: 獸耳, 蘿莉, 催眠
        size_mb: 15.9
        analyzed:            <- 空 = 还没纳入过统计
        downloaded: ...
    """
    target = str(params.get("path") or "").strip()
    if not target:
        raise RuntimeError("要补全哪个目录？传 path。")
    root = Path(target).expanduser()
    if not root.is_dir():
        raise RuntimeError("目录不存在：" + str(root))

    jm = _jm()
    try:
        import jm_pick

        client = jm_pick._meta_client()
    except Exception:  # noqa: BLE001
        client = None

    done = []
    skipped = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        files = sorted(
            [f.name for f in child.iterdir() if f.suffix.lower() in IMG_SUFFIX]
        )
        if not files:
            continue

        existing = read_tags_txt(child)
        aid = str(existing.get("jm") or "").strip()

        # 没有 jm 号就联网找一次（很慢，但只做一次）
        if not aid and client is not None:
            aid = _match_jm_id(jm, client, child.name)

        detail = None
        if aid and client is not None and not existing.get("tags"):
            try:
                detail = client.get_album_detail(aid)
            except Exception:  # noqa: BLE001
                detail = None

        dims = jm._measure_pages(child, files) if files else {}
        total = sum((child / f).stat().st_size for f in files if (child / f).exists())

        lines = [
            "jm: " + aid,
            "title: " + str(existing.get("title") or child.name),
            "author: " + str(existing.get("author") or (_author_of(detail) if detail else "")),
            "pages: " + str(len(files)),
        ]
        if dims:
            lines.append("dims: " + dims["main"])
            if len(dims["all"].split()) > 1:
                lines.append("dims_all: " + dims["all"])
        tags = existing.get("tags") or (jm._album_tags(detail) if detail else [])
        lines.append("tags: " + ", ".join(tags))
        lines.append("size_mb: " + str(round(total / 1048576, 1)))
        lines.append("analyzed: " + str(existing.get("analyzed") or ""))
        lines.append("downloaded: " + str(existing.get("downloaded") or time.strftime("%Y-%m-%d %H:%M")))

        (child / "标签.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

        if aid and jm_pick is not None:
            try:
                jm_pick._write_index(Path.home() / "Desktop", aid, child, len(files))
            except Exception:  # noqa: BLE001
                pass

        done.append({"folder": child.name, "jm": aid, "pages": len(files), "tags": tags})
        print("[dsh-jm] 补全 " + (aid or "?") + " " + child.name[:30], file=sys.stderr)

    return {
        "ok": True,
        "path": str(root),
        "completed": done,
        "count": len(done),
        "missingJm": [d["folder"] for d in done if not d["jm"]],
    }


def _author_of(album):
    if album is None:
        return ""
    try:
        return _jm()._author(album)
    except Exception:  # noqa: BLE001
        return ""


def cmd_record_favorites(params: dict) -> dict:
    """
    把「精选截图/还没分析的」这些图纳入统计，然后**归档**到「已经分析的」。

    为什么要归档而不是原地留着：
        精选目录是给"我喜欢什么"当基准的。如果分析过的图还留在原地，
        下次一跑又把它们算一遍 —— 权重翻倍，基准会被自己污染。
        物理挪走是最不会出错的去重方式（比记文件名靠谱，用户改个名就废了）。
    """
    root = Path(str(params.get("path") or "")).expanduser()
    if not root.is_dir():
        raise RuntimeError("目录不存在：" + str(root))
    src = root / "还没分析的"
    if not src.is_dir():
        raise RuntimeError("没找到「还没分析的」子目录：" + str(src))
    dst = root / "已经分析的"
    dst.mkdir(parents=True, exist_ok=True)

    files = sorted([f for f in src.iterdir() if f.is_file() and f.suffix.lower() in IMG_SUFFIX])
    if not files:
        return {"ok": True, "moved": 0, "note": "「还没分析的」里没有新图。"}

    try:
        import jm_style
    except Exception:  # noqa: BLE001
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import jm_style

    rows = []
    for f in files:
        try:
            feat = jm_style.features_of(f)
        except Exception:
            continue
        if jm_style._is_blank_or_ad(feat):
            continue
        feat["file"] = f.name
        try:
            from PIL import Image

            with Image.open(f) as im:
                feat["dims"] = str(im.size[0]) + "x" + str(im.size[1])
        except Exception:  # noqa: BLE001
            feat["dims"] = ""
        rows.append(feat)

    # 档案：留着给"为什么推荐这个"当依据
    archive = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": len(files),
        "analyzed": len(rows),
        "features": rows,
    }
    (dst / "画风档案.json").write_text(
        json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    moved = 0
    for f in files:
        try:
            target = dst / f.name
            if target.exists():
                stem, suffix = f.stem, f.suffix
                target = dst / (stem + "_" + str(int(time.time())) + suffix)
            shutil_move(f, target)
            moved += 1
        except Exception as exc:  # noqa: BLE001
            print("[dsh-jm] 移动失败 " + f.name + "：" + str(exc), file=sys.stderr)

    keys = ("sat", "light", "contrast", "edge", "skin", "warm")
    baseline = {k: round(jm_style._median([r[k] for r in rows]), 4) for k in keys} if rows else {}
    if baseline:
        baseline["files"] = len(rows)

    return {
        "ok": True,
        "moved": moved,
        "analyzed": len(rows),
        "archive": str(dst / "画风档案.json"),
        "baseline": baseline,
    }


def shutil_move(src, dst):
    import shutil

    shutil.move(str(src), str(dst))


def _weighted_tags(tag_rank, books_total=1):
    """
    标签权重 = 占比 x 逆频率（IDF）。

    🔴 为什么要乘 IDF，而不是纯按占比：
       纯占比下「中文」（14/15 本）权重 0.4，而「催眠」（2 本）只有 0.06 ——
       结果是推荐里永远只有大众口味，小众的那点偏好被淹没。
       可用户当初收这两本，恰恰是因为喜欢；推荐一个正中这种小众口味的本子，
       带来的惊喜远大于再推一本"又一本萝莉"。

       IDF = log((N+1) / (count+1)) + 1
         · 命中所有本子的 tag → idf≈1，权重就是占比，不吃亏
         · 只有 1 本的 tag → idf≈2.9，把占比放大近 3 倍
       这样小众 tag 有存在感，但**不喧宾夺主** ——
       一个只出现过 1 次的 tag 权重上限也就 0.19，仍低于主力 tag。

    ⚠️ 不做归一化到 1：那会让"很多本都有同一个 tag"和"只有一本有"在数值上等价，
       等于把大众口味直接抹平，又走回另一个极端。
    """
    out = {}
    if not tag_rank:
        return out
    import math

    n = max(1, int(books_total))
    total = sum(t["count"] for t in tag_rank) or 1
    for t in tag_rank:
        count = max(1, int(t["count"]))
        share = count / float(total)
        idf = math.log((n + 1.0) / (count + 1.0)) + 1.0
        out[t["tag"].lower()] = round(share * idf, 4)
    return out


def _is_niche(count, books_total):
    """
    小众 = 在库里只出现过一次、且总量不到一成的 tag。

    🔴 判据必须收得这么紧，否则"小众分"会退化成常数：
       我一开始用 count<=2 且 <20%，15 本的库里一堆 tag 都符合，
       结果**每一本候选都命中至少 2 个小众 tag**，小众分全是 0.12 ——
       等于给所有本子加同一个数，一点区分度都没有，加权白做。

       收紧成「占比 < 10%」之后（15 本 → 只有 count=1 的算），
       小众才真的是"你只收过那一本"，命中它才有惊喜可言。
    """
    if count != 1:
        return False
    return count / float(max(1, books_total)) < 0.1


def cmd_recommend(params: dict) -> dict:
    """
    出一份「有理有据」的推荐。

    打分不是拍脑袋的：每个候选本子都会被算三项，
    而且三项都会写进结果里 —— 用户能看见为什么排这个位置。

        tagScore    候选的 tag 和你的标签榜重合度（按权重加权）
        styleScore  候选封面画风和精选基准的接近程度（距离转成分数）
        freshBonus  未看过 + 近期更新的小额加分

    最后按总分排。刻意保留原始三项数值，让人能判断"到底是哪一条把它顶上来的"。
    """
    jm = _jm()
    try:
        import jm_pick
        import jm_style
    except Exception:  # noqa: BLE001
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import jm_pick
        import jm_style

    prefs = load_prefs()
    lib = str(params.get("path") or prefs.get("libraryPath") or "").strip()
    snap = str(params.get("favoritePath") or prefs.get("favoritePath") or "").strip()
    want = max(1, min(int(params.get("top") or 10), 40))

    # 1) 收藏库画像
    lib_root = Path(lib).expanduser() if lib else None
    books = []
    if lib_root and lib_root.is_dir():
        for child in sorted(lib_root.iterdir()):
            if not child.is_dir():
                continue
            files = [f for f in child.iterdir() if f.suffix.lower() in IMG_SUFFIX]
            if not files:
                continue
            meta = read_tags_txt(child)
            info = _scan_book(child)
            info["path"] = str(child)
            info["tags"] = meta.get("tags") or []
            info["jm"] = meta.get("jm") or ""
            info["analyzed"] = meta.get("analyzed") or ""
            info["style"] = jm_style.book_style(child, count=4)
            books.append(info)

    tag_rank = _tag_ranking(books)
    weights = _weighted_tags(tag_rank, len(books))
    weight_total = sum(weights.values()) or 1.0
    niche_counts = {
        t["tag"].lower(): t["count"] for t in tag_rank
        if _is_niche(t["count"], len(books))
    }
    lib_style = jm_style.aggregate([b["style"] for b in books if b.get("style")])

    # 2) 精选基准（优先用它，它才是"真正喜欢的"）
    fav_style = {}
    if snap:
        fav_root = Path(snap) / "已经分析的"
        if fav_root.is_dir():
            try:
                archive = json.loads((fav_root / "画风档案.json").read_text(encoding="utf-8"))
                rows = archive.get("features") or []
                if rows:
                    keys = ("sat", "light", "contrast", "edge", "skin", "warm")
                    fav_style = {k: round(jm_style._median([r[k] for r in rows]), 4) for k in keys}
                    fav_style["files"] = len(rows)
            except Exception:  # noqa: BLE001
                fav_style = {}
    baseline = fav_style or lib_style

    # 3) 拿标签榜前几个 tag 去站内搜，合并候选
    seen_ids = parse_seen(prefs.get("seen") or "")
    # 🔴 已拥有的 jm 号必须取**三个来源的并集**，只看收藏库会漏：
    #
    #     ① 收藏库里的标签（用户主动归档的）
    #     ② 桌面索引 .dsh-jm-index.json（所有下过的，包括后来被删/搬走的）
    #     ③ prefs.seen 里用户手填的
    #
    #   我最初只用了 ①，结果那 9 本"下过但已经不在收藏库"的书完全没被排除 ——
    #   推荐里又出现一遍，用户看到的就是"这不是我下过吗"。
    #   宁可少推，也不要推重复的：用户的原话是"确定不会下载到重复的"。
    owned = {str(b.get("jm") or "") for b in books if b.get("jm")}
    try:
        import jm_pick

        for aid in jm_pick._read_index(Path.home() / "Desktop").keys():
            if aid:
                owned.add(str(aid))
    except Exception as exc:  # noqa: BLE001
        print("[dsh-jm] 读桌面索引失败（去重会变弱）：" + str(exc), file=sys.stderr)
    owned.update(str(x) for x in parse_seen(prefs.get("seen") or ""))
    owned.discard("")
    for aid in owned:
        seen_ids.add(aid)

    client = jm_pick._meta_client()
    cands = {}
    queries = [t["tag"] for t in tag_rank[:6] if t["count"] >= 2]
    if not queries:
        queries = ["中文"]

    # 🔴 这里以前是"候选全量逐个查详情 + 下封面"，最坏 300 本 = 600 次网络往返，
    #    串行跑十几分钟。设置页的按钮点下去就是**假死**：前端 fetch 一直挂着，
    #    没有任何进度反馈，用户只能以为卡了。
    #
    #    现在三道闸：
    #      ① 每个 tag 只取前 PER_QUERY 条候选（不整页吞）
    #      ② 查详情并发跑（8 路），这一步只是拿 tags，很快
    #      ③ 只有粗排靠前的 STYLE_TOP 本才下封面算画风 —— 封面是最贵的一步
    #
    #    再加一个总预算：超时就交已经算完的那部分，不留"永远不返回"的可能。
    import concurrent.futures

    PER_QUERY = int(params.get("perQuery") or 40)
    STYLE_TOP = int(params.get("styleTop") or 40)
    BUDGET = float(params.get("budget") or 240)
    deadline = time.time() + BUDGET

    for q in queries:
        try:
            page = client.search_site(q, page=1)
        except Exception as exc:  # noqa: BLE001
            print("[dsh-jm] 搜索 " + q + " 失败：" + str(exc), file=sys.stderr)
            continue
        for aid, info in list(page.content)[:PER_QUERY]:
            aid = str(aid)
            if aid in seen_ids or aid in owned or aid in cands:
                continue
            cands[aid] = {
                "id": aid,
                "title": str(info.get("name") or ""),
                "author": str(info.get("author") or ""),
                "date": str(info.get("adddate") or ""),
                "tags": [],
                "hitFrom": q,
            }

    def _fetch_tags(aid):
        """只拿 tags 和页数。失败就当空 —— 一个候选挂了不该拖累整批。"""
        try:
            album = client.get_album_detail(aid)
            return aid, jm._album_tags(album), int(getattr(album, "page_count", 0) or 0)
        except Exception:  # noqa: BLE001
            return aid, [], 0

    ids = list(cands.keys())
    if ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for aid, tags, pages in pool.map(_fetch_tags, ids):
                cands[aid]["tags"] = tags
                cands[aid]["pages"] = pages

    def _cover_style(aid):
        """下封面算画风。封面是 500KB 级的图，这一步最贵。"""
        try:
            url = jm_pick._cover_url(aid)
            import tempfile as _tf
            import shutil as _sh

            tmpdir = Path(_tf.mkdtemp(prefix="dsh-jm-rec-"))
            dst = tmpdir / (aid + ".jpg")
            ok = False
            for proxy in [jm.proxy_url(), None]:
                if jm_pick._fetch_cover(url, dst, proxy):
                    ok = True
                    break
            score = 0.0
            if ok and dst.exists():
                feat = jm_style.features_of(dst)
                if baseline:
                    dist = jm_style.distance(feat, baseline)
                    score = round(max(0.0, 3.0 - dist) / 3.0, 4)
            _sh.rmtree(tmpdir, ignore_errors=True)
            return aid, score
        except Exception:  # noqa: BLE001
            return aid, 0.0

    scored = []
    for aid, c in cands.items():
        tags_low = [t.lower() for t in c["tags"]]
        # 归一化：除以全部 tag 权重之和。
        # ⚠️ 不归一化的话 IDF 会把分数抬到 0.8+，那 0.6 的权重就名不副实了。
        raw_tag = sum(weights.get(t, 0) for t in tags_low)
        c["tagScore"] = round(raw_tag / (weight_total or 1.0), 4)
        c["hitTags"] = [t for t in tags_low if t in weights][:8]
        c["nicheHits"] = [t for t in tags_low if t in niche_counts]
        c["nicheScore"] = round(min(0.12, 0.05 * len(c["nicheHits"])), 4)
        c["styleScore"] = 0.0
        c["score"] = c["tagScore"]
        scored.append(c)

    # 粗排：先按 tag 分挑出值得下封面的那批
    scored.sort(key=lambda x: -(x["tagScore"] + x["nicheScore"]))
    style_targets = []
    for c in scored:
        if len(style_targets) >= STYLE_TOP or time.time() > deadline:
            break
        style_targets.append(c["id"])

    if style_targets:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            for aid, score in pool.map(_cover_style, style_targets):
                for c in scored:
                    if c["id"] == aid:
                        c["styleScore"] = score
                        break

    # 补最终分数
    for c in scored:
        fresh = 0.05 if c["date"] >= time.strftime(
            "%Y-%m-%d", time.localtime(time.time() - 86400 * 90)
        ) else 0.0
        c["score"] = round(
            c["tagScore"] * 0.6 + c["styleScore"] * 0.35 + c["nicheScore"] + fresh, 4
        )
        c["why"] = _why(c["hitTags"], c["tags"], c["styleScore"],
                        c["pages"], c["nicheHits"])

    scored.sort(key=lambda x: -x["score"])

    # 🔴 探索配额：前 70% 按分排，剩下的名额**优先给戳中小众 tag 的**。
    #
    #   纯按分数排的话，小众 tag 命中奖励太小（上限 0.12），
    #   拼不过一本同时命中「蘿莉+中文+中出」的。那用户永远看不到探索项，
    #   加权的意义也就没了。所以这里做两段式：主流区排分，探索区专门留给小众。
    #
    #   不想看小众本？传 explore=0 关掉，退回纯排序。
    explore_ratio = params.get("explore")
    explore_ratio = 0.3 if explore_ratio is None else max(0.0, min(float(explore_ratio), 0.6))
    explore_n = int(round(want * explore_ratio)) if want >= 4 else 0

    main_quota = want - explore_n
    top = scored[:main_quota]
    picked = {c["id"] for c in top}

    rest = [c for c in scored[main_quota:] if c["id"] not in picked]
    # 探索位优先：小众命中多的排前面，其次是画风分高的
    # 探索区：先保证"戳中小众"的进来，同组内按分数降序。
    #
    #   ⚠️ 不能按"小众命中个数"排：命中 8 个冷 tags 的杂食本会挤掉
    #      命中 2 个但分数高得多的。用户看到的列表会是乱的（分数忽高忽低），
    #      这比"探索位不够惊喜"更糟 —— 列表失去可读性就没人看了。
    rest.sort(key=lambda c: (0 if (c.get("nicheHits") or []) else 1, -c["score"]))
    for c in rest:
        if len(top) >= want:
            break
        top.append(c)
    for c in top:
        c["slot"] = "explore" if c["id"] not in {x["id"] for x in scored[:main_quota]} else "main"

    # 报告落在**精选截图**目录，不是收藏库 ——
    # 收藏库是"素材"，推荐是"结论"。用户要的是打开精选那个文件夹就看到推荐，
    # 而不是回头去翻库目录。
    report_root = Path(snap) if snap and Path(snap).is_dir() else (lib_root if lib_root else None)
    report = (Path(report_root) / "个性化推荐.md") if report_root and Path(report_root).is_dir() else None
    if report is not None:
        lines = []
        lines.append("# 个性化推荐")
        lines.append("")
        lines.append("- 生成时间：" + time.strftime("%Y-%m-%d %H:%M:%S"))
        lines.append("- 依据库：" + str(lib_root))
        lines.append("- 画风基准：" + ("精选截图（" + str(baseline.get("files", 0)) + " 张）" if fav_style else "收藏库 15 本"))
        lines.append("- 已排除：已下载 " + str(len(owned)) + " 本、已看过 " + str(len(parse_seen(prefs.get('seen') or ''))) + " 个号")
        lines.append("")
        lines.append("## 你的口味（标签命中本数）")
        lines.append("")
        for t in tag_rank[:12]:
            lines.append("- " + t["tag"] + "：" + str(t["count"]) + " 本")
        lines.append("")
        lines.append("## 推荐")
        lines.append("")
        lines.append("| # | JM号 | 页数 | 总分 | 标签分 | 画风分 | 小众分 | 位 | 命中标签 | 标题 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for i, c in enumerate(top, 1):
            slot = "探索" if c.get("slot") == "explore" else "主力"
            lines.append(
                "| " + str(i) + " | " + c["id"] + " | " + str(c["pages"]) + " | " + str(c["score"])
                + " | " + str(c["tagScore"]) + " | " + str(c["styleScore"])
                + " | " + str(c.get("nicheScore") or 0) + " | " + slot + " | "
                + "、".join(c["hitTags"][:5]) + " | " + c["title"][:40].replace("|", "/") + " |"
            )
        lines.append("")
        lines.append("## 为什么是这几本")
        lines.append("")
        for i, c in enumerate(top, 1):
            lines.append(str(i) + ". **JM" + c["id"] + "** " + c["title"][:44])
            lines.append("   - " + c["why"])
        lines.append("")
        lines.append("> 打分权重：标签 0.6 / 画风 0.35 / 小众命中 最高 0.12 / 新本 0.05。")
        lines.append("> 标签权重 = 占比 x 逆频率(IDF)：冷门 tag 更值钱，但不会盖过主力口味。")
        lines.append("> 「探索」位是专门留给小众口味的（默认占 30%），可以传 explore=0 关掉。")
        lines.append("> 画风分是拿**封面**和精选基准比的，只能近似 —— 封面和正文画风可能不同。")
        report.write_text("\n".join(lines), encoding="utf-8")

    return {
        "ok": True,
        "count": len(top),
        "candidates": len(cands),
        "baselineFrom": "favorite" if fav_style else "library",
        "report": str(report) if report else "",
        "items": [
            {
                "id": c["id"], "title": c["title"], "pages": c["pages"],
                "score": c["score"], "tagScore": c["tagScore"],
                "styleScore": c["styleScore"], "hitTags": c["hitTags"],
                "nicheHits": c.get("nicheHits") or [], "slot": c.get("slot", "main"),
                "why": c.get("why", ""),
            }
            for c in top
        ],
    }


def _why(hits, tags, style_score, pages, niche_hits=None):
    """把打分翻成一句人话。推荐必须有依据，否则就是个随机数生成器。"""
    parts = []
    if niche_hits:
        parts.append("戳中你的小众口味「" + "、".join(niche_hits[:3]) + "」")
    if hits:
        others = [t for t in hits[:3] if t not in (niche_hits or [])]
        if others:
            parts.append("命中你常看的「" + "、".join(others) + "」")
    if not hits and not niche_hits:
        parts.append("标签没直接命中，靠画风相近入选")
    if style_score > 0.6:
        parts.append("画风和精选基准很接近")
    elif style_score > 0.3:
        parts.append("画风接近")
    if pages and pages > 150:
        parts.append("篇幅较长（" + str(pages) + " 页），适合当单行本看")
    elif pages and pages <= 30:
        parts.append("短篇（" + str(pages) + " 页）")
    return "；".join(parts)


def cmd_prefs(params: dict) -> dict:
    """读或写配置。带 patch 就写，不带就只读。"""
    patch = params.get("patch")
    if isinstance(patch, dict):
        data = save_prefs(patch)
    else:
        data = load_prefs()
    seen = parse_seen(data.get("seen") or "")
    lib = str(data.get("libraryPath") or "").strip()
    fav = str(data.get("favoritePath") or "").strip()
    return {
        "ok": True,
        "prefs": data,
        "seenCount": len(seen),
        "prefsPath": str(prefs_path()),
        "libraryExists": bool(lib) and Path(lib).expanduser().is_dir(),
        "favoriteExists": bool(fav) and Path(fav).expanduser().is_dir(),
    }


def _scan_book(folder: Path) -> dict:
    """数一本：页数、总大小、主流尺寸、格式。"""
    files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in IMG_SUFFIX]
    if not files:
        return {}
    sizes = {}
    total = 0
    widths = []
    heights = []
    try:
        from PIL import Image

        have_pil = True
    except Exception:
        have_pil = False

    # 只抽 3 张看尺寸就够 —— 一本 200 页的全解码要等半天，
    # 而同一本的图几乎必然同尺寸（站点统一压的）。
    sample = files if len(files) <= 3 else [files[0], files[len(files) // 2], files[-1]]
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            pass
        sizes[f.suffix.lower()] = sizes.get(f.suffix.lower(), 0) + 1
    if have_pil:
        for f in sample:
            try:
                with Image.open(f) as im:
                    widths.append(im.size[0])
                    heights.append(im.size[1])
            except Exception:
                continue
    w = max(set(widths), key=widths.count) if widths else 0
    h = max(set(heights), key=heights.count) if heights else 0
    return {
        "name": folder.name,
        "pages": len(files),
        "mb": round(total / 1048576, 1),
        "width": w,
        "height": h,
        "formats": sizes,
    }


def _verdict(avg: float) -> str:
    if avg <= 30:
        return "短篇为主"
    if avg <= 80:
        return "中篇为主"
    return "长篇 / 单行本为主"


def read_tags_txt(folder: Path) -> dict:
    """
    读本子目录里的 标签.txt。

    格式见 jm.py 的 _write_tags_txt。解析故意宽松：
    没有这个文件就返回空（说明是旧版本下的，或者用户自己放的图），
    不报错也不影响分析 —— 老收藏照样能出画风报告，只是少一段 tag 统计。
    """
    f = folder / "标签.txt"
    if not f.is_file():
        return {}
    out = {"tags": []}
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "tags":
            out["tags"] = [t.strip() for t in value.split(",") if t.strip()]
        elif key in ("jm", "author", "title", "pages", "dims", "dims_all",
                     "size_mb", "analyzed", "downloaded"):
            out[key] = value
    return out


def mark_analyzed(folder: Path) -> bool:
    """
    在 标签.txt 里把 analyzed 打上日期，表示"这本已经进过画像了"。

    ⚠️ 只改 analyzed 这一行，其他行原样保留 —— 这个文件是用户会直接打开看的，
       重写整份会把手工加的备注弄丢。
    """
    f = folder / "标签.txt"
    if not f.is_file():
        return False
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return False

    stamp = time.strftime("%Y-%m-%d")
    out = []
    found = False
    for line in lines:
        if line.lower().startswith("analyzed:"):
            out.append("analyzed: " + stamp)
            found = True
        else:
            out.append(line)
    if not found:
        out.append("analyzed: " + stamp)
    try:
        f.write_text("\n".join(out) + "\n", encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def _tag_ranking(books: list) -> list:
    """
    统计每个 tag 在多少本里出现过。

    ⚠️ 统计的是「本数」不是「出现次数」：一本带两次"蘿莉"还是算 1 本。
       否则标签多的本子会主导排名，结论会变成"你喜欢标签写得全的作者"。
    """
    # ⚠️ 统计范围是**全部**，不是"只算没标记过的"。
    #
    #   这里我最初写错了一版：跳过 analyzed 非空的，结果用户新加一本之后，
    #   报告只统计那一本 —— 画像从 15 本塌成 1 本，结论全废。
    #   "防止重复加权"要防的是**同一本被数两次**（复制了两份、重下留了旧目录），
    #   而不是"上次算过的这次不算"。这两件事不一样。
    #
    #   现在的做法：
    #     · 按 jm 号去重，同一本只算一次（这才是防重复加权）；
    #     · analyzed 只用来标记"算过了"，报告里区分本次新增，不参与筛选。
    counter = {}
    seen_jm = set()
    for b in books:
        jm_id = str(b.get("jm") or "").strip()
        if jm_id:
            if jm_id in seen_jm:
                continue          # 同一本出现两次（复制/重下残留），只算一次
            seen_jm.add(jm_id)
        for tag in set(b.get("tags") or []):
            counter[tag] = counter.get(tag, 0) + 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"tag": t, "count": n} for t, n in ranked]


def _style_block(books: list, jm_style) -> tuple:
    """
    给每本算画风，再合成一份基准画像。

    ⚠️ 这里很贵：每本解 8 张图。33 本就是 264 次 PIL 解码。
       实测每张 30~60ms（缩到 480 以内），33 本约 10 秒。
       再多就得考虑加缓存，现在不值得。
    """
    styles = []
    for b in books:
        st = jm_style.book_style(Path(b["path"]))
        if st:
            b["style"] = st
            styles.append(st)
    return styles, jm_style.aggregate(styles)


def cmd_analyze_v2(params: dict) -> dict:
    """兼容入口：先补全元数据，再分析。设置页走这个。"""
    target = str(params.get("path") or "").strip() or str(load_prefs().get("libraryPath") or "").strip()
    if target and Path(target).expanduser().is_dir():
        try:
            cmd_complete({"path": target})
        except Exception as exc:  # noqa: BLE001
            print("[dsh-jm] 补全元数据失败（继续分析）：" + str(exc), file=sys.stderr)
    return cmd_analyze(params)


def cmd_analyze(params: dict) -> dict:
    """
    扫库文件夹，在**那个文件夹里**写一份 个性化分析.md。

    刻意只写一份 Markdown、不改动任何漫画文件：用户要的是「这份目录长什么样」，
    不是让工具去重排他的收藏。
    """
    target = str(params.get("path") or "").strip()
    if not target:
        target = str(load_prefs().get("libraryPath") or "").strip()
    if not target:
        raise RuntimeError("还没填库文件夹路径。先在设置里填一个目录。")

    root = Path(target).expanduser()
    if not root.is_dir():
        raise RuntimeError("这个目录不存在或不是文件夹：" + str(root))

    books = []
    loose = [f for f in root.iterdir() if f.is_file() and f.suffix.lower() in IMG_SUFFIX]
    for child in sorted(root.iterdir()):
        if child.is_dir():
            info = _scan_book(child)
            if info:
                info["path"] = str(child)
                info["tags"] = read_tags_txt(child).get("tags") or []
                books.append(info)

    total_pages = sum(b["pages"] for b in books)
    total_mb = round(sum(b["mb"] for b in books), 1)

    dims = {}
    for b in books:
        if b["width"] and b["height"]:
            key = str(b["width"]) + "x" + str(b["height"])
            dims[key] = dims.get(key, 0) + 1
    top_dims = sorted(dims.items(), key=lambda kv: -kv[1])[:5]

    short = [b for b in books if b["pages"] <= 30]
    mid = [b for b in books if 30 < b["pages"] <= 80]
    long_books = [b for b in books if b["pages"] > 80]

    fmts = {}
    for b in books:
        for k, v in b["formats"].items():
            fmts[k] = fmts.get(k, 0) + v

    avg_pages = round(total_pages / len(books), 1) if books else 0
    seen = parse_seen(load_prefs().get("seen") or "")

    # 标签榜 + 画风：这两段是「个性化」的主体，比页数统计有用得多
    tag_rank = _tag_ranking(books)
    try:
        import jm_style
    except Exception:  # noqa: BLE001
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        import jm_style
    style_rows, baseline = _style_block(books, jm_style)

    # 分层分类：技法（互斥）+ 内容（可叠加）
    try:
        import jm_classify
    except Exception:  # noqa: BLE001
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import jm_classify

    tech_summary = jm_classify.summarize(
        [b["style"] for b in books if b.get("style")]
    )

    # 精选目录：用户自己挑的好图。全库平均是"收藏了什么"，精选才是"真正喜欢什么"。
    fav_dir = params.get("favoritePath")
    if not fav_dir:
        fav_dir = load_prefs().get("favoritePath") or ""
    fav_style = {}
    fav_count = 0
    fav_notes = []
    if str(fav_dir).strip():
        fav_root = Path(str(fav_dir)).expanduser()
        if fav_root.is_dir():
            fav_files = [
                f for f in fav_root.rglob("*")
                if f.is_file() and f.suffix.lower() in IMG_SUFFIX
            ]
            fav_count = len(fav_files)
            if fav_count:
                feat_rows = []
                for f in fav_files[:200]:
                    try:
                        feat = jm_style.features_of(f)
                    except Exception:
                        continue
                    if jm_style._is_blank_or_ad(feat):
                        continue
                    feat_rows.append(feat)
                if feat_rows:
                    keys = ("sat", "light", "contrast", "edge", "skin", "warm")
                    fav_style = {k: round(jm_style._median([r[k] for r in feat_rows]), 4) for k in keys}
                    fav_style["files"] = len(feat_rows)
                    fav_notes = jm_style.describe(fav_style)
                    if baseline:
                        fav_style["vsLibrary"] = jm_style.distance(fav_style, baseline)

    lines = []
    lines.append("# 个性化分析")
    lines.append("")
    lines.append("- 目录：" + str(root))
    lines.append("- 生成时间：" + time.strftime("%Y-%m-%d %H:%M:%S"))
    fresh_books = [b for b in books if not str(b.get("analyzed") or "").strip()]
    lines.append("- 识别到 " + str(len(books)) + " 本，共 " + str(total_pages) + " 页，" + str(total_mb) + " MB")
    if fresh_books:
        lines.append("- 本次新增 " + str(len(fresh_books)) + " 本（之前没算过）")
    if loose:
        lines.append("- 根目录还有 " + str(len(loose)) + " 张散图（没算进上面的统计）")
    lines.append("")
    lines.append("## 收藏画像")
    lines.append("")
    lines.append("- 平均每本 " + str(avg_pages) + " 页，属于" + _verdict(avg_pages) + "。")
    lines.append("- 页数分布：<=30 页 " + str(len(short)) + " 本 / 31~80 页 " + str(len(mid)) + " 本 / >80 页 " + str(len(long_books)) + " 本")
    if top_dims:
        lines.append("- 主流分辨率（决定是手机版还是高清收藏）：")
        for dim, n in top_dims:
            lines.append("  - " + dim + "：" + str(n) + " 本")
    if fmts:
        fmt_text = "、".join(str(k) + "(" + str(v) + ")" for k, v in sorted(fmts.items(), key=lambda kv: -kv[1]))
        lines.append("- 文件格式：" + fmt_text)
    if seen:
        lines.append("- 已标记看过的 jm 号：" + str(len(seen)) + " 个（推荐时会跳过）")
    lines.append("")
    if tag_rank:
        lines.append("## 标签偏好（按命中本数排）")
        lines.append("")
        total_books = len(books) or 1
        for item in tag_rank[:24]:
            pct = round(item["count"] * 100.0 / total_books)
            bar = "#" * max(1, int(round(pct / 5.0)))
            lines.append("- " + item["tag"] + "：" + str(item["count"]) + " 本（" + str(pct) + "%） " + bar)
        lines.append("")
        lines.append("> 没标签的本子是旧版本下的（那时还没写 标签.txt）。重下一遍就有了。")
        lines.append("")
    else:
        lines.append("## 标签偏好")
        lines.append("")
        lines.append("> 这些本子都还没有 标签.txt。用新版本重下一遍，标签会跟着图片一起落地。")
        lines.append("")

    if tech_summary and tech_summary.get("total"):
        lines.append("## 画风流派分布")
        lines.append("")
        lines.append("技法（互斥，一本只归一类）+ 内容（可叠加）。")
        lines.append("「A+B」表示这本同时具备两种画法，离两类都很近 —— 不硬塞。")
        lines.append("")
        lines.append("| 技法 | 本数 | 占比 |")
        lines.append("| --- | --- | --- |")
        # ⚠️ 这里必须传**每一本的原始判定**，不是聚合后的品类列表。
        #    我第一版传的是 tech_summary["tech"]（聚合后只剩 7 项），
        #    于是 total=7 < 8，"全库仅此一本"的提示永远不触发 ——
        #    手办那本明明只有 1 本，报告里却什么都没标。
        techs = [lab["tech"] for lab in tech_summary.get("labels") or []]
        for t in tech_summary["tech"]:
            note = jm_classify.rarity_note(t["tech"], techs)
            lines.append(
                "| " + t["tech"] + " " + note + " | " + str(t["count"])
                + " | " + str(round(t["ratio"] * 100)) + "% |"
            )
        lines.append("")
        if tech_summary.get("content"):
            lines.append("内容分布：")
            lines.append("")
            for c in tech_summary["content"]:
                lines.append(
                    "- " + c["content"] + "：" + str(c["count"]) + " 本（"
                    + str(round(c["ratio"] * 100)) + "%）"
                )
            lines.append("")
        if tech_summary.get("mixed"):
            lines.append(
                "> " + str(tech_summary["mixed"]) + " 本属于混合型（两种画法都沾），已单独标注。"
            )
            lines.append("")

    if baseline:
        lines.append("## 画风画像")
        lines.append("")
        lines.append("采样规则：跳过首 3 页和尾 3 页（首尾常是封面 / 汉化组广告），中间等距取 8 张，")
        lines.append("每个指标取中位数。白底和纯色底自动丢弃。")
        lines.append("")
        for line in jm_style.describe(baseline):
            lines.append("- " + line)
        lines.append("")
        lines.append("| 指标 | 值 | 含义 |")
        lines.append("| --- | --- | --- |")
        lines.append("| 饱和度 | " + str(baseline.get("sat")) + " | 0=黑白，0.4+=浓彩 |")
        lines.append("| 亮度 | " + str(baseline.get("light")) + " | 越低越暗调 |")
        lines.append("| 对比度 | " + str(baseline.get("contrast")) + " | 高=硬光硬影 |")
        lines.append("| 线密度 | " + str(baseline.get("edge")) + " | 高=线条繁复 |")
        lines.append("| 肤色占比 | " + str(baseline.get("skin")) + " | 高=肉体特写多 |")
        lines.append("| 暖色倾向 | " + str(baseline.get("warm")) + " | 正=偏粉橙，负=偏蓝青 |")
        lines.append("")
        lines.append("## 与本画风最接近的本子")
        lines.append("")
        ranked = []
        for b in books:
            if not b.get("style"):
                continue
            ranked.append((jm_style.distance(b["style"], baseline), b))
        ranked.sort(key=lambda kv: kv[0])
        if ranked:
            for dist, b in ranked[:8]:
                lines.append("- 距离 " + str(dist) + " ｜ " + b["name"][:42])
            lines.append("")
            lines.append("> 距离越小越接近这个基准。想找新本子时，拿这个基准去比，比看标签准。")
            lines.append("")

    if fav_style:
        lines.append("## 精选画风（你亲手挑的那些）")
        lines.append("")
        lines.append("这个比上面的「画风画像」重要得多：全库平均只能说明你**收藏**了什么，")
        lines.append("精选目录才是你**真正喜欢**什么。两者不一样时，以这段为准。")
        lines.append("")
        lines.append("- 共 " + str(fav_count) + " 张，有效分析 " + str(fav_style.get("files", 0)) + " 张")
        for line in fav_notes:
            lines.append("- " + line)
        if fav_style.get("vsLibrary") is not None:
            d = fav_style["vsLibrary"]
            if d < 1.0:
                lines.append("- 与全库画风距离 " + str(d) + "（高度一致，说明你挑的和收藏的口味统一）")
            elif d < 2.5:
                lines.append("- 与全库画风距离 " + str(d) + "（略有偏差，精选更偏你的个人偏好）")
            else:
                lines.append("- 与全库画风距离 " + str(d) + "（偏差明显 —— 收藏里有很多其实你不是最喜欢的）")
        lines.append("")
        lines.append("| 指标 | 精选 | 全库 |")
        lines.append("| --- | --- | --- |")
        for k, label in (("sat", "饱和度"), ("light", "亮度"), ("contrast", "对比度"),
                         ("edge", "线密度"), ("skin", "肤色占比"), ("warm", "暖色倾向")):
            lines.append("| " + label + " | " + str(fav_style.get(k)) + " | " + str((baseline or {}).get(k)) + " |")
        lines.append("")
    elif str(fav_dir).strip():
        lines.append("## 精选画风")
        lines.append("")
        lines.append("> 精选目录里没找到图。把喜欢的那几页复制进去再来一次。")
        lines.append("")

    lines.append("## 逐本清单")
    lines.append("")
    lines.append("| 本子 | 页数 | 大小 | 分辨率 | 画风 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for b in books:
        dim = (str(b["width"]) + "x" + str(b["height"])) if b["width"] else "-"
        name = b["name"].replace("|", "/")
        lab = jm_classify.format_label(jm_classify.label_of(b.get("style") or {})) if b.get("style") else "-"
        lines.append(
            "| " + name[:44] + " | " + str(b["pages"]) + " | " + str(b["mb"]) + "MB | "
            + dim + " | " + lab.replace("|", "/") + " |"
        )
    lines.append("")

    out = root / "个性化分析.md"
    out.write_text("\n".join(lines), encoding="utf-8")

    # 算完打标。放在最后：中途抛异常时不该留下"已分析"的假标记，
    # 否则那本会被永久跳过，画像里就永远缺一块。
    marked = 0
    for b in books:
        try:
            if mark_analyzed(Path(b["path"])):
                marked += 1
        except Exception:  # noqa: BLE001
            pass

    return {
        "ok": True,
        "path": str(root),
        "report": str(out),
        "books": len(books),
        "pages": total_pages,
        "mb": total_mb,
        "avgPages": avg_pages,
        "topDims": [{"dim": d, "count": n} for d, n in top_dims],
        "seenCount": len(seen),
        "looseImages": len(loose),
        "topTags": tag_rank[:12],
        "style": baseline,
        "favoriteStyle": fav_style,
        "favoriteCount": fav_count,
        "marked": marked,
    }
