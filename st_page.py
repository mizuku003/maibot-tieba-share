"""麦麦贴吧搬石 —— 把贴吧帖子页原样搬成一张长图。

思路：不自己排版，直接取移动版帖子页的 HTML，去掉脚本、补上必需的样式，把正文插图换成
我们自己下好的 base64 图，再交给宿主的渲染服务整页截图 —— 出来就是贴吧手机版的样子。

两个必须知道的前提：

1. 宿主渲染器默认不让页面联网。样式表、图标都在 CDN 上，所以调用方要显式开 allow_network。
2. 帖子页默认只展开前两层，第 3 层起带 class_hide_flag（display:none）。这里注入 CSS 打开，
   并按 max_floors 把多余的楼层收掉。

这一层只做字符串处理，不联网、不依赖宿主，方便单独测。
"""

from __future__ import annotations

import re

from st_tieba import images_of

TIEBA_ORIGIN = "https://tieba.baidu.com"
MOBILE_PATH = "/mo/q/m"
BASE_HREF = TIEBA_ORIGIN + MOBILE_PATH

# 手机版页面的宽度，截图出来的排版才和贴吧 App 里看到的一致
PAGE_WIDTH = 414

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.I | re.S)
_SCRIPT_OPEN_RE = re.compile(r"<script\b[^>]*>", re.I | re.S)
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.I)
_IMG_SRC_IN_TAG_RE = re.compile(
    r"""(?:data-original|data-src|src)=["'](?P<u>[^"']+)["']""", re.I | re.S
)
# 找「可能要补图的标签」：占位 div 和真 img，一次扫完
_IMAGE_SPOT_RE = re.compile(r"<div\b[^>]*>|<img\b[^>]*>", re.I | re.S)
# 首楼多图的拼贴块：服务端只渲染 3 个格子，其余图在 contentTotal 里
_PB_IMGS_RE = re.compile(r"""<div[^>]*\bid=["']pb_imgs["'][^>]*>.*?</div>\s*</div>""", re.I | re.S)
_PROTOCOL_RE = re.compile(r"""(?P<attr>\b(?:href|src|data-src|data-original)=)(?P<q>["'])//""", re.I)

# 图片占位一律认「标签里有没有 data-url」，判据统一交给 st_tieba.images_of：
#   正文插图 j_media_thumb_holder、单图首楼 pb_img_item、多图首楼 pb_imgs_top /
#   pb_imgs_bottom_left / pb_imgs_bottom_right 都带 data-url；容器 div（pb_imgs、
#   pb_imgs_div）不带，所以不会把容器当成图片。多认了 data-url 就自动覆盖上面三种。

# 页面样式：两个 %d 是页面宽度
_CSS_COMMON = """
html,body{margin:0!important;padding:0!important;background:#f2f3f5!important;}
body{width:%dpx!important;max-width:%dpx!important;overflow-x:hidden!important;}
/* 顶栏是 fixed 的，去掉以后要把外层的上边距一起收掉，不然最上面留一条白带；
   #main 还给顶栏留了 45px 的 margin-top，不一起清掉就会在标题上方空出一块 */
#glob{padding-top:0!important;margin-top:0!important;}
#main{margin-top:0!important;padding-top:0!important;}
/* 帖子页默认把第 3 层往后折叠起来，这里全部展开 */
li.list_item.post_list_item,.class_hide_flag{display:block!important;}
/* 只留帖子本身：去顶栏、去 App 引导、去广告、去页脚、去下载弹窗、去楼中楼「打开 App 看评论」按钮。
   注意页脚容器的类名是 pb-footer（连字符），不是 pb_footer，写错就整块留在图里。
   楼中楼那块有 normal / from-toutiao 两份，一起收掉。 */
#i_head,.pb_mask,.wake_app_tip,.app_wakeup_component,.bdps_login_ad,.liuliang_pop,
.pb_transfer_app,.j_pb_footer,.pb-footer,.j_footer,.footer_logo,
.footer_link,.footer_read,.footer_read_link,.j_footer_link,
.pb_prompt_background,.pb_prompt_content,.hongbao_close,.hongbao_fire,
.father-cut-daoliu-normal-box,.father-cut-daoliu-from-toutiao-box,
.father-cut-recommend-normal-box,.father-cut-recommend-from-toutiao-box,
.pb_cut_hot_recommend,.operate_statistic_wrapper,.more_newspinner,.pb_new_popup,
.post_star,.pb_newshare,.mini_program,.pb_send_bar,.recommend,.d_post_recommend,
.pb_ads,[class*="ad_"],#diversBanner,.list_item_more_operation,.poster_only,
.img_desc,.pb_imgs_weixin{display:none!important;}
/* 正文插图：补到的铺满宽度，没补到的整块收掉，别在图上留一片灰框 */
.st-page-img-wrap{width:100%%!important;height:auto!important;margin:6px 0!important;}
.st-page-img{display:block!important;width:100%%!important;height:auto!important;}
.st-page-miss,.st-page-miss-img{display:none!important;}
/* 首楼多图的拼贴块在贴吧自己的 CSS 里是 display:none，靠 pb_imgs 那个 JS 组件
   在运行时放开。我们剥掉了脚本，所以它永远不显示 —— 首楼多图会一张都看不到。
   这里直接放开，并把三个格子改成竖排（拼贴在手机宽度下本来也是三张挤一起）。
   另外贴吧给 .pb_imgs img 写了 position:absolute + translate 居中，格子自身又是
   float，撑不出高度；下面必须把图片改回文档流，否则整个拼贴块高度是 0。 */
#pb_imgs{display:block!important;height:auto!important;overflow:visible!important;}
#pb_imgs_div{height:auto!important;overflow:visible!important;}
#pb_imgs_div::after{content:"";display:block;clear:both;}
.pb_imgs_top,.pb_imgs_bottom_left,.pb_imgs_bottom_right{
  width:100%%!important;height:auto!important;float:none!important;margin:0!important;}
#pb_imgs img,.pb_imgs img{
  position:static!important;transform:none!important;top:auto!important;left:auto!important;
  width:100%%!important;height:auto!important;}
.st-page-img-extra{clear:both!important;}
.st-page-img-extra .st-page-img-wrap:first-child{margin-top:0!important;}
"""


