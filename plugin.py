"""麦麦贴吧搬石 —— 让麦麦把贴吧的帖子搬进群。

只做三件事：逛吧、取帖、发到群里。签到、回帖、私信一概不碰。

设计取舍：

1. **不登录**。贴吧对「一个 cookie 都不带」的请求会拦成 403，先去百度首页拿一次访客
   cookie 再回来就正常了 —— 所以插件不需要任何人提供账号或票据。
2. **被拦就停**。触发「百度安全验证」时立刻结束本轮并冷却 15 分钟，而不是换个姿势继续
   敲门；已经搬过的记录保留，下一轮接着来。
3. **两种呈现各自独立**：文本 + 图片（一层层发）、一张长图（整帖渲染）。两个开关都开
   就都发；长图渲染失败会自动退回文本，不让用户什么都收不到。
4. **去重靠本地记录**（插件 data 目录里的 JSON），不动宿主的数据库 ——
   记录丢了最多重搬一次，不会影响机器人本身。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json as _json
import re
import sys as _sys
import time
import urllib.parse
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

_PLUGIN_DIR = Path(__file__).resolve().parent
# 插件目录名带连字符，不能当包导入：把自身目录塞进 sys.path 再导同级模块。
if str(_PLUGIN_DIR) not in _sys.path:
    _sys.path.insert(0, str(_PLUGIN_DIR))

_DEFAULT_FORWARD_NAME = "？！石！？"


def _file_uri(path: Path) -> str:
    """把本地路径转成 ``file:///`` URI。

    Windows 下必须是三个斜杠（``file:///H:/...``）：NapCat 的 checkUriType 对
    ``file:`` 开头的 URI 在 win32 上按 ``uri.slice(8)`` 取路径，正好跳过
    ``file:///``；两斜杠也会退化成 ``slice(7)`` 而侥幸可用，但不能指望。
    反斜杠要换成斜杠，盘符前的斜杠不能少。

    路径里可能有空格或中文（主程序装在 ``D:/我的机器人/...`` 是常事），
    而 NapCat 会对 ``file:`` 的 URI 做 ``decodeURIComponent``，所以这里必须
    按 URL 规则转义：不转义的话空格与中文会被解成别的字节，轻则取不到图，
    重则 ``%`` 后面跟错字符直接抛异常。斜杠与盘符冒号要留原样。
    """
    try:
        resolved = Path(path).resolve()
    except Exception:  # noqa: BLE001
        resolved = Path(path)
    text = str(resolved).replace("\\", "/")
    if not text.startswith("/"):
        text = "/" + text
    return "file://" + urllib.parse.quote(text, safe="/:")


def _read_manifest_version() -> str:
    try:
        with open(_PLUGIN_DIR / "_manifest.json", encoding="utf-8") as handle:
            return str(_json.load(handle).get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


_PLUGIN_VERSION = _read_manifest_version()

from maibot_sdk import Command, MaiBotPlugin, Tool  # noqa: E402
from maibot_sdk.types import ToolParameterInfo, ToolParamType  # noqa: E402

from st_card import build_thread_html, build_thread_text, image_data_uri  # noqa: E402
from st_config import StoneConfig, in_quiet_hours, migrate_config, resolve_forums  # noqa: E402
from st_page import build_page_html, placeholder_urls  # noqa: E402
from st_store import StoneStore  # noqa: E402
from st_tieba import (  # noqa: E402
    StoneBlockedError,
    StoneTiebaClient,
    StoneTiebaError,
    _direct_image_url,
    content_total_images,
    to_origin_image,
)


# 长图分段：Chromium 的截图画布有高度上限，一张图太高会直接截失败。
# 高度只能估：正文按每行 36 字、行高 27 像素，图片按卡片宽等比缩放的真实比例算。
_MAX_CARD_HEIGHT = 6800.0
_TEXT_LINE_HEIGHT = 27.0
_TEXT_CHARS_PER_LINE = 36.0
_HEAD_HEIGHT = 320.0
_FLOOR_PADDING = 66.0
_DEFAULT_IMAGE_HEIGHT = 620.0

# 合并转发里一条帖子最多贡献几个节点（层多了聊天记录翻起来也累）
_FORWARD_FLOOR_NODES = 12

# 发视频走 ctx.send.custom("videourl", ...)：宿主没有原生视频组件，但会把它包成
# DictComponent 原样透传，适配器再把 dict 里的 videourl 编成真视频段（实测链路）。
# 视频是最慢的一次能力调用——两个 MB 实测要几秒，几十 MB 更久，而适配器侧
# action_timeout_sec 默认只有 15 秒，所以必须显式加时，否则会「其实发出去了却判超时」
# 又降级重发一遍，群里收到两条。参考宿主的 jm2pdf 插件，同样是 300 秒。
_VIDEO_SEND_TIMEOUT_MS = 300_000
# 合成聊天记录同样慢：一条转发里要塞好几个节点、每个节点还有 base64 长图，
# 同样得显式加时。少了它，一次慢转发会被判超时抛异常，走进 `if not sent` 那条
# 降级分支把视频再发一遍，群里就多出一份。
_FORWARD_TIMEOUT_MS = 300_000
# 一条合并转发是一个 RPC 帧，上限 16MB（host transport/base.py 的 MAX_FRAME_SIZE）。
#
# 算这个预算必须知道两件事，都跟直觉不一样：
#
#   1. 图片要 base64，比原图胀 4/3；
#   2. **宿主会把 base64 在段里存两份**。capabilities/core.py:411 的
#      ``_normalize_plugin_segment`` 对 image 段是 ``ns = dict(segment)`` 之后
#      再补一个 ``binary_data_base64``，原来那个 ``content`` 键并不删。于是同一个
#      字符串在 JSON 里出现两遍，实际占用是原图的 2 x 4/3 = 2.67 倍。
#
# 合起来：一张图能进帧的原文件上限只有 6.000 MiB（不是 12MB），整条聊天记录里
# 所有图片的「单份 base64 长度」加起来不能超过约 6.5MB。这里按**单份 base64 的
# 长度**累加（代码里就是 ``len(blob)``），所以预算取 6MB，留出昵称、楼号、JSON
# 结构等零碎的余量。实测纯图文 5 帖 x 12 楼 x 2 张 200KB 图，单份合计 31MB ——
# 不放预算这条聊天记录必然发不出去，而失败形式是整条丢，用户什么都看不到。
_FORWARD_FRAME_BUDGET_BYTES = 6 * 1024 * 1024
# 每个转发节点的固定开销（昵称、user_id、message_id、JSON 括号等）的粗略估值。
_FORWARD_NODE_OVERHEAD = 256
# 一个「本地路径引用」图片段在帧里的开销：路径字符串 + 外壳 JSON 的估值。
# 走这条路时图片本体根本不进帧，所以只有这点字节要算。
_PATH_SEGMENT_COST = 512

# 只搬哪种帖：列表页只能看出「有没有 media_flash」，但那只标记了首楼，
# 楼中楼里的视频看不出来，所以过滤放在取到详情之后按真实解析结果做。
# 「只搬视频帖」这类筛选多留几倍候选：列表页标记偶尔失准（视频帖被标成 0），
# 预筛看走眼时还能靠详情页复核捞回来。多取的只会多花几次详情页请求。
_FILTER_CANDIDATE_FACTOR = 3
_FILTER_ALL = "不限"
_FILTER_TEXT = "只发图文帖"
_FILTER_VIDEO = "只发视频帖"

# 工具「搬贴吧」只挑帖型，不管发法 —— 怎么发一律照配置走。
#   any   不限（用配置里「只搬哪种帖」那个值，默认）
#   text  只要图文帖      video 只要视频帖
_KIND_ANY = "any"
_KIND_TEXT = "text"
_KIND_VIDEO = "video"
# 工具调用最多一次搬几条（模型给大了就钳住，免得一次淹了整个群）
_TOOL_MAX_COUNT = 5
# 工具那一轮要改读配置的「工具」一节。_kind_override 本来就是「本轮临时生效的
# 设置」，顺手借它背一个标记，不必再加一个实例属性；空串仍是「照配置走」。
_TOOL_OPTIONS_FLAG = "__tool__"
# 吧名允许的字符：中英文数字下划线加间隔号，长度限制跟贴吧本身差不多
_FORUM_NAME_RE = re.compile(r"^[0-9A-Za-z_\u4e00-\u9fff·]{1,20}$")


def _thread_kind(value: Any) -> str:
    """把工具传来的帖型参数翻成配置里的说法，认不出的一律当「不限」。

    工具对模型只暴露 any/text/video 三个短词（模型不会念错、token 也省），
    内部统一用配置面板那一套中文值，免得两套词在代码里来回翻译。
    """
    text = str(value or "").strip().lower()
    if text == _KIND_TEXT:
        return _FILTER_TEXT
    if text == _KIND_VIDEO:
        return _FILTER_VIDEO
    return _FILTER_ALL


# 长图样式：自绘卡片（插件排版）/ 贴吧原页面（整页原样截图）
_CARD_STYLE_CARD = "自绘卡片"
_CARD_STYLE_PAGE = "贴吧原页面"
# 原页面模式的截图参数：按手机屏宽截、允许联网去取 CDN 上的样式表、
# 补进页面的图按卡片宽留 820 像素就够清楚（页面里文字区就这么宽）
_PAGE_VIEWPORT_WIDTH = 414
_PAGE_VIEWPORT_HEIGHT = 900
_PAGE_RENDER_TIMEOUT_MS = 60000
_PAGE_IMAGE_MAX_WIDTH = 820
# 原页面模式要把页面里每个图片占位都补上，缺一张就是一个洞（补不到的占位会被 CSS 收掉，
# 于是在长图里凭空少一张图）。所以这里不受 max_images 限制，只用这个硬上限兜住超长帖。
_PAGE_IMAGE_LIMIT = 80
# 单张长图也走 RPC，而图片在帧里要 base64 两份（见 _FORWARD_FRAME_BUDGET_BYTES 的
# 说明），所以一张长图的**原文件**上限是 6.000 MiB。渲染出来的 PNG 是纯色块居多的
# 截图，很容易就 8~12MB —— 超了这张图压根发不出去，而且是抛异常、用户什么都看不到。
# 所以渲染完先按体积试，太大就降低设备缩放重渲几档，再不行才认输退回文本。
_PAGE_IMAGE_SOFT_LIMIT_BYTES = 6 * 1024 * 1024
# 重渲时依次尝试的设备缩放（越小越省体积，但字也越糊）。
_PAGE_RENDER_SCALES = (2.0, 1.5, 1.25, 1.0)
# 渲染的 RPC 超时要在宿主渲染超时之上再留一点余量：让宿主自己先超时并回一个干净的
# 错误，而不是这边先掐断 RPC 把一个还在跑的渲染丢掉。
_RENDER_RPC_SLACK_MS = 15_000
# 自绘卡片是本地 HTML（不联网），渲染快得多，给 45 秒足够。
_CARD_RENDER_TIMEOUT_MS = 45_000

# 长图落盘目录（插件 data 目录下的 pages/）。
#
# 为什么要把长图写进文件、而不是直接把 base64 塞进转发节点：
# 整条合并转发是**一个** RPC 帧，上限 16MB（宿主 transport/base.py 的
# MAX_FRAME_SIZE）。base64 进帧还要被宿主存两份，于是「一张 6MB 的长图就吃掉
# 整条预算」，后面的帖子只能被踢出去单独发 —— 用户看到的就是刷屏。
#
# 改成让节点只带一个本地路径后，帧里就只有两百来字节，**与图片体积无关**：
# 实测 4 张各 6MB 的长图（合计 23.4MB）进同一条转发，插件发出的帧仍只有 1.4KB。
# 这条路能成是因为 NapCat 的 uriToLocalFile 明确认 file:// 前缀
# （napcat.mjs 的 checkUriType：`uri.startsWith("file:")` -> Local，且本地文件
# 原样使用不复制），已在 chatbot 测试组真机验证。
_PAGE_CACHE_FILENAME = "page_%s_%d.png"
# 落盘的长图保留份数：只留最近这些张，避免长年累月把磁盘塞满。
# 转发是异步投递的，删太早会让 QQ 那边取不到图，所以按「张数」而不是「时间」清。
_PAGE_CACHE_KEEP = 60
# 给协议端留出的读文件时间：刚落盘的图在这一段时间内绝不删。
# NapCat 收到 file:// 之后要自己去读盘再上传，这条转发是异步投递的，
# 上一张贴出去、下一轮马上开始清理时，前面那张可能还没被读完。
_PAGE_CACHE_CLEAN_DELAY_S = 600.0
# 清理节流：一轮搬运会落盘上百张小图，每张都去 glob 一遍整个目录太浪费
# （目录常驻几十个文件，一轮下来就是上万次同步 stat，还会卡住事件循环）。
# 改成每隔这么多秒最多真正扫一次目录。
_PAGE_CACHE_PRUNE_INTERVAL_S = 30.0

# 维护类子命令的拒绝文案，只放管理员
_MAINTENANCE_DENIED = (
    "这条命令只有管理员能用（会立刻往群里发消息、或改动搬运记录）。\n"
    "让管理员在插件设置 →「权限 · 管理员」里加上你的 QQ 号，"
    "或者直接用主程序终端发这条命令。"
)

_HELP_TEXT = "\n".join(
    [
        "麦麦贴吧搬石 · 用法",
        "/搬石　　　　　　管理员：按配置搬一轮，发到当前群",
        "/搬视频　　　　　管理员：只搬视频帖",
        "/搬石 状态　　　看每个吧搬了多少、最后一次是什么时候（管理员）",
        "/搬石 重置 [吧]　清空搬运记录，下次会重新搬（管理员）",
        "/搬石 重置 冷却　解除反爬冷却，不影响搬运记录（管理员）",
        "/搬石 帮助　　　这条说明",
        "",
        "",
        "自动搬石在插件配置里开：选要逛的吧、定时（隔多久或每天定点）、",
        "发哪些群、要「文本 + 图片」还是「一张长图」、要不要打包成聊天记录。",
        "谁能用维护命令在插件配置 →「权限」里配，管理员直接填 QQ 号。",
    ]
)


# 宿主拼「引用回复 + 用户正文」时留下的包装。被引用原文总是包在
# 「[回复了…的消息: …]」这类壳子里，而用户这次敲的正文一定在壳子外面。
# 只认这些壳子 —— 不能把冒号也算进来，否则「/搬石 状态：xxx」这种正常输入
# 会被误判成引用而整条丢掉。
_QUOTE_SHELL_RE = re.compile(r"[\[【]\s*(?:回复|引用)[^\]】]{0,200}[\]】]")


def _quoted_command(text: str, command: str) -> bool:
    """命令是不是藏在被引用的原文里。

    宿主把「[回复了某人的消息: 原文] 用户正文」拼成一段文本再交给命令匹配，
    被引用原文里的命令会被一并匹配上。文本里凡是被「[回复了…]」包住的那一段，
    其中的命令都不算数；壳子外面的命令照常执行。
    """
    for shell in _QUOTE_SHELL_RE.finditer(text):
        if command in shell.group(0):
            return True
    return False


def _scoped_user_id(platform: Any, user_id: Any) -> str:
    """拼出 platform:user_id（平台名小写，和宿主的拼法一致）。"""
    plat = str(platform or "").strip().lower()
    uid = str(user_id or "").strip()
    if not plat or not uid:
        return ""
    return plat + ":" + uid


def _permission_set(values: Any) -> set[str]:
    """把宿主那份 platform:user_id 名单规范成集合；形状不对就当空集。"""
    allowed: set[str] = set()
    if not isinstance(values, (list, tuple, set, frozenset)):
        return allowed
    for value in values:
        text = str(value or "")
        if ":" not in text:
            continue
        plat, uid = text.split(":", 1)
        scoped = _scoped_user_id(plat, uid)
        if scoped:
            allowed.add(scoped)
    return allowed


_DIGITS_RE = re.compile(r"\d{5,}")


def _admin_ids(values: Any) -> set[str]:
    """管理员名单 → 纯数字 QQ 号集合。

    面板上直接填数字就行；顺手写成 ``qq:123456``、``123456@qq.com`` 的也认，
    免得从别处抄来的格式被静默无视。取不出数字的条目直接丢掉。
    """
    found: set[str] = set()
    if not isinstance(values, (list, tuple, set, frozenset)):
        return found
    for value in values:
        text = "".join(
            chr(ord(ch) - 0xFEE0) if "\uff10" <= ch <= "\uff19" else ch for ch in str(value or "")
        )
        found.update(_DIGITS_RE.findall(text))
    return found


def _group_set(values: Any) -> set[str]:
    """群号名单 → 纯数字集合；形状不对就当空集。"""
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {_clean_id(value) for value in values if _clean_id(value)}


def _pick_live_session(items: list[dict[str, Any]]) -> dict[str, Any]:
    """同一群的几条会话记录里，挑最可能是「能用」的那一条。

    宿主的会话表里偶尔对同一个群留下两行，其中一行是没归好档的孤儿行。行里能看出
    死活的线索是 ``account_id`` / ``user_id`` 以及有没有群名 —— 有账号能说明这条
    会话绑着某个已登录的账号，孤儿行这些字段往往是空的。挑不出来（全都一样、
    或者线索字段宿主根本没给）就退回第一条，保持原来的行为。
    """
    def score(item: dict[str, Any]) -> int:
        total = 0
        for key in ("account_id", "user_id", "group_name", "platform"):
            if str(item.get(key) or "").strip():
                total += 1
        return total

    return max(items, key=score)


def _clean_id(value: Any) -> str:
    """把群号洗成纯数字串。用户可能填「群 123456」或者带空格的全角数字。"""
    text = str(value or "").strip()
    if not text:
        return ""
    text = "".join(chr(ord(ch) - 0xFEE0) if "\uff10" <= ch <= "\uff19" else ch for ch in text)
    digits = re.sub(r"\D", "", text)
    return digits


def _parse_hhmm(text: Any) -> tuple[int, int] | None:
    """把 9:30 / 09：30 / 21点 解析成 (时, 分)。读不出来返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    raw = "".join(chr(ord(ch) - 0xFEE0) if "\uff10" <= ch <= "\uff19" else ch for ch in raw)
    for token in ("：", ".", "点", "时", "時", "h", "H"):
        raw = raw.replace(token, ":")
    # 「21点」「9:」这类写法收尾会留一个冒号，去掉再解析
    raw = raw.strip().strip(":").strip()
    matched = re.match(r"^(\d{1,2})(?::(\d{1,2}))?$", raw)
    if not matched:
        return None
    hour = int(matched.group(1))
    minute = int(matched.group(2) or 0)
    # 24:00 是「今天结束」的常见写法，当成次日零点（也就是 0:00）。只放行 24:00 这一个，
    # 24:30 之类仍然算错，否则「当天最后一个时刻」就没有明确的说法了。
    if hour == 24 and minute == 0:
        return 0, 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _bad_daily_times(values: Any) -> list[str]:
    """挑出 daily_times 里认不出来的写法，好让用户知道哪条没生效。

    不这么报的话，「24:30」「明天早上」这类填错会被静默丢掉：定时照跑，
    但跑的时刻跟用户想的不一样，而且没有任何提示。
    """
    out: list[str] = []
    for item in values or []:
        text = str(item or "").strip()
        if text and _parse_hhmm(text) is None:
            out.append(text)
    return out


