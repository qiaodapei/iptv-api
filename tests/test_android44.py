import os
import tempfile
import unittest

from utils.android44 import (
    CATEGORY_ORDER,
    FALLBACK_CATEGORY,
    MAX_TV_HEIGHT,
    MAX_TV_WIDTH,
    TV_CHANNEL_BUDGET,
    TV_MAX_BYTES,
    TV_MAX_CHANNELS,
    category_for,
    classify_codec,
    filter_for_tv,
    group_for_tv,
    parse_resolution,
    quality_rank,
    render_m3u,
    write_android44,
)


def item(url, codec="h264", resolution="1920x1080", speed=5.0, delay=100, **extra):
    data = {
        "url": url,
        "video_codec": codec,
        "resolution": resolution,
        "speed": speed,
        "delay": delay,
    }
    data.update(extra)
    return data


class CodecTests(unittest.TestCase):
    def test_h264_spellings_are_accepted(self):
        for raw in ("h264", "H264", "avc1.64001f", "h.264", "x264"):
            self.assertEqual(classify_codec(raw), "h264", raw)

    def test_hevc_is_rejected(self):
        for raw in ("hevc", "h265", "HVC1", "hev1"):
            self.assertEqual(classify_codec(raw), "unsafe", raw)

    def test_other_codecs_are_rejected(self):
        for raw in ("av01", "mpeg4", "vp9", "mpeg2video"):
            self.assertEqual(classify_codec(raw), "unsafe", raw)

    def test_missing_codec_is_unknown_not_unsafe(self):
        # 采集不到编码与「编码不安全」是两件事：前者交给 allow_unknown_codec
        # 决定，后者无条件拒绝。混在一起会让日志失去排查价值。
        for raw in (None, "", "   "):
            self.assertIsNone(classify_codec(raw), raw)


class ResolutionTests(unittest.TestCase):
    def test_parse_variants(self):
        self.assertEqual(parse_resolution("1920x1080"), (1920, 1080))
        self.assertEqual(parse_resolution("1280X720"), (1280, 720))
        self.assertEqual(parse_resolution("1920*1080"), (1920, 1080))
        self.assertIsNone(parse_resolution("720p"))
        self.assertIsNone(parse_resolution("0x0"))
        self.assertIsNone(parse_resolution(None))

    def test_non_string_resolution_is_treated_as_unknown(self):
        # 上游数据里 resolution 字段可能是 dict 或数字，抛异常会连带整份列表写不出去。
        for raw in ({"width": 1920}, 1080, [1920, 1080], 1.5):
            self.assertIsNone(parse_resolution(raw), raw)

    def test_non_string_codec_is_treated_as_unknown(self):
        for raw in ({"codec": "h264"}, 264, ["h264"]):
            self.assertIsNone(classify_codec(raw), raw)

    def test_quality_rank_prefers_high_definition(self):
        self.assertEqual(quality_rank("1920x1080"), 0)
        self.assertEqual(quality_rank("1280x720"), 0)
        self.assertEqual(quality_rank("960x540"), 1)
        self.assertEqual(quality_rank("640x480"), 2)
        self.assertEqual(quality_rank(None), 2)


