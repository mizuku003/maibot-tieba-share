"""麦麦贴吧搬石 —— 长图卡片渲染。

把一条帖子的首页楼层拼成一张竖图，交给宿主的渲染器出图：
    ctx.render.html2png(html, selector="#card", full_page=True, device_scale_factor=2.0)

三条硬约束（照宿主实现来的，踩过就废）：

1. 根节点必须带 id="card"，宿主的截图器按这个选择器找元素。
2. 宿主渲染器没有外网，HTML 里出现的图片一律得是 base64 data URI，
   直接写 https 图片地址会渲成空白。
3. Chromium 的截图画布有高度上限，一张图太高会直接截失败。
   所以调用方要按层数分段（见 plugin.py 的 _plan_card_chunks），
   这里只负责画一段，并在标题上标出「第几段」。
"""

from __future__ import annotations

import base64
import html
import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:  # Pillow 是宿主自带的，缺了也能跑（只是不压缩）
    from PIL import Image

    _HAS_PIL = True
except Exception:  # noqa: BLE001
    Image = None  # type: ignore[assignment]
    _HAS_PIL = False

DEFAULT_WIDTH = 830
DEFAULT_ACCENT = "#e0a85c"


_CARD_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { background: #0e1015; }
#card {
  width: #WIDTH#px;
  background: linear-gradient(180deg, #171a22 0%, #12141a 100%);
  color: #e8e6e1;
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans SC", system-ui, sans-serif;
  padding-bottom: 6px;
}
.st-head { padding: 28px 32px 22px; border-bottom: 1px solid #ffffff14; }
.st-tagline { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
.st-forum {
  font-size: 13px; font-weight: 700; letter-spacing: .5px;
  color: #12141a; background: #ACCENT#; border-radius: 4px; padding: 3px 10px;
}
.st-kind { font-size: 12px; letter-spacing: 2px; color: #7d7a74; text-transform: uppercase; }
.st-title { font-size: 27px; font-weight: 700; line-height: 1.42; color: #f2efe9; }
.st-meta { margin-top: 12px; font-size: 13px; color: #8d8a84; }
.st-meta b { color: #ACCENT#; font-weight: 700; }
.st-floor { padding: 20px 32px; border-bottom: 1px solid #ffffff10; }
.st-fhead { display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; }
.st-fn { font-size: 13px; font-weight: 700; color: #ACCENT#; }
.st-author { font-size: 13px; color: #cfcbc2; }
.st-time { margin-left: auto; font-size: 12px; color: #75726c; }
.st-text { font-size: 15.5px; line-height: 1.8; color: #ddd9d1; white-space: pre-wrap; word-break: break-word; }
.st-empty { font-size: 14px; color: #75726c; font-style: italic; }
.st-img { display: block; width: 100%; height: auto; margin-top: 12px; border-radius: 8px; }
.st-foot {
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 32px 22px; font-size: 12px; color: #6f6c66;
}
.st-foot .st-brand { color: #ACCENT#; letter-spacing: 1px; }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _sniff_mime(raw: bytes) -> str:
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def image_data_uri(raw: bytes, *, max_width: int = 1080, quality: int = 85) -> str:
    """把图片字节转成 data URI，顺手等比缩小并压成 JPEG。

    压不动（没有 Pillow、动图、格式不认）就原样 base64。
    """
    if not raw:
        return ""
    if _HAS_PIL:
        try:
            with Image.open(io.BytesIO(raw)) as im:
                fmt = str(im.format or "").upper()
                if fmt == "GIF":
                    return "data:image/gif;base64," + base64.b64encode(raw).decode("ascii")
                work = im.convert("RGB")
                width, height = work.size
                resized = False
                if max_width and width > max_width:
                    work = work.resize(
                        (int(max_width), max(1, int(height * max_width / width))), Image.LANCZOS
                    )
                    resized = True
                buf = io.BytesIO()
                work.save(buf, format="JPEG", quality=int(quality), optimize=True)
                data = buf.getvalue()
                if not resized and len(data) >= len(raw):
                    # 压完反而更大（小图、纯色图）就留着原图
                    return "data:%s;base64,%s" % (_sniff_mime(raw), base64.b64encode(raw).decode("ascii"))
                return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[搬石/渲染] 图片压缩失败，按原图发送：%s", exc)
    return "data:%s;base64,%s" % (_sniff_mime(raw), base64.b64encode(raw).decode("ascii"))


def _floors_html(
    floors: list[Any], images: dict[str, str], *, max_images: int
) -> tuple[str, int]:
    """画楼层。返回（HTML, 用掉的图片张数）。"""
    parts: list[str] = []
    used = 0
    for floor in floors:
        text = str(getattr(floor, "text", "") or "").strip()
        picture_html: list[str] = []
        for url in list(getattr(floor, "images", None) or []):
            if used >= max_images:
                break
            uri = images.get(url)
            if not uri:
                continue
            picture_html.append('<img class="st-img" src="%s" alt="">' % uri)
            used += 1
        if not text and not picture_html:
            continue
        fn = int(getattr(floor, "fn", 0) or 0)
        author = str(getattr(floor, "author", "") or "").strip() or "匿名"
        stamp = str(getattr(floor, "time", "") or "").strip()
        head = (
            '<div class="st-fhead"><span class="st-fn">%d楼</span>'
            '<span class="st-author">%s</span>'
            '<span class="st-time">%s</span></div>' % (fn, _esc(author), _esc(stamp))
        )
        body = '<div class="st-text">%s</div>' % _esc(text) if text else ""
        parts.append('<div class="st-floor">%s%s%s</div>' % (head, body, "".join(picture_html)))
    return "".join(parts), used


def build_thread_html(
    thread: Any,
    floors: list[Any],
    *,
    images: dict[str, str] | None = None,
    width: int = DEFAULT_WIDTH,
    max_images: int = 9,
    part: int = 0,
    parts: int = 1,
    accent: str = DEFAULT_ACCENT,
) -> str:
    """把一个帖子的若干楼层渲染成卡片 HTML。

    Args:
        thread: StoneThread。
        floors: 这一段要画的楼层（StoneFloor 列表）。
        images: 原图地址到 data URI 的映射，没命中的图会被跳过。
        width: 卡片宽度（像素）。
        max_images: 整张卡片最多放几张图。
        part/parts: 分段信息，parts 大于 1 时标题后面会标「第 n/m 段」。
    """
    forum = str(getattr(thread, "forum", "") or "").strip() or "贴吧"
    title = str(getattr(thread, "title", "") or "").strip() or "（无标题）"
    replies = int(getattr(thread, "replies", 0) or 0)
    body_html, used = _floors_html(list(floors), images or {}, max_images=max_images)
    if not body_html:
        body_html = '<div class="st-floor"><div class="st-empty">这一段没有可显示的楼层</div></div>'
    suffix = ""
    if parts > 1:
        suffix = "（第 %d/%d 段）" % (int(part) + 1, int(parts))
    meta = "<b>%d</b> 回复 · 本段 <b>%d</b> 层 · <b>%d</b> 图" % (replies, len(list(floors)), used)
    css = _CARD_CSS.replace("#ACCENT#", accent)
    css = css.replace("#WIDTH#", str(int(width or DEFAULT_WIDTH)))
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<style>%s</style></head><body>"
        "<div id=\"card\">"
        "<div class=\"st-head\">"
        "<div class=\"st-tagline\"><span class=\"st-forum\">%s</span>"
        "<span class=\"st-kind\">tieba</span></div>"
        "<div class=\"st-title\">%s%s</div>"
        "<div class=\"st-meta\">%s</div>"
        "</div>"
        "%s"
        "<div class=\"st-foot\"><span class=\"st-brand\">麦麦贴吧搬石</span>"
        "<span>tid %s</span></div>"
        "</div></body></html>"
    ) % (
        css,
        _esc(forum),
        _esc(title),
        _esc(suffix),
        meta,
        body_html,
        _esc(str(getattr(thread, "tid", "") or "")),
    )


def build_thread_text(
    thread: Any, floors: list[Any], *, max_images: int = 9, with_images: bool = True
) -> str:
    """纯文本回退：渲染失败或关掉卡片时用这个发。"""
    forum = str(getattr(thread, "forum", "") or "").strip()
    title = str(getattr(thread, "title", "") or "").strip() or "（无标题）"
    lines = ["【%s】%s" % (forum, title) if forum else "【%s】" % title]
    used = 0
    for floor in list(floors):
        text = str(getattr(floor, "text", "") or "").strip()
        if not text:
            continue
        fn = int(getattr(floor, "fn", 0) or 0)
        author = str(getattr(floor, "author", "") or "").strip() or "匿名"
        lines.append("%d楼 %s：%s" % (fn, author, text))
        if with_images:
            pics = len(list(getattr(floor, "images", None) or []))
            if pics and used < max_images:
                take = min(pics, max_images - used)
                used += take
                lines.append("（本层 %d 张图）" % take)
    return "\n".join(lines)