def _image_size(raw: bytes) -> tuple[int, int]:
    """量一下图片尺寸，用来估长图高度。量不出来返回 (0, 0)。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            return image.size
    except Exception:  # noqa: BLE001
        return (0, 0)


def _card_style(cfg: Any) -> str:
    """读长图样式。认不出来（比如配置被手改坏了）就按自绘卡片走。"""
    if str(getattr(cfg, "card_style", "") or "") == _CARD_STYLE_PAGE:
        return _CARD_STYLE_PAGE
    return _CARD_STYLE_CARD


def _select_floor_images(
    floors: Sequence[Any], available: Any, limit: int
) -> list[list[str]]:
    """按楼层顺序挑出这次真正会画的图，总共不超过 limit 张。"""
    if limit <= 0:
        return [[] for _ in floors]
    left = int(limit)
    picked: list[list[str]] = []
    for floor in floors:
        urls: list[str] = []
        for url in list(getattr(floor, "images", None) or []):
            if left <= 0:
                break
            if url in available and url not in urls:
                urls.append(url)
                left -= 1
        picked.append(urls)
    return picked


def _seg_images(
    chunk: Sequence[Any], floors: Sequence[Any], floor_images: Sequence[Any], available: Any
) -> dict[str, str]:
    """取出这一段要画的图，同一张图只出现在一段里。"""
    order = {id(floor): index for index, floor in enumerate(floors)}
    images: dict[str, str] = {}
    for floor in chunk:
        index = order.get(id(floor))
        if index is None:
            continue
        for url in floor_images[index]:
            if url in available:
                images[url] = available[url]
    return images


def _plan_card_chunks(
    floors: Sequence[Any],
    sizes: dict[str, tuple[int, int]],
    width: int,
    floor_images: Sequence[Any] | None = None,
) -> list[list[Any]]:
    """把楼层切成若干段，保证每段渲出来的长图不会高到截不下来。

    这只是估算：正文按字数折行，图片按卡片宽等比缩放后的真实比例算 ——
    估高了最多多切一图，估低了才会渲染失败，所以宁可保守。
    传了 floor_images 就只算这次真会画的图，没下载到的图不占高度。
    """
    chunks: list[list[Any]] = []
    current: list[Any] = []
    height = _HEAD_HEIGHT
    for position, floor in enumerate(floors):
        text = str(getattr(floor, "text", "") or "")
        lines = max(1.0, len(text) / _TEXT_CHARS_PER_LINE + text.count("\n"))
        height += _FLOOR_PADDING + lines * _TEXT_LINE_HEIGHT
        if floor_images is None:
            urls = list(getattr(floor, "images", None) or [])
        else:
            urls = list(floor_images[position]) if position < len(floor_images) else []
        for url in urls:
            size = sizes.get(url)
            if size and size[0]:
                height += width * size[1] / size[0] + 14.0
            else:
                height += _DEFAULT_IMAGE_HEIGHT
        current.append(floor)
        if height >= _MAX_CARD_HEIGHT:
            chunks.append(current)
            current = []
            height = _HEAD_HEIGHT
    if current:
        chunks.append(current)
    return chunks


class _ToolOptions:
    """工具那一轮用的发送选项：工具节有的取工具节，没有的回落到「发送」一节。

    _send_options() 的调用点都只按属性名取值（send_media / send_card / post_link
    …），这里把两个 section 拼成一层属性视图就够了，不必动那些调用点。
    """

    __slots__ = ("_tool", "_send")

    def __init__(self, config: Any) -> None:
        object.__setattr__(self, "_tool", getattr(config, "tool", None))
        object.__setattr__(self, "_send", getattr(config, "send", None))

    def __getattr__(self, name: str) -> Any:
        tool = object.__getattribute__(self, "_tool")
        if tool is not None and hasattr(tool, name):
            value = getattr(tool, name)
            if value is not None:
                return value
        send = object.__getattribute__(self, "_send")
        return getattr(send, name)


class StonePlugin(MaiBotPlugin):
    """麦麦贴吧搬石插件。"""

    config_model = StoneConfig

    # -- 生命周期 ---------------------------------------------------------

    async def on_load(self) -> None:
        self._client: StoneTiebaClient | None = None
        self._store: StoneStore | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # 手动轮次在后台跑，这里留着任务本身，卸载时好一起收掉
        self._jobs: set[asyncio.Task] = set()
        # 「现在有一轮在跑」。不能只看 _lock：后台任务从派发到真正拿到锁之间
        # 还有一小段空窗，这期间第二个人敲命令会以为没人跑，于是两轮并行。
        self._busy = False
        # 同一条消息 id 只认一次：引用/重投递都可能让宿主把同一条命令再喂进来
        self._seen_message_ids: deque[str] = deque(maxlen=64)
        # 工具调用期间临时指定的「只搬哪种帖」；空串表示照配置走
        self._kind_override: str = ""
        # 上次真正扫过落盘目录的时刻，用来节流清理（落盘很频繁，扫目录很贵）
        self._page_cache_pruned_at: float = 0.0
        self._setup()
        cfg = self.config
        self.ctx.logger.info(
            "[搬石] 已加载 v%s：吧=%s；自动搬石=%s；呈现=%s",
            _PLUGIN_VERSION,
            "、".join(resolve_forums(cfg.stone)) or "（没配）",
            "开" if cfg.schedule.auto_enabled else "关",
            self._mode_label(),
        )
        self._maybe_start_loop()

    async def on_unload(self) -> None:
        await self._cancel_loop()
        await self._cancel_jobs()
        if self._store is not None:
            # 记录是纯本机去重状态，卸载前落一次盘
            self._store.flush()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self.ctx.logger.info("[搬石] 已卸载")

    def normalize_plugin_config(self, config_data):
        # 面板默认值改过之后，老 config.toml 里那个键还留着旧默认（比如空的屏蔽词列表），
        # 而宿主补齐/归一化都是「键存在就用旧值」，于是新默认永远盖不上去。
        # 先按迁移表把「没动过」的字段换成新默认，再走 SDK 原本的校验补齐。
        changed = migrate_config(config_data) if isinstance(config_data, dict) else False
        normalized, auto_changed = super().normalize_plugin_config(config_data)
        return normalized, bool(changed or auto_changed)

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del config_data
        if scope != "self":
            return
        # 改配置时正在跑的手动/工具轮次不在 self._task 里（那个只装定时任务），
        # 直接 aclose 会让它们抛「client has been closed」并且用户看不到任何反应。
        # 拿住同一把锁就等于「等这一轮跑完再换 client」；_cancel_loop 也放进锁内，
        # 免得它取消定时任务的同时那一轮正用着 client。
        async with self._lock:
            await self._cancel_loop()
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            self._setup()
            self._maybe_start_loop()
        self.ctx.logger.info("[搬石] 配置已更新（version=%s），运行时已重建", version)

    # -- 装配 -------------------------------------------------------------

    def _data_dir(self) -> Path:
        try:
            base = Path(self.ctx.paths.data_dir)
        except Exception:  # noqa: BLE001 —— 老版本 SDK 没有 paths
            base = _PLUGIN_DIR / "data"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _page_cache_dir(self) -> Path:
        """长图落盘目录。转发节点里放的是这里的 file:// 路径。"""
        base = self._data_dir() / "pages"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _save_page_image(self, image_b64: str) -> str:
        """把渲染好的长图写进落盘目录，返回 ``file:///`` 形式的绝对路径。

        写盘失败时返回空串，调用方据此退回 base64 —— 路径引用只是「省帧体积」，
        不该因为磁盘只读之类的小事让整条帖子发不出去。
        """
        return self._save_blob_image(image_b64, log_failure=True)

    def _save_blob_image(self, image_b64: str, *, log_failure: bool = False) -> str:
        """把一段 base64 图片写盘并返回 ``file:///`` 路径；失败返回空串。

        正文小图用同一个目录：它们和长图一样都只是「给协议端读的临时文件」，
        按同一个份数上限一起清理即可。
        """
        try:
            data = base64.b64decode(image_b64)
        except Exception:  # noqa: BLE001
            return ""
        try:
            directory = self._page_cache_dir()
            path = directory / (_PAGE_CACHE_FILENAME % (uuid.uuid4().hex, int(time.time())))
            path.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            if log_failure:
                self.ctx.logger.warning("[搬石] 长图落盘失败，这条改用内嵌图片：%s", exc)
            return ""
        self._prune_page_cache()
        return _file_uri(path)

    def _prune_page_cache(self, *, keep: int | None = None) -> None:
        """只留最近 ``keep`` 张落盘图片，多出来的按修改时间从旧到新删掉。

        两个约束：
        * ``_PAGE_CACHE_CLEAN_DELAY_S`` 之内的文件一律不碰 —— 协议端可能正读着；
        * 每 ``_PAGE_CACHE_PRUNE_INTERVAL_S`` 秒最多真扫一次目录，否则一轮上百张
          小图会把同步 stat 打成几万次，白堵事件循环。
        """
        limit = _PAGE_CACHE_KEEP if keep is None else keep
        now = time.time()
        # getattr 兜底：清理只是维护动作，不该因为实例状态缺失把整轮搬运带崩
        last = float(getattr(self, "_page_cache_pruned_at", 0.0) or 0.0)
        if now - last < _PAGE_CACHE_PRUNE_INTERVAL_S:
            return
        self._page_cache_pruned_at = now
        try:
            files = sorted(
                self._page_cache_dir().glob("page_*.png"),
                key=lambda item: item.stat().st_mtime,
            )
        except Exception:  # noqa: BLE001
            return
        # 保护期内（刚落盘、协议端可能还没读完）的一律留下，不参与淘汰。
        files = [item for item in files if now - item.stat().st_mtime >= _PAGE_CACHE_CLEAN_DELAY_S]
        for stale in files[: max(0, len(files) - limit)]:
            try:
                stale.unlink()
            except Exception:  # noqa: BLE001
                pass

    def _setup(self) -> None:
        cfg = self.config
        self._client = StoneTiebaClient(
            timeout=float(cfg.fetch.timeout),
            proxy=str(cfg.fetch.proxy or "").strip(),
            delay_seconds=float(cfg.fetch.delay_seconds),
            retries=2,
            user_agent=str(cfg.fetch.user_agent or "").strip(),
        )
        self._store = StoneStore(
            self._data_dir() / "moved.json", keep_per_forum=int(cfg.stone.keep_records)
        )
        self._store.load()

    def _mode_label(self) -> str:
        cfg = self.config.send
        parts: list[str] = []
        if cfg.send_media:
            parts.append("文本+图片")
        if cfg.send_card:
            parts.append("长图（贴吧原页面）" if _card_style(cfg) == _CARD_STYLE_PAGE else "长图")
        label = " / ".join(parts) or "（两种都没开，只会写日志）"
        if cfg.forward:
            label += "（打包成聊天记录）"
        return label

    # -- 定时 -------------------------------------------------------------

    def _maybe_start_loop(self) -> None:
        cfg = self.config
        if not cfg.plugin.enabled:
            self.ctx.logger.info("[搬石] 配置里没启用插件，不启动定时")
            return
        if not cfg.schedule.auto_enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._schedule_loop())

    async def _cancel_loop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("[搬石] 定时任务退出时抛出：%s", exc)

    async def _cancel_jobs(self) -> None:
        """收掉所有还在跑的手动/工具轮次。"""
        jobs = list(self._jobs)
        self._jobs.clear()
        for job in jobs:
            if not job.done():
                job.cancel()
        for job in jobs:
            try:
                await job
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug("[搬石] 后台轮次退出时抛出：%s", exc)

    def _spawn_job(self, coro: Any) -> None:
        """把一轮搬运丢到后台跑，命令本身立刻回执。

        宿主对命令有 60 秒超时，而一轮搬运动辄一分多钟，同步等必然被截断 ——
        用户那边只看得到超时，搬运却在后台继续，看起来就像「说不认识却搬了」。
        """
        job = asyncio.create_task(coro)
        self._jobs.add(job)

        def _done(_task: asyncio.Task) -> None:
            self._jobs.discard(_task)
            self._busy = False

        job.add_done_callback(_done)

    # 定点「已经跑过」的判定容差。宿主里 asyncio.sleep 只保证「至少」睡够，
    # 实测普遍多睡 0.5~2 毫秒，所以醒来时 now 几乎必然已经越过定点。判定必须
    # 容忍这点漂移，否则同一轮会被当成「还没到」而顺延到明天（见 _next_fire）。
    _FIRE_TOLERANCE = timedelta(seconds=90)

    def _next_fire(self, *, after: datetime | None = None) -> datetime | None:
        """按「每天定点」算出的下一个触发时刻；没配定点就返回 None。

        ``after`` 是「已经跑到过的那一刻」。**判定不能写成 `target < now`**：
        以前就是那么写的，而 `asyncio.sleep` 只保证至少睡够，醒来时 now 比定点
        晚个一两毫秒是常态 —— 那一毫秒让 `target < now` 成立，目标被顺延到明天，
        这个「明天」又被记进 ``fired``、接着再被 ``skip`` 顶掉一天，于是变成
        **两天才跑一次**。用户看到的就是「不是设定的时间」。

        另一条同样要命的旧写法是「今天这个点已经过去就算命中」：插件 20:00 启动、
        配了 08:30/09:30/12:00/20:00 时，前三个点当天都已过去，于是连着补跑好几
        轮 —— 用户看到的就是「一次连续发两次」。所以这里**只认还没到的时刻**。

        现在只拿 ``after`` 判断「跑过没有」，完全不看 now 与定点的先后，采样漂移
        再也影响不到「算出来的是哪一天」。
        """
        cfg = self.config.schedule
        stamps = [parsed for parsed in (_parse_hhmm(x) for x in cfg.daily_times) if parsed]
        if not stamps:
            return None
        now = datetime.now()
        best: datetime | None = None
        for hour, minute in stamps:
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if after is not None and target - after <= self._FIRE_TOLERANCE:
                # 今天这个点这一轮已经跑过了，挪到明天。
                target = target + timedelta(days=1)
            elif after is None and target <= now:
                # 还没跑过任何一轮，而今天这个点已经过去了：别再补跑，
                # 直接等明天。第一次算的时候连补好几轮正是「一次连发两次」。
                target = target + timedelta(days=1)
            if best is None or target < best:
                best = target
        return best

    def _next_wait(self, *, after: datetime | None = None) -> float:
        """下一次该等多久（秒）。

        定点模式返回「到那个时刻还差几秒」，间隔模式返回一个完整间隔。

        **不要在这里设 30 秒之类的兜底下限**：以前写成 `max(30.0, 差值)`，
        于是在定点前 30 秒内计算时（比如 20:59:45 算 21:00），差值被抬到 30 秒，
        实际到 21:00:15 才跑 —— 用户看到的就是「不是设定的时间」。下限只该
        用来挡住「差值为 0 或负数」这种退化情形。
        """
        target = self._next_fire(after=after)
        if target is not None:
            return max(0.0, (target - datetime.now()).total_seconds())
        now = datetime.now()
        return max(0.0, (self._interval_target(after=now) - now).total_seconds())

    def _last_baseline(self, *, before: datetime) -> datetime:
        """间隔模式的「上一个基准时刻」，周期就是从这里开始数的。

        基准时刻（默认 0:00）决定周期落在哪些点上：配 0:00 + 120 分钟，就是
        0、2、4……22 点各一次。以前是从「插件启动的那一刻」起算，所以「每 60
        分钟」实际是「启动后每 60 分钟」，跟整点对不上 —— 用户看到的就是
        「不是设定的时间」，而且每次重启都会再漂一次。读不出基准就退回把
        before 当基准（等价于旧行为），不因为一个笔误把定时整个停掉。
        """
        parsed = _parse_hhmm(getattr(self.config.schedule, "base_time", ""))
        if parsed is None:
            return before
        hour, minute = parsed
        base = before.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if base > before:
            base = base - timedelta(days=1)
        return base

    def _interval_target(self, *, after: datetime) -> datetime:
        """间隔模式下 after 之后的下一个时间点（对齐基准时刻）。"""
        interval = max(60.0, float(self.config.schedule.interval_minutes) * 60.0)
        base = self._last_baseline(before=after)
        elapsed = (after - base).total_seconds()
        steps = int(elapsed // interval) + 1
        return base + timedelta(seconds=steps * interval)

    def _lead_seconds(self) -> float:
        """「提前开始」的秒数，只认 0~3600 秒；读不出来的值按 0 处理。"""
        raw = getattr(self.config.schedule, "lead_seconds", 0)
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            return 0.0
        if seconds <= 0:
            return 0.0
        return float(min(seconds, 3600))

    def _interval_wait(self, started_at: float) -> float:
        """间隔模式的下一次等待：从**上一轮开始跑的时刻**起算。

        若写成「跑完再等一个完整间隔」，实际周期就是「间隔 + 一轮耗时」：
        实测配 60 分钟却每隔 61 分钟跑一次（00:18 → 01:19 → 02:20，每轮约一分钟）。
        扣掉已经花掉的时间，周期才真的等于配置值；一轮要是跑超了一个间隔，
        就直接安排下一轮（下限 0），不追赶、不补跑。
        """
        interval = max(60.0, float(self.config.schedule.interval_minutes) * 60.0)
        return max(0.0, interval - (time.monotonic() - started_at))

    async def _schedule_loop(self) -> None:
        # 刚启动就先等一个完整间隔（设了指定时刻就等到那个点），别一开机就搬一轮。
        #
        # 关键：本轮的 ``target`` 必须在**睡之前**就定下来，睡醒后绝不重算。
        # 以前是睡醒后才第一次采样 now 去算 target，而 sleep 只保证至少睡够、
        # 醒来时往往已过定点一两毫秒 —— 那一毫秒让目标被顺延到明天，接着再被
        # skip 顶掉一天，于是两天才跑一次。
        fired: datetime | None = None
        # 间隔模式下「这一轮距离开跑还有多久（秒）」，在睡之前就算好。None 表示还
        # 没算过，下一轮该在哪个时刻开跑要重新按「基准时刻」推。
        #
        # 必须用变量把它带过一轮：以前是在循环**末尾**调 _interval_wait 算出
        # 「扣掉本轮耗时后还要等多久」，赋值给 wait 之后循环立刻回到顶部，又被
        # 顶部的完整间隔覆盖 —— 白算。于是实际周期 = 间隔 + 一轮耗时，实测配
        # 3600 秒却每隔约 3860 秒才跑一次，且误差随每轮耗时累积。
        pending: float | None = None
        while True:
            # 这一轮该在哪个绝对时刻发布；None 表示取完帖子就直接发。
            post_at: datetime | None = None
            lead = self._lead_seconds()
            fire = self._next_fire(after=fired)
            fixed = fire is not None
            if fixed:
                # 定点模式：直接对着那个时刻。提前量只是让「取帖」先开始，
                # 发布仍然卡在用户设的整点上。
                fired = fire
                # 切回定点后，间隔模式留下的「待跑时间」作废，免得跨模式生效。
                pending = None
                left = (fire - datetime.now()).total_seconds()
                if lead and left > lead:
                    # 先去贴吧把帖子取好（贴吧慢的时候这点时间正好用上），到了
                    # 到点再发出去 —— 这样「发」的时刻始终是准的，取帖快慢不影响。
                    wait = max(0.0, left - lead)
                    post_at = fire
                else:
                    wait = max(0.0, left)
            elif pending is not None:
                # 间隔模式，第二轮起：等待时间上一轮末尾就算好了（扣掉了本轮耗时），
                # 开跑后再过提前量就是发布时刻 —— 提前量只决定「什么时候发」，
                # 不改变开跑间隔，周期始终等于「隔多少分钟搬一次」。
                wait = pending
                pending = None
                if lead:
                    post_at = datetime.now() + timedelta(seconds=wait + lead)
            else:
                # 间隔模式，第一轮：按「基准时刻」推出下一个基准整点，再往前挪一个
                # 提前量当开跑时刻 —— 于是每一轮的发布时刻都精确落在基准网格上
                # （0:00 + 120 分钟就是 0、2、4……22 点整），既不改周期，也不让
                # 发布时刻随取帖快慢漂移。now 只采一次，免得 int(wait) 少 1 秒。
                now = datetime.now()
                fire = self._interval_target(after=now + timedelta(seconds=lead))
                wait = max(0.0, (fire - now).total_seconds() - lead)
                if lead:
                    post_at = fire
            self.ctx.logger.debug("[搬石] 下一次搬运在 %s 秒后", int(wait))
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                raise
            started_at = time.monotonic()
            # 免打扰时段：跳过这一轮，不补发。排期本身照旧往下走 —— 醒来时若还在
            # 时段内就整轮丢掉，等下一个网格点；醒来已在时段外就正常搬。
            if in_quiet_hours(self.config.schedule):
                self.ctx.logger.info(
                    "[搬石] 当前在免打扰时段（%s~%s），跳过本轮",
                    self.config.schedule.quiet_start,
                    self.config.schedule.quiet_end,
                )
                if not fixed:
                    pending = self._interval_wait(started_at)
                continue
            try:
                if post_at is not None:
                    self.ctx.logger.info(
                        "[搬石] 提前开跑，%s 再发", post_at.strftime("%m-%d %H:%M")
                    )
                summary = await self._run_round("定时", post_at=post_at)
                self.ctx.logger.info("[搬石] 定时一轮结束：%s", summary)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[搬石] 定时一轮失败：%s", exc, exc_info=True)
            # 定点模式按绝对时刻算，下一轮的 target 在循环开头重新求；间隔模式
            # 要扣掉这一轮已经花掉的时间，否则周期会越拖越长。
            if not fixed:
                pending = self._interval_wait(started_at)

    # -- 一轮搬运 ---------------------------------------------------------

    async def _run_round(
        self,
        trigger: str,
        streams: list[str] | None = None,
        *,
        only_forums: Sequence[str] | None = None,
        count: int = 0,
        only_kind: str = "",
        post_at: datetime | None = None,
    ) -> str:
        """搬一轮。streams 给了就只发这些会话（手动模式），否则按群配置发。

        only_forums / count / only_kind 是工具调用时临时改「逛哪些吧、每个吧
        搬几条、只要哪种帖」用的，其余一切照配置走 —— 工具不插手怎么发。
        """
        # 不能写成 if self._lock.locked(): return —— 检查和获取之间会让出控制权，
        # 两个调用可能同时通过检查，于是并行搬两轮。直接尝试获取才是原子的。
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=0.01)
        except (asyncio.TimeoutError, TimeoutError):
            return "上一轮还没跑完，这次跳过"
        try:
            cfg = self.config
            client = self._client
            store = self._store
            # 先装覆盖再判前置条件：下面任何一个提前 return 都不会走到 finally，
            # 漏装的话上一轮的「只发视频帖」会跟着漏到下一轮去。
            self._kind_override = only_kind or ""
            try:
                if client is None or store is None:
                    return "插件还没准备好，稍后再试"
                if only_forums:
                    forums = [
                        str(name).strip() for name in only_forums if str(name or "").strip()
                    ]
                else:
                    forums = resolve_forums(cfg.stone)
                if not forums:
                    return "配置里没勾也没填要逛的吧"
                per_forum = int(count) if int(count) > 0 else int(cfg.stone.count_per_forum)
                return await self._run_locked(
                    cfg, client, store, forums, per_forum, streams, trigger, post_at
                )
            finally:
                self._kind_override = ""
        finally:
            self._lock.release()

    async def _run_locked(
        self,
        cfg: Any,
        client: Any,
        store: Any,
        forums: Sequence[str],
        per_forum: int,
        streams: list[str] | None,
        trigger: str,
        post_at: datetime | None = None,
    ) -> str:
        """_run_round 的主体：进来时一定已经拿住锁。"""
        # collected 里每项带上「属于哪个吧」，发送成功后才好按吧记账
        collected: list[tuple[Any, Any]] = []
        forum_of: dict[str, str] = {}
        blocked = ""
        for forum in forums:
            try:
                threads = await client.fetch_thread_list(
                    forum, pages=int(cfg.fetch.max_pages)
                )
            except StoneBlockedError as exc:
                blocked = str(exc)
                break
            except StoneTiebaError as exc:
                self.ctx.logger.warning("[搬石] %s 的帖子列表没取到：%s", forum, exc)
                continue
            want = int(per_forum) if int(per_forum) > 0 else int(cfg.stone.count_per_forum)
            picks = self._pick_threads(forum, threads, limit=per_forum)
            # picks 里是「候选」（筛选模式下会多取几倍，用来熬过列表页标记看走眼），
            # 这里才真正数到 want 条为止：复核通过的凑够数就停，剩下的候选连详情页
            # 都不用抓。计数按吧分别算，collected 是各吧汇总的。
            got = 0
            for thread in picks:
                if want > 0 and got >= want:
                    break
                try:
                    detail = await client.fetch_thread_detail(
                        thread,
                        max_floors=int(cfg.fetch.max_floors),
                        max_chars=int(cfg.fetch.max_chars),
                        max_comments=int(getattr(cfg.fetch, "max_comments", 3) or 0),
                    )
                except StoneBlockedError as exc:
                    blocked = str(exc)
                    break
                except StoneTiebaError as exc:
                    self.ctx.logger.warning("[搬石] 帖子 %s 取正文失败：%s", thread.tid, exc)
                    continue
                if not detail.floors:
                    self.ctx.logger.debug("[搬石] 帖子 %s 没有解析到楼层，跳过", thread.tid)
                    continue
                if not self._passes_filter(detail):
                    self.ctx.logger.debug(
                        "[搬石] 帖子 %s 不符合「%s」，跳过",
                        thread.tid,
                        self._filter_mode(),
                    )
                    # 筛选掉的和发送无关，现在就能记账，不用等投递
                    store.mark(forum, thread.tid, thread.title)
                    continue
                collected.append((thread, detail))
                forum_of[thread.tid] = forum
                got += 1
            store.mark_run(forum)
            if blocked:
                break

        delivered = 0
        failed = 0
        host_error = ""
        if collected:
            if streams is not None:
                targets = streams
            else:
                targets, host_error = await self._target_streams()
            payloads = await self._prepare_payloads(collected)
            # 「提前开始」模式下帖子已经取好了，这里把剩下的时间补回来，
            # 好让发出去的时刻正好是用户设的那个点。
            await self._hold_until(post_at)
            for stream_id in targets:
                try:
                    await self._deliver(stream_id, collected, payloads)
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("[搬石] 往 %s 发送失败：%s", stream_id, exc)
                    failed += 1
                    continue
                delivered += 1
            # 只有「每个目标都收到了」才记「搬过了」。
            # 去重是帖子级的，任何一个群漏收都意味着它再也等不到这条 ——
            # 哪怕是永久丢帖。所以只要有一个目标失败就整条不记账，
            # 让下一轮重搬一次（收到过的群会重复看到，比收不到强）。
            if targets and not failed:
                for thread, _detail in collected:
                    store.mark(forum_of.get(thread.tid, ""), thread.tid, thread.title)
        store.flush()

        if blocked:
            summary = "被贴吧拦住了，本轮先停下：%s" % blocked
        elif not collected:
            summary = "本轮没有新帖可搬（要么都搬过了，要么没够门槛）"
        else:
            summary = "本轮搬了 %d 条" % len(collected)
            if delivered:
                # 说「群」而不是「会话」：这两个数以前是同一个，直到发现同一个群
                # 可能占两行会话记录（见 _target_streams 的去重说明）。那时摘要
                # 会写成「发到 2 个会话」——明明只发了一个群两条，读日志的人以为
                # 一切正常，bug 就这么藏了一个月。现在按群数说，对得上就是对的。
                summary += "，发到 %d 个群" % delivered
            if failed:
                # 有群没收到就不记账，下一轮会重搬；说清楚免得用户以为是重复发
                summary += "，%d 个群没发成功（这几条下一轮会重搬一次）" % failed
            elif not delivered:
                if host_error:
                    summary += "，但没问到会话列表（主程序可能正忙，过会儿再试）"
                elif streams is None:
                    summary += "，但没有找到可发的群（检查群配置或机器人是否在群里说过话）"
        self.ctx.logger.info("[搬石] %s结束：%s", trigger, summary)
        return summary

    async def _hold_until(self, post_at: datetime | None) -> None:
        """等到该发的那一刻再发。

        贴吧取帖快慢不定；「提前开始」就是先取好帖、再掐着点发，让发布时刻
        严格等于用户设的那个点。过了点就直接发，不补等。
        """
        if post_at is None:
            return
        left = (post_at - datetime.now()).total_seconds()
        if left <= 0:
            self.ctx.logger.info(
                "[搬石] 已经过了 %s，不再等，直接发", post_at.strftime("%H:%M")
            )
            return
        self.ctx.logger.info(
            "[搬石] 帖子备好了，等到 %s 再发（还有 %d 秒）",
            post_at.strftime("%H:%M"),
            int(left),
        )
        await asyncio.sleep(left)

    def _filter_mode(self) -> str:
        """当前生效的「只搬哪种帖」：工具调用时是它临时指定的，平时是配置值。"""
        override = str(getattr(self, "_kind_override", "") or "")
        if override:
            return override
        return str(getattr(self.config.send, "thread_filter", _FILTER_ALL) or _FILTER_ALL)

    def _passes_filter(self, detail: Any) -> bool:
        """这条帖子符不符合「只搬哪种帖」（详情页兜底判断）。

        正常走的是 _pick_threads 里按列表页标记的预筛；这里只在拿不到
        列表标记（thread_type=0，比如老页码或结构变动）时按真解析结果兜底。
        """
        mode = self._filter_mode()
        if mode == _FILTER_ALL:
            return True
        has_video = any(getattr(f, "videos", None) for f in getattr(detail, "floors", []))
        if mode == _FILTER_VIDEO:
            return has_video
        if mode == _FILTER_TEXT:
            return not has_video
        return True

    def _pick_threads(self, forum: str, threads: Sequence[Any], limit: int = 0) -> list[Any]:
        """从列表里挑出这一轮要搬的帖子。limit > 0 时按它取，否则用配置里的条数。

        列表页的 ``data-thread-type`` 能先筛掉不合口味的帖子，省下详情页那次请求；
        但那是**预筛，不是定论**：贴吧偶尔把视频帖标成 0，真伪要等详情页解析出来
        才知道（见 :meth:`_passes_filter`）。所以「只搬视频帖」时多留一截候选，
        免得预筛看走眼就一条都挑不出来、后面的视频帖连被看一眼的机会都没有。
        """
        cfg = self.config.stone
        want = int(limit) if int(limit) > 0 else int(cfg.count_per_forum)
        mode = self._filter_mode()
        # 要发的条数就这么多，但用于「拿详情页复核」的候选允许超标。
        # 上限按候选数走，多取的部分只多花几次详情页请求，不会被发出去。
        if mode == _FILTER_ALL:
            budget = want
        else:
            budget = max(want, want * _FILTER_CANDIDATE_FACTOR)
        store = self._store
        keywords = [str(word).strip().lower() for word in cfg.block_keywords if str(word).strip()]
        min_agree = int(getattr(cfg, "min_agree", 0) or 0)
        # 这一页有没有读到过点赞数：全是 0 就当作没读到，别把解析失败当成「零赞」。
        page_has_agree = any(int(getattr(x, "agree", 0) or 0) > 0 for x in threads)
        picked: list[Any] = []
        # 收满 budget 就够了。这里**不**替 _run_locked 的复核收窄候选：「只要视频帖」
        # 时列表页标记偶尔失准（视频帖被标成 0），多留几倍正是为了在复核阶段捞回来；
        # 收窄成 want 会让预筛一失准就整轮挑不出视频帖。
        for thread in threads:
            if cfg.skip_top and bool(getattr(thread, "is_top", False)):
                continue
            if int(getattr(thread, "replies", 0) or 0) < int(cfg.min_replies):
                continue
            # 点赞数只有列表页内联 JSON 里有，读不到时字段就是 0。
            # 但 0 既可能是「真的零赞」也可能是「没解析到」；对置顶/公告这类帖子，
            # 当成零赞筛掉是错的 —— 所以只在**这一页确实读到过**点赞数据时才按它筛。
            if min_agree > 0 and page_has_agree:
                if int(getattr(thread, "agree", 0) or 0) < min_agree:
                    continue
            title = str(getattr(thread, "title", "") or "").lower()
            if any(word in title for word in keywords):
                continue
            # 这里就能按列表页的 data-thread-type 筛掉不合口味的帖子，
            # 不用先把详情页抓回来 —— 详情页一次请求比列表页贵得多。
            if mode != _FILTER_ALL and not self._type_matches(thread, mode):
                continue
            if store is not None and store.known(forum, thread.tid):
                continue
            picked.append(thread)
            if len(picked) >= budget:
                break
        return picked

    @staticmethod
    def _type_matches(thread: Any, mode: str) -> bool:
        """按列表页标记判断帖子类型。

        视频帖的标记是 data-thread-type="40"，普通帖是 "0"（另有 33/71 等
        其它类型）。所以「是不是视频」可以直接算出来，不存在「标记缺失」：
        只有拿不到 li 属性时 thread_type 才留 0，那和普通帖同义，按非视频算。
        """
        has_video = bool(getattr(thread, "has_video", False))
        if mode == _FILTER_VIDEO:
            return has_video
        if mode == _FILTER_TEXT:
            return not has_video
        return True

    async def _target_streams(self) -> tuple[list[str], str]:
        """按黑白名单挑出要发的会话。

        Returns:
            (会话列表, 出错原因)。出错原因是 "host" 表示没问着宿主（RPC 故障），
            空串表示问着了、就是没有可发的群 —— 两者要分清楚，
            否则宿主机临时抽风却提示用户「检查群配置」，纯属让人白查。
        """
        cfg = self.config.send
        include = {_clean_id(x) for x in cfg.include_groups}
        include.discard("")
        exclude = {_clean_id(x) for x in cfg.exclude_groups}
        exclude.discard("")
        try:
            resp = await self.ctx.chat.get_all_streams(platform="qq")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[搬石] 取会话列表失败：%s", exc)
            return [], "host"
        streams = resp.get("streams") if isinstance(resp, dict) else resp
        if not isinstance(streams, list):
            self.ctx.logger.warning("[搬石] 会话列表形状不认识，本轮不发")
            return [], "host"
        # 同一个群在宿主的会话表里可能留下不止一行：一行规规矩矩带着群名和账号，
        # 另一行是没归好档的孤儿行（group_id 一样，其它字段空着）。宿主把这些行
        # 原样列出来，不去重的话这里会挑回两个 session_id，同一轮对同一个群把整批
        # 内容投递两遍 —— 用户看到的就是「一次连续发两次」。
        #
        # 光按群号只留一行还不够：**留哪一行是要挑的**。会话表按插入序返回，先出现的
        # 未必是能用的那行；万一留下的是孤儿行，消息发不出去，而因为群号在白名单里
        # 命中了，摘要照样写「发到 N 个群」，用户只看到少收一条、查不出原因。
        # 所以先按群号分组，每组挑一个「最像真的」的会话，挑不出来才退回第一个。
        groups: dict[str, list[dict[str, Any]]] = {}
        degenerated: list[str] = []
        seen: set[str] = set()
        for item in streams:
            if not isinstance(item, dict):
                continue
            if item.get("is_group_session") is False:
                continue
            group_id = _clean_id(item.get("group_id"))
            session_id = str(item.get("session_id") or "").strip()
            if not session_id:
                continue
            if group_id and group_id in exclude:
                continue
            if include and (not group_id or group_id not in include):
                # 配了白名单但这条会话认不出群号：宁可不发，也不要发错群
                continue
            if not group_id:
                # 认不出群号：白名单模式下上面已经拦掉了，这里只可能是黑名单模式。
                # 没有群号可归组，只能按 session_id 去重 —— 宿主偶尔会把同一条会话
                # 列两遍，不去重就等于对同一个会话发两遍，正是要防的那种重复。
                if session_id in seen:
                    self.ctx.logger.info(
                        "[搬石] 会话 %s 在列表里出现多次，只发一条", session_id
                    )
                    continue
                seen.add(session_id)
                degenerated.append(session_id)
                continue
            groups.setdefault(group_id, []).append(item)

        out: list[str] = []
        taken: set[str] = set()
        for group_id, items in groups.items():
            chosen = _pick_live_session(items)
            if len(items) > 1:
                self.ctx.logger.info(
                    "[搬石] 群 %s 有 %d 条会话记录，只发一条（留下 session=%s）",
                    group_id,
                    len(items),
                    chosen.get("session_id"),
                )
            session_id = str(chosen.get("session_id") or "").strip()
            if not session_id:
                continue
            taken.add(session_id)
            out.append(session_id)
        # 认不出群号的那些会话放在最后收。这里**必须**跟已经选中的按会话 id 去重：
        # 缺 group_id 的行归不了组，只能走这条路，而宿主列出的行并不保证字段整齐 ——
        # 同一个 session_id 完全可能既有一行带群号（已进 out）、又有一行不带。少了
        # 这一步，同一个会话就会进 targets 两遍，一轮里对同一个群投递两次，用户看到
        # 的就是「一次连续发两次」。按 id 硬去重，任何来源都一样对待。
        for session_id in degenerated:
            if session_id in taken:
                self.ctx.logger.info(
                    "[搬石] 会话 %s 同一轮里出现了两次，只发一条", session_id
                )
                continue
            taken.add(session_id)
            out.append(session_id)
        return out, ""

    async def _prepare_payloads(
        self, collected: Sequence[tuple[Any, Any]]
    ) -> dict[str, dict[str, Any]]:
        """把每条帖子的图片下载好，只下这一轮真用得上的。

        - 自绘卡片 / 文本 + 图片：要「长图用的 data URI + 直接发图用的 base64」，按楼层顺序取前几张。
        - 贴吧原页面：要「页面里每个图片占位的 data URI」，按页面出现顺序取前几张 ——
          页面自己的图要靠脚本才加载得出来，截图时是灰框，所以只能我们下好嵌进去。
        - 视频：要 mp4 字节，走发视频那条通道；下不动或超限就只留链接。
        """
        cfg = self._send_options()
        client = self._client
        payloads: dict[str, dict[str, Any]] = {}
        if client is None:
            return payloads
        style = _card_style(cfg)
        want_floors = bool(cfg.send_media) or (
            bool(cfg.send_card) and style != _CARD_STYLE_PAGE
        )
        want_page = bool(cfg.send_card) and style == _CARD_STYLE_PAGE
        limit = int(cfg.max_images)
        max_width = int(cfg.image_max_width)
        quality = int(cfg.image_quality)
        for thread, detail in collected:
            uris: dict[str, str] = {}
            blobs: dict[str, str] = {}
            sizes: dict[str, tuple[int, int]] = {}
            page: dict[str, str] = {}
            if want_floors:
                thumb_of: dict[str, str] = {}
                order: list[str] = []
                for floor in detail.floors:
                    for url, thumb in zip(floor.images, floor.thumbs):
                        if url and url not in thumb_of:
                            thumb_of[url] = thumb
                            order.append(url)
                if limit > 0:
                    order = order[:limit]
                else:
                    order = []
                for url in order:
                    try:
                        raw = await client.download_image(
                            url, thumb=thumb_of.get(url, ""), referer=thread.mobile_url
                        )
                    except StoneBlockedError as exc:
                        self.ctx.logger.warning("[搬石] 取图被拦住，剩下的图不取了：%s", exc)
                        break
                    except Exception as exc:  # noqa: BLE001
                        self.ctx.logger.debug("[搬石] 取图失败 %s：%s", url, exc)
                        continue
                    if not raw:
                        continue
                    uri = image_data_uri(raw, max_width=max_width, quality=quality)
                    if not uri:
                        continue
                    uris[url] = uri
                    blobs[url] = uri.split(",", 1)[1] if "," in uri else ""
                    sizes[url] = _image_size(raw)
            if want_page:
                # 原页面模式不看 max_images：那个值是「文本+图片一次发几张」，
                # 拿到这里用就会让第 N 张往后全变空洞。这里按页面占位数全补，只受硬上限约束。
                page_raw = str(getattr(detail, "raw", "") or "")
                spots = placeholder_urls(page_raw)[:_PAGE_IMAGE_LIMIT]
                # 首楼多图时 DOM 只有 3 个格子，其余图只在 contentTotal 载荷里，
                # 所以要把它们也下下来 —— 否则长图里那层只有 3 张。
                have = {signed for signed, _u in spots}
                extra_spots: list[tuple[str, str]] = []
                for signed in content_total_images(page_raw):
                    if signed in have:
                        continue
                    have.add(signed)
                    direct = to_origin_image(_direct_image_url(signed)) or signed
                    extra_spots.append((signed, direct))
                spots = (spots + extra_spots)[:_PAGE_IMAGE_LIMIT]
                for signed, url in spots:
                    try:
                        raw = await client.download_image(
                            url, thumb=signed, referer=thread.mobile_url
                        )
                    except StoneBlockedError as exc:
                        self.ctx.logger.warning("[搬石] 补图被拦住，剩下的图不补了：%s", exc)
                        break
                    except Exception as exc:  # noqa: BLE001
                        self.ctx.logger.debug("[搬石] 补图失败 %s：%s", url, exc)
                        continue
                    if not raw:
                        continue
                    uri = image_data_uri(
                        raw,
                        max_width=min(max_width, _PAGE_IMAGE_MAX_WIDTH),
                        quality=quality,
                    )
                    if uri:
                        page[signed] = uri
            # 视频不再下载：发送走 videourl 段，只把直链交给协议端自己去取。
            # 以前在这里下整段视频再转 base64，会撞上 RPC 单帧 16MB 的上限
            # （base64 再胀 1/3，原视频过 12MB 就必失败），白下载还发不出去。
            payloads[thread.tid] = {
                "uris": uris,
                "blobs": blobs,
                "sizes": sizes,
                "page": page,
            }
        return payloads

    # -- 发送 -------------------------------------------------------------

    def _send_options(self) -> Any:
        """当前生效的发送选项。

        平时就是配置里的「发送」一节。麦麦自己调工具搬石时改用「工具」一节 ——
        那一节可以单独配（只发长图、不带链接之类），免得改工具的行为还得连手动
        搬的样式一起动。工具那一节没配到的项，回落到「发送」一节。
        """
        # getattr 兜底：_kind_override 由 _setup 建立，用 object.__new__ 造出来的
        # 测试桩没有它，缺了就当作「没打标记」。
        if getattr(self, "_kind_override", "") == _TOOL_OPTIONS_FLAG:
            return _ToolOptions(self.config)
        return self.config.send

    def _forward_name(self) -> str:
        """合并转发里显示的发送者名字，配置留空时用默认值。"""
        name = str(getattr(self._send_options(), "forward_name", "") or "").strip()
        return name or _DEFAULT_FORWARD_NAME

    def _thread_header(self, thread: Any) -> str:
        forum = str(getattr(thread, "forum", "") or "").strip() or "贴吧"
        title = str(getattr(thread, "title", "") or "").strip() or "（无标题）"
        replies = int(getattr(thread, "replies", 0) or 0)
        head = "【%s】%s" % (forum, title)
        if bool(getattr(self._send_options(), "post_link", True)):
            return "%s\n%s · %d 回复" % (head, getattr(thread, "url", ""), replies)
        return "%s\n%d 回复" % (head, replies)

    def _forward_digest(self, thread: Any, detail: Any, fallback: str) -> str:
        """长图进不了聊天记录时，退回节点里用的纯正文提要。

        首行必须去掉。``build_thread_text`` 的第一行固定是「【吧】标题」，而每
        个帖子在聊天记录里已经有一个标题节点了；提要再带一次，这一帖就会在
        聊天记录里出现两遍，其中一遍只有标题 —— 用户报的「有的帖子重复发送，
        有的没内容」正是这里（``probe_stone213_forward_dup.py`` 复现）。
        """
        digest = str(fallback or "").strip()
        if not digest:
            digest = build_thread_text(
                thread,
                detail.floors,
                max_images=int(self._send_options().max_images),
            ).strip()
        lines = digest.split("\n")
        # 只吃掉落单的首行；确实没有正文时返回空串，调用方就不补节点了。
        return "\n".join(lines[1:]).strip()

    async def _deliver(
        self,
        stream_id: str,
        collected: Sequence[tuple[Any, Any]],
        payloads: dict[str, dict[str, Any]],
    ) -> None:
        cfg = self._send_options()
        # 发视频是独立开关：只开了它、关掉卡片和图文时，也该把视频发出去，
        # 所以这里不能拿 send_card/send_media 当总闸。
        want_video = bool(getattr(cfg, "send_video", True))
        if not (cfg.send_card or cfg.send_media or want_video):
            return
        if cfg.forward:
            nodes: list[dict[str, Any]] = []
            # 卡片和图文都关着时不要去合成转发：那会造出一条只有视频的空壳聊天记录，
            # 视频本来就该单独发。
            overflow: list[tuple[Any, Any]] = []
            if cfg.send_card or cfg.send_media:
                nodes, overflow = await self._forward_nodes(collected, payloads)
            sent = False
            if nodes:
                sent = bool(
                    await self.ctx.send.forward(
                        nodes,
                        stream_id,
                        timeout_ms=_FORWARD_TIMEOUT_MS,
                        storage_message=False,
                    )
                )
            # 聊天记录装不下的帖子要补发，否则它们会被当成「已经发过了」，
            # 下一轮直接跳过 —— 用户永远等不到这几条。
            #
            # 转发整条失败时（包括所有帖子都因为长图太大被退回来的情况，那时
            # ``nodes`` 是空的、压根没发出转发），聊天记录里的帖子也等于没发，
            # 一并按「单独发」补上 —— 只补视频是不够的，正文和长图同样会丢，
            # 而它们已经被记成「已搬过」，下一轮不会重来。
            missed = list(overflow) if sent else list(collected)
            if missed:
                self.ctx.logger.info(
                    "[搬石] 有 %d 条帖子没能进聊天记录，改单独发", len(missed)
                )
                for thread, detail in missed:
                    await self._send_cards(
                        stream_id, thread, detail, payloads.get(thread.tid) or {}
                    )
                    await self._send_plain(
                        stream_id, thread, detail, payloads.get(thread.tid) or {}
                    )
                await self._send_videos(stream_id, missed, payloads)
            return
        if cfg.send_card:
            for thread, detail in collected:
                await self._send_cards(stream_id, thread, detail, payloads.get(thread.tid) or {})
        if cfg.send_media:
            for thread, detail in collected:
                await self._send_plain(stream_id, thread, detail, payloads.get(thread.tid) or {})
        await self._send_videos(stream_id, collected, payloads)

    async def _send_videos(
        self,
        stream_id: str,
        collected: Sequence[tuple[Any, Any]],
        payloads: dict[str, dict[str, Any]],
    ) -> None:
        """把这条帖子的视频发出去，走 ``videourl`` 段。

        这里只把贴吧的 mp4 直链交给宿主，视频本体由协议端自己去取，**不经过
        插件与主程序之间的 RPC 通道**。这一点很关键：那条通道的单帧上限写死在
        ``src/plugin_runtime/transport/base.py`` 的 ``MAX_FRAME_SIZE = 16MB``，
        而视频要以 base64 传输会胀大约 1/3，于是原视频一旦超过 12MB 就会在发送
        时抛 ``[E_UNKNOWN] 帧大小 N 超过最大限制 16777216``——过去「大视频发不
        出去」的真正原因就在这里，跟 QQ 服务端的大小限制无关。改传 URL 之后帧
        里只有两百来个字符，实测 28.2MB 的视频照发不误。

        ``video.url`` 是带 ``tbau`` 签名的直链，签名按天过期；发送紧接着抓取
        进行，所以来得及。真过期了也只会退化成「补一条链接」，不会丢帖。
        """
        cfg = self._send_options()
        if not bool(getattr(cfg, "send_video", True)):
            return
        limit = self._video_limit_bytes()
        for thread, detail in collected:
            all_videos = self._thread_videos(detail)
            if not all_videos:
                continue
            # 逐条视频记，而不是整帖一个布尔：某一条失败时，它也必须出现在
            # 补发的链接里，否则它就无声无息地没了。
            missing: list[str] = []
            # 只发带直链的：没直链的段发出去也是个空壳。
            sendable = [video for video in all_videos if video.url]
            for video in sendable:
                if await self._video_too_big(video, thread, limit):
                    continue
                try:
                    ok = await self.ctx.send.custom(
                        "videourl",
                        {"file": video.url},
                        stream_id,
                        timeout_ms=_VIDEO_SEND_TIMEOUT_MS,
                        storage_message=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("[搬石] 发视频失败 %s：%s", video.url, exc)
                    ok = False
                if not ok:
                    self.ctx.logger.warning("[搬石] 视频没发出去，改成发链接：%s", video.url)
                    missing.append(video.url)
            # 没发成的那些补一条链接，免得用户不知道有视频
            if missing:
                await self.ctx.send.text(
                    "视频：" + "\n".join(missing), stream_id, storage_message=False
                )

    def _video_limit_bytes(self) -> int:
        """「视频大小上限」换成字节；0 表示不限制。"""
        mb = int(getattr(self._send_options(), "video_max_mb", 0) or 0)
        return mb * 1024 * 1024

    async def _video_too_big(self, video: Any, thread: Any, limit: int) -> bool:
        """这条视频是否超过上限（超了就整条跳过，连链接都不发）。

        上限的意义不是「QQ 收不收得了」——视频走 ``videourl`` 段由协议端自取，
        多大都不经过插件的 RPC 通道——而是**别让单次搬运拖太久**：协议端下载
        一个大视频会占住那一轮。

        问不到大小时（服务端不回长度、或探测请求失败）一律按「不大」处理：
        宁可偶尔搬久一点，也不要因为问不到就默默把视频全丢掉。

        反爬拦截在这里**不当异常抛**：抛出去会一路冒到 ``_deliver``，让这个群算
        「发送失败」，进而整批不记账 —— 下一轮重搬，群里重复看到同一批帖子。
        视频发不出去顶多少发一个视频，不值得把整轮的记账赔进去，所以按「不大」
        处理，照发（协议端自己去取直链，与贴吧的 IP 封锁无关）。
        """
        if limit <= 0:
            return False
        # getattr 兜底：用 object.__new__ 造的测试桩没有 _client。
        client = getattr(self, "_client", None)
        if client is None:
            return False
        try:
            size = await client.probe_video_size(video, referer=getattr(thread, "mobile_url", "") or "")
        except StoneBlockedError as exc:
            self.ctx.logger.warning("[搬石] 查视频大小时撞上反爬，这条照发：%s", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[搬石] 查视频大小失败 %s：%s", video.url, exc)
            return False
        if size is None:
            return False
        if size <= limit:
            return False
        self.ctx.logger.info(
            "[搬石] 视频 %.1f MB 超过上限 %d MB，跳过：%s",
            size / 1048576.0,
            limit // 1048576,
            video.url,
        )
        return True

    async def _card_images(
        self, thread: Any, detail: Any, payload: dict[str, Any]
    ) -> tuple[list[str], list[str], str]:
        """把一条帖子渲染成长图；返回 (base64 列表, 落盘路径列表, 渲染不出来时的退回文本)。

        两个列表一一对应：``paths[i]`` 是 ``images[i]`` 落盘后的 ``file://`` 路径，
        落盘失败时对应项是空串。转发节点优先用路径（帧里只有两百来字节、与图片
        体积无关），路径为空才退回 base64。
        """
        cfg = self._send_options()
        if _card_style(cfg) == _CARD_STYLE_PAGE:
            raw_html = str(getattr(detail, "raw", "") or "")
            if raw_html:
                html = build_page_html(
                    raw_html,
                    images=payload.get("page") or {},
                    max_floors=int(self.config.fetch.max_floors),
                )
                image_b64 = await self._render_page(html)
                if image_b64:
                    return [image_b64], [self._save_page_image(image_b64)], ""
                self.ctx.logger.warning("[搬石] 原页面长图渲染失败，这条退回文本")
                return [], [], build_thread_text(
                    thread, detail.floors, max_images=int(cfg.max_images)
                )
            self.ctx.logger.warning("[搬石] 没留下页面原文，这条改用自绘卡片")
        uris: dict[str, str] = payload.get("uris") or {}
        sizes: dict[str, tuple[int, int]] = payload.get("sizes") or {}
        width = int(cfg.card_width)
        floors = list(detail.floors)
        floor_images = _select_floor_images(floors, uris, int(cfg.max_images))
        chunks = _plan_card_chunks(floors, sizes, width, floor_images)
        if not chunks:
            return [], [], ""
        images: list[str] = []
        paths: list[str] = []
        for index, chunk in enumerate(chunks):
            segment = _seg_images(chunk, floors, floor_images, uris)
            html = build_thread_html(
                thread,
                chunk,
                images=segment,
                width=width,
                max_images=len(segment),
                part=index,
                parts=len(chunks),
            )
            # 同样盯着体积：自绘卡片比原页面短，但长帖切出来的分片一样可能超标，
            # 超了这张就发不出去。降档重渲，还不行才退回文本。
            image_b64 = ""
            for scale in _PAGE_RENDER_SCALES:
                try:
                    result = await self._render_card_html(html, scale)
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning(
                        "[搬石] 长图渲染抛异常（缩放 %s）：%s", scale, exc
                    )
                    result = None
                candidate = ""
                if isinstance(result, dict):
                    candidate = str(result.get("image_base64") or "")
                if not candidate:
                    continue
                raw_bytes = len(candidate) * 3 // 4
                if raw_bytes <= _PAGE_IMAGE_SOFT_LIMIT_BYTES:
                    image_b64 = candidate
                    break
                self.ctx.logger.warning(
                    "[搬石] 卡片长图 %.1f MB 超过单张上限，降档重渲：%sx",
                    raw_bytes / 1048576.0,
                    scale,
                )
            if not image_b64:
                # 渲染不出来不能什么都不发：退回纯文本
                self.ctx.logger.warning("[搬石] 长图渲染失败或太大，这条退回文本")
                return [], [], build_thread_text(
                    thread, detail.floors, max_images=int(cfg.max_images)
                )
            images.append(image_b64)
            paths.append(self._save_page_image(image_b64))
        return images, paths, ""

    async def _render_page(self, html: str) -> str:
        """把整页 HTML 交给宿主渲染器截成一张长图；失败返回空串。

        这条路径必须开 allow_network：页面样式表和图标都在 CDN 上，宿主渲染器默认断网。

        渲染完还要**看体积**。长图是一张要过 RPC 的图片，而图片在帧里 base64 存两份，
        原文件上限只有 6.000 MiB；贴吧原页面又长又花，2 倍缩放出来的 PNG 轻易就 8~12MB。
        超限的话 ``ctx.send.image`` 会抛异常，用户那边一张图都看不到，而插件只是记一行
        日志 —— 等于白搬。所以大了就降档重渲（缩放越小体积越小，代价是字更糊），
        几档都还不行才认输退回文本。

        另外**不能走 ``ctx.render.html2png``**。SDK 那个包装不收 ``timeout_ms``，RPC 侧
        一律按默认的 30 秒算（``runner/rpc_client.py``），而 ``render_timeout_ms`` 只传给
        宿主渲染器、管不了 RPC。贴吧原页面冷启动渲染本来就常要 20 秒以上，一超 30 秒
        这边就先抛 ``[E_TIMEOUT] cap.call 超时`` 放弃了，宿主其实还在渲 —— 白等一场，
        退回去发文本。所以这里直接用 ``call_host_method`` 把 RPC 超时也按渲染超时给足。
        """
        for scale in _PAGE_RENDER_SCALES:
            try:
                result = await self._render_html2png(html, scale, _PAGE_RENDER_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[搬石] 原页面渲染抛异常（缩放 %s）：%s", scale, exc)
                continue
            image_b64 = ""
            if isinstance(result, dict):
                image_b64 = str(result.get("image_base64") or "")
            if not image_b64:
                continue
            # base64 长度反推原始字节：base64 是 4 个字符存 3 字节。
            raw_bytes = len(image_b64) * 3 // 4
            if raw_bytes <= _PAGE_IMAGE_SOFT_LIMIT_BYTES:
                return image_b64
            self.ctx.logger.warning(
                "[搬石] 原页面长图 %.1f MB 超过单张上限 %.1f MB，降档重渲：%sx",
                raw_bytes / 1048576.0,
                _PAGE_IMAGE_SOFT_LIMIT_BYTES / 1048576.0,
                scale,
            )
        self.ctx.logger.warning("[搬石] 原页面长图降档后仍然太大，这条退回文本")
        return ""

    async def _render_card_html(self, html: str, scale: float) -> Any:
        """渲染自绘卡片（选择器 ``#card``）。

        和 :meth:`_render_html2png` 同样要绕开 SDK 包装的 30 秒默认超时 ——
        长帖切出来的分片一样能渲染很久。
        """
        payload = {
            "capability": "render.html2png",
            "args": {
                "html": html,
                "selector": "#card",
                "viewport": {},
                "device_scale_factor": scale,
                "full_page": True,
                "omit_background": False,
                "wait_until": "load",
                "wait_for_selector": "",
                "wait_for_timeout_ms": 0,
                "render_timeout_ms": _CARD_RENDER_TIMEOUT_MS,
                "allow_network": False,
            },
        }
        result = await self.ctx.call_host_method(
            "cap.call", payload=payload, timeout_ms=_CARD_RENDER_TIMEOUT_MS + _RENDER_RPC_SLACK_MS
        )
        return self.ctx._normalize_capability_result("render.html2png", result)

    async def _render_html2png(self, html: str, scale: float, timeout_ms: int) -> Any:
        """渲染 HTML，并且让 RPC 超时跟着渲染超时走（默认 30 秒根本不够）。

        ``ctx.render.html2png`` 是 SDK 的薄包装，参数里没有 ``timeout_ms``，于是不管
        你 ``render_timeout_ms`` 传多大，RPC 层都只等 30 秒。直接调 ``cap.call`` 才能
        把超时传下去。``cap.call`` 在 SDK 的 ``_ALLOWED_RAW_HOST_METHODS`` 里，合规。
        """
        payload = {
            "capability": "render.html2png",
            "args": {
                "html": html,
                "selector": "body",
                "viewport": {
                    "width": _PAGE_VIEWPORT_WIDTH,
                    "height": _PAGE_VIEWPORT_HEIGHT,
                },
                "device_scale_factor": scale,
                "full_page": True,
                "omit_background": False,
                "wait_until": "load",
                "wait_for_selector": "",
                "wait_for_timeout_ms": 0,
                "render_timeout_ms": timeout_ms,
                "allow_network": True,
            },
        }
        # 插件自己再多等一会儿：宿主判超时会先返回错误，这里只是兜住 RPC 层，
        # 免得它比宿主更早放弃、把一个还在渲的页面扔掉。
        result = await self.ctx.call_host_method(
            "cap.call", payload=payload, timeout_ms=timeout_ms + _RENDER_RPC_SLACK_MS
        )
        return self.ctx._normalize_capability_result("render.html2png", result)

    async def _send_cards(
        self, stream_id: str, thread: Any, detail: Any, payload: dict[str, Any]
    ) -> None:
        images, paths, fallback = await self._card_images(thread, detail, payload)
        if not images:
            if fallback:
                await self.ctx.send.text(fallback, stream_id, storage_message=False)
            return
        for index, image_b64 in enumerate(images):
            # 单独发长图时也优先用落盘路径：走 base64 的话一张图就要过一次
            # 16MB 的 RPC 帧，路径只有两百来字节。路径为空才退回内嵌。
            page = paths[index] if index < len(paths) else ""
            await self._send_page_image(stream_id, page, image_b64)

    async def _send_page_image(self, stream_id: str, page_uri: str, image_b64: str) -> None:
        """发一张长图：有落盘路径就走路径，否则退回内嵌 base64。"""
        if page_uri:
            try:
                sent = await self.ctx.send.custom(
                    "imageurl",
                    {"file": page_uri},
                    stream_id,
                    storage_message=False,
                    timeout_ms=_VIDEO_SEND_TIMEOUT_MS,
                )
                if sent is not False:
                    return
                self.ctx.logger.warning("[搬石] 长图按本地路径发送没成功，改用内嵌图片")
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[搬石] 长图按本地路径发送失败，改用内嵌图片：%s", exc)
        await self.ctx.send.image(image_b64, stream_id, storage_message=False)

    async def _send_plain(
        self, stream_id: str, thread: Any, detail: Any, payload: dict[str, Any]
    ) -> None:
        blobs: dict[str, str] = payload.get("blobs") or {}
        await self.ctx.send.text(self._thread_header(thread), stream_id, storage_message=False)
        for floor in detail.floors:
            text = str(getattr(floor, "text", "") or "").strip()
            if text:
                await self.ctx.send.text(
                    "%d楼 %s：%s" % (int(floor.fn), floor.author or "匿名", text),
                    stream_id,
                    storage_message=False,
                )
            for url in list(floor.images or []):
                blob = blobs.get(url)
                if not blob:
                    continue
                await self.ctx.send.image(blob, stream_id, storage_message=False)

    async def _forward_nodes(
        self, collected: Sequence[tuple[Any, Any]], payloads: dict[str, dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[tuple[Any, Any]]]:
        """把本轮所有帖子打包成一条聊天记录：文字、图片、长图、视频都装进去。

        **要盯着帧大小**。整条聊天记录是**一个** RPC 帧，上限 16MB（宿主
        ``transport/base.py`` 的 ``MAX_FRAME_SIZE``），而图片是以 base64 塞进段里的，
        体积还因为宿主的归一化翻倍（详见 ``_FORWARD_FRAME_BUDGET_BYTES``）。实测 5 帖
        x 12 楼 x 2 张 200KB 的正文图就有 31MB，5 帖各带一张 2MB 长图也有 19.6MB ——
        都必然撞上限，整条聊天记录发不出去，用户看到的是「搬了半天什么也没有」。
        所以这里维护一个预算，快满了就收窄：先少放楼层，再放弃这条记录。

        返回 ``(nodes, overflow)``：``overflow`` 是**因为装不下而没能进这条聊天记录**
        的帖子，调用方必须把它们单独发掉。这不是可选的礼节 —— 转发成功的话 ``sent``
        是 True，没人会察觉内容被截掉了，而这几条帖子又会被记成「已搬过」，于是
        永远补不回来。

        长图装不进预算时**整帖**退出去（``deferred``），不要在聊天记录里再补一个
        只有标题的节点：标题节点前面已经发过，补出来的是重复内容，用户看到的
        是「同一帖出现两遍、其中一遍没正文」。
        """
        cfg = self._send_options()
        # 长图进不了这条聊天记录、又交代不清正文的帖子，整帖交回给 _deliver
        # 单独发。**不能只把长图丢掉、再补一个提要节点** —— 标题节点上面已经
        # 发过了，补出来的那个只会是第二次标题，用户看到的就是「有的帖子重复
        # 发送，有的没内容」（``probe_stone213_forward_dup.py`` 复现的就是它）。
        deferred: list[tuple[Any, Any]] = []
        nodes: list[dict[str, Any]] = []
        used_bytes = 0
        position = -1
        # 帧里除了图片还有别的字段（昵称、楼号文字、JSON 结构），留出余量。
        budget = _FORWARD_FRAME_BUDGET_BYTES
        for position, (thread, detail) in enumerate(collected):
            first = len(nodes)
            payload = payloads.get(thread.tid) or {}
            blobs: dict[str, str] = payload.get("blobs") or {}
            header = {"type": "text", "data": self._thread_header(thread)}
            nodes.append(
                {
                    "nickname": self._forward_name(),
                    "user_id": "",
                    "segments": [header],
                }
            )
            header_cost = len(str(header.get("data") or "")) + _FORWARD_NODE_OVERHEAD
            used_bytes += header_cost
            if cfg.send_card:
                images, paths, fallback = await self._card_images(thread, detail, payload)
                if images:
                    # 长图优先用「本地路径引用」：节点里只放一个 file:// 路径，
                    # 帧里是两百来字节，**与图片有多大无关**。这样一张 6MB 的长图
                    # 不再吃掉整条预算，后面的帖子也不必被踢出去单独发。
                    # 落盘失败（paths 对应项为空串）才退回内嵌 base64。
                    segments: list[dict[str, Any]] = []
                    embedded_cost = 0
                    for index, b64 in enumerate(images):
                        page = paths[index] if index < len(paths) else ""
                        if page:
                            segments.append(
                                {
                                    "type": "dict",
                                    "data": {
                                        "type": "imageurl",
                                        "data": {"file": page},
                                    },
                                }
                            )
                            # 路径段的帧开销虽然只有两百来字节，但也得记账，
                            # 否则预算算的是「内嵌部分」而不是「整条节点」。
                            embedded_cost += _PATH_SEGMENT_COST
                        else:
                            segments.append({"type": "image", "content": b64})
                            embedded_cost += len(b64)
                    if used_bytes + embedded_cost > budget:
                        # 只在内嵌兜底时才会撞预算。这帖整帖退出去单独发：
                        # 长图和正文都还在，只是不再留在聊天记录里。
                        self.ctx.logger.info(
                            "[搬石] 第 %s 帖的长图装不下这条聊天记录了（%d 字节），这帖改单独发",
                            thread.tid,
                            embedded_cost,
                        )
                        del nodes[first:]
                        used_bytes -= header_cost
                        deferred.append((thread, detail))
                        continue
                    used_bytes += embedded_cost
                    nodes.append(
                        {
                            "nickname": self._forward_name(),
                            "user_id": "",
                            "segments": segments,
                        }
                    )
                elif fallback:
                    # 只有长图渲染不出来时才走这里。提要首行是标题，而标题节点
                    # 上面已经发过，去掉它，节点里只留正文。
                    digest = self._forward_digest(thread, detail, fallback)
                    if digest:
                        nodes.append(
                            {
                                "nickname": self._forward_name(),
                                "user_id": "",
                                "segments": [{"type": "text", "data": digest}],
                            }
                        )
                        used_bytes += len(digest) + _FORWARD_NODE_OVERHEAD
            if not cfg.send_media:
                self._attach_videos(nodes, first, detail, payload)
                continue
            used = 0
            for floor in detail.floors:
                if used >= _FORWARD_FLOOR_NODES:
                    break
                segments: list[dict[str, Any]] = []
                text = str(getattr(floor, "text", "") or "").strip()
                if text:
                    segments.append(
                        {
                            "type": "text",
                            "data": "%d楼：%s" % (int(floor.fn), text),
                        }
                    )
                for url in list(floor.images or []):
                    blob = blobs.get(url)
                    if not blob:
                        continue
                    # 正文小图同样优先落盘走路径：省下的不只是这几百 KB，
                    # 更是「一张大图吃掉整条预算」的连锁反应。落盘失败才内嵌。
                    page = self._save_blob_image(blob)
                    if page:
                        segments.append(
                            {
                                "type": "dict",
                                "data": {"type": "imageurl", "data": {"file": page}},
                            }
                        )
                        continue
                    # 图片段的 base64 必须放在 content 里：宿主按 content 写
                    # binary_data_base64。装不下就丢掉这一张，后面更小的兴许还塞得进。
                    if used_bytes + len(blob) > budget:
                        continue
                    used_bytes += len(blob)
                    segments.append({"type": "image", "content": blob})
                if not segments:
                    continue
                nodes.append(
                    {
                        "nickname": str(getattr(floor, "author", "") or "").strip()
                        or self._forward_name(),
                        "user_id": "",
                        "segments": segments,
                    }
                )
                used_bytes += _FORWARD_NODE_OVERHEAD
                used += 1
            self._attach_videos(nodes, first, detail, payload)
            # 连纯文字都塞满了，再往后只会更糟：停在这里。
            # **但剩下的帖子不能就这么算了**：这条聊天记录待会儿照样发得出去，
            # `sent` 会是 True，没有任何地方会发现内容被截掉了 —— 那些帖子就此
            # 人间蒸发，而且因为 _run_locked 认为一切正常，它们还会被标记成
            # 「已搬过」，下一轮也不会补。所以把剩下的帖子交回去，由 _deliver
            # 单独一条条发：宁可多几条消息，也不能悄无声息地丢帖子。
            if used_bytes > budget:
                self.ctx.logger.info(
                    "[搬石] 这条聊天记录已经装到 %d 字节，后面 %d 条帖子改单独发",
                    used_bytes,
                    len(collected) - position - 1,
                )
                return nodes, list(collected[position + 1 :]) + deferred
        return nodes, deferred

    def _attach_videos(
        self,
        nodes: list[dict[str, Any]],
        first: int,
        detail: Any,
        payload: dict[str, Any],
    ) -> None:
        """把这一帖的视频挂到它最后一个转发节点的末尾，让视频留在聊天记录里。"""
        segments = self._video_segments(detail, payload)
        if segments and len(nodes) > first:
            nodes[-1]["segments"].extend(segments)

    def _video_segments(self, detail: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """构造合并转发节点里的视频消息段（走 URL，不用 base64）。

        转发节点里的段同样要「双层」写：宿主把非 file 的自定义段一律包成
        ``DictComponent(data=内层)`` 并丢掉外层 ``type``，适配器拿到的是
        ``{"type":"dict","data":内层}``，只有内层自带 ``type`` 才认得出。
        内层用 ``videourl``（带直链），而不是 ``video`` + base64——后者会让
        整个转发帧撞上 16MB 的 RPC 单帧上限。
        """
        del payload  # 视频不再预下载，用不上
        if not bool(getattr(self._send_options(), "send_video", True)):
            return []
        segments: list[dict[str, Any]] = []
        # 只发带直链的：没直链的段发出去也是个空壳。
        sendable = [video for video in self._thread_videos(detail) if video.url]
        for video in sendable:
            segments.append(
                {
                    "type": "video",
                    "data": {
                        "type": "videourl",
                        "data": {"file": video.url},
                    },
                }
            )
        return segments

    @staticmethod
    def _thread_videos(detail: Any) -> list[Any]:
        """按楼层顺序列出这一帖里的所有视频。"""
        return [v for floor in detail.floors for v in getattr(floor, "videos", [])]

    # -- 工具 -------------------------------------------------------------

    @Tool(
        "tieba_share",
        description=(
            "去贴吧搬帖子发到当前会话。用户说「搬个石」「搬点贴吧的帖子」"
            "「去XX吧搬一条」「搬个视频帖」时用它。只读不写：逛指定的吧"
            "（不填就用配置里勾好的），取最新的帖子、跳过已经搬过的，一次最多 5 条。"
            "默认搬 1 条；发出来的样子（图文、长图还是聊天记录）由插件配置决定，"
            "不用你操心。"
        ),
        # 宿主给组件的默认 RPC 超时只有 60 秒，而逛一个吧再取帖发帖常要一两分钟。
        # 不声明的话宿主会先判超时把结果丢掉，插件这边却还在跑。
        timeout_ms=300000,
        parameters=[
            ToolParameterInfo(
                name="forum",
                param_type=ToolParamType.STRING,
                description=(
                    "贴吧的吧名，只写吧名本身、不带末尾的「吧」字，"
                    "例如「搬石」「孙笑川」。留空就用配置里勾好的吧。"
                ),
                required=False,
            ),
            ToolParameterInfo(
                name="count",
                param_type=ToolParamType.INTEGER,
                description="搬几条，默认 1，最多 5。",
                required=False,
                default=1,
            ),
            ToolParameterInfo(
                name="thread_type",
                param_type=ToolParamType.STRING,
                description=(
                    "只搬哪种帖：any＝不限（默认，用配置里的设置）；"
                    "text＝只要图文帖；video＝只要视频帖。"
                    "用户说「搬个视频帖」「只要带图的」时用它。"
                ),
                required=False,
                enum_values=[_KIND_ANY, _KIND_TEXT, _KIND_VIDEO],
                default=_KIND_ANY,
            ),
        ],
    )
    async def handle_move_tool(self, **kwargs: Any) -> dict[str, Any]:
        """模型调用的搬石工具：只发当前会话，一次一两条。

        怎么发（图文 / 长图 / 聊天记录 / 带不带链接）一律照配置走 ——
        工具只决定「搬哪条、搬几条、只搬哪种帖」这三件事。
        """
        if not bool(getattr(self.config.plugin, "enabled", False)):
            return {"success": True, "content": "搬石插件没启用，先让人在插件设置里打开。"}
        tool_cfg = getattr(self.config, "tool", None)
        if tool_cfg is not None and not bool(getattr(tool_cfg, "enabled", True)):
            return {"success": True, "content": "搬石工具被关掉了，让人在插件设置 →「工具」里打开。"}
        # 配置里没有「工具」一节时（老版本 config.toml 没这一节，宿主不会自动补），
        # tool_cfg 会是 None，下面几个 getattr 全都变成「没配」。不能让模型因此报错，
        # 也不能让「工具箱一次最多几条」这类默认值凭空消失：统一回落到这里的默认值。
        if tool_cfg is None:
            tool_cfg = getattr(self.config, "send", None)
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not stream_id:
            return {"success": False, "content": "拿不到当前会话，发不出去。"}

        forum = str(kwargs.get("forum") or "").strip()
        # 贴吧的 kw 要的是吧名本身，模型经常会顺手带上末尾的「吧」字，这里削掉
        if len(forum) > 1 and forum.endswith("吧"):
            forum = forum[:-1]
        allowed_forums = [str(x).strip() for x in (getattr(tool_cfg, "forums", None) or [])]
        allowed_forums = [x for x in allowed_forums if x]
        if forum and allowed_forums:
            # 面板上限定了吧就别让模型乱逛：比对时忽略末尾的「吧」字
            if forum not in {x[:-1] if x.endswith("吧") and len(x) > 1 else x for x in allowed_forums}:
                return {
                    "success": True,
                    "content": "这个吧不在允许范围里，能搬的是：" + "、".join(allowed_forums) + "。",
                }
        if forum and not _FORUM_NAME_RE.fullmatch(forum):
            return {
                "success": False,
                "content": "吧名看着不对，只写吧名本身（比如「搬石」），别带链接或符号。",
            }

        try:
            want = int(kwargs.get("count") or 0)
        except (TypeError, ValueError):
            want = 0
        cap = int(getattr(tool_cfg, "max_count", _TOOL_MAX_COUNT) or _TOOL_MAX_COUNT)
        cap = max(1, min(cap, _TOOL_MAX_COUNT))
        want = 1 if want <= 0 else min(want, cap)

        kind = _thread_kind(kwargs.get("thread_type"))
        # 「不限」直接留空，让 _pick_threads 走配置里那个值，别用工具的默认把配置顶掉
        only_kind = "" if kind == _FILTER_ALL else kind
        if not forum and allowed_forums:
            # 模型没说逛哪个吧：只逛允许的那些
            only_forums_arg = allowed_forums
        else:
            only_forums_arg = [forum] if forum else None

        # 同 _cmd_move：也派到后台去跑，结果由插件自己发到会话里。
        # 这里不能同步等 —— 贴吧一轮动辄一两分钟，等它跑完再回话，
        # 模型那句回复就晚了，宿主那边也可能先判超时。
        if self._busy or self._lock.locked():
            return {"success": True, "content": "上一轮还在搬，等它跑完再试。"}
        self._busy = True
        self._spawn_job(
            self._manual_round(
                stream_id,
                only_forums=only_forums_arg,
                count=want,
                only_kind=only_kind,
                trigger="工具",
            )
        )
        return {"success": True, "content": "好，开始搬石了，搬完直接发到这个会话里。"}

    # -- 命令 -------------------------------------------------------------

    @Command(
        "搬石",
        description=(
            "麦麦贴吧搬石：把贴吧的帖子搬到群里。"
            "/搬石 按配置搬一轮 | /搬视频 只搬视频帖 | /搬石 状态 | "
            "/搬石 重置 [吧名] | /搬石 帮助（维护命令要管理员，在插件设置的「权限」里配）"
        ),
        # 命令必须出现在整段文本最前面。宿主用 search() 匹配，不锚定的话
        # 「这个 /搬石 插件不错」这种闲聊里的提及也会被当成命令。
        # 引用回复时宿主把被引用原文拼在最前，正文命令因此不再处于开头 ——
        # 那是想要的效果（引用的命令不该重跑），handler 里的引用守卫再兜一层。
        #
        # 字符类里的几处讲究，动之前先看：
        #   [^\S\n] 代替 \s —— \s 含换行，会让 rest 吞掉下一行；
        #   sub/rest 都排除 ] ） ) 】 」 』 —— 闭括号不是子命令的一部分。
        #     （早先 sub 只排除 ]，尾部带括号时正则引擎会选「让 sub 不参与、
        #      整段塞进 rest」这条路径，于是 sub 变空 → 被当成裸命令 →
        #      管理员直接搬一轮。引用场景下这就是「想查状态结果搬了一轮」。）
        #   \Z 代替 $ —— $ 认末尾那个换行之前的位置，会跨行匹配。
        pattern=r"^[/／]搬石(?:[^\S\n]+(?P<sub>[^\s/／\]）)】」』]+))?(?:[^\S\n]+(?P<rest>[^\n\]）)】」』]*?))?[^\S\n]*[\]）)】」』]?\Z",
        permission="operator",
    )
    async def handle_stone(self, **kwargs: Any) -> tuple[bool, str, int]:
        matched = kwargs.get("matched_groups") or {}
        sub = str(matched.get("sub") or "").strip()
        rest = str(matched.get("rest") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "")
        if not stream_id:
            return False, "缺少聊天流 ID", 1
        # 引用一条写着「/搬石 …」的消息时，宿主会把被引用原文拼进这次文本，
        # 于是命令被"带"进来又执行一遍。检测到命令来自引用部分就放行不处理。
        full_text = str(kwargs.get("processed_plain_text") or kwargs.get("plain_text") or "")
        quoted = full_text and (
            _quoted_command(full_text, "/搬石") or _quoted_command(full_text, "／搬石")
        )
        if quoted:
            self.ctx.logger.info("[搬石] 命令出现在被引用内容里，跳过：%r", full_text[:80])
            return True, "引用内容里的命令，已跳过", 1
        # 同一条消息只处理一次：宿主超时后重投、或引用带出同一条命令时，
        # 不能让同一句话再搬一轮。
        message_id = str(kwargs.get("message_id") or "").strip()
        if message_id:
            if message_id in self._seen_message_ids:
                self.ctx.logger.info("[搬石] 同一条消息重复进入命令链，跳过：%s", message_id)
                return True, "重复消息，已跳过", 1
            self._seen_message_ids.append(message_id)
        is_admin = await self._may_maintain(
            str(kwargs.get("platform") or ""),
            str(kwargs.get("user_id") or ""),
            stream_id,
            kwargs.get("is_local_operator") is True,
        )
        low = sub.lower()
        if not sub:
            # 管理员敲 /搬石 就是想现在搬；其他人看到用法说明
            if is_admin:
                return await self._cmd_move(stream_id)
            await self.ctx.send.text(_HELP_TEXT, stream_id, storage_message=False)
            return True, "用法说明", 1
        if low in ("帮助", "help", "?"):
            await self.ctx.send.text(_HELP_TEXT, stream_id, storage_message=False)
            return True, "用法说明", 1
        if low in ("状态", "status"):
            if not is_admin:
                await self.ctx.send.text(_MAINTENANCE_DENIED, stream_id, storage_message=False)
                return True, "维护命令需要管理员权限", 1
            await self.ctx.send.text(self._status_text(), stream_id, storage_message=False)
            return True, "状态", 1
        if low in ("重置", "reset"):
            if not is_admin:
                await self.ctx.send.text(_MAINTENANCE_DENIED, stream_id, storage_message=False)
                return True, "维护命令需要管理员权限", 1
            # 「/搬石 重置 冷却」单独用来解除反爬冷却：记录照旧留着，
            # 否则用户想把冷却清掉就只能连搬运记录一起清。
            if rest in ("冷却", "block", "反爬"):
                if self._client is not None:
                    self._client.reset_block()
                await self.ctx.send.text(
                    "反爬冷却已解除，下次搬石会重新请求贴吧。",
                    stream_id,
                    storage_message=False,
                )
                return True, "冷却已解除", 1
            count = self._store.reset(rest) if self._store is not None else 0
            if self._store is not None:
                self._store.flush()
            await self.ctx.send.text(
                "已清空 %d 条搬运记录%s，下次会重新搬。" % (count, ("（%s）" % rest) if rest else ""),
                stream_id,
                storage_message=False,
            )
            return True, "记录已重置", 1
        if low in ("搬", "搬石"):
            if not is_admin:
                await self.ctx.send.text(_MAINTENANCE_DENIED, stream_id, storage_message=False)
                return True, "维护命令需要管理员权限", 1
            return await self._cmd_move(stream_id)
        if low in ("视频", "搬视频", "video"):
            if not is_admin:
                await self.ctx.send.text(_MAINTENANCE_DENIED, stream_id, storage_message=False)
                return True, "维护命令需要管理员权限", 1
            return await self._cmd_move(stream_id, only_kind=_FILTER_VIDEO, what="搬视频")
        if low in ("图文", "搬图文", "图"):
            if not is_admin:
                await self.ctx.send.text(_MAINTENANCE_DENIED, stream_id, storage_message=False)
                return True, "维护命令需要管理员权限", 1
            return await self._cmd_move(stream_id, only_kind=_FILTER_TEXT, what="搬图文")
        await self.ctx.send.text(
            "不认识的子命令，打「/搬石 帮助」看看。", stream_id, storage_message=False
        )
        return True, "未知子命令", 1

    @Command(
        "搬视频",
        description=(
            "麦麦贴吧搬石：只搬视频帖，不看配置里的「只搬哪种帖」。"
            "/搬视频 立刻搬一轮视频帖（要管理员，在插件设置的「权限」里配）"
        ),
        # 同一个正则套路，只把命令名换成「搬视频」，后缀仍收进 rest 备用。
        pattern=r"^[/／]搬视频[^\S\n]*(?P<rest>[^\n\]）)】」』]*?)[^\S\n]*[\]）)】」』]?\Z",
        permission="operator",
    )
    async def handle_video(self, **kwargs: Any) -> tuple[bool, str, int]:
        """「/搬视频」：只搬视频帖。

        用户说「举个例子 /搬视频 就是只搬视频内容，/搬石 就是按照配置内容来」，
        所以这条命令不读配置里的「只搬哪种帖」，一律按视频筛。
        """
        guard = self._command_guard(kwargs, "/搬视频")
        if guard is not None:
            return guard
        stream_id = str(kwargs.get("stream_id") or "")
        if not stream_id:
            return False, "缺少聊天流 ID", 1
        is_admin = await self._may_maintain(
            str(kwargs.get("platform") or ""),
            str(kwargs.get("user_id") or ""),
            stream_id,
            kwargs.get("is_local_operator") is True,
        )
        if not is_admin:
            await self.ctx.send.text(_MAINTENANCE_DENIED, stream_id, storage_message=False)
            return True, "维护命令需要管理员权限", 1
        return await self._cmd_move(stream_id, only_kind=_FILTER_VIDEO, what="搬视频")

    def _command_guard(self, kwargs: dict[str, Any], command: str) -> tuple[bool, str, int] | None:
        """命令入口的两道公共守卫：引用不算数、同一条消息只处理一次。

        返回 None 表示放行；返回三元组就是直接交给宿主的应答。
        """
        full_text = str(kwargs.get("processed_plain_text") or kwargs.get("plain_text") or "")
        if full_text and (
            _quoted_command(full_text, command) or _quoted_command(full_text, command.replace("/", "／"))
        ):
            self.ctx.logger.info("[搬石] 命令出现在被引用内容里，跳过：%r", full_text[:80])
            return True, "引用内容里的命令，已跳过", 1
        message_id = str(kwargs.get("message_id") or "").strip()
        if message_id:
            if message_id in self._seen_message_ids:
                self.ctx.logger.info("[搬石] 同一条消息重复进入命令链，跳过：%s", message_id)
                return True, "重复消息，已跳过", 1
            self._seen_message_ids.append(message_id)
        return None

    async def _cmd_move(
        self, stream_id: str, *, only_kind: str = "", what: str = "搬石"
    ) -> tuple[bool, str, int]:
        """立刻回一句「开始搬了」，真正那一轮丢后台跑。

        宿主给命令 60 秒，而一轮搬石常常超过它。同步等的话宿主会判超时并把结果
        丢掉，可插件这边还在跑 —— 用户既看不到回执，搬运又照常发生。所以命令
        本身只负责应答，搬运交给后台任务，跑完再往群里发结果。

        only_kind 非空时只搬那一种帖子（「/搬视频」用），what 只影响回执里那句话。
        """
        if self._busy or self._lock.locked():
            await self.ctx.send.text(
                "上一轮还在搬，等它跑完再来。", stream_id, storage_message=False
            )
            return True, "上一轮还没跑完", 1
        self._busy = True
        self._spawn_job(self._manual_round(stream_id, only_kind=only_kind))
        await self.ctx.send.text(
            "好，开始%s了，跑完把结果发这里。" % what, stream_id, storage_message=False
        )
        return True, "已开始搬运", 1

    async def _manual_round(
        self,
        stream_id: str,
        *,
        only_forums: list[str] | None = None,
        count: int = 0,
        only_kind: str = "",
        trigger: str = "手动",
    ) -> None:
        """后台跑的那一轮：结果或错误都往群里发一条。

        命令入口只传 stream_id；模型工具入口还会带上吧名、条数和只要哪种帖。
        """
        try:
            summary = await self._run_round(
                trigger,
                streams=[stream_id],
                only_forums=only_forums,
                count=count,
                only_kind=only_kind,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[搬石] 手动轮次异常：%s", exc, exc_info=True)
            summary = "这轮搬石出错了：%s（详细原因看主程序日志）" % exc
        try:
            await self.ctx.send.text(summary, stream_id, storage_message=False)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[搬石] 手动结果发不出去：%s", exc)

    def _status_text(self) -> str:
        cfg = self.config
        lines = ["麦麦贴吧搬石 · 状态"]
        lines.append(
            "版本 %s｜自动搬石 %s｜呈现 %s"
            % (
                _PLUGIN_VERSION,
                "开" if cfg.schedule.auto_enabled else "关",
                self._mode_label(),
            )
        )
        forums = resolve_forums(cfg.stone)
        lines.append("要逛的吧：" + ("、".join(forums) if forums else "（没配）"))
        if cfg.schedule.daily_times:
            lines.append("定时：每天 " + "、".join(str(x) for x in cfg.schedule.daily_times))
        else:
            base = str(getattr(cfg.schedule, "base_time", "") or "").strip() or "0:00"
            lines.append(
                "定时：从 %s 起每 %d 分钟" % (base, int(cfg.schedule.interval_minutes))
            )
        if self._lead_seconds():
            lines.append("提前 %d 秒开搬，到点再发" % int(self._lead_seconds()))
        bad = _bad_daily_times(cfg.schedule.daily_times)
        if bad:
            # 认不出来的时刻会被丢掉，剩下几个照跑；不说的话用户只会觉得「怎么没按我设的来」
            lines.append(
                "⚠ 这几个时刻没认出来、不会生效：" + "、".join(bad) + "（写成 9:30 这样就行）"
            )
        stats = self._store.stats() if self._store is not None else {}
        if not stats:
            lines.append("还没有搬过任何帖子。")
        else:
            lines.append("搬运记录：")
            for forum, info in sorted(stats.items()):
                lines.append(
                    "· %s：最近 %d 条在档，累计 %d 条，最后 %s"
                    % (
                        forum,
                        int(info.get("recent") or 0),
                        int(info.get("total") or 0),
                        str(info.get("last_run") or "（没记）"),
                    )
                )
        if self._client is not None:
            left = self._client.blocked_remaining()
            if left > 0:
                lines.append("反爬冷却中，还剩 %d 秒。" % int(left))
        lines.append("维护命令权限：" + self._admin_label())
        return "\n".join(lines)

    def _admin_label(self) -> str:
        """状态里回显谁能用维护命令 —— 免得「说管理员能用却没地方配」。"""
        section = getattr(self.config, "admin", None)
        names = sorted(_admin_ids(getattr(section, "admins", None)))
        groups = sorted(_group_set(getattr(section, "allow_groups", None)))
        parts: list[str] = []
        parts.append("管理员 " + "、".join(names) if names else "管理员（还没填）")
        if groups:
            parts.append("放行群 " + "、".join(groups))
        parts.append("主程序终端")
        return "；".join(parts)

    async def _may_maintain(
        self, platform: str, user_id: str, stream_id: str, local_operator: bool
    ) -> bool:
        """维护类命令能不能执行。

        放行途径：主程序终端；插件设置「权限」一节里那张 QQ 号名单；该节里的
        「管理员群」写明的群（群里谁都能用）。群按会话里的群号比对，比聊天流
        ID 好认得多。

        两张表都留空时才回落到宿主 ``[plugin] permission`` —— 那是宿主给整个
        插件系统用的名单，改它会影响别的插件。一旦用户在这里填了人，就以这里
        为准，不再看宿主那份；否则会出现「名单里明明没他，他还是能用」这种
        对不上的行为。
        """
        if local_operator:
            return True
        section = getattr(self.config, "admin", None)
        admins = _admin_ids(getattr(section, "admins", None))
        groups = _group_set(getattr(section, "allow_groups", None))
        uid = _clean_id(user_id)
        if uid and uid in admins:
            return True
        group = _clean_id(stream_id)
        if group and group in groups:
            return True
        # 上面两张表任意一张填了人，就按名单说了算：没命中就是没权限。
        if admins or groups:
            return False
        scoped = _scoped_user_id(platform, user_id)
        if not scoped:
            return False
        try:
            configured = await self.ctx.config.get("plugin.permission", [])
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[搬石] 读 [plugin] permission 失败：%s", exc)
            return False
        if not isinstance(configured, (list, tuple, set, frozenset)):
            return False
        return scoped in _permission_set(configured)


def create_plugin() -> StonePlugin:
    return StonePlugin()
