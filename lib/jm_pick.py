"""tag 挑选：搜一批 → 下封面并编号 → 让用户挑 → 只下挑中的。

为什么单独一个文件：jm.py 里已经塞了下载、作者、探活三套逻辑，
再往里加挑选会把文件撑到没法看。这里只负责「挑选」这条线，
共用的东西（代理、超时、客户端构造、下载）在函数里延迟 import jm 拿。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def _jm():
    """延迟导入，避免 jm.py 反向 import 本模块时循环。"""
    import jm

    return jm


def _safe(text, limit=40):
    return _jm().safe_name(text, limit)


def _pick_dir(tag, order, time_range):
    """桌面/jm挑选/<tag>/<order-time>/，同一组合重挑就覆盖。"""
    root = _jm().desktop_dir() / "jm挑选" / _safe(tag) / _safe(f"{order}-{time_range}", 24)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cover_url(album_id):
    """封面 URL 走图片 CDN 域名，跟正文图同一套。"""
    from jmcomic import JmModuleConfig, JmcomicText

    domains = list(getattr(JmModuleConfig, "DOMAIN_IMAGE_LIST", None) or [])
    url = JmcomicText.get_album_cover_url(str(album_id), image_domain=(domains[0] if domains else None), size="")
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        base = domains[0] if domains else "cdn-msp.jmapiproxy2.cc"
        url = f"https://{base}{url}"
    return url


def _fetch_cover(url, out, proxy, timeout=20):
    """下一张封面。失败返回 False，不抛 —— 一张挂不该毁掉整批。"""
    jm = _jm()
    req = urllib.request.Request(url)
    req.add_header("User-Agent", jm.UA_FOR_PROBE)
    req.add_header("Referer", "https://18comic.ink/")
    handlers = [
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
        urllib.request.HTTPSHandler(context=jm._ssl_ctx()),
    ]
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = resp.read()
        if len(body) < 512:
            return False
        tmp = out.with_suffix(".part")
        tmp.write_bytes(body)
        tmp.replace(out)
        return True
    except Exception as exc:
        print(f"[dsh-jm] 封面失败 {url}：{type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def _cover_dims(path):
    """量封面尺寸，给挑选清单标一下。读不出来就返回空串，不报错。"""
    try:
        from PIL import Image

        with Image.open(path) as im:
            return str(im.size[0]) + "x" + str(im.size[1])
    except Exception:
        return ""


def _search_by_tag(client, keyword, order, time_range, top):
    """
    按 tag 搜前 top 条。

    排序是**两个字段拼出来的**：站点的 o 收 {order}_{time}，例如本月最多浏览 = mv_m，
    全时段就是裸 mv。jmcomic 的 search_site 内部才拼，这里直接用 search()，
    所以 time 得自己拼 —— 否则「本月最热」会悄悄退化成「全时段最热」。
    """
    from jmcomic import JmMagicConstants as C

    order_map = {
        "latest": C.ORDER_BY_LATEST,
        "views": C.ORDER_BY_VIEW,
        "likes": C.ORDER_BY_LIKE,
        "score": C.ORDER_BY_SCORE,
        "comments": C.ORDER_BY_COMMENT,
        "pictures": C.ORDER_BY_PICTURE,
    }
    time_map = {"all": C.TIME_ALL, "today": C.TIME_TODAY, "week": C.TIME_WEEK, "month": C.TIME_MONTH}
    o = order_map.get(order, C.ORDER_BY_LATEST)
    t = time_map.get(time_range, C.TIME_ALL)

    items = []
    page_no = 1
    page_count = 1
    while page_no <= page_count and len(items) < top and page_no <= 5:
        page = client.search(keyword, page_no, 0, o, t, C.CATEGORY_ALL, None)
        try:
            page_count = int(page.page_count or 1)
        except Exception:
            page_count = 1
        for item in list(page.content):
            if isinstance(item, (tuple, list)):
                aid = item[0] if len(item) > 0 else 0
                info = item[1] if len(item) > 1 else {}
            else:
                aid = getattr(item, "album_id", None) or 0
                info = {}
            info = info if isinstance(info, dict) else {}
            items.append({
                "id": int(aid or 0),
                "title": str(info.get("name") or "").strip(),
                "author": str(info.get("author") or "").strip(),
                "date": str(info.get("adddate") or "").strip(),
            })
            if len(items) >= top:
                break
        page_no += 1
    return items[:top]


def _state_file():
    return _jm()._state_path().with_name("last_pick.json")


def remember_pick(folder, payload, items):
    """记下这一轮挑了什么。用户下一句只说「3」，得能对回 jm 号。"""
    try:
        _state_file().write_text(
            json.dumps(
                {
                    "folder": str(folder),
                    "tag": payload.get("tag"),
                    "order": payload.get("order"),
                    "time": payload.get("time"),
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "items": [{"no": i["no"], "id": i["id"], "title": i["title"]} for i in items],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[dsh-jm] 记挑选失败：{exc}", file=sys.stderr)


def last_pick():
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _search_by_author(client, author, top):
    """
    按作者搜他的全部作品。

    ⚠️ search_author 是**模糊**搜索：搜「今澤」会把作者名里含这几个字的
       其他人也捞回来。所以要按 author 字段再过一遍，只留真正同名的。
       （这个坑在做 jm_author 时踩过一次，同样的处理搬过来。）
    """
    items = []
    page_no = 1
    page_count = 1
    while page_no <= page_count and len(items) < top and page_no <= 10:
        page = client.search_author(author, page=page_no)
        try:
            page_count = int(page.page_count or 1)
        except Exception:  # noqa: BLE001
            page_count = 1
        for aid, info in list(page.content):
            info = info if isinstance(info, dict) else {}
            name = str(info.get("author") or "")
            if name and not _same_person(name, author):
                continue
            items.append({
                "id": int(aid or 0),
                "title": str(info.get("name") or "").strip(),
                "author": name or author,
                "date": str(info.get("adddate") or "").strip(),
            })
            if len(items) >= top:
                break
        page_no += 1
    return items


def _same_person(a, b):
    """作者名归一化比较：去括号内容、去空白、小写后互相包含。"""
    import re as _re

    def norm(s):
        s = str(s or "").lower()
        s = _re.sub(r"[（(\[【].*?[）)\]】]", " ", s)
        return _re.sub(r"[\s_\-·・.,，、/|]+", "", s)

    x, y = norm(a), norm(b)
    if not x or not y:
        return False
    return x in y or y in x


def cmd_pick(params):
    """
    按 tag 找「最新 / 最热」的前 N 本，封面下到桌面并编号，等用户挑。

    编号必须和索引文件同时写出来：只有图没有号，用户说「我要第 5 个」时，
    就只能靠文件名排序猜 —— 而 01 和 1 在资源管理器里排出来的顺序不一样，迟早对错。
    索引.json 给机器读，索引.txt 给人看。
    """
    jm = _jm()
    # 两种模式：
    #   tag    站内关键词搜索（按热度/时间排）
    #   author 作者维度搜索（main_tag=2），结果里 author 字段才对得上人
    #
    # ⚠️ 两者不能混用：tag 搜索会把"标题里带这几个字"的全捞进来，
    #    而按作者找人要的是"这个人画的全集"，不筛就是一堆同名噪声。
    mode = str(params.get("mode") or "tag").strip().lower()
    keyword = str(params.get("tag") or params.get("author") or "").strip()
    if not keyword:
        raise RuntimeError("tag 不能为空")

    top = max(1, min(int(params.get("top") or 10), 40))
    order = str(params.get("order") or "latest").strip().lower()
    time_range = str(params.get("time") or "all").strip().lower()

    attempts = []
    forced = params.get("proxy")
    if forced is not None:
        attempts = [forced or None]
    else:
        fallback = jm.proxy_url()
        attempts = ([fallback] if fallback else []) + [None]

    works = []
    used = "无（直连）"
    last_error = ""

    for index, proxy in enumerate(attempts):
        base = Path(tempfile.mkdtemp(prefix="dsh-jm-pick-"))
        try:
            option = jm.make_option(base, proxy, 4)
            client = option.new_jm_client()
            if mode == "author":
                works = _search_by_author(client, keyword, top)
            else:
                works = _search_by_tag(client, keyword, order, time_range, top)
            used = proxy or "无（直连）"
            break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if index == len(attempts) - 1:
                raise RuntimeError(
                    f"搜索失败（{last_error}）。"
                    + ("直连和代理都试过了。" if len(attempts) > 1 else "当前是直连；挂个代理再试。")
                ) from exc
            continue
        finally:
            shutil.rmtree(base, ignore_errors=True)

    if not works:
        return {
            "ok": False,
            "tag": keyword,
            "order": order,
            "time": time_range,
            "count": 0,
            "items": [],
            "note": "这个 tag 在这个排序下一条都没搜到，换个写法，或把 time 放宽到 all。",
        }

    dest = params.get("dest")
    folder = Path(dest) if dest else _pick_dir(keyword, order, time_range)
    folder.mkdir(parents=True, exist_ok=True)
    for old in list(folder.glob("*.jpg")) + list(folder.glob("*.part")):
        old.unlink(missing_ok=True)

    width = 2 if len(works) < 100 else 3
    items = []
    cover_ok = 0

    for i, work in enumerate(works, start=1):
        name = f"{i:0{width}d}.jpg"
        out = folder / name
        ok = False
        for proxy in attempts:
            if _fetch_cover(_cover_url(work["id"]), out, proxy):
                ok = True
                break
        if ok:
            cover_ok += 1
        items.append({
            "no": i,
            "file": name if ok else "",
            # 封面尺寸：封面是站点按原始尺寸给的，能粗略反映这本的清晰度档位。
            # 真正的正文分辨率只有下完才知道（写进 标签.txt 的 dims）。
            "dims": _cover_dims(folder / name) if ok else "",
            "id": work["id"],
            "title": work["title"],
            "author": work["author"],
            "date": work["date"],
            "cover": ok,
        })
        print(f"[dsh-jm] 封面 {i}/{len(works)} JM{work['id']} {'OK' if ok else 'FAIL'}", file=sys.stderr)

    (folder / "索引.json").write_text(
        json.dumps({"tag": keyword, "order": order, "time": time_range, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [f"tag: {keyword}    排序: {order}    时间: {time_range}", ""]
    for it in items:
        flag = "有图" if it["cover"] else "缺图"
        dim = ("  " + it["dims"]) if it.get("dims") else ""
        lines.append(
            f"{it['no']:>2}. [{flag}] JM{it['id']}{dim}  {it['title']}  ({it['author']}, {it['date']})"
        )
    lines.append("")
    lines.append("挑好后说「第 3 个」或「3,7,9」。")
    (folder / "索引.txt").write_text("\n".join(lines), encoding="utf-8")

    remember_pick(folder, {"tag": keyword, "order": order, "time": time_range}, items)

    return {
        "ok": True,
        "tag": keyword,
        "order": order,
        "time": time_range,
        "count": len(items),
        "covers": cover_ok,
        "folder": str(folder),
        "index_txt": str(folder / "索引.txt"),
        "index_json": str(folder / "索引.json"),
        "items": items,
        "network": used,
        "note": "" if cover_ok == len(items) else f"{len(items) - cover_ok} 张封面没下下来（清单里标了缺图），不影响挑。",
    }


def _build_note(failed, deferred, max_pages):
    """把「失败」和「暂缓」分开说清楚 —— 这两种在用户眼里是完全不同的事。"""
    parts = []
    if deferred:
        ids = "、".join(f"JM{d['id']}" for d in deferred[:6])
        more = f" 等 {len(deferred)} 本" if len(deferred) > 6 else ""
        parts.append(
            f"{len(deferred)} 本超过 {max_pages} 页没下（{ids}{more}），"
            "要下的话说一声，我再补。"
        )
    if failed:
        parts.append(f"{len(failed)} 本失败，用同样的编号重跑会自动跳过已完成的。")
    return " ".join(parts)


def _resolve_numbers(numbers):
    pick = last_pick()
    table = {int(i["no"]): i for i in (pick.get("items") or [])}
    if not table:
        raise RuntimeError("还没有挑选记录。先按 tag 搜一轮（jm_pick），再来挑。")
    raw = numbers if isinstance(numbers, (list, tuple)) else str(numbers).replace("，", ",").split(",")
    picked = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        if not text.isdigit():
            raise RuntimeError(f"编号要是数字，收到的是「{text}」")
        no = int(text)
        if no not in table:
            raise RuntimeError(f"第 {no} 个不在这一轮里（这轮只有 1~{len(table)}）")
        picked.append(table[no])
    return [{"id": int(p["id"]), "title": p.get("title") or ""} for p in picked]


def _resolve_ids(ids):
    raw = ids if isinstance(ids, (list, tuple)) else str(ids).replace("，", ",").split(",")
    out = []
    for item in raw:
        text = str(item).strip().lstrip("jmJM")
        if text.isdigit():
            out.append({"id": int(text), "title": ""})
    return out


_INDEX_NAME = ".dsh-jm-index.json"


def _index_path(out_root):
    return Path(out_root) / _INDEX_NAME


def _read_index(out_root):
    try:
        return json.loads(_index_path(out_root).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_index(out_root, album_id, folder, pages):
    """
    记一笔「这个 jm 号下到哪个目录了」。

    🔴 为什么需要这张表：同名不同形。API 的 `album?id=` 返回的是**简体**标题
       （「マゾ妖怪 トイレの花子さん」→「绫枷ちよこ…」），而下载走的是另一条接口，
       拿到的 `album.name` 是**繁体**（「綾枷ちよこ…」）。两者 safe_name 之后
       是两个不同目录名，于是「按标题找已下载目录」永远找不到 ——
       skip_existing 形同虚设，每跑一次都把同样几本重下一遍。
       用 id 做键就绕开了这个坑。
    """
    try:
        data = _read_index(out_root)
        data[str(album_id)] = {"folder": Path(folder).name, "pages": int(pages)}
        _index_path(out_root).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[dsh-jm] 记下载索引失败：{exc}", file=sys.stderr)


# 🔴 一次批量里复用同一个 client。
#
#   之前 `_album_meta` 直接调 `jm.cmd_info()`，而 cmd_info 每次都
#   `JmOption.default() + make_option() + 域名探活 + 图片CDN探活`
#   —— 一轮 6~30 秒。门限判断是**每本都要查页数**的，6 本就是 3 分钟起步，
#   用户在聊天里看到的就是"点了没反应"，插件层还可能先超时。
#   这里缓存一个 client，整批共用。
_META_CLIENT = {}


def _meta_client():
    if "c" in _META_CLIENT:
        return _META_CLIENT["c"]
    jm = _jm()
    base = Path(tempfile.mkdtemp(prefix="dsh-jm-meta-"))
    option = jm.make_option(base, jm.proxy_url(), 2)
    _META_CLIENT["c"] = option.new_jm_client()
    _META_CLIENT["base"] = base
    return _META_CLIENT["c"]


def _album_meta(jm, out_root, album_id):
    """
    拿 (标题, 页数)。优先查索引 —— 索引里有就不发网络请求。

    ⚠️ 索引只记「下过的」，没下过的查不到，只能联网问一次 info。
       门限判断发生在下载之前，所以这一问是躲不开的；
       但批量里同一本只问一次，不会重复。
    """
    hit = _read_index(out_root).get(str(album_id))
    if hit:
        return str(hit.get("folder") or ""), int(hit.get("pages") or 0)
    try:
        album = _meta_client().get_album_detail(str(album_id))
        return str(getattr(album, "name", "") or ""), int(getattr(album, "page_count", 0) or 0)
    except Exception:  # noqa: BLE001
        return "", 0


def _existing_folder(jm, out_root, album_id):
    """
    这本是不是已经在 out_root 下完整下过了。

    返回 (目录, 已有页数, 应有页数)；没下过返回 None。
    先查 id 索引（可靠），再回退按标题猜（索引还没建时的老数据）。
    判据只看**页数**：半本会判不达标并重下。
    """
    img = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

    # ⚠️ 先看索引再联网。`cmd_info` 是一次真实请求（慢链路 5~30 秒），
    #    6 本挨个查就是好几分钟，用户那边等着像卡死。索引里已经存了这个 id
    #    对应的目录和页数，直接数文件就够了，一次网络都不用发。
    hit = _read_index(out_root).get(str(album_id))
    if hit and hit.get("folder"):
        folder = Path(out_root) / hit["folder"]
        if folder.is_dir():
            have = len([f for f in folder.iterdir() if f.suffix.lower() in img])
            want = int(hit.get("pages") or 0)
            if have:
                return folder, have, want or have

    # 索引里没有（第一次跑，或早先版本下过）：这次只能联网问一次，
    # 问到之后顺手按标题猜目录 —— 猜中了就回填索引，下次就快了。
    try:
        info = jm.cmd_info({"id": str(album_id)})
    except Exception:
        return None          # 查不到详情就当没下过，交给下载流程去报真错
    want = int(info.get("pages") or 0)
    title = (info.get("title") or "").strip()
    if not title:
        return None
    folder = Path(out_root) / jm.safe_name(title, 60)
    if folder.is_dir():
        have = len([f for f in folder.iterdir() if f.suffix.lower() in img])
        if have:
            _write_index(out_root, album_id, folder, want or have)
            return folder, have, want
    return None


def cmd_pick_download(params):
    """
    按编号（或直接按 id）把挑中的那几本下到桌面。

    两条路都留着：用户说「第3个」走编号，用户直接贴号走 id。
    """
    jm = _jm()
    if params.get("id"):
        targets = _resolve_ids(params["id"])
    elif params.get("number") is not None:
        targets = _resolve_numbers(params["number"])
    else:
        raise RuntimeError("要么给 number（第几个），要么给 id（jm 号）")

    if not targets:
        raise RuntimeError("没解析出要下哪几本")

    out_root = Path(params.get("dest") or jm.desktop_dir())
    try:
        out_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"存不了这个目录：{out_root}\n原因：{exc}") from exc

    threads = max(1, min(int(params.get("threads") or 6), 16))
    # ⚠️ 上限必须给到小时级。站上有 1900+ 页的合集本（比如 1489276 那个
    #    「提瓦特人形譚 1-5」），按实测 80 页/分钟算要跑 20 分钟以上，
    #    以前封顶 900 秒 = 必超时，而且超时是在**全部下完之前**掐掉的，
    #    下一次重来又从第 1 页开始 —— 永远下不完。
    per_book = max(60, min(int(params.get("per_book_timeout") or 300), 3600))

    skip_existing = params.get("skip_existing")
    skip_existing = True if skip_existing is None else bool(skip_existing)

    # 页数门限：超过就先不碰，攒到 deferred 里交给上层问用户。
    # 0 或负数 = 不限制。
    max_pages = int(params.get("max_pages") or 0)

    downloaded = []
    failed = []
    skipped = []
    deferred = []
    for index, target in enumerate(targets, start=1):
        tid = str(target["id"])

        # 先对账再决定下不下。判据是**页数够不够**，不是"文件夹在不在" ——
        # 上一轮超时中断会留下半本，只看着"有目录"就跳过的话，用户永远拿到残缺的那份。
        if skip_existing:
            hit = _existing_folder(_jm(), out_root, tid)
            if hit is not None:
                folder, have, want = hit
                if want and have >= want:
                    skipped.append({
                        "id": int(tid),
                        "title": folder.name,
                        "folder": str(folder),
                        "pages": have,
                    })
                    print(f"[dsh-jm] [{index}/{len(targets)}] 跳过 JM{tid}（已有 {have}/{want} 页）", file=sys.stderr)
                    continue

        # 🔴 大本先拦下来。用户明确要的是「超过 60 页先别下，结束再问」——
        #    这个顺序不能反：先下完再问等于把带宽和时间先花了。
        if max_pages > 0:
            meta_title, pages = _album_meta(_jm(), out_root, tid)
            if pages and pages > max_pages:
                deferred.append({
                    "id": int(tid),
                    "title": meta_title or target["title"],
                    "pages": pages,
                    "reason": f"{pages} 页，超过 {max_pages} 页上限",
                })
                print(f"[dsh-jm] [{index}/{len(targets)}] 暂缓 JM{tid}（{pages} 页 > {max_pages}）", file=sys.stderr)
                continue

        print(f"[dsh-jm] [{index}/{len(targets)}] 开始 JM{tid}", file=sys.stderr)
        started = time.time()
        try:
            final_dir, files, real_title = jm._download_into(tid, out_root, threads, per_book)
        except Exception as exc:
            failed.append({"id": int(tid), "title": target["title"], "error": str(exc)[:300]})
            continue
        total_bytes = 0
        for f in files:
            p = final_dir / f
            if p.exists():
                total_bytes += p.stat().st_size
        _write_index(out_root, tid, final_dir, len(files))
        downloaded.append({
            "id": int(tid),
            "title": real_title,
            "folder": str(final_dir),
            "pages": len(files),
            "size_mb": round(total_bytes / 1048576, 1),
            "seconds": round(time.time() - started, 1),
        })
        jm._remember_download(tid, real_title)

    # 🔴 ok 表示「这次调用本身有没有跑通」，不代表每本都下到了。
    #
    #   以前这里写的是 ok = not failed，结果只要有一本失败就整批 ok:false，
    #   插件端 runDriver 看到 ok:false 就当整体失败抛出去 —— 用户在聊天里
    #   看到的是一句「dsh-jm: 未知错误」（因为这里没填 error 字段），
    #   而实际上 6 本已经躺在桌面上了。这个误导我踩过两次才定位到。
    #
    #   现在：全挂才算 ok:false 并给出 error；部分失败用 ok:true + partial:true，
    #   失败明细留在 failed 里，由上层如实转述。
    all_failed = bool(targets) and not downloaded and not skipped and not deferred
    payload = {
        "ok": not all_failed,
        "requested": len(targets),
        "downloaded": downloaded,
        "skipped": skipped,
        "deferred": deferred,
        "failed": failed,
        # partial 让调用方一眼看出"成功但有欠账"，不用自己比 len 去推
        "partial": bool(failed) and bool(downloaded),
        "note": _build_note(failed, deferred, max_pages),
    }

    # 首次使用的提示：批量入口也要给，而且**整批只给一次**。
    # 这里检查的是和 jm.cmd_download 同一个磁盘标记，所以：
    #   · 用户先单本下过 → 这里不再提示
    #   · 用户第一次就批量下 6 本 → 只在这一处提示一次，不会 6 条
    if downloaded and jm._should_hint():
        jm._mark_hinted()
        payload["note"] = (payload["note"] + " " if payload["note"] else "") + jm._HINT_TEXT
    if all_failed:
        payload["error"] = "; ".join(
            f"JM{f['id']}: {f['error']}" for f in failed
        )[:600] or "全部失败"
    # 整批跑完把 meta client 的临时目录清掉
    try:
        shutil.rmtree(_META_CLIENT.pop("base", ""), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    _META_CLIENT.clear()
    return payload