class CategoryTests(unittest.TestCase):
    def test_display_order_matches_our_six_groups(self):
        self.assertEqual(
            CATEGORY_ORDER,
            ("央视频道", "卫视频道", "地方频道", "少儿频道", "电影频道", "网络直播"),
        )

    def test_channel_name_beats_source_group(self):
        # 实测源分组大面积标错：CGTN英语 标成「APTV专享」、CCTV-1 标成「general」、
        # 河北卫视 标成「4K频道」。频道名是更强信号。
        self.assertEqual(category_for("CGTN英语"), "央视频道")
        self.assertEqual(category_for("CCTV-1"), "央视频道")
        self.assertEqual(category_for("CCTV13"), "央视频道")
        self.assertEqual(category_for("河北卫视"), "卫视频道")

    def test_narrow_rules_run_before_wide_ones(self):
        # 「广东少儿」含省名，必须先被少儿规则抓住。
        self.assertEqual(category_for("广东少儿"), "少儿频道")
        # 「上海电影」含直辖市名，必须先被电影规则抓住。
        self.assertEqual(category_for("上海电影"), "电影频道")
        self.assertEqual(category_for("卡酷少儿"), "少儿频道")
        self.assertEqual(category_for("东方影视频道"), "电影频道")

    def test_satellite_suffix_variants_stay_satellite(self):
        for name in ("东方卫视4K", "江苏卫视4K", "湖南卫视"):
            self.assertEqual(category_for(name), "卫视频道", name)

    def test_source_label_breaks_ties_when_the_name_says_nothing(self):
        # 名字是外文台名或纯地方台名时，源标签是唯一线索。实测案例：
        # 「九台」标成「纪录频道」、「让子弹飞」标成「点播电影」、
        # 「Arirang」标成「国际频道」。
        self.assertEqual(category_for("让子弹飞", "点播电影"), "电影频道")
        self.assertEqual(category_for("万州三峡移民", "地方频道"), "地方频道")
        self.assertEqual(category_for("九台", "纪录频道"), "地方频道")
        self.assertEqual(category_for("Arirang", "国际频道"), "海外频道")

    def test_channel_name_still_beats_a_wrong_label(self):
        # 标签错了也不能翻车：CGTN英语 标「APTV专享」、CCTV-1 标「general」、
        # 河北卫视 标「4K频道」、广东少儿 混在「☘️广东频道」里。
        self.assertEqual(category_for("CGTN英语", "APTV专享"), "央视频道")
        self.assertEqual(category_for("CCTV-1", "general"), "央视频道")
        self.assertEqual(category_for("河北卫视", "4K频道"), "卫视频道")
        self.assertEqual(category_for("东方卫视4K", "4K频道"), "卫视频道")
        self.assertEqual(category_for("广东少儿", "☘️广东频道"), "少儿频道")

    def test_label_emoji_prefix_is_stripped(self):
        self.assertEqual(category_for("某台", "☘️北京频道"), "地方频道")
        self.assertEqual(category_for("某台", "📡卫视频道"), "卫视频道")

    def test_greater_china_labels_join_local(self):
        # 六类方案里港澳台并入地方频道，不单列。
        self.assertEqual(category_for("某台", "港澳台频道"), "地方频道")

    def test_overseas_group_is_appended_last_and_only_when_used(self):
        tv_overseas = {"国际频道": {"Arirang": [item("http://a/1.m3u8")]}}
        groups = group_for_tv(tv_overseas)
        self.assertEqual([c for c, _ in groups], ["海外频道"])
        tv_domestic = {"央视频道": {"CCTV-1": [item("http://a/1.m3u8")]}}
        self.assertNotIn("海外频道", [c for c, _ in group_for_tv(tv_domestic)])

    def test_unknown_channel_falls_back(self):
        self.assertEqual(category_for(""), FALLBACK_CATEGORY)
        self.assertEqual(category_for("某不存在的台"), FALLBACK_CATEGORY)


class FilterTests(unittest.TestCase):
    def test_hevc_is_dropped(self):
        data = {"c": {"CCTV-1": [item("http://a/1.m3u8", codec="hevc")]}}
        self.assertEqual(filter_for_tv(data), {})

    def test_entries_with_headers_are_dropped(self):
        # 电视端的 M3uParser 明确忽略 #EXTVLCOPT，带请求头的源会变成
        # 「条目在、一播就 403」的死条目。
        data = {"c": {"CCTV-1": [item("http://a/1.m3u8", headers={"Referer": "x"})]}}
        self.assertEqual(filter_for_tv(data), {})
        data = {"c": {"CCTV-1": [item("http://a/1.m3u8", headers={})]}}
        self.assertIn("CCTV-1", filter_for_tv(data)["c"])

    def test_above_1080p_is_dropped(self):
        data = {"c": {"CCTV-1": [item("http://a/1.m3u8", resolution="3840x2160")]}}
        self.assertEqual(filter_for_tv(data), {})

    def test_1080p_is_kept(self):
        data = {"c": {"CCTV-1": [item("http://a/1.m3u8", resolution="1920x1080")]}}
        self.assertIn("CCTV-1", filter_for_tv(data)["c"])

    def test_unknown_resolution_is_kept(self):
        # 大多数源没有 RESOLUTION 标记，硬性要求分辨率会砍掉九成可用源。
        data = {"c": {"CCTV-1": [item("http://a/1.m3u8", resolution=None)]}}
        self.assertIn("CCTV-1", filter_for_tv(data)["c"])

    def test_unknown_codec_follows_the_switch(self):
        data = {"c": {"CCTV-1": [item("http://a/1.m3u8", codec=None)]}}
        self.assertIn("CCTV-1", filter_for_tv(data, allow_unknown_codec=True)["c"])
        self.assertEqual(filter_for_tv(data, allow_unknown_codec=False), {})

    def test_channel_without_any_surviving_line_disappears(self):
        # 留一个点了就黑屏的空频道，比没有这个频道更糟。
        data = {"c": {
            "CCTV-1": [item("http://a/1.m3u8")],
            "CCTV-2": [item("http://a/2.m3u8", codec="hevc")],
        }}
        self.assertEqual(list(filter_for_tv(data)["c"]), ["CCTV-1"])

    def test_high_definition_sorts_before_low(self):
        # 默认播第一条：65 分的 240p 排到 63 分的 1080p 前面就会一开就糊。
        data = {"c": {"湖南卫视": [
            item("http://b/low.m3u8", resolution="640x480", speed=9.0, delay=1),
            item("http://b/hd.m3u8", resolution="1920x1080", speed=3.0, delay=200),
        ]}}
        kept = filter_for_tv(data)["c"]["湖南卫视"]
        self.assertEqual(kept[0]["url"], "http://b/hd.m3u8")

    def test_lines_per_channel_is_capped(self):
        data = {"c": {"CCTV-1": [item(f"http://a/{i}.m3u8") for i in range(6)]}}
        self.assertEqual(len(filter_for_tv(data)["c"]["CCTV-1"]), 2)

    def test_infinite_speed_does_not_outrank_real_measurements(self):
        # inf 是 IPv6 未实测时填的假分数。
        data = {"c": {"CCTV-1": [
            item("http://a/fake.m3u8", resolution="640x480", speed=float("inf"), delay=1),
            item("http://a/real.m3u8", resolution="1920x1080", speed=3.0, delay=200),
        ]}}
        kept = filter_for_tv(data)["c"]["CCTV-1"]
        self.assertEqual(kept[0]["url"], "http://a/real.m3u8")


