"""麦麦贴吧搬石 —— 配置模型。

面板上的文字约定：节的说明来自类的 docstring，字段下面那行小灰字来自 hint ——
两者都只写一句话，细节留 README；字段的 description 不进面板，保持一句话即可。
"""

from __future__ import annotations

from typing import Literal

from maibot_sdk import Field, PluginConfigBase

# 面板上可以勾的内置吧：都是贴吧的通用大吧。
# 注意：贴吧的 kw 参数要的是吧名本身，很多吧名带上末尾「吧」字反而查不到，所以这里都不带。
PRESET_FORUMS: tuple[str, ...] = (
    "搬石",
    "小黑盒笑话",
    "弱智",
    "地狱笑话",
    "dinner笑话",
    "孙笑川",
    "核战避难所",
    "2ch",
    "复制粘贴",
    "反二次元",
    "冷笑话",
    "幽默老中",
)



class PluginSection(PluginConfigBase):
    """插件总开关。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=False,
        description="是否启用插件（安装后默认关闭）",
        json_schema_extra={
            "label": "启用插件",
            "hint": "关掉后不再响应任何命令",
        },
    )
    config_version: str = Field(
        default="1.0.2",
        description="配置版本号，请勿手动修改",
        json_schema_extra={"label": "配置版本", "hidden": True},
    )


class StoneSection(PluginConfigBase):
    """搬哪些吧、每轮搬几条。"""

    __ui_label__ = "搬石"
    __ui_icon__ = "hammer"
    __ui_order__ = 1

    preset_forums: list[
        Literal[
            "搬石",
            "小黑盒笑话",
            "弱智",
            "地狱笑话",
            "dinner笑话",
            "孙笑川",
            "核战避难所",
            "2ch",
            "复制粘贴",
            "反二次元",
            "冷笑话",
            "幽默老中",
        ]
    ] = Field(
        default_factory=lambda: ["搬石"],
        description="内置吧，可勾选多个",
        json_schema_extra={
            "label": "内置吧（可多选）",
            "hint": "内置的常用吧，可勾选多个",
            "x-widget": "select",
        },
    )
    forums: list[str] = Field(
        default_factory=list,
        description="自己填的吧（一行一个）",
        json_schema_extra={
            "label": "自己加的吧",
            "hint": "一行一个吧名；末尾带不带「吧」字均可",
        },
    )
    count_per_forum: int = Field(
        default=3,
        ge=1,
        le=20,
        description="每轮从每个吧搬几条新帖",
        json_schema_extra={
            "label": "每个吧搬几条",
            "hint": "每个吧取最后回复最新的几条",
        },
    )
    min_replies: int = Field(
        default=0,
        ge=0,
        le=1000,
        description="只搬回复数不少于多少的帖子",
        json_schema_extra={"label": "最少回复数", "hint": "回复数低于此值的帖子不搬；0 = 不限"},
    )
    skip_top: bool = Field(
        default=True,
        description="跳过置顶帖",
        json_schema_extra={"label": "跳过置顶帖", "hint": "不搬吧内置顶帖"},
    )
    block_keywords: list[str] = Field(
        default_factory=list,
        description="标题里含这些词就不搬（一行一个）",
        json_schema_extra={"label": "标题屏蔽词", "hint": "标题含这些词的帖子不搬；留空 = 不屏蔽"},
    )
    keep_records: int = Field(
        default=500,
        ge=50,
        le=5000,
        description="每个吧保留多少条搬运记录用于去重",
        json_schema_extra={"label": "去重记录条数", "hint": "每个吧保留的去重记录条数；超出后丢弃最早的记录"},
    )


def resolve_forums(section: StoneSection) -> list[str]:
    """把面板上勾的内置吧和自己填的吧合成一份：去空、去重、保持顺序。"""

    merged: list[str] = []
    seen: set[str] = set()
    chosen = list(getattr(section, "preset_forums", None) or [])
    chosen += list(getattr(section, "forums", None) or [])
    for raw in chosen:
        name = str(raw or "").strip()
        if not name:
            continue
        # 「搬石吧」和「搬石」指向同一个吧，只留先出现的那个（内置吧排在前面）
        key = name[:-1] if len(name) > 1 and name.endswith("吧") else name
        if key in seen:
            continue
        seen.add(key)
        merged.append(name)
    return merged


class ScheduleSection(PluginConfigBase):
    """自动搬石：每隔一段时间，或每天定点搬一轮。"""

    __ui_label__ = "定时"
    __ui_icon__ = "clock"
    __ui_order__ = 2

    auto_enabled: bool = Field(
        default=False,
        description="是否按下面的时间自动搬石",
        json_schema_extra={
            "label": "自动搬石",
            "hint": "开启后按下面的时间自动搬运",
        },
    )
    interval_minutes: int = Field(
        default=120,
        ge=10,
        le=1440,
        description="每隔多少分钟搬一次",
        json_schema_extra={
            "label": "隔多少分钟搬一次",
            "hint": "两次自动搬运的间隔分钟数；填了「每天定点」后本项失效",
        },
    )
    base_time: str = Field(
        default="0:00",
        description="间隔计时的起点",
        json_schema_extra={
            "label": "基准时刻",
            "hint": "间隔从这一刻往后数，如 0:00 + 120 分钟 = 0、2、4…22 点",
        },
    )
    lead_seconds: int = Field(
        default=30,
        ge=0,
        le=3600,
        description="提前多少秒开始准备",
        json_schema_extra={
            "label": "提前开始（秒）",
            "hint": "提前多少秒开始取帖，到点准时发出",
        },
    )
    daily_times: list[str] = Field(
        default_factory=list,
        description="每天几点搬（24 小时制，一行一个）",
        json_schema_extra={
            "label": "每天定点",
            "hint": "每天几点搬，如 09:30、21:00，一行一个",
        },
    )


class SendSection(PluginConfigBase):
    """搬出来的帖子在群里长什么样。"""

    __ui_label__ = "发送"
    __ui_icon__ = "send"
    __ui_order__ = 3

    send_media: bool = Field(
        default=True,
        description="用文本加图片的形式发",
        json_schema_extra={"label": "发文本 + 图片", "hint": "逐层发文字和插图"},
    )
    send_card: bool = Field(
        default=False,
        description="把整帖渲染成一张长图发",
        json_schema_extra={"label": "发一张长图", "hint": "整帖截成一张长图"},
    )
    card_style: Literal["自绘卡片", "贴吧原页面"] = Field(
        default="贴吧原页面",
        description="长图用哪种样式",
        json_schema_extra={
            "label": "长图样式",
            "hint": "贴吧原页面 = 按手机版页面截图；自绘卡片 = 插件自己排版",
            "x-widget": "select",
        },
    )
    forward: bool = Field(
        default=False,
        description="用合并转发打包成聊天记录",
        json_schema_extra={"label": "打包成聊天记录", "hint": "把本轮内容合并成一条聊天记录"},
    )
    forward_name: str = Field(
        default="？！石！？",
        description="聊天记录里显示的名字",
        json_schema_extra={
            "label": "聊天记录名字",
            "hint": "聊天记录里显示的发件人名字",
        },
    )
    send_video: bool = Field(
        default=True,
        description="帖子里的视频直接发出来",
        json_schema_extra={
            "label": "发视频",
            "hint": "视频帖连同视频一起发",
        },
    )
    video_max_mb: int = Field(
        default=30,
        ge=0,
        le=500,
        description="视频文件大小上限（MB）",
        json_schema_extra={
            "label": "视频大小上限",
            "hint": "单位 MB；超过后跳过该视频；0 = 不限",
        },
    )
    post_link: bool = Field(
        default=True,
        description="每条帖子带上原帖链接",
        json_schema_extra={
            "label": "带原帖链接",
            "hint": "是否在内容里附带原帖地址",
        },
    )
    thread_filter: Literal["不限", "只发图文帖", "只发视频帖"] = Field(
        default="不限",
        description="这一轮只搬哪种帖子",
        json_schema_extra={
            "label": "只搬哪种帖",
            "hint": "按列表页的类型标记筛",
            "x-widget": "select",
        },
    )
    max_images: int = Field(
        default=9,
        ge=0,
        le=30,
        description="每条帖子最多发几张图",
        json_schema_extra={"label": "每条最多几张图", "hint": "只影响「发文本 + 图片」；0 = 只发文字"},
    )
    image_max_width: int = Field(
        default=1080,
        ge=320,
        le=2560,
        description="图片超过这个宽度就等比缩小",
        json_schema_extra={"label": "图片最大宽度", "hint": "超过后按比例缩小"},
    )
    image_quality: int = Field(
        default=85,
        ge=40,
        le=100,
        description="图片压缩质量（JPEG）",
        json_schema_extra={"label": "图片压缩质量", "hint": "建议 80~90；数值越高越清晰也越占空间"},
    )
    card_width: int = Field(
        default=830,
        ge=600,
        le=1200,
        description="长图宽度（像素）",
        json_schema_extra={"label": "长图宽度", "hint": "只对「自绘卡片」有效"},
    )
    include_groups: list[str] = Field(
        default_factory=list,
        description="只往这些群发（填群号，一行一个）",
        json_schema_extra={"label": "只发这些群", "hint": "只往这些群发，填群号；留空 = 不限"},
    )
    exclude_groups: list[str] = Field(
        default_factory=list,
        description="这些群不发（填群号，一行一个）",
        json_schema_extra={"label": "这些群不发", "hint": "这些群不发，优先于上面的名单"},
    )


class FetchSection(PluginConfigBase):
    """抓取细节，默认值一般不用动。"""

    __ui_label__ = "抓取"
    __ui_icon__ = "sliders-horizontal"
    __ui_order__ = 5

    max_pages: int = Field(
        default=1,
        ge=1,
        le=5,
        description="列表翻几页",
        json_schema_extra={"label": "列表翻几页", "hint": "列表翻页数，每页 30 条"},
    )
    max_floors: int = Field(
        default=30,
        ge=1,
        le=60,
        description="每条帖子最多搬几层",
        json_schema_extra={"label": "每帖最多几层", "hint": "每帖最多取几层；免登录只给首页 30 层"},
    )
    max_chars: int = Field(
        default=800,
        ge=0,
        le=5000,
        description="单层正文最多留几个字",
        json_schema_extra={"label": "单层最多留几个字", "hint": "单层文字最多留几个字；0 = 不截断"},
    )
    delay_seconds: float = Field(
        default=3.0,
        ge=0.0,
        le=30.0,
        description="两次请求之间歇多少秒",
        json_schema_extra={"label": "请求间隔（秒）", "hint": "两次请求之间的间隔秒数；请勿设为 0"},
    )
    timeout: int = Field(
        default=30,
        ge=5,
        le=120,
        description="单次 HTTP 请求超时（秒）",
        json_schema_extra={"label": "请求超时（秒）", "hint": "单次请求超时秒数；网络较慢时调大"},
    )
    proxy: str = Field(
        default="",
        description="访问贴吧时用的 HTTP 代理地址",
        json_schema_extra={"label": "（高级）代理", "hint": "留空为直连；例如 http://127.0.0.1:7890"},
    )
    user_agent: str = Field(
        default="",
        description="自定义 User-Agent",
        json_schema_extra={"label": "（高级）User-Agent", "hint": "留空时使用内置手机 UA"},
    )


class AdminSection(PluginConfigBase):
    """谁能用维护命令。"""

    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 6

    admins: list[str] = Field(
        default_factory=list,
        description="管理员的 QQ 号（一行一个）",
        json_schema_extra={
            "label": "管理员",
            "hint": "能用维护命令的 QQ 号，一行一个",
        },
    )
    allow_groups: list[str] = Field(
        default_factory=list,
        description="允许用维护命令的群（一行一个群号）",
        json_schema_extra={
            "label": "管理员群",
            "hint": "这些群里谁都能用维护命令；一行一个群号",
        },
    )


class ToolSection(PluginConfigBase):
    """麦麦自己搬石时用的参数，与「发送」互不影响。"""

    __ui_label__ = "工具"
    __ui_icon__ = "wrench"
    __ui_order__ = 4

    enabled: bool = Field(
        default=False,
        description="是否让麦麦能自己搬石",
        json_schema_extra={
            "label": "让麦麦自己搬",
            "hint": "开启后麦麦可在聊天里自己搬石",
        },
    )
    forums: list[str] = Field(
        default_factory=list,
        description="限定麦麦只能搬这些吧（一行一个）",
        json_schema_extra={
            "label": "限定吧",
            "hint": "限定麦麦只能搬这些吧；留空 = 不限",
        },
    )
    max_count: int = Field(
        default=5,
        ge=1,
        le=10,
        description="麦麦一次最多搬几条",
        json_schema_extra={
            "label": "一次最多几条",
            "hint": "麦麦一次最多搬几条",
        },
    )
    send_media: bool = Field(
        default=False,
        description="工具搬石时发文本加图片",
        json_schema_extra={"label": "发文本 + 图片", "hint": "逐层发文字和插图"},
    )
    send_card: bool = Field(
        default=True,
        description="工具搬石时发一张长图",
        json_schema_extra={"label": "发一张长图", "hint": "整帖截成一张长图"},
    )
    post_link: bool = Field(
        default=False,
        description="工具搬石时带原帖链接",
        json_schema_extra={"label": "带原帖链接", "hint": "是否在内容里附带原帖地址"},
    )
    max_images: int = Field(
        default=9,
        ge=0,
        le=30,
        description="工具搬石时每条最多几张图",
        json_schema_extra={"label": "每条最多几张图", "hint": "只影响「发文本 + 图片」；0 = 只发文字"},
    )
    thread_filter: Literal["不限", "只发图文帖", "只发视频帖"] = Field(
        default="不限",
        description="工具搬石时只搬哪种帖",
        json_schema_extra={
            "label": "只搬哪种帖",
            "hint": "麦麦指定要视频帖时以此为准",
            "x-widget": "select",
        },
    )


class StoneConfig(PluginConfigBase):
    """麦麦贴吧搬石 —— 顶层配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    stone: StoneSection = Field(default_factory=StoneSection)
    admin: AdminSection = Field(default_factory=AdminSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    send: SendSection = Field(default_factory=SendSection)
    tool: ToolSection = Field(default_factory=ToolSection)
    fetch: FetchSection = Field(default_factory=FetchSection)
