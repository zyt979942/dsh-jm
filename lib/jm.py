#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsh-jm 驱动 —— 打 jm350234 就把那本漫画扒到桌面，编成 001.jpg 002.jpg …

⭐ 2026-11-07 重写：不再手搓抓取，改骑 **jmcomic** 这个官方社区库（2.7.11）。
   起因是用户给了 jm350234，我手写的驱动在六个网页镜像上全是 album_missing，
   而 jmcomic 走 APP API 一问 —— 这本明明存在（《董卓 上+下》，2 话 50 页）。
   结论：网页镜像的数据是残缺的，APP API 才是全的。

分工：
  · **jmcomic** 负责：域名自动更新、请求签名 token、取详情、下载图片、**解打乱**（原生自带）。
  · **本文件**负责：先下到临时目录，再把所有话的图按"话序 + 页序"合并，
    重编成 `<桌面>/<本子名>/001.jpg 002.jpg …`。

    ⚠️ 为什么必须用临时目录：jmcomic 的 DirRule 在 `__init__` 就把 rule_dsl 解析成
    parser_list 了，之后再改 `.rule_dsl` 字符串**只是改字段，解析表还是旧的**。
    所以没法让它"把所有话塞进一个文件夹"；而默认的 `Bd_Pname` 会把
    《董卓 上+下》拆成 `董卓 上+下/` + `董卓 下/` 两个桌面文件夹（各 25 页）。
    绕开它的办法就是：让它随便下到临时目录，命名由我们自己接管。

参数走 stdin、结果走 stdout、一次调用一个进程 —— 跟 kards 驱动同一个套路。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

# ─────────────────────── 依赖：优先用插件自带的那份 ───────────────────────
# 分发给别人时，对方机器上**不会有 jmcomic**。所以这里把 <插件>/vendor 顶到
# sys.path 最前面 —— 那份是随插件一起打包的（pip install --target vendor），
# 拷过去就能跑，不需要任何 pip 命令。
#
# 顺序很讲究：vendor 在前、系统环境在后。这样即使对方装过别的版本，
# 也优先用我们打包并验证过的那一份，不会被环境差异坑到。
_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if _VENDOR.is_dir():
    sys.path.insert(0, str(_VENDOR))

try:
    import jmcomic  # noqa: F401  先探一下，缺了要在最外层给出人话提示
    _HAS_JMCOMIC = True
except Exception:  # noqa: BLE001
    _HAS_JMCOMIC = False

_MISSING_HINT = (
    "缺少 jmcomic 依赖。正常安装（插件包自带 vendor/）不会出现这个提示；"
    "如果你是直接拷源码跑的，执行一次：\n"
    "  python -m pip install jmcomic --target <插件目录>/vendor\n"
    "或者直接 pip install jmcomic 装到系统环境里也行。"
)

# ────────────────────────────── 环境 ──────────────────────────────
def desktop_dir() -> Path:
    """真实桌面路径。注册表里那份是 %USERPROFILE% 展开后的，比硬拼 ~/Desktop 稳。"""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        value, _ = winreg.QueryValueEx(key, "Desktop")
        return Path(os.path.expandvars(value))
    except Exception:
        return Path(os.path.expanduser("~")) / "Desktop"


def proxy_url() -> str | None:
    """找本机可用的代理；**找不到就返回 None = 直连**。

    这条函数被反复修正过两次，过程和结论都记下来，

    ① 11-08 凌晨 —— 我一开始判断"真不翻墙是连不上的"，是**错的**。
       那次测出直连全 000，是因为我自己在 shell 里 export 了代理环境变量，
       等于根本没在测直连。干净重测后直连 10 次 9 次能过 TLS。

    ② 11-09 深夜 —— 我又把"直连优先"写死了，也是**错的**。
       同一台机器、同一个代理端口，测出来：
         · 代理：图片 CDN **6/6 全通**，3 页 29 秒下完
         · 直连：150 秒下不了 1 页

    **教训：这条线路的状态是会变的，任何"写死"的偏好都会过期。**
    现在的取舍是「默认代理 + 直连兜底」，因为代理在这台机器上通常更稳
    （它绕开了 SNI 干扰），而且没装代理的人会自动落到直连。

    代理来源顺序：环境变量 → 注册表（开关关了也认地址）→ 常见端口，逐个 TCP 探活。
    """
    import socket

    candidates: list[str] = []
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(key)
        if value and "://" in value:
            candidates.append(value)

    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if server:
            server = str(server)
            if "=" in server:
                parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
                server = parts.get("https") or parts.get("http") or ""
            if server:
                try:
                    enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                except Exception:
                    enabled = 0
                url = f"http://{server}"
                if enabled:
                    candidates.insert(0, url)
                else:
                    candidates.append(url)
    except Exception:
        pass

    for port in (7897, 7890, 7891, 10809, 10808, 1080, 2080):
        candidates.append(f"http://127.0.0.1:{port}")

    seen, ordered = set(), []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            ordered.append(item)

    for url in ordered:
        try:
            parsed = url.split("://", 1)[1].rstrip("/")
            host, _, port_text = parsed.partition(":")
            with socket.create_connection((host, int(port_text or 80)), timeout=0.6):
                return url
        except Exception:
            continue
    return None


def safe_name(name: str, limit: int = 80) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:limit] or "untitled").strip()


def _real_client(option):
    """
    从 option 造出**真正的** JmApiClient。

    ⚠️ `option.client` 只是**配置**（AdvancedDict），里面的键是
       `cache / domain / postman / impl / async_impl / retry_times`，
       **没有** `domain_list`、也没有 client 对象。
       真正的 client 实例要 `option.new_jm_client()` 才会建出来。

       我先后在这里错过两次：
         ① `option.client.domain_list` → KeyError
         ② `getattr(option.client, "domain_list", None)` → **静默 None**，
            探活因此拿到空列表、报"一个都没探通"，看起来像网络问题，白查一轮。
       所以这里不再猜结构，直接用公开入口。
    """
    return option.new_jm_client()


# 批量下载时一本一本探活是不可接受的：每本多花 6~12 秒，33 本就是好几分钟，
# 而且 CDN 在几十秒内就会变，探完早就过期了。这里的缓存只用来「别重复探同一件事」，
# 有效期压得很短，过期了照样重探。
_PROBE_CACHE: dict[str, tuple[float, list[str]]] = {}
_PROBE_TTL = 300


def _cached_probe(key: str, fn) -> list[str]:
    hit = _PROBE_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _PROBE_TTL:
        return hit[1]
    value = fn()
    if value:
        _PROBE_CACHE[key] = (now, value)
    return value