def _page_css(max_floors: int) -> str:
    """拼出注入用的 CSS。max_floors 大于 0 时只留前几层。"""
    css = _CSS_COMMON % (PAGE_WIDTH, PAGE_WIDTH)
    keep = int(max_floors or 0)
    if keep > 0:
        # 楼层都是同一个 ul 下的兄弟节点，用 nth-child 裁最省事
        css += "ul.father_cut_list_class>li:nth-child(n+%d){display:none!important;}" % (keep + 1)
    return css


def placeholder_urls(raw: str) -> list[tuple[str, str]]:
    """列出页面里所有要补图的图片，按出现顺序去重。

    图片在页面里有两种装法，两边都得收：

    1. ``<div data-url=...>`` 占位（正文插图 j_media_thumb_holder、单图首楼
       pb_img_item、多图首楼 pb_imgs_top / pb_imgs_bottom_left / pb_imgs_bottom_right）。
       容器 div（pb_imgs、pb_imgs_div）不带 data-url，交给 images_of 放过即可。
    2. 真 ``<img>`` 标签（部分楼层直接给 img，不是占位 div）。

    以前只扫 div，第二种就永远不会被预下成 base64 —— 长图里留一片灰框。
    这里和 images_of 的口径必须一致，否则「替换时认得的」和「下载时认得的」对不上。

    Returns:
        [(钥匙, 原图直链)]。钥匙是 data-url 或 img 的 src，既当下载兜底键，
        也当后面替换时的查找键。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _IMAGE_SPOT_RE.finditer(raw or ""):
        tag = match.group(0)
        origin, thumbs = images_of(tag)
        if not thumbs:
            continue
        signed = thumbs[0]
        if signed in seen:
            continue
        seen.add(signed)
        out.append((signed, origin[0] if origin else signed))
    return out


def _lookup(tag: str, pool: dict[str, str]) -> tuple[str, str]:
    """返回 (命中的池键, data URI)；没命中就是 ("", "")。"""
    origin, thumbs = images_of(tag)
    if not thumbs:
        return "", ""
    for key in list(thumbs) + list(origin):
        uri = pool.get(key, "")
        if uri:
            return key, uri
    return "", ""


def _lookup_uri(tag: str, pool: dict[str, str]) -> str:
    """在补图池里找这个标签对应的 data URI，找不到返回空串。"""
    return _lookup(tag, pool)[1]


def _lookup_key(tag: str, pool: dict[str, str]) -> str:
    """在补图池里找这个标签命中的那个键（用来判断哪些属性该换）。"""
    return _lookup(tag, pool)[0]


def _swap_placeholder(tag: str, pool: dict[str, str]) -> str:
    """把一个图片占位 div 换成我们下好的图；没下到就换成待隐藏的占位。

    只换开标签：占位本身是空 div，原来的 </div> 正好收掉这里新开的 div。
    解析不出图片的标签原样放回去（容器 div、非图片的 data-url 都在这里被放过）。
    """
    if not images_of(tag)[1]:
        return tag
    uri = _lookup_uri(tag, pool)
    if not uri:
        return '<div class="st-page-miss">'
    return '<div class="st-page-img-wrap"><img class="st-page-img" src="%s">' % uri


def _swap_img_tag(tag: str, pool: dict[str, str]) -> str:
    """把真 ``<img>`` 的 src 换成下好的图。

    这里**不能**照搬 _swap_placeholder 换成 div —— img 是空元素、没有闭合标签，
    换成 div 会让整份文档的标签配平垮掉（渲染出来就是一片错位）。
    图没下到就整块收掉（加 style 隐藏），同样不留下灰框。
    """
    if not images_of(tag)[1]:
        return tag
    uri = _lookup_uri(tag, pool)
    if not uri:
        return '<img class="st-page-miss-img" style="display:none!important">'
    # 懒加载的 img 常常同时挂着 data-src（真图）和 src（占位/loading.gif），
    # 浏览器最后看的是 src —— 只换一个的话长图里还是裂图。所以凡是值指向本次
    # 这张图的属性命中全换掉（count=0），其余属性（class、宽高等）原样留着。
    target = _lookup_key(tag, pool)
    return _IMG_SRC_IN_TAG_RE.sub(
        lambda m: m.group(0) if m.group("u") != target else m.group(0).replace(
            m.group("u"), uri, 1
        ),
        tag,
    )


def _expand_first_floor_images(html: str, images: dict[str, str]) -> str:
    """把首楼多图的「9图」拼贴块展开成全部图。

    贴吧移动版对首楼多图只服务端渲染 3 个格子（pb_imgs_top / _bottom_left /
    _bottom_right），外加一个写着「9图」的角标，其余 6 张**只在内联 JS 的
    contentTotal 里**，DOM 里根本没有对应标签 —— 光靠替换占位（_swap_placeholder）
    补不出来，长图里那层就只有 3 张。

    这里在拼贴块**后面**补一个补图区，把池子里还没出现在拼贴块里的图按顺序摆上，
    并把角标上的「N图」改成真实张数。拼贴块本身保持原样（它还原贴吧观感）。
    """
    if not images:
        return html
    block = _PB_IMGS_RE.search(html)
    if not block:
        return html
    # 拼贴块里的格子此刻已经换成了 data URI —— 只能按「src 的值」反查它对应池子里的
    # 哪个键。绝不能拿值去重（两张不同的图理论上可能有同样的 URI，测试桩里更是
    # 全都一样），那会把该补的图当成「已经在了」而整块不补。
    by_uri: dict[str, list[str]] = {}
    for key, uri in images.items():
        by_uri.setdefault(uri, []).append(key)
    shown_keys: set[str] = set()
    for m in _IMG_SRC_IN_TAG_RE.finditer(block.group(0)):
        uri = m.group("u")
        if not uri.startswith("data:"):
            continue
        for key in by_uri.get(uri, ()):  # 同一个 URI 可能对应多个键，全部算已出现
            shown_keys.add(key)
    if not shown_keys:
        return html
    # 池子是 dict、保序，顺序就是页面出现顺序；已经出现在拼贴块里的不再补
    extra = [uri for key, uri in images.items() if key not in shown_keys]
    if not extra:
        return html
    add = "".join(
        '<div class="st-page-img-wrap"><img class="st-page-img" src="%s"></div>' % uri
        for uri in extra
    )
    insert = '<div class="st-page-img-extra">%s</div>' % add
    at = block.end()
    return html[:at] + insert + html[at:]


def build_page_html(
    raw: str, *, images: dict[str, str] | None = None, max_floors: int = 0
) -> str:
    """把帖子页 HTML 收拾成可以直接交给渲染服务的整页。

    Args:
        raw: 帖子页原始 HTML。
        images: {data-url 或原图直链: data URI}，用来替换正文插图占位。
        max_floors: 最多留几层，0 表示不裁。
    """
    html = str(raw or "")
    if not html:
        return ""
    # 脚本一去，页面只剩静态结构，截图时不会有弹窗和二次跳转
    html = _SCRIPT_RE.sub("", html)
    html = _SCRIPT_OPEN_RE.sub("", html)
    # 协议相对地址（//tb1.bdstatic.com/...）在本地文档里没法解析，补成 https
    html = _PROTOCOL_RE.sub(lambda m: m.group("attr") + m.group("q") + "https://", html)
    head = _HEAD_OPEN_RE.search(html)
    inject = '<base href="%s"><style>%s</style>' % (BASE_HREF, _page_css(max_floors))
    if head:
        html = html[: head.end()] + inject + html[head.end() :]
    else:
        html = inject + html
    pool = {str(k): str(v) for k, v in (images or {}).items() if v}

    def _sub(m: "re.Match[str]") -> str:
        tag = m.group(0)
        if tag[:4].lower() == "<img":
            return _swap_img_tag(tag, pool)
        return _swap_placeholder(tag, pool)

    html = _IMAGE_SPOT_RE.sub(_sub, html)
    # 补图要在替换之后做：那时拼贴块里的 3 张已经换成 data URI，才认得出
    # 「哪几张已经在了」，剩下的才是要补的。
    return _expand_first_floor_images(html, pool)
