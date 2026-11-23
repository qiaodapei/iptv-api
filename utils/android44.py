"""Android 4.4 电视出口。

给只有系统 MediaPlayer 的老电视（实测机型：康佳 LED49K70U / Realtek RTD2995D /
Android 4.4.2 / API 19 / 1GB RAM）单独导出一份 m3u。它与通用输出共用同一批
测速结果，但多做了四层过滤，因为这台机器的限制是硬的：

1. **只认 H.264。** RTD2995D 硬解 H.264，HEVC/H.265 一播就黑屏。iptv-api 采集了
   ``video_codec`` 却从不用于筛选（判断质量的 ``channel_result_rejection`` 里没有
   任何编码判断），所以 HEVC 源只要码率大，在默认的 ``sort_by=speed`` 下反而排最前。
   这里按编码白名单剔除。

2. **不认 ``#EXTVLCOPT``。** 应用侧的 ``M3uParser`` 明确忽略该行（源码注释里写着
   "recognised and ignored on purpose"），所以带自定义请求头的源会变成「条目在、
   一播就 403/404」的死条目。凡 ``headers`` 非空的源直接剔除。

3. **面板只有 1080p。** 高于 1920x1080 的源白占带宽，还会因为解码压力更大而更容易
   卡顿，因此设上限。

4. **解析器有体积与条数硬顶。** ``MAX_BYTES = 2MB``、``MAX_CHANNELS = 2000``，
   超了整份列表直接解析失败。这里留足余量做裁剪。

除了过滤，排序也不同于通用输出：电视上选台是按分组左右切的，所以同一频道先按清晰度
再按分数取靠前的线路，避免出现「65 分的 240p」排在「63 分的 1080p」前面、一点开就是
糊的画面（这正是不带这条规则时的实际现象）。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from typing import Iterable

# --- 电视端硬限制，须与 LivingRoomTV 的 M3uParser 常量保持一致 ----------------
TV_MAX_BYTES = 2 * 1024 * 1024
TV_MAX_CHANNELS = 2000
# 留出余量：分组标题行、#EXTINF 属性、以及将来加台标等都会吃字节。
TV_BYTE_BUDGET = 1_900_000
TV_CHANNEL_BUDGET = 1500
# 每频道最多几条备用线路。电视上一次只播一条，备线过多只会撑大文件。
TV_LINES_PER_CHANNEL = 2

# --- 编码白名单 --------------------------------------------------------------
# ffprobe 的 codec_name 取值是 h264 / hevc / mpeg2video 这类小写下划线形式，
# 但第三方源的 m3u8 CODECS 属性可能写成 avc1.64001f、H264、h.264，所以统一
# 去掉分隔符再比前缀。
_H264_TOKENS = ("h264", "avc", "x264")
_HEVC_TOKENS = ("hevc", "h265", "hvc1", "hev1")
# 以下编码即使不是 H.264 也可能被某些固件软解，但实测 RTD2995D 不稳，不放行。
_UNSAFE_TOKENS = ("mpeg4", "vc1", "vp8", "vp9", "av1", "mpeg2", "theora")

# --- 分辨率上限 --------------------------------------------------------------
MAX_TV_WIDTH = 1920
MAX_TV_HEIGHT = 1080

# --- 分组：与 iptv-scan/config/groups.yaml 的六类方案一致 ---------------------
# 顺序即电视上的显示顺序。用户方案里没有「其他」分组，认不出来的一律进网络直播。
CATEGORY_ORDER = (
    "央视频道",
    "卫视频道",
    "地方频道",
    "少儿频道",
    "电影频道",
    "网络直播",
)
FALLBACK_CATEGORY = "网络直播"
# 海外频道不在用户点名的六类里，但 iptv-api 拉的是全球源，把 Arirang、CNN 这类
# 硬塞进「地方频道」比多一个尾部小组更糟。与 iptv-scan 同口径：只有真的存在
# 海外频道时才追加到列表末尾。
OVERSEAS_CATEGORY = "海外频道"

_CENTRAL = ("央视", "cctv", "央卫", "cgtn")
_SATELLITE = ("卫视",)
_KIDS = (
    "少儿", "儿童", "幼儿", "kid", "动画", "animation", "cartoon", "卡通",
    "family", "亲子", "动漫", "哈哈炫动", "金鹰卡通", "优漫卡通", "嘉佳卡通",
    "炫动", "卡酷",
)
_MOVIE = (
    "电影", "movie", "影视", "点播", "1905", "剧场", "电视剧", "series",
    "影院", "大片", "春晚",
)
_LOCAL = (
    "地方", "城市", "都市", "本地",
    "北京", "上海", "天津", "重庆",
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
    "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州",
    "云南", "陕西", "甘肃", "青海", "内蒙古", "广西", "西藏", "宁夏", "新疆",
    "石家庄", "太原", "沈阳", "长春", "哈尔滨", "南京", "杭州", "合肥", "福州",
    "南昌", "济南", "郑州", "武汉", "长沙", "广州", "海口", "成都", "贵阳",
    "昆明", "西安", "兰州", "西宁", "呼和浩特", "南宁", "拉萨", "银川",
    "乌鲁木齐", "苏州", "无锡", "常州", "徐州", "宁波", "温州", "绍兴", "金华",
    "台州", "深圳", "珠海", "佛山", "东莞", "中山", "惠州", "汕头", "湛江",
    "青岛", "烟台", "潍坊", "临沂", "洛阳", "襄阳", "宜昌", "株洲", "岳阳",
    "常德", "新闻", "news", "资讯", "公共", "综合", "general", "经济", "财经",
    "business", "生活", "lifestyle", "美食", "cooking", "旅游", "travel",
    "购物", "shop", "体育", "sport", "足球", "篮球", "台球", "高尔夫", "网球",
    "搏击", "音乐", "music", "演唱会", "mv", "纪录", "documentary", "纪实",
    "探索", "discovery", "综艺", "娱乐", "entertainment", "comedy", "喜剧",
    "经典", "教育", "education", "科学", "science", "文化", "culture", "法治",
    "科教", "宗教", "religious", "gospel", "福音",
)
_NETLIVE = (
    "直播平台", "网络直播", "虎牙", "斗鱼", "哔哩", "bilibili", "抖音", "快手",
    "直播", "live", "aptv", "埋堆堆", "网络", "在线",
)
_OVERSEAS = (
    "海外", "国际频道", "overseas", "international", "foreign",
)
# 港澳台在六类方案里并入地方频道（iptv-scan 同口径）。
_GREATER_CHINA = ("港澳台", "港澳", "台湾", "香港", "澳门")

# 匹配用的规则表：顺序即优先级，窄品类在前，宽品类在后。
_RULES = (
    ("央视频道", _CENTRAL),
    ("卫视频道", _SATELLITE),
    ("少儿频道", _KIDS),
    ("电影频道", _MOVIE),
    ("地方频道", _LOCAL),
    ("海外频道", _OVERSEAS),
    ("网络直播", _NETLIVE),
)

# 源给的分类标签常带 emoji 前缀（☘️北京频道、📺央视频道），先剥掉再匹配。
_LABEL_NOISE = re.compile(r"[^\w]+", re.UNICODE)


def _ascii_fold(text: str) -> str:
    """把小写的编码名压成可比对的形态：去掉 ``.`` ``_`` ``-`` 与空格。"""
    if not isinstance(text, str):
        return ""
    return re.sub(r"[._\-\s]", "", text.strip().lower())


def classify_codec(raw: str | None) -> str | None:
    """把编码名归类成 ``"h264"`` / ``"unsafe"`` / ``None``。

    ``None`` 表示信息缺失——调用方需要自己决定是放行还是拒绝。这里不做决定，
    因为「采集不到编码」和「编码不安全」是两件事，混在一起会让日志失去意义。
    """
    folded = _ascii_fold(raw if isinstance(raw, str) else "")
    if not folded:
        return None
    if any(folded.startswith(token) for token in _H264_TOKENS):
        return "h264"
    if any(folded.startswith(token) for token in _HEVC_TOKENS):
        return "unsafe"
    if any(token in folded for token in _UNSAFE_TOKENS):
        return "unsafe"
    # 认不出的编码名（例如 "unknown"、"copy"）按不安全处理：电视黑屏的代价
    # 比漏掉一个源大得多。
    return "unsafe"


def parse_resolution(raw) -> tuple[int, int] | None:
    """解析 ``"1920x1080"`` / ``"1280X720"`` / ``"1920*1080"`` / ``"1080p"`` / ``"1080i"``。

    非字符串一律当作「分辨率未知」：上游数据里这个字段可能是 dict 或数字
    （不同源、不同版本写法不一），在这里抛异常会连带整份列表写不出去。

    简写形式（``1080p``/``720i``）必须支持：iptv-scan 的 ``quality`` 字段就是这种
    写法，只认 ``宽x高`` 会让上界过滤对整批源失效，4K 源直接混进 1080p 面板的列表。
    只有高度时按 16:9 推宽度，只为满足返回值形状，上界判定用高度。
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    match = re.fullmatch(r"(\d+)\s*[xX*]\s*(\d+)", text)
    if match:
        width, height = int(match.group(1)), int(match.group(2))
        return (width, height) if width > 0 and height > 0 else None
    short = re.fullmatch(r"(\d+)\s*[pPiI]", text)
    if short:
        height = int(short.group(1))
        return (height * 16 // 9, height) if height > 0 else None
    return None


def quality_rank(raw: str | None) -> int:
    """清晰度档位：0 高清 / 1 标清 / 2 低清或未知。

    电视上「第一条线路」就是默认播放的那条，所以档位必须参与排序。未知按最低档，
    与 iptv-scan 的 ``quality_tier`` 保持同一口径：评分可以公平，选台必须务实。
    """
    size = parse_resolution(raw)
    if size is None:
        return 2
    height = size[1]
    if height >= 720:
        return 0
    return 1 if height >= 540 else 2


def _clean_label(text) -> str:
    """把源给的分类标签压成可匹配的小写串（剥 emoji、空白与标点）。"""
    if not isinstance(text, str):
        return ""
    return _LABEL_NOISE.sub("", text).casefold()


def _match_keywords(lowered: str) -> str | None:
    """按固定优先级做关键词匹配，返回分类名或 ``None``。

    顺序是硬要求：窄规则必须排在宽规则前，否则「广东少儿」会被省名规则吃掉。
    """
    if not lowered:
        return None
    for category, tokens in _RULES:
        if any(token in lowered for token in tokens):
            return category
    return None


def category_for(name: str, raw_group=None) -> str:
    """按频道名与源标签归到六类之一。

    频道名是主信号。实测源给的 ``group-title`` 大面积错误——CGTN英语 标成
    「APTV专享」、CCTV-1 标成「general」、河北卫视 标成「4K频道」——所以名字里
    出现 cctv/cgtn/央视/卫视 时必须先按名字定性，不能先信标签。

    但标签也不能完全丢：名字是外文台名或纯地方台名时，标签是唯一线索
    （「九台」在源里标「纪录频道」、「让子弹飞」标「点播电影」、「Arirang」
    标「国际频道」，只看名字会全部掉进兜底的网络直播）。
    """
    lowered = (name or "").casefold()
    if not lowered:
        return FALLBACK_CATEGORY

    # 第一梯队：名字自报身份，强于任何标签。
    if any(token in lowered for token in _CENTRAL):
        return "央视频道"
    if any(token in lowered for token in _SATELLITE):
        return "卫视频道"

    # 第二梯队：窄品类按名字匹配（少儿/电影先于地方，避免被省名吃掉）。
    named = _match_keywords(lowered)
    if named in ("少儿频道", "电影频道"):
        return named

    # 第三梯队：源标签。此时名字还没定性，标签比「地方频道」这种宽规则可靠。
    label = _clean_label(raw_group)
    if label:
        if any(token in label for token in _GREATER_CHINA):
            return "地方频道"
        labelled = _match_keywords(label)
        if labelled:
            return labelled

    # 第四梯队：名字里的宽规则（省名、新闻、体育……）。
    if named:
        return named
    return FALLBACK_CATEGORY


def _has_headers(item: dict) -> bool:
    headers = item.get("headers")
    if not headers:
        return False
    if isinstance(headers, dict):
        return any(str(k).strip() for k in headers)
    return bool(str(headers).strip())


def _is_h264(item: dict, allow_unknown: bool) -> bool:
    kind = classify_codec(item.get("video_codec"))
    if kind == "h264":
        return True
    if kind is None:
        return allow_unknown
    return False


def _within_bounds(item: dict) -> bool:
    size = parse_resolution(item.get("resolution"))
    if size is None:
        # 分辨率未知照样放行：ffprobe 只在开了 filter_resolution 时才跑，
        # 大多数源没有 RESOLUTION 标记，硬性要求会砍掉九成以上的可用源。
        return True
    width, height = size
    return width <= MAX_TV_WIDTH and height <= MAX_TV_HEIGHT


def _item_score(item: dict) -> float:
    try:
        value = float(item.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0
    # inf 是 IPv6 未实测时填入的假分数，不能让它排到真实测速的前面。
    if value != value or value == float("inf") or value == float("-inf"):
        return 0.0
    return value


def _sort_key(item: dict) -> tuple:
    """同一频道内的线路排序：清晰度优先，再分数，再延迟，最后 URL 保证稳定。"""
    try:
        delay = int(item.get("delay") or 10**9)
    except (TypeError, ValueError):
        delay = 10**9
    if delay < 0:
        delay = 10**9
    return (quality_rank(item.get("resolution")), -_item_score(item), delay, str(item.get("url") or ""))


def filter_for_tv(data: dict, allow_unknown_codec: bool = True) -> dict:
    """从 ``{cate: {name: [item, ...]}}`` 里挑出电视能播的条目。

    返回同样的嵌套结构，只保留通过过滤的频道；一个都过不了的频道整体消失
    （电视上留一个点了黑屏的空频道比没有更糟）。
    """
    result: dict[str, dict[str, list]] = {}
    for cate, channel_obj in (data or {}).items():
        for name, items in (channel_obj or {}).items():
            kept: list[dict] = []
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                if _has_headers(item):
                    continue
                if not _is_h264(item, allow_unknown_codec):
                    continue
                if not _within_bounds(item):
                    continue
                kept.append(item)
            if not kept:
                continue
            kept.sort(key=_sort_key)
            result.setdefault(cate, {})[name] = kept[:TV_LINES_PER_CHANNEL]
    return result


def group_for_tv(tv_data: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """按六类归组，返回 ``[(分类, [(频道名, url), ...]), ...]``（已按显示顺序排）。

    去重按 ``(分类, 频道名, url)``：同一个频道可能同时出现在源的多个 raw group 里
    （数据是 ``{raw_cate: {name: [...]}}``），不去重会在电视上出现同名两条。
    """
    buckets: dict[str, list[tuple[str, str]]] = {name: [] for name in CATEGORY_ORDER}
    seen: set[tuple[str, str, str]] = set()
    for source_cate, channel_obj in (tv_data or {}).items():
        for name, items in (channel_obj or {}).items():
            # 源标签只在名字定不了性时才用得上；名字自报身份时它会被忽略。
            category = category_for(name, source_cate)
            bucket = buckets.setdefault(category, [])
            for item in items:
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                key = (category, name, url)
                if key in seen:
                    continue
                seen.add(key)
                bucket.append((name, url))
    # 六类按显示顺序在前；海外频道（以及任何将来新增的类别）按首次出现顺序追加到末尾。
    ordered = list(CATEGORY_ORDER) + [k for k in buckets if k not in CATEGORY_ORDER]
    return [(category, buckets[category]) for category in ordered if buckets.get(category)]


def _water_fill(wants: list[int], budget: int) -> list[int]:
    """按「每个分类都先拿到公平份额，剩下的再给还想要的分类」分配 ``budget``。

    顺序填满（前一个分类吃光预算）会让排在前面的「地方频道」把 1500 条名额全部
    占掉，少儿/电影/网络直播一条都出不来——实测真实数据就是这个结果。电视用户
    按分类浏览，缺一整类比每个分类都少几条严重得多。
    """
    quotas = [0] * len(wants)
    remaining = max(0, int(budget))
    active = [index for index, want in enumerate(wants) if want > 0]
    while active and remaining > 0:
        share = remaining // len(active)
        if share <= 0:
            # 剩余不够每个分类一条，按显示顺序发到用完为止。
            for index in active:
                if remaining <= 0:
                    break
                quotas[index] += 1
                remaining -= 1
            break
        still_active = []
        for index in active:
            need = wants[index] - quotas[index]
            give = min(need, share)
            quotas[index] += give
            remaining -= give
            if need > give:
                still_active.append(index)
        if len(still_active) == len(active):
            # 没有任何分类被满足，说明每类都拿到了整份 share，进入下一轮。
            continue
        active = still_active
    return quotas


def _entry_text(category: str, name: str, url: str) -> str:
    return f'#EXTINF:-1 group-title="{category}",{name}\n{url}\n'


def render_m3u(groups, max_bytes: int = TV_BYTE_BUDGET,
               max_channels: int = TV_CHANNEL_BUDGET) -> tuple[str, int]:
    """渲染成 m3u，超出上限时按分类公平裁剪，返回 ``(text, 保留的频道数)``。

    以**频道**为单位裁剪（不按分类整体、更不按行）：按分类整体切会整块砍掉
    「地方频道」这种大分组；按行切会留下没有 URL 的 ``#EXTINF``，那是让播放器
    整份解析失败的最快办法。

    名额在分类之间按 ``_water_fill`` 公平分配，不是顺序填满——否则排在前面的
    大类会把预算吃光。

    刻意不写 ``#EXTVLCOPT`` 与 ``x-tvg-url``：电视端不认 header 行，EPG 地址则会
    多一次解析负担，而当前播放器没有节目单界面。
    """
    prepared = []
    for category, channels in groups or []:
        entries = []
        for name, url in channels:
            entry = _entry_text(category, str(name), str(url))
            entries.append((entry, len(entry.encode("utf-8"))))
        if entries:
            prepared.append((category, entries))

    if not prepared:
        return "#EXTM3U\n", 0

    header_sizes = [len(f"\n# ===== {category} =====\n".encode("utf-8")) for category, _ in prepared]
    total_channels = sum(len(entries) for _, entries in prepared)
    total_bytes = len("#EXTM3U\n") + sum(
        header_sizes[index] + sum(size for _, size in entries)
        for index, (_, entries) in enumerate(prepared)
    )
    if total_channels <= max_channels and total_bytes <= max_bytes:
        quotas = [len(entries) for _, entries in prepared]
    else:
        # 两道上限各自算一份名额，取小的那个：字节与条数必须同时满足。
        channel_quotas = _water_fill([len(entries) for _, entries in prepared], max_channels)
        budget_bytes = max(0, max_bytes - len("#EXTM3U\n") - sum(header_sizes))
        byte_quotas = _water_fill(
            [sum(size for _, size in entries) for _, entries in prepared], budget_bytes
        )
        # byte_quotas 的单位是字节，要换算回「能放几条」。
        quotas = []
        for index, (_, entries) in enumerate(prepared):
            allowed = byte_quotas[index]
            taken = 0
            fits = 0
            for _, size in entries:
                if taken + size > allowed:
                    break
                taken += size
                fits += 1
            quotas.append(min(channel_quotas[index], fits, len(entries)))

    out: list[str] = ["#EXTM3U\n"]
    count = 0
    for index, (category, entries) in enumerate(prepared):
        quota = quotas[index] if index < len(quotas) else len(entries)
        if quota <= 0:
            continue
        out.append(f"\n# ===== {category} =====\n")
        for entry, _ in entries[:quota]:
            out.append(entry)
            count += 1
    text = "".join(out)
    if not text.endswith("\n"):
        text += "\n"
    return text, count


def write_android44(data: dict, path: str, max_channels: int = TV_CHANNEL_BUDGET,
                    allow_unknown_codec: bool = True) -> dict:
    """过滤 + 渲染 + 原子写盘，返回统计信息供日志使用。"""
    tv_data = filter_for_tv(data, allow_unknown_codec=allow_unknown_codec)
    groups = group_for_tv(tv_data)
    text, channels = render_m3u(groups, max_channels=max_channels)
    payload = text.encode("utf-8")
    target_dir = os.path.dirname(path) or "."
    os.makedirs(target_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", delete=False, dir=target_dir,
        prefix=os.path.basename(path) + ".tmp.",
    ) as tmp:
        tmp.write(payload)
        tmp_path = tmp.name
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass

    categories: dict[str, int] = {}
    codecs: dict[str, int] = {}
    for block in text.split("\n\n"):
        match = re.search(r'^# ===== (.+) =====$', block, re.MULTILINE)
        if not match:
            continue
        categories[match.group(1)] = block.count("#EXTINF")
    for _cate, channel_obj in tv_data.items():
        for _name, items in channel_obj.items():
            for item in items:
                key = str(item.get("video_codec") or "(未知)")
                codecs[key] = codecs.get(key, 0) + 1

    digest = hashlib.sha256(payload).hexdigest()[:16]
    return {
        "channels": channels,
        "bytes": len(payload),
        "categories": categories,
        "codecs": codecs,
        "digest": digest,
    }