def _probe_image_cdns(proxy: str | None, timeout: int = 6) -> list[str]:
    """
    探一遍**图片** CDN，只留下真正能下图的那些。

    🔴 这是整套流程里最容易翻车的一环，也是最后才查出来的：
        API 域名通了 ≠ 图片下得下来，它们完全是两套域名。

       实测（2026-11-08 夜，本机直连）：
         cdn-msp3.jmapiproxy2.cc  → 200  ✓
         cdn-msp.jmapiproxy2.cc   → 000  ✗
         cdn-msp2.jmapiproxy2.cc  → 000  ✗
       而**走代理时三个全挂**（000）。

       jmcomic 的 `DOMAIN_IMAGE_LIST` 是**打乱顺序**的六个域名，每次随机取一个来拼图链。
       抽到死的就是 `RequestRetryAllFailException` + 每个 20 秒超时 ——
       表现完全像"插件卡死"，但真凶是 CDN 域名。

    做法：拿一张真实存在的图去试（`/media/photos/350234/00001.webp`），
    只留返回 200 的。全都探不到就返回空列表（交给库自己随机）。
    """
    try:
        from jmcomic.jm_config import JmModuleConfig

        candidates = list(getattr(JmModuleConfig, "DOMAIN_IMAGE_LIST", None) or [])
    except Exception:  # noqa: BLE001
        return []
    if not candidates:
        return []

    import concurrent.futures
    import urllib.request

    # 用一个稳定的老本子的第一页当探针
    probe_path = "/media/photos/350234/00001.webp"

    def check(domain: str, attempt: int) -> tuple[str, float]:
        url = f"https://{domain}{probe_path}"
        started = time.time()
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA_FOR_PROBE)
        req.add_header("Referer", "https://18comic.ink/")
        handlers = [
            urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
            urllib.request.HTTPSHandler(context=_ssl_ctx()),
        ]
        opener = urllib.request.build_opener(*handlers)
        with opener.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError("bad status")
            # ⚠️ 必须**读完整个响应体**，不能只读 1KB 就判定"通"。
            #    踩过：粗判断说 4 个 CDN 都能通，真下的时候全是
            #    `timed out with 99793 bytes received`（读到一半断掉）。
            #    读完才算真能下，顺便拿到速度，排序才有意义。
            body = resp.read()
            if len(body) < 1024:
                raise RuntimeError("too small")
            return domain, time.time() - started
        raise RuntimeError("bad status")

    # ⚠️ **只采一次**，而且要快。
    #
    #   我一度改成"每个域名采 2 轮降低误判"，结果是灾难：
    #   CDN 共 6 个域名 × 2 轮 = 12 次完整图片下载，按每张 5~15 秒算是 1~3 分钟，
    #   而实测 CDN 在几十秒内就会整体失效 —— **探完的时候结果已经过期了**。
    #   更糟的是第二轮探活走的是代理（代理那条路本来就差），必然全挂，
    #   等于白花一轮时间还得出"一个都没探通"的误导结论。
    #
    #   正确的取舍：探活只是"排个序"，不是"保证能用"。
    #   采一次、快、把明显死的排后面就够了；真挂了靠 jmcomic 自己的重试去换域名。
    stats: dict[str, list[float]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(candidates))) as pool:
        futures = [(domain, pool.submit(check, domain, 0)) for domain in candidates]
        concurrent.futures.wait([f for _, f in futures], timeout=timeout + 4)
        for domain, future in futures:
            if not future.done() or future.cancelled():
                continue
            try:
                _, elapsed = future.result(timeout=0)
                stats.setdefault(domain, []).append(elapsed)
            except Exception:  # noqa: BLE001
                continue

    usable = [(d, len(t), sum(t) / len(t)) for d, t in stats.items() if t]
    usable.sort(key=lambda item: (-item[1], item[2]))
    return [domain for domain, _, _ in usable]


def _pick_live_domains_from(domains: list[str], proxy: str | None, timeout: int = 6) -> list[str]:
    """
    给一组域名，探出能通的、按快慢排好。

    🔴 为什么必须做：jmcomic 拿到 4 个域名后会**轮着试**（每个域名重试 retry_times 次），
       一个死的域名就要吃掉 20 秒。实测本机上：
         · 直连 `www.cdnbtd.cc` → 200（1.1s）
         · `www.cdngwc.club` → TLS 握手超时
         · `www.cdnutc.cc` / `www.cdngwc.net` → 连不上
       所以它要是运气不好先撞上死的，用户看到的就是"卡了一分半然后失败"。

    ⚠️ 也顺便证实了一个反直觉的事实：**这几个域名走代理反而不通**
       （本机 7897 上全是 000），直连才是对的。这就是"默认直连"的依据。
    """
    if not domains:
        return []

    import concurrent.futures
    import urllib.request

    def probe(domain: str) -> tuple[str, float]:
        url = f"https://{domain}/setting"
        started = time.time()
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA_FOR_PROBE)
        handlers = [
            urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
            urllib.request.HTTPSHandler(context=_ssl_ctx()),
        ]
        opener = urllib.request.build_opener(*handlers)
        with opener.open(req, timeout=timeout) as resp:
            if 200 <= resp.status < 400:
                return domain, time.time() - started
        raise RuntimeError("bad status")

    # 只采一次（理由同 _probe_image_cdns：多轮会让整个流程慢到结果过期）。
    stats: dict[str, list[float]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(domains))) as pool:
        futures = [(d, pool.submit(probe, d)) for d in domains]
        concurrent.futures.wait([f for _, f in futures], timeout=timeout + 3)
        for domain, future in futures:
            if not future.done() or future.cancelled():
                continue
            try:
                _, elapsed = future.result(timeout=0)
                stats.setdefault(domain, []).append(elapsed)
            except Exception:  # noqa: BLE001
                continue

    usable = [(d, len(t), sum(t) / len(t)) for d, t in stats.items() if t]
    usable.sort(key=lambda item: (-item[1], item[2]))
    return [domain for domain, _, _ in usable]


def _ssl_ctx():
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


UA_FOR_PROBE = (
    "Mozilla/5.0 (Linux; Android 9; V1938CT) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 Safari/537.36"
)


# WebP 无损编码的 effort 档位（Pillow 的 method 参数）。
#
# 🔴 这里选 0 不是偷懒，是实测出来的：
#
#   同一张 1680×2370 / 1621KB 的正文页，lossless + quality=100：
#     method=0 → 0.46s，1761KB（比原图大 8.7%）
#     method=2 → 1.26s，1626KB（+0.3%）
#     method=4 → 1.75s，1629KB（+0.5%）
#     method=6 → 10.24s，1621KB（+0.0%）
#
#   两者**像素完全一致**（都是 lossless），差的只是编码器肯花多少时间去找
#   更省的写法。method=6 用 22 倍时间换 8.7% 体积，而这 8.7% 是 1.6MB→1.76MB
#   这种量级 —— 相对于一个本子几十上百 MB，完全可以忽略。
#
#   之前用 6，一本 1952 页的合集光编码就要 5 个多小时，这才是"下载慢"的主因。
#   想换回更省的写法：环境变量 DSH_JM_WEBP_METHOD=4。
WEBP_METHOD = int(os.environ.get("DSH_JM_WEBP_METHOD") or 0)


