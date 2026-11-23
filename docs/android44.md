# Android 4.4 电视专用列表

本 fork 在 `IPTV-API` 之上新增了一条输出：`output/android44.m3u`，与原有的
`output/result.m3u` 并列生成，专门给只硬解 H.264 的老电视使用。

- 上游项目：[Guovin/iptv-api](https://github.com/Guovin/iptv-api)
- 本 fork：[qiaodapei/iptv-api](https://github.com/qiaodapei/iptv-api)
- 许可证：AGPL-3.0（继承上游，未变更）

## 为什么需要这条输出

通用列表对播放设备没有假设，而老电视有一串硬限制。这四条不是「优化」，是「能不能播」：

### 1. 只硬解 H.264

上游采集了 `video_codec`（`utils/ffmpeg/probe.py` 走 ffprobe，RTSP/RTMP 走 ffmpeg stderr），
但**从不用于筛选** —— `utils/channel_quality.py` 的 `channel_result_rejection()` 全文只有
`unreachable` / `filtered_speed` / `filtered_resolution` 三种拒绝理由，没有编码判断。

后果：HEVC 源码率通常更大，在默认 `sort_by = speed` 下会排在最前面，而只支持 H.264 硬解的
设备打开就是黑屏。这不是「不够好」，是「有害」。

### 2. 不解析 `#EXTVLCOPT`

上游的 `utils/tools.py: convert_to_m3u()` 会给带自定义请求头（UA/Referer）的源写入
`#EXTVLCOPT:http-<key>=<value>` 行。

部分播放器会忽略这类行 —— 结果不是「播放失败」，而是「条目在、点了 403/404」，排查成本更高。
所以带请求头的源一律不进这条列表。

### 3. 分辨率不超过 1080p

`MAX_TV_WIDTH = 1920` / `MAX_TV_HEIGHT = 1080`。更高分辨率在这些设备上不增加观感，只增加带宽。

分辨率**未知的源照样放行** —— 绝大多数源不写 `RESOLUTION`，硬性要求会砍掉九成可用源。

### 4. 解析上限

参考实现（客厅电视项目里的 `M3uParser`）有两个硬顶：

| 常量 | 值 | 含义 |
|---|---|---|
| `MAX_BYTES` | `2 * 1024 * 1024` | 整份列表字节上限 |
| `MAX_CHANNELS` | `2000` | 合并后的**频道**数上限（不是线路数） |

模块里对应的安全值：`TV_BYTE_BUDGET = 1_900_000`、`TV_CHANNEL_BUDGET = 1500`，
可通过 `android44_max_channels` 调整（上限 2000）。

## 分类

固定六类，显示顺序即下面的顺序：

1. 央视频道
2. 卫视频道
3. 地方频道
4. 少儿频道
5. 电影频道
6. 网络直播

另有 `海外频道`，只在真的有海外频道时才出现，排在最后。

**分类判定是四梯队**，顺序是硬要求：

1. **频道名自报身份** —— 名字里带 `cctv` / `cgtn` / `央视` → 央视频道；带 `卫视` → 卫视频道
2. **名字窄品类** —— 少儿、电影
3. **源分组标签** —— 剥掉 emoji 后匹配（`港澳台` 并入地方频道）
4. **名字宽规则** —— 省名、新闻、体育、纪录等
5. 兜底 → 网络直播

第 1 步必须排在最前，因为源标签经常是错的。实测例子：

| 频道 | 源给的标签 | 正确分类 |
|---|---|---|
| `CGTN英语` | `APTV专享` | 央视频道 |
| `CCTV-1` | `general` | 央视频道 |
| `河北卫视` | `4K频道` | 卫视频道 |
| `九台` | `纪录频道` | 地方频道 |
| `让子弹飞` | `点播电影` | 电影频道 |

第 3 步存在的价值：`九台` / `让子弹飞` / `Arirang` 这类名字本身看不出品类，只能靠标签。

## 名额分配

输出的线路总数有上限，而「地方频道」的源数量往往是其他分类的几十倍。如果按顺序填满，
排在后面的大分类会把额度吃光，小分类**一整类消失** —— 用户按分类浏览时，
缺一整类比每类少几条严重得多。

所以用 `_water_fill()` 做注水式分配：每轮给所有还想要额度的分类等额份额，
不够一整轮就按顺序发到用完。字节上限与条数上限各算一份额度，取小的那个。

## 每个频道最多两条线路

`TV_LINES_PER_CHANNEL = 2`。第一条是主线路，第二条做冗余。
排序键 `(清晰度档位, -测速, 延迟, url)` —— 高清优先，其次看实测速率。

`inf` 分数按 0 处理：那是 IPv6 源未实测时填的占位假分数，不能让它压过真实测速的源。

## 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `open_android44_result` | `True` | 是否生成这条列表 |
| `android44_file` | `output/android44.m3u` | 输出路径 |
| `android44_max_channels` | `1500` | 线路数上限（1–2000） |
| `android44_allow_unknown_codec` | `True` | 编码未知的源是否保留 |

`android44_allow_unknown_codec = False` 时只保留**已确认** H.264 的源，列表会明显变短。
默认 `True` 是因为多数源不写编码信息，关掉会砍掉大量可用源。

## HTTP 路由

| 路由 | 说明 |
|---|---|
| `/android44` | 下载列表 |
| `/android44/m3u` | 同上 |

## 实现位置

新增（上游没有）：

- `utils/android44.py` —— 全部逻辑
- `tests/test_android44.py` —— 43 项测试
- `docs/android44.md` —— 本文

对上游文件的改动**全部是纯新增，零删除零修改**：

| 文件 | 增 | 删 | 内容 |
|---|---|---|---|
| `config/config.ini` | +8 | 0 | 4 条配置 + 注释 |
| `utils/config.py` | +22 | 0 | 4 个属性 + 2 条 schema + 2 个 boolean key |
| `service/app.py` | +9 | 0 | 2 条路由 |
| `utils/channel.py` | +39 | 0 | 1 个函数 + 1 处调用 |

## 与上游同步

因为改动是纯新增，`git rebase upstream/master` 基本不会冲突。真正可能冲突的只有两处：

1. `utils/channel.py:write_channel_to_file()` 里插入的那一行调用
2. `service/app.py` 里的那段路由块

上游若改动了这两处附近的代码，手工重加即可 —— `utils/android44.py` 整个模块不受影响。

```bash
git fetch upstream
git rebase upstream/master
```

## 已知边界

- 这条列表只保证「格式和编码电视能读」，**不保证每条源当前真的能播**。
  宿主机探测通过 ≠ 电视能播（实测数据：577 条宿主机判定可用，电视侧只有 80 条真能播，13.9%）。
  要真机可用性，需要在电视侧再做三层校验（索引 m3u8 → 变体 → 首个分片，且分片首字节为 `0x47`）。
- 不生成 EPG。
- 不带 `x-tvg-url`。