class RenderTests(unittest.TestCase):
    def test_groups_follow_display_order(self):
        tv = {
            "raw1": {"CCTV-1": [item("http://a/1.m3u8")]},
            "raw2": {"湖南卫视": [item("http://b/1.m3u8")]},
            "raw3": {"浙江新闻": [item("http://c/1.m3u8")]},
        }
        text, count = render_m3u(group_for_tv(tv))
        self.assertEqual(count, 3)
        central = text.index("# ===== 央视频道 =====")
        satellite = text.index("# ===== 卫视频道 =====")
        local = text.index("# ===== 地方频道 =====")
        self.assertLess(central, satellite)
        self.assertLess(satellite, local)

    def test_no_extvlcopt_and_no_tvg_url(self):
        # 电视端不认 header 行；EPG 地址会多一次解析负担，而播放器没有节目单界面。
        tv = {"raw1": {"CCTV-1": [item("http://a/1.m3u8")]}}
        text, _ = render_m3u(group_for_tv(tv))
        self.assertNotIn("#EXTVLCOPT", text)
        self.assertNotIn("x-tvg-url", text)
        self.assertTrue(text.startswith("#EXTM3U"))

    def test_same_url_is_written_once(self):
        # 同一频道可能同时出现在源的多个 raw group 里，不去重电视上会看到同名两条。
        tv = {
            "raw1": {"CCTV-1": [item("http://a/1.m3u8")]},
            "raw2": {"CCTV-1": [item("http://a/1.m3u8")]},
        }
        text, count = render_m3u(group_for_tv(tv))
        self.assertEqual(count, 1)
        self.assertEqual(text.count("http://a/1.m3u8"), 1)

    def test_every_extinf_is_followed_by_a_url(self):
        # 没有 URL 的 #EXTINF 会让播放器整份解析失败。
        tv = {"raw1": {f"CCTV-{i}": [item(f"http://a/{i}.m3u8")] for i in range(1, 20)}}
        text, _ = render_m3u(group_for_tv(tv))
        lines = [line for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if line.startswith("#EXTINF"):
                self.assertTrue(
                    lines[index + 1].startswith("http"),
                    f"第 {index + 1} 行 #EXTINF 后面没有 URL",
                )

    def test_channel_budget_truncates_whole_channels(self):
        tv = {"raw1": {f"频道{i}": [item(f"http://a/{i}.m3u8")] for i in range(50)}}
        text, count = render_m3u(group_for_tv(tv), max_channels=10)
        self.assertEqual(count, 10)
        self.assertEqual(text.count("#EXTINF"), 10)

    def test_byte_budget_truncates_whole_channels(self):
        tv = {"raw1": {f"频道{i:03d}": [item(f"http://a/{i}.m3u8")] for i in range(200)}}
        text, count = render_m3u(group_for_tv(tv), max_bytes=800)
        self.assertLessEqual(len(text.encode("utf-8")), 800)
        self.assertGreater(count, 0)
        self.assertLess(count, 200)
        self.assertEqual(text.count("#EXTINF"), count)

    def test_budget_smaller_than_one_entry_yields_empty_list(self):
        # 单个条目都放不下时宁可输出空列表，也不能截半条——留下没有 URL 的
        # #EXTINF 会让播放器整份解析失败，那比少几个频道严重得多。
        tv = {"raw1": {"CCTV-1": [item("http://a/1.m3u8")]}}
        text, count = render_m3u(group_for_tv(tv), max_bytes=10)
        self.assertEqual(count, 0)
        self.assertEqual(text, "#EXTM3U\n")

    def test_budgets_match_the_player_limits(self):
        # 这两个数字来自 LivingRoomTV 的 M3uParser，改动必须同步。
        self.assertEqual(TV_MAX_BYTES, 2 * 1024 * 1024)
        self.assertEqual(TV_MAX_CHANNELS, 2000)
        self.assertLessEqual(TV_CHANNEL_BUDGET, TV_MAX_CHANNELS)
        self.assertLessEqual(MAX_TV_WIDTH, 1920)
        self.assertLessEqual(MAX_TV_HEIGHT, 1080)


class FairAllocationTests(unittest.TestCase):
    def test_a_huge_category_does_not_starve_the_small_ones(self):
        # 实测真实数据时踩到的：顺序填满会让「地方频道」吃掉 1500 条全部名额，
        # 少儿/电影/网络直播一条都出不来。用户按分类浏览，缺一整类比每类少几条严重。
        big = {f"地方{i}综合": [item(f"http://a/l{i}.m3u8")] for i in range(500)}
        groups = [
            ("地方频道", [("地方%d综合" % i, "http://a/l%d.m3u8" % i) for i in range(500)]),
            ("少儿频道", [("少儿%d" % i, "http://a/k%d.m3u8" % i) for i in range(20)]),
            ("电影频道", [("电影%d" % i, "http://a/m%d.m3u8" % i) for i in range(20)]),
        ]
        text, count = render_m3u(groups, max_channels=100)
        self.assertEqual(count, 100)
        for category in ("地方频道", "少儿频道", "电影频道"):
            self.assertIn("# ===== %s =====" % category, text)

    def test_small_categories_get_everything_they_have(self):
        groups = [
            ("地方频道", [("地方%d" % i, "http://a/l%d.m3u8" % i) for i in range(500)]),
            ("少儿频道", [("少儿%d" % i, "http://a/k%d.m3u8" % i) for i in range(3)]),
        ]
        text, count = render_m3u(groups, max_channels=100)
        self.assertEqual(count, 100)
        # 少儿频道只有 3 条，必须全部保留（计数含 group-title 里的分类名，故用 EXTINF 行数）。
        self.assertEqual(
            len([l for l in text.splitlines() if l.startswith("#EXTINF") and l.endswith("少儿0")]), 1,
        )
        self.assertEqual(text.count('group-title="少儿频道"'), 3)

    def test_byte_budget_is_respected_with_fair_allocation(self):
        groups = [
            ("地方频道", [("地方%d" % i, "http://a/l%d.m3u8" % i) for i in range(500)]),
            ("少儿频道", [("少儿%d" % i, "http://a/k%d.m3u8" % i) for i in range(500)]),
        ]
        text, count = render_m3u(groups, max_bytes=5000)
        self.assertLessEqual(len(text.encode("utf-8")), 5000)
        self.assertGreater(count, 0)
        self.assertIn("地方频道", text)
        self.assertIn("少儿频道", text)


class WriteTests(unittest.TestCase):
    def test_writes_utf8_file_and_reports_stats(self):
        data = {"raw1": {
            "CCTV-1": [item("http://a/1.m3u8")],
            "湖南卫视": [item("http://b/1.m3u8")],
        }}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "android44.m3u")
            stats = write_android44(data, path)
            self.assertTrue(os.path.isfile(path))
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            self.assertEqual(stats["channels"], 2)
            self.assertEqual(stats["bytes"], len(text.encode("utf-8")))
            self.assertEqual(stats["categories"]["央视频道"], 1)
            self.assertEqual(stats["categories"]["卫视频道"], 1)
            self.assertEqual(stats["codecs"], {"h264": 2})
            self.assertEqual(len(stats["digest"]), 16)

    def test_digest_changes_when_content_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.m3u")
            first = write_android44({"c": {"CCTV-1": [item("http://a/1.m3u8")]}}, path)
            second = write_android44({"c": {"CCTV-1": [item("http://a/2.m3u8")]}}, path)
            self.assertNotEqual(first["digest"], second["digest"])

    def test_options_are_honoured(self):
        data = {"c": {
            f"频道{i}": [item(f"http://a/{i}.m3u8", codec=None)]
            for i in range(20)
        }}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.m3u")
            kept = write_android44(data, path, max_channels=5)
            self.assertEqual(kept["channels"], 5)
            # allow_unknown_codec=False 时未探测到编码的源全部剔除。
            strict = write_android44(
                data, path, allow_unknown_codec=False
            )
            self.assertEqual(strict["channels"], 0)

    def test_empty_input_still_writes_a_valid_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.m3u")
            stats = write_android44({}, path)
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            self.assertTrue(text.startswith("#EXTM3U"))
            self.assertEqual(stats["channels"], 0)


if __name__ == "__main__":
    unittest.main()