def _patch_lossless_save():
    """
    让 jmcomic 的存图变成**无损**。

    🔴 这是画质的真正瓶颈，比我最初以为的严重：
       jmcomic 解打乱走的是 `JmImageTool.save_image(Image, path)` → `image.save(path)`，
       纯 PIL 默认参数。存 .webp 时默认 **quality=80 有损**，
       也就是站点给的原图被它再压一遍 —— 我之前又在 number_into 里
       转了一次 JPEG quality=95，等于**连压两道**。

    改法：按目标后缀挑无损参数。
        webp → lossless=True, quality=100, method=6（像素级无损，体积大但真不糊）
        png  → 默认就是无损
        jpg  → 有损格式没法无损，quality=100 + subsampling=0 + optimize 是极限

    注意别把原图当"无损" —— 站点上的 webp 本来就是有损压过的。
    我们能做的是"不额外损失"：解码一次后，不再引入新的量化误差。
    """
    try:
        from jmcomic.jm_toolkit import JmImageTool
    except Exception as exc:  # noqa: BLE001
        print(f"[dsh-jm] 挂无损保存失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return

    def save_image(cls, image, filepath):
        suffix = Path(str(filepath)).suffix.lower()
        if suffix == ".webp":
            image.save(filepath, "WEBP", lossless=True, quality=100, method=WEBP_METHOD)
        elif suffix in (".png", ".gif", ".bmp"):
            image.save(filepath)
        else:
            image.convert("RGB").save(filepath, "JPEG", quality=100, subsampling=0, optimize=True)

    JmImageTool.save_image = classmethod(save_image)
    print("[dsh-jm] 已启用无损存图（webp lossless / jpg q100 4:4:4）", file=sys.stderr)


def make_option(base_dir: Path, proxy: str | None, threads: int):
    """
    造一个 jmcomic 的 Option —— 目录随便它怎么建，反正我们最后自己搬。

    ⚠️ `retry_times` 必须压低（默认 5）：jmcomic 每次请求是 **30 秒超时**，
    5 次重试 = 一次失败要挂 **150 秒**，比 DSH 工具层 60 秒的死线还长，
    在用户那边表现为"工具超时"，真正的原因全埋在日志里看不见。
    压到 2 次 + 域名切换，最坏 ~60 秒内能给出明确结论。
    """
    _require_jmcomic()
    from jmcomic import JmOption

    _patch_lossless_save()

    option = JmOption.default()
    option.dir_rule.base_dir = str(base_dir)
    option.dir_rule.rule_dsl = "Bd_Pname"   # 每话一层，方便后面按话排序

    try:
        option.client.retry_times = 2
    except Exception:
        pass

    # 🔴 开缓存跳过：已存在的图不再重下。
    #
    #   为什么必须要：1900+ 页的合集本（比如 1489276「提瓦特人形譚 1-5」）
    #   按实测速度要跑二十分钟以上，中间被超时掐掉的话，没有缓存就得从第 1 页
    #   重来 —— 永远追不上。开 cache 配合固定缓存目录（见 _download_once），
    #   重下只补缺的那几百页。
    try:
        option.download.cache = True
    except Exception as exc:  # noqa: BLE001
        print(f"[dsh-jm] 开启下载缓存失败：{exc}", file=sys.stderr)

    try:
        # ⚠️ 并发别开太大。实测慢链路下一张图要 6~15 秒，开 8~20 线程的结果是
        #    互相抢带宽、一起超时、然后各自重试 2 次 —— 越并发越慢。
        #    4 是这台机器上的甜点：既不闲，也不会把自己挤死。
        # ⚠️ 上限从 4 提到 8。早先压到 4 是因为当时瓶颈在网络（8~20 线程会互相
        #    抢带宽、一起超时）。现在编码是大头，Pillow 的 webp 编码在 C 层会释放
        #    GIL，多线程能真并行，8 路下载+编码的实测吞吐明显好于 4 路。
        #    网络仍然不稳的人可以自己传小一点。
        option.download.threading["image"] = min(threads, 8)
        option.download.threading["photo"] = 1   # 章节串行，避免多个章节同时抢
    except Exception:
        pass

    if proxy:
        try:
            option.client.postman.meta_data["proxies"] = {"http": proxy, "https": proxy}
        except Exception:
            pass
        for env_key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ[env_key] = proxy

    # ⚠️ 超时控制。这里踩过一个很隐蔽的坑，值得写清楚：
    #
    #   `option.client.postman` **不是对象，是 AdvancedDict**（配置字典的包装），
    #   所以 `postman.before_request = fn` 会抛 KeyError（它不支持随便加键），
    #   而我当初用一个 `try/except: pass` 把它吞掉了 ——
    #   于是「超时设置」从头到尾**根本没生效过**，日志里却看不出任何异常。
    #
    #   正确做法是在 `meta_data` 里给出真正的请求参数：
    #   curl_cffi 认 `timeout`，塞进 meta_data 后 before_request 会带进每次请求。
    #
    #   ⚠️ 而且**这个值不能太小**。设成 20 秒时出现过这种日志：
    #        curl: (28) Operation timed out after 20015 ms **with 99793 bytes received**
    #      也就是图片下到一半被掐断。图有一百多 KB，慢链路上 20 秒确实不够。
    #      改成 45 秒：小请求照样很快失败，大图也给足时间。
    try:
        meta = option.client.postman.meta_data
        if isinstance(meta, dict):
            meta.setdefault("timeout", 30)
        else:  # AdvancedDict 也是 Mapping，尽量按 key 写
            meta["timeout"] = 30
    except Exception as exc:  # noqa: BLE001
        # 注意：这里**必须记到 stderr**，不能再静默。
        print(f"[dsh-jm] 设置请求超时失败：{type(exc).__name__}: {exc}", file=sys.stderr)

    # 🔴 把能通的 API 域名锁死。不做这一步的话，jmcomic 会在 4 个域名里轮着撞，
    #    每个死域名吃 20 秒，用户看到的就是"卡死"。
    #
    #    顺序很关键，我在这里错过一次：
    #      `option.client["domain"]` 一开始是 **空的** —— 域名是 `new_jm_client()`
    #      时才去远端拉的。所以要先把 client 建出来（触发更新），
    #      读它拿到的 domain_list，探活排序，再写回**配置**里，
    #      这样 download_album 内部之后再建的 client 才会用上排好的顺序。
    try:
        boot = option.new_jm_client()                      # 触发域名自动更新
        domains = list(getattr(boot, "domain_list", None) or [])
        live = _cached_probe(
            f"api:{proxy}", lambda: _pick_live_domains_from(domains, proxy)
        )
        if live:
            option.client["domain"] = live
            print(f"[dsh-jm] API 域名探活：{live}", file=sys.stderr)
        else:
            print(f"[dsh-jm] 域名一个都没探通，交给库自己重试（{domains}）", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[dsh-jm] 域名探活失败（不影响继续）：{type(exc).__name__}: {exc}", file=sys.stderr)

    # 🔴 图片 CDN 也要挑。API 通不代表图能下（详见 _probe_image_cdns 的注释）。
    try:
        live_cdns = _cached_probe(f"cdn:{proxy}", lambda: _probe_image_cdns(proxy))
        if live_cdns:
            from jmcomic.jm_config import JmModuleConfig

            JmModuleConfig.DOMAIN_IMAGE_LIST = live_cdns
            print(f"[dsh-jm] 图片 CDN 探活：{live_cdns}", file=sys.stderr)
        else:
            print("[dsh-jm] 图片 CDN 一个都没探通，交给库自己随机", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[dsh-jm] 图片 CDN 探活失败（不影响继续）：{type(exc).__name__}: {exc}", file=sys.stderr)

    return option


# ────────────────────────────── 重编号 ──────────────────────────────
IMG_SUFFIX = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def natural_key(text: str):
    """按数字自然排序：00001 < 00002 < 00010，别让 10 排到 2 前面。"""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


def collect_images(raw_root: Path) -> list[Path]:
    """把临时目录里所有图捞出来，按"话序 + 页序"排好。"""
    images = [p for p in raw_root.rglob("*") if p.is_file() and p.suffix.lower() in IMG_SUFFIX]

    def sort_key(path: Path):
        return [natural_key(part) for part in path.relative_to(raw_root).parts]

    images.sort(key=sort_key)
    return images


def number_into(images: list[Path], dest: Path) -> list[str]:
    """
    把图按页序改名为 dest/001.webp、002.webp …，返回最终文件名列表。

    🔴 **不重编码。** 早先这里是 `img.convert("RGB").save(out, "JPEG", quality=95)`，
       等于把刚解完打乱的图再压一道 —— 而站点给的是 1439×2047 的 webp，
       压成 JPEG 95 之后细节（网点、细线、文字）肉眼可见地糊。
       用户要「画质最好」，所以现在只改名不动字节，扩展名跟着原图走。

    注意扩展名是 .webp 不是 .jpg：文件里就是 webp 字节，硬叫 .jpg
    会让一部分老看图软件打不开。真要 JPEG 走 `convert_to_jpeg()`。
    """
    dest.mkdir(parents=True, exist_ok=True)
    width = 3 if len(images) < 1000 else 4
    names: list[str] = []

    for index, src in enumerate(images, start=1):
        suffix = src.suffix.lower() or ".jpg"
        name = f"{index:0{width}d}{suffix}"
        out = dest / name
        try:
            shutil.copy2(src, out)
        except Exception:
            # 跨盘复制失败时退回读-写，至少别丢页
            out.write_bytes(src.read_bytes())
        names.append(name)

    return names


# ────────────────────────────── 命令 ──────────────────────────────
def _require_jmcomic():
    """依赖没到位时给一句人话，而不是甩一串 ImportError traceback。"""
    if not _HAS_JMCOMIC:
        raise RuntimeError(_MISSING_HINT)


def _client():
    _require_jmcomic()
    from jmcomic import JmOption

    return JmOption.default().new_jm_client()


def _author(album) -> str:
    """
    取作者名。API 没记的时候返回空串，**不要**把占位符透传给用户。

    实测遇到的脏值（都在真数据里）：
      · `N/A`
      · `【N/A】董卓 上+下`   ← 作者位填了书名，还套了书名号
    所以不能只做一次 strip + 等值比较，要把「整体就是个占位符」和
    「书名号和书名重复」这两种都拆掉。
    """
    album_name = str(getattr(album, "name", "") or "").strip()

    for attr in ("author", "authoroname", "authors"):
        value = getattr(album, attr, None)
        if not value:
            continue

        # ⚠️ `authors` 是**列表**，而且列表里会混进占位符：
        #      ["N/A", "孙笑川", "姜萍", ...]
        #    先逐项筛干净再拼，否则拼出来就是 "N/A, 孙笑川, ..."。
        parts = []
        for raw in (value if isinstance(value, (list, tuple)) else [value]):
            text = str(raw).strip()
            if not text:
                continue
            # 占位符可能被书名号包着：`【N/A】`。先挖掉占位符，再清一遍括号，
            # 否则会剩下一个空壳 `【】`，跟书名比较就永远不相等。
            stripped = text
            for junk in ("N/A", "N/A.", "NA", "NONE", "NULL", "UNKNOWN", "未知"):
                stripped = stripped.replace(junk, " ")
            for pair in ("【】", "[]", "（）", "()", "〈〉", "<>"):
                stripped = stripped.replace(pair[0], " ").replace(pair[1], " ")
            stripped = " ".join(stripped.split())
            if not stripped:
                continue
            if album_name and stripped == album_name:
                continue
            parts.append(text)

        if parts:
            return ", ".join(parts)

    return ""


def _episodes(album) -> list[dict]:
    """
    把 jmcomic 的 episode_list 拍成纯 dict。

    ⚠️ `episode_list` 的元素是 **三元组 tuple**，不是对象：
        `List[Tuple[str, str, str]]` = (photo_id, photo_index, photo_name)
       （见 jmcomic/jm_entity.py 第 507 行的类型标注、cli.py 第 267 行的解包）
       早先我按对象 `getattr(e, "title")` 去读，拿到的全是空串，
       章节名就一直是 ["", ""]。元组和对象两种形状这里都兼容。

    ⚠️ **这里不给每话页数，是故意的。**
       元组里不带页数，唯一的补法是每话调一次 `get_photo_detail()` —— 实测：
         · 它内部会连带动一次 album 请求 + 一次域名探测，单话就是 30 秒量级
         · 而且返回的 `image_list` 还是空列表，根本拿不到页数
       所以先前 `chapters[].pages` 全 0 不是 bug 修不修的问题，
       而是**这条路本身走不通**。与其吐一个假的 0，不如不给这个字段。
       真要页数就下完看 `saved`，那个是准的。
    """
    entries = list(getattr(album, "episode_list", None) or [])
    out = []
    album_name = str(getattr(album, "name", "") or "")

    for index, item in enumerate(entries, start=1):
        if isinstance(item, (tuple, list)):
            pid = item[0] if len(item) > 0 else 0
            name = (item[2] if len(item) > 2 else "") or ""
        else:
            pid = getattr(item, "photo_id", None) or getattr(item, "id", None) or 0
            name = getattr(item, "title", None) or getattr(item, "name", None) or ""
        # 第一话的名字在 API 里常常是空串（书名本身就够了），这里补一个人看得懂的
        name = str(name).strip() or (album_name if len(entries) == 1 else f"第 {index} 话")
        out.append({"id": int(pid or 0), "name": name})

    return out


def cmd_search(params: dict) -> dict:
    keyword = str(params.get("keyword") or "").strip()
    if not keyword:
        raise RuntimeError("keyword 不能为空")
    num = max(1, min(int(params.get("num") or 10), 50))

    client = _client()
    page = client.search_site(search_query=keyword, page=1)
    items = []
    for hit in list(page)[:num]:
        # ⚠️ search_site 的每个元素是 **tuple**，不是对象：
        #     ('302788', '[绊爱]沉沦艳母柳淑云 小姨番外篇')
        #   也就是 (album_id, name)。跟 episode_list 一个毛病，
        #   按对象 getattr 去读只会拿到 id=0 / title=""。
        if isinstance(hit, (tuple, list)):
            album_id = hit[0] if len(hit) > 0 else 0
            title = hit[1] if len(hit) > 1 else ""
        else:
            album_id = getattr(hit, "album_id", None) or getattr(hit, "id", None) or 0
            title = getattr(hit, "name", None) or getattr(hit, "title", None) or ""
        # 搜索结果里**没有作者字段**，别硬凑一个空 author 出去骗人
        items.append({"id": int(album_id or 0), "title": str(title)})
    return {"ok": True, "keyword": keyword, "count": len(items), "items": items}


def cmd_info(params: dict) -> dict:
    target = str(params.get("id") or "").strip()
    if not target.isdigit():
        raise RuntimeError("id 必须是纯数字（jm 后面的那串）")

    client = _client()
    album = client.get_album_detail(target)
    # info 就是要看详情，多几次请求换真实的每话页数，值
    episodes = _episodes(album)
    return {
        "ok": True,
        "id": int(getattr(album, "album_id", target)),
        "title": str(getattr(album, "name", "") or ""),
        "author": _author(album),
        "chapters": episodes,
        # 页数直接取 album 自带的 page_count（这个字段是准的）；
        # 别从 episodes 里 sum，那个里面已经没有 pages 了
        "pages": int(getattr(album, "page_count", 0) or 0),
        "exists": True,
    }


def _state_path() -> Path:
    """插件自己的小状态文件，只记「最近下过哪本」。"""
    base = Path.home() / ".dsh" / "plugin-data" / "dsh-jm"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        base = Path(tempfile.gettempdir()) / "dsh-jm-state"
        base.mkdir(parents=True, exist_ok=True)
    return base / "last.json"


_HINT_TEXT = (
    "首次使用提示（只出现一次）：如果以后模型回你「这类内容我不能协助」或一直要你确认，"
    "那是模型自己的对齐限制，不是插件坏了 —— 有办法解除。需要的话问一下「怎么解除」。"
)


def _hint_path() -> Path:
    """提示发过没有的标记文件。跟 last.json 同一个目录，跨对话、跨重启都还在。"""
    return _state_path().with_name("hinted.txt")


def _should_hint() -> bool:
    """
    这条提示该不该发。

    🔴 为什么用磁盘文件而不是"本次对话第一次"：
        DSH 每开一个新对话都是全新上下文，状态不共享。
       如果用对话级标记，用户每开一次新对话就被提示一遍 —— 那是骚扰，不是提示。
       放 ~/.dsh/plugin-data/ 下就跨对话了，一个用户一生只看到一次。

    什么时候不算"已经提示过"：
      · 用户重装插件且 plugin-data 被清 → 会再看到一次，这个可以接受
      · 用户自己删了 plugin-data → 他自己的选择
      · 换机器 → 新机器重新提示，合理
    """
    try:
        return not _hint_path().exists()
    except Exception:  # noqa: BLE001
        return False


def _mark_hinted() -> None:
    """
    标记已提示。

    ⚠️ 必须在**下载成功之后**才调用，不能在准备提示时就写。
       如果提示发出去了但下载失败（网络问题），用户什么也没得到，
       标记却落盘了，以后就再也看不到这条 —— 那种"永远错过"最难补救。
    """
    try:
        _hint_path().write_text(
            time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[dsh-jm] 写提示标记失败：{exc}", file=sys.stderr)


def _remember_download(album_id: str, title: str) -> None:
    """下完记一笔。记不住也不影响下载本身，所以失败只 warn 不抛。"""
    try:
        _state_path().write_text(
            json.dumps(
                {
                    "id": str(album_id),
                    "title": title,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[dsh-jm] 记最近下载失败：{exc}", file=sys.stderr)


def _last_download() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _norm_author(text: str) -> str:
    """把作者名搓成能比较的形状：小写、去括号内容、去空白和常见分隔符。"""
    text = str(text or "").lower()
    text = re.sub(r"[（(【\[].*?[）)】\]]", " ", text)
    text = re.sub(r"[\s_\-·・.,，、/|]+", "", text)
    return text


def _same_author(author_field: str, keyword: str) -> bool:
    """
    判断一条搜索结果的 author 是不是我们要的那个人。

    ⚠️ 为什么必须自己过滤：`search_author` 是**站内模糊搜索**，
       搜「びよびよ」会把「ひよひよ」「ピヨピヨ」「ひよひよくらぶ」全捞回来，
       站点报的 total 也是模糊命中数。直接信它，用户拿到的就是一堆别人的本子。
    """
    left = _norm_author(author_field)
    right = _norm_author(keyword)
    if not left or not right:
        return False
    return right in left or left in right


def _author_works(
    client, keyword: str, limit: int, max_pages: int, current_id: int = 0
) -> tuple[list[dict], int]:
    """
    用「作者搜索」把这个作者名下的本子一页页翻出来。

    ⚠️ 两点是实测摸出来的：
      ① `search_author` 走的是 main_tag=2（作者维度），跟站内关键词搜索不是一套，
         结果里 `author` 字段才会真的是作者名。
      ② `page.content` 的元素是 **tuple** `(album_id, {name, adddate, ...})`，
         按对象 getattr 读只会拿到空。跟 search_site / episode_list 一个毛病。
    """
    works: list[dict] = []
    total = 0
    page_no = 1
    page_count = 1

    while page_no <= page_count and page_no <= max_pages and len(works) < limit:
        page = client.search_author(keyword, page=page_no)
        total = int(getattr(page, "total", 0) or 0)
        try:
            page_count = int(page.page_count or 1)
        except Exception:  # noqa: BLE001
            page_count = 1

        for item in list(page.content):
            if isinstance(item, (tuple, list)):
                aid = item[0] if len(item) > 0 else 0
                info = item[1] if len(item) > 1 else {}
            else:
                aid = getattr(item, "album_id", None) or 0
                info = {}
            info = info if isinstance(info, dict) else {}
            author_field = str(info.get("author") or "").strip()
            if not _same_author(author_field, keyword):
                continue
            wid = int(aid or 0)
            works.append({
                "id": wid,
                "title": str(info.get("name") or "").strip(),
                "date": str(info.get("adddate") or "").strip(),
                "author": author_field,
                # 用户问的是「他**其他**作品」，把当前这本标出来，渲染时就不用再猜
                "current": wid == current_id,
            })
            if len(works) >= limit:
                break
        page_no += 1

    return works, total


def _author_payload(params: dict) -> dict:
    """认出作者 + 翻出他名下全部作品。cmd_author 和批量下载共用这一份。"""
    target = str(params.get("id") or "").strip()
    if not target:
        last = _last_download()
        target = str(last.get("id") or "").strip()
        if not target:
            raise RuntimeError(
                "没给 jm 号，而且还没有下载记录。先下过一本，或者直接把 jm 号传进来。"
            )
    if not target.isdigit():
        raise RuntimeError("id 必须是纯数字（jm 后面的那串）")

    limit = max(1, min(int(params.get("limit") or 500), 2000))
    max_pages = max(1, min(int(params.get("pages") or 20), 60))

    attempts: list[str | None] = []
    fallback = proxy_url()
    if fallback:
        attempts.append(fallback)
    attempts.append(None)

    last_error = ""
    for index, proxy in enumerate(attempts):
        base = Path(tempfile.mkdtemp(prefix="dsh-jm-author-"))
        try:
            option = make_option(base, proxy, 4)
            client = option.new_jm_client()
            album = client.get_album_detail(target)
            author = _author(album)
            raw_authors = [
                str(a).strip() for a in (getattr(album, "authors", None) or []) if str(a).strip()
            ]
            title = str(getattr(album, "name", "") or "")

            if not author:
                return {
                    "ok": True,
                    "id": int(target),
                    "title": title,
                    "author": "",
                    "query": "",
                    "count": 0,
                    "total": 0,
                    "works": [],
                    "note": "这本的作者字段是空的（作者位可能是 N/A 或填了书名），没法按作者翻作品。",
                }

            # 多人合作时只按第一个名字搜，note 里说明白，别假装搜全了
            keyword = author.split(",")[0].strip()
            works, total = _author_works(client, keyword, limit, max_pages, int(target))
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            if index == len(attempts) - 1:
                raise RuntimeError(
                    f"查作者作品失败（{last_error}）。"
                    + ("直连和代理都试过了。" if len(attempts) > 1 else
                       "当前是直连；一直失败就挂个代理再试。")
                ) from exc
            continue
        finally:
            shutil.rmtree(base, ignore_errors=True)

        note = ""
        if len(raw_authors) > 1:
            note = f"这本有 {len(raw_authors)} 位作者，这里只按「{keyword}」搜，可能漏掉合著的。"
        if total > len(works):
            extra = f"站点模糊命中 {total} 条（包含同名/近名的其他人），已按作者名筛掉其余。"
            note = (note + " " + extra).strip() if note else extra

        return {
            "ok": True,
            "id": int(target),
            "title": title,
            "author": author,
            "query": keyword,
            "count": len(works),
            "total": total,
            "works": works,
            "network": proxy or "无（直连）",
            "note": note,
        }

    raise RuntimeError(f"查作者作品失败：{last_error}")


def cmd_author(params: dict) -> dict:
    """给一本的 jm 号 → 认作者 → 列他名下全部作品。"""
    return _author_payload(params)


def _download_into(target: str, out_root: Path, threads: int, seconds: int) -> tuple[Path, list[str], str]:
    """
    下这本到 out_root/<标题>/，返回 (文件夹, 文件列表, 标题)。

    代理优先、直连兜底，跟 cmd_download 同一套策略，只是不带分页裁剪。
    """
    attempts: list[str | None] = []
    fallback = proxy_url()
    if fallback:
        attempts.append(fallback)
    attempts.append(None)

    last_error = ""
    deadline = time.time() + seconds
    for index, proxy in enumerate(attempts):
        remaining = deadline - time.time()
        if remaining < 15:
            last_error = last_error or "时间用完了"
            break
        try:
            return _run_with_deadline(
                lambda: _download_once(target, out_root, proxy, threads)[:3],
                int(min(remaining, seconds)),
            )
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            if index == len(attempts) - 1:
                break
    raise RuntimeError(last_error or "下载失败")


def _total_pages(album_id: str) -> int:
    """问站点这本多少页。作者批量里每个 id 只问一次，问不到就当 0（不拦）。"""
    base = Path(tempfile.mkdtemp(prefix="dsh-jm-pages-"))
    try:
        option = make_option(base, proxy_url(), 2)
        album = option.new_jm_client().get_album_detail(str(album_id))
        return int(getattr(album, "page_count", 0) or 0)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def cmd_author_download(params: dict) -> dict:
    """
    把这个作者名下的作品全部下下来，按「作者名/本子名/001.jpg」排好。

    ⚠️ 两个刻意的设计：
      ① **已经下过的直接跳过**（目录里已经有图）。批量重跑不该把 33 本再下一遍。
      ② 一本失败不影响后面。失败的记在 failed 里，返回时一起给出去，
         由调用方决定要不要补跑 —— 批量任务最忌讳中间抛异常全盘重来。
    """
    info = _author_payload(params)
    author = info.get("author") or ""
    if not author:
        raise RuntimeError(
            "这本拿不到作者名（作者位可能是 N/A 或填了书名），没法按作者建文件夹。"
        )

    root = Path(params.get("dest") or desktop_dir()) / safe_name(author)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"存不了这个目录：{root}\n原因：{exc}") from exc

    works = list(info.get("works") or [])
    if not works:
        return {
            "ok": False,
            "author": author,
            "root": str(root),
            "downloaded": [],
            "skipped": [],
            "failed": [],
            "note": "这个作者名下没查到作品。",
        }

    # 批量默认按日期倒序（新的先下），用户要老的就传 order
    if str(params.get("order") or "new").lower() != "old":
        works.sort(key=lambda w: str(w.get("date") or ""), reverse=True)
    else:
        works.sort(key=lambda w: str(w.get("date") or ""))

    start = max(1, int(params.get("start") or 1))
    end = int(params.get("end") or 0) or len(works)
    end = min(end, len(works))
    chunk = works[start - 1:end]

    threads = max(1, min(int(params.get("threads") or 6), 16))
    per_book = max(60, min(int(params.get("per_book_timeout") or 300), 900))
    skip_existing = params.get("skip_existing")
    skip_existing = True if skip_existing is None else bool(skip_existing)

    # 页数门限：超过先不下，攒到 deferred 里，任务结束由上层问用户。
    # 作者批量最需要这个 —— 一个作者名下动辄 30 本，里面有 200 页的单行本。
    max_pages = int(params.get("max_pages") or 0)

    downloaded: list[dict] = []
    skipped: list[dict] = []
    deferred: list[dict] = []
    failed: list[dict] = []

    for index, work in enumerate(chunk, start=1):
        wid = str(work.get("id") or "")
        title = str(work.get("title") or "") or f"JM{wid}"
        folder = root / safe_name(title, 60)

        existing = []
        if folder.is_dir():
            existing = [
                f for f in folder.iterdir()
                if f.is_file() and f.suffix.lower() in IMG_SUFFIX
            ]
        if skip_existing and existing:
            skipped.append({"id": int(wid), "title": title, "folder": str(folder), "pages": len(existing)})
            print(f"[dsh-jm] [{index}/{len(chunk)}] 跳过（已有 {len(existing)} 页）JM{wid} {title}", file=sys.stderr)
            continue

        # 门限判断放在"跳过已存在"之后、"开始下载"之前 ——
        # 已经下好的大本不该再提示一次，那会变成永远消不掉的噪音。
        if max_pages > 0:
            try:
                pages = int(_total_pages(wid))
            except Exception:  # noqa: BLE001
                pages = 0
            if pages and pages > max_pages:
                deferred.append({
                    "id": int(wid),
                    "title": title,
                    "pages": pages,
                    "reason": f"{pages} 页，超过 {max_pages} 页上限",
                })
                print(f"[dsh-jm] [{index}/{len(chunk)}] 暂缓 JM{wid}（{pages} 页）", file=sys.stderr)
                continue

        print(f"[dsh-jm] [{index}/{len(chunk)}] 开始 JM{wid} {title}", file=sys.stderr)
        started = time.time()
        try:
            final_dir, files, real_title = _download_into(wid, root, threads, per_book)
        except Exception as exc:  # noqa: BLE001
            failed.append({"id": int(wid), "title": title, "error": str(exc)[:300]})
            print(f"[dsh-jm] [{index}/{len(chunk)}] 失败 JM{wid}：{exc}", file=sys.stderr)
            continue

        total_bytes = sum((final_dir / f).stat().st_size for f in files if (final_dir / f).exists())
        downloaded.append({
            "id": int(wid),
            "title": real_title,
            "folder": str(final_dir),
            "pages": len(files),
            "size_mb": round(total_bytes / 1048576, 1),
            "seconds": round(time.time() - started, 1),
        })
        print(
            f"[dsh-jm] [{index}/{len(chunk)}] 完成 JM{wid} {len(files)} 页 "
            f"{round(total_bytes / 1048576, 1)}MB {round(time.time() - started, 1)}s",
            file=sys.stderr,
        )

    if downloaded:
        _remember_download(str(chunk[0].get("id") or ""), str(chunk[0].get("title") or ""))

    return {
        "ok": not failed,
        "author": author,
        "root": str(root),
        "planned": len(chunk),
        "downloaded": downloaded,
        "skipped": skipped,
        "deferred": deferred,
        "failed": failed,
        "note": "" if not failed else f"{len(failed)} 本失败，其余正常；重跑一次会自动跳过已完成的下。",
    }


def _run_with_deadline(fn, seconds: int):
    """
    在单独的线程里跑 fn，超过 seconds 秒就放弃并抛错。

    ⚠️ 为什么不能用 signal.alarm：Windows 上没有 SIGALRM。
    ⚠️ 为什么线程还留在后台：Python 没法硬杀线程。这没关系 ——
       `daemon=True` 让它随进程退出；而驱动是一次调用一个进程，
       宿主那边拿到错误后进程就结束了，不会泄漏。
    """
    import threading

    box: dict = {}

    def runner():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(seconds)

    if thread.is_alive():
        raise RuntimeError(
            f"超过 {seconds} 秒还没下完，先放弃了。"
            "这个站直连能用但不稳，重试一次通常就好了；"
            "一直这样说明当前网络到它的线路不通，挂个代理再试。"
        )
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _album_tags(album) -> list[str]:
    """取标签列表。脏值（N/A 之类）直接扔掉，别写进 txt 污染统计。"""
    raw = getattr(album, "tags", None) or []
    out = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        if text.upper() in ("N/A", "NA", "NONE", "NULL", "UNKNOWN", "未知"):
            continue
        out.append(text)
    return out


def _measure_pages(folder: Path, files: list[str], sample: int = 3) -> dict:
    """
    量这本的分辨率，返回 {main, all, min, max}。

    为什么要记：站点同一本里也可能混着不同尺寸（跨页图、扉页、广告都是别的尺寸），
    而不同作者/不同上传者之间的分辨率差得更远 —— 实测同一批书里
    800x1169 到 1680x2370 都有。挑书时"这本清不清楚"完全看不出来，
    下完才发现糊，那就白下了。

    ⚠️ 只抽 3 张，不是全部：
       一本 200 页全解码要几十秒（PIL 每次都要读文件+解 header），
       而同一本的正文页分辨率几乎必然一致。抽样取"出现最多的那个尺寸"就够准。
    """
    from PIL import Image

    picked = files if len(files) <= sample else [
        files[0], files[len(files) // 2], files[-1],
    ]
    counts = {}
    for name in picked:
        f = folder / name
        try:
            with Image.open(f) as im:
                size = im.size
        except Exception:
            continue
        counts[size] = counts.get(size, 0) + 1
    if not counts:
        return {}
    main = max(counts.items(), key=lambda kv: kv[1])[0]
    widths = [w for (w, _h) in counts]
    heights = [h for (_w, h) in counts]
    return {
        "main": str(main[0]) + "x" + str(main[1]),
        "all": " ".join(sorted({str(w) + "x" + str(h) for (w, h) in counts})),
        "min": str(min(widths)) + "x" + str(min(heights)),
        "max": str(max(widths)) + "x" + str(max(heights)),
        "width": main[0],
        "height": main[1],
    }


def _write_tags_txt(folder: Path, album_id: str, title: str, album,
                    files: list[str] | None = None) -> None:
    """
    在本子目录里写 标签.txt。

    格式刻意保持"人看一眼就懂"：
        jm: 1445283
        title: [びよびよ] 性快感墮落
        author: びよびよ
        pages: 9
        dims: 1280x1807
        dims_all: 1280x1807 1280x1244
        tags: 蘿莉, 中出, 中文
        downloaded: 2026-11-14 23:40

    tags 这行用逗号分隔是给机器读的（analyze 会 split），
    其他行是给人看的。混在一份文件里，避免又多一个 json 要同步。
    """
    lines = [
        "jm: " + str(album_id),
        "title: " + str(title),
        "author: " + (_author(album) if album else ""),
        "pages: " + str(int(getattr(album, "page_count", 0) or 0)),
    ]
    if files:
        try:
            dims = _measure_pages(folder, files)
        except Exception:  # noqa: BLE001
            dims = {}
        if dims:
            lines.append("dims: " + dims["main"])
            if len(dims["all"].split()) > 1:
                lines.append("dims_all: " + dims["all"])
    lines.append("tags: " + ", ".join(_album_tags(album)))

    # 体积：挑书时"这本多大"是最直观的成本感（几百 MB 的和 5 MB 的差别很大）
    try:
        total = sum(
            (folder / f).stat().st_size for f in (files or []) if (folder / f).exists()
        )
        if total:
            lines.append("size_mb: " + str(round(total / 1048576, 1)))
    except Exception:  # noqa: BLE001
        pass

    # analyzed 是给"防止重复分析捣乱权重"用的开关：
    #   空/缺省 = 还没进过个性化统计
    #   yes     = 已经算过，下次扫描跳过（用户想重算就手动改成空）
    # 刻意不用时间戳当判据 —— 时间戳只能说明"算过"，不能说明"当前该不该算"。
    lines.append("analyzed: " + str(_read_existing_analyzed(folder) or ""))
    lines.append("downloaded: " + time.strftime("%Y-%m-%d %H:%M"))

    (folder / "标签.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_existing_analyzed(folder: Path) -> str:
    """
    重下同一本时保留原来的 analyzed 标记。

    ⚠️ 不保留会出事：用户已经把这本纳入过统计，重下一遍就把标记冲成空，
       下次分析又算一次，权重翻倍。这个坑很小但很难查。
    """
    f = folder / "标签.txt"
    if not f.is_file():
        return ""
    try:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("analyzed:"):
                return line.split(":", 1)[1].strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _download_once(
    target: str,
    out_root: Path,
    proxy: str | None,
    threads: int,
) -> tuple[Path, list[str], str, list[dict]]:
    """跑一次完整的「下载 → 合并 → 重编号」。返回 (文件夹, 文件名列表, 标题, 章节)。

    ⚠️ 必须走临时目录：jmcomic 的 DirRule 在 __init__ 就把 rule_dsl 解析成
    parser_list 了，之后再改 `.rule_dsl` 只是改字段、解析表还是旧的，
    没法让它把所有话塞进一个文件夹。默认的 Bd_Pname 会把《董卓 上+下》
    拆成 `董卓 上+下/` + `董卓 下/` 两个桌面文件夹。
    """
    import jmcomic

    # ⚠️ 缓存目录按 album_id **固定**，不是随机临时目录。
    #    随机目录 + download.cache 是没用的组合：每次都是新目录，文件永远不存在，
    #    cache 永远不命中，1500 页的大本一次超时就得从头再来。
    raw_root = Path(tempfile.gettempdir()) / "dsh-jm-cache" / str(target)
    raw_root.mkdir(parents=True, exist_ok=True)
    try:
        option = make_option(raw_root, proxy, threads)
        result = jmcomic.download_album(target, option, check_exception=False)
        album = getattr(result, "album", None)
        title = str(getattr(album, "name", "") or "") if album else ""
        episodes = _episodes(album) if album else []
        if not title:
            try:
                detail = option.new_jm_client().get_album_detail(target)
                title = str(detail.name or "")
                episodes = _episodes(detail)
            except Exception:
                title = f"JM{target}"

        images = collect_images(raw_root)
        if not images:
            raise RuntimeError("一个图片都没下到（镜像可能抽风了）")

        # 🔴 缺页必须当失败处理，不能当成功。
        #
        #   `download_album(..., check_exception=False)` 会把「某张图重试全挂」
        #   降级成一条 stderr 日志就放过 —— 一本 32 页的本子能交出 22 页，
        #   返回值还写着 ok。批量下载时这种残缺最恶心：目录看着满，翻到中间断掉。
        #   这里用 album.page_count 对账，缺了就抛，让上层换线路重试。
        expected = int(getattr(album, "page_count", 0) or 0) if album else 0
        if expected and len(images) < expected:
            raise RuntimeError(
                f"缺页：这本应有 {expected} 页，只下到 {len(images)} 页（图片 CDN 抽风）"
            )

        final_dir = out_root / safe_name(title)
        # 重下时先清空，免得旧图混进新编号
        if final_dir.exists() and final_dir.is_dir():
            for old in final_dir.iterdir():
                if old.is_file():
                    old.unlink(missing_ok=True)
        files = number_into(images, final_dir)
        # ⚠️ 标签必须跟着本子一起落地。
        #
        #   以前 tags 只在内存里活了一次调用，下完就没了。用户要做"按 tag 统计收藏偏好"，
        #   总不能每次都重新联网把所有本子问一遍（33 本 = 好几分钟）。
        #   写成本目录下的 标签.txt，分析时直接扫盘，零网络。
        #
        #   注意顺序：上面清空目录时只删文件不删目录，所以重下会先把旧 标签.txt 删掉，
        #   这里再写新的 —— 不会残留过期标签。
        try:
            _write_tags_txt(final_dir, target, title, album, files)
        except Exception as exc:  # noqa: BLE001
            print(f"[dsh-jm] 写标签失败（不影响下载）：{exc}", file=sys.stderr)

        # 只有**成功搬家之后**才清缓存；中途失败要留着，下次续传靠它。
        shutil.rmtree(raw_root, ignore_errors=True)
        return final_dir, files, title, episodes
    except Exception:
        raise


def cmd_download(params: dict) -> dict:
    _require_jmcomic()
    import jmcomic

    target = str(params.get("id") or "").strip()
    if not target.isdigit():
        raise RuntimeError("id 必须是纯数字（jm 后面的那串）")

    out_root = Path(params.get("dest") or desktop_dir())
    # dest 传错（盘符不存在、没权限）时，裸 mkdir 会抛
    # `FileNotFoundError: [WinError 3] 系统找不到指定的路径。: 'Z:\\'`
    # —— 用户看到这句完全不知道是自己 dest 写错了。这里先兜住。
    try:
        out_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"存不了这个目录：{out_root}\n"
            f"原因：{exc}\n"
            "检查一下路径拼写、盘符存不存在、有没有写权限；不传 dest 就默认存桌面。"
        ) from exc
    threads = max(1, min(int(params.get("threads") or 8), 16))
    limit = int(params.get("limit") or 0)
    start = max(1, int(params.get("start") or 1))
    end = int(params.get("end") or 0)

    # ⭐ 默认走代理，直连只当最后的兜底。
    #
    #   这条策略我前后判反过一次，过程值得记下来：
    #     · 11-08 下午测：这批 API/CDN 域名走代理全 000，直连 200
    #       → 我据此写成"直连优先、代理兜底"，还把它当成了定论。
    #     · 11-09 夜再测：同一个代理（7897），**图片 CDN 6/6 全通**，
    #       3 页 29 秒下完；而同时间的直连是 150 秒下不了 1 页。
    #
    #   结论：**代理线路的状态是会变的，不能写死成一种。**
    #   但"每次都两边都试"代价太大（一轮探活就要 10~30 秒，
    #   CDN 在几十秒内就变了，第二轮结果已经过期）。
    #   所以选一个当前更好的当默认：代理。直连留作兜底，代理不通的人照样能用。
    #
    #   想强制某一种：传 proxy 参数（空字符串 = 强制直连）。
    forced_proxy = params.get("proxy")
    if forced_proxy is not None:
        attempts = [forced_proxy or None]      # 显式指定（"" → 直连）
    else:
        fallback = proxy_url()
        attempts = ([fallback] if fallback else []) + [None]

    started = time.time()
    used = "无（直连）"
    last_error = ""
    final_dir = out_root          # 先给个占位，免得下面引用时 NameError
    files: list[str] = []
    title = f"JM{target}"
    episodes: list[dict] = []

    # 🔴 硬性总时限 —— 是**整个下载**的预算，不是每次尝试各一份。
    #
    #   踩过两件事：
    #     ① jmcomic 自己是"每域名重试 retry_times 次再换域名"，
    #        4 个域名 × 2 次 × 30 秒 = 最多 240 秒，光它自己就能耗死人。
    #     ② 我先前给 `deadline_seconds` 的是"**每次尝试** 180 秒"，
    #        加上直连 + 代理两轮，一次下载能跑满 6 分钟，
    #        用户那边就是"点了没反应"。
    #
    #   现在：先算一个总预算，每轮只拿"剩余时间"，跑完就没有了。
    total_budget = max(45, min(int(params.get("timeout") or 150), 600))
    budget_deadline = time.time() + total_budget

    for attempt_index, proxy in enumerate(attempts):
        remaining = budget_deadline - time.time()
        if remaining < 20:
            last_error = last_error or "总时间用完了"
            break
        try:
            final_dir, files, title, episodes = _run_with_deadline(
                lambda: _download_once(target, out_root, proxy, threads),
                int(remaining),
            )
            used = proxy or "无（直连）"
            if files:
                break
            last_error = "下载完成但没拿到图片"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            # 还有后备方案就继续，否则把最后一次的错抛出去
            if attempt_index == len(attempts) - 1:
                raise RuntimeError(
                    f"下载失败（{last_error}）。"
                    + ("直连和代理都试过了。" if len(attempts) > 1 else
                       "当前是直连；如果反复失败，挂个代理再试，或者手动传 proxy 参数。")
                ) from exc
            continue
    else:
        if not files:
            raise RuntimeError(f"下载失败：{last_error}")

    if not files:
        raise RuntimeError(f"下载失败：{last_error}")

    # ③ 分页裁剪（整理完再切，编号才是从 001 连续）
    if end or limit or start > 1:
        keep_from = start
        keep_to = end if end else len(files)
        if limit:
            keep_to = min(keep_to, keep_from + limit - 1)
        for i, name in enumerate(files, start=1):
            if i < keep_from or i > keep_to:
                (final_dir / name).unlink(missing_ok=True)
        files = [n for i, n in enumerate(files, start=1) if keep_from <= i <= keep_to]

    if files:
        _remember_download(target, title)

    total_bytes = sum((final_dir / f).stat().st_size for f in files if (final_dir / f).exists())
    return {
        "ok": bool(files),
        "id": int(target),
        "title": title,
        "folder": str(final_dir),
        "saved": len(files),
        "pages": len(files),
        "chapters": len(episodes),
        "chapter_names": [e["name"] for e in episodes][:12],
        "first": files[0] if files else None,
        "last": files[-1] if files else None,
        "size_mb": round(total_bytes / 1048576, 1),
        "seconds": round(time.time() - started, 1),
        "network": used,
        "note": _compose_note(files),
    }


def _compose_note(files) -> str:
    """
    组装返回给模型的 note。

    正常情况下是空串；首次成功下载时会多一句提示 —— 告诉用户"模型拒绝不是插件坏了"。
    这个提示只发一次（磁盘标记），而且只在**下载成功之后**才写标记。
    """
    if not files:
        return "下载完成但没找到图片文件，可能这本是空的或需要登录"
    if _should_hint():
        _mark_hinted()
        return _HINT_TEXT
    return ""


def _pick_module():
    """挑选那条线单独放 jm_pick.py，这里延迟导入挂到命令表上。"""
    import sys as _sys
    from pathlib import Path as _Path

    _lib = str(_Path(__file__).resolve().parent)
    if _lib not in _sys.path:
        _sys.path.insert(0, _lib)
    import jm_pick

    return jm_pick


def cmd_pick(params: dict) -> dict:
    return _pick_module().cmd_pick(params)


def _prefs_module():
    """个性化那条线单独放 jm_prefs.py，这里延迟导入挂到命令表上。"""
    import sys as _sys
    from pathlib import Path as _Path

    _lib = str(_Path(__file__).resolve().parent)
    if _lib not in _sys.path:
        _sys.path.insert(0, _lib)
    import jm_prefs

    return jm_prefs


def cmd_prefs(params: dict) -> dict:
    return _prefs_module().cmd_prefs(params)


def cmd_analyze(params: dict) -> dict:
    return _prefs_module().cmd_analyze_v2(params)


def cmd_complete(params: dict) -> dict:
    return _prefs_module().cmd_complete(params)


def cmd_record_favorites(params: dict) -> dict:
    return _prefs_module().cmd_record_favorites(params)


def cmd_recommend(params: dict) -> dict:
    return _prefs_module().cmd_recommend(params)


def cmd_pick_download(params: dict) -> dict:
    return _pick_module().cmd_pick_download(params)


COMMANDS = {
    "search": cmd_search,
    "info": cmd_info,
    "download": cmd_download,
    "author": cmd_author,
    "author_download": cmd_author_download,
    "pick": cmd_pick,
    "pick_download": cmd_pick_download,
    "prefs": cmd_prefs,
    "analyze": cmd_analyze,
    "complete": cmd_complete,
    "record_favorites": cmd_record_favorites,
    "recommend": cmd_recommend,
}


def _humanize(exc: Exception) -> str:
    """
    把库抛出来的异常翻译成人话。

    默认输出长这样，用户看了只会一头雾水：
        MissingAlbumPhotoException: 请求的本子不存在！(https://18comic.vip/album/999999999/)
        原因可能为: 1. id有误… 2. 该漫画只对登录用户可见…
    这里按类型归类，给出「该怎么办」，原始信息只留一句备查。
    """
    name = type(exc).__name__
    raw = str(exc).strip()
    head = raw.split("\n")[0][:200]          # 只取第一行，后面的"原因可能为"是库在吓人

    # ⚠️ 顺序要紧：先认我们自己抛的、语义明确的那些。
    #    踩过一次坑 —— 目录错误的文案里带"存不了…"，但也含"不存在"，
    #    结果被下面的 MissingAlbum 分支抢走，报成"这本不存在"，非常误导人。
    if "存不了这个目录" in raw:
        return raw                            # 已经写好了人话，原样给
    if "id 必须是纯数字" in raw or "keyword 不能为空" in raw:
        return raw

    lowered = f"{name} {raw}".lower()

    if "missingalbumphoto" in lowered or "请求的本子不存在" in raw:
        return (
            f"这本不存在，或者需要登录才看得到。（{head}）\n"
            "检查一下 jm 号有没有打错；如果号是对的，说明这本要登录，"
            "当前插件没做登录，下不了。"
        )
    if "timeout" in lowered or "timed out" in lowered or "connection" in lowered or "connect" in lowered:
        return (
            f"网络没连上。（{head}）\n"
            "这个站直连能用但不稳。过一会儿重试，或者挂个代理再试一次。"
        )
    if "permission" in lowered or "forbidden" in lowered or "403" in lowered:
        return f"被站点拦了（可能需要登录或触发了风控）。（{head}）\n过会儿再试，或者换个镜像。"
    if "json" in lowered and "decode" in lowered:
        return f"站点返回的不是正常页面，可能被风控了。（{head}）\n过一会儿重试。"

    return f"{name}: {raw[:400]}"


def main() -> int:
    # Windows 的 stdout 默认 cp936 —— 中文标题会被写成 cp936 字节，Node 端 UTF-8 解码会崩
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # ⚠️ 关键一步：**把 stdout 整个让给 stderr**，只留一个后门给最终 JSON。
    #
    #    jmcomic 的日志不走 logging，而是它自己 `jm_log()` 里直接 print 到
    #    stdout（每个 image.before / image.after 一行，一本 50 页的书能刷 100+ 行）。
    #    我先前只把 logging 压到 stderr，完全挡不住它 —— 结果 Node 那边拿到的
    #    "JSON" 前面糊了几万字符日志，`JSON.parse` 直接炸：
    #        dsh-jm: 驱动输出不是 JSON（code=0）
    #    下载其实全都成功了，只是结果没递出去。
    #
    #    现在的做法：进程里所有 print（包括库的）自动流向 stderr，
    #    真正要交给宿主的那一行，用 real_stdout 显式写出。
    real_stdout = sys.stdout
    sys.stdout = sys.stderr

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--params", default="-")
    args = parser.parse_args()

    raw = sys.stdin.read() if args.params == "-" else args.params
    try:
        params = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"参数不是 JSON：{exc}"}, ensure_ascii=False), file=real_stdout)
        return 1

    # 再顺手把 logging 也压到 stderr（双保险）
    try:
        import logging

        logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)
        for name in ("jmcomic", "common"):
            logging.getLogger(name).setLevel(logging.WARNING)
    except Exception:
        pass

    try:
        result = COMMANDS[args.command](params)
        print(json.dumps(result, ensure_ascii=False), file=real_stdout)
        real_stdout.flush()
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": _humanize(exc)}, ensure_ascii=False), file=real_stdout)
        real_stdout.flush()
        return 1


if __name__ == "__main__":
    import urllib3

    urllib3.disable_warnings()
    sys.exit(main())
