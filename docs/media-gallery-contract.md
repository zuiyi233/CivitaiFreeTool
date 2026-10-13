# 多图展示 + 视频预览 — 跨模块契约（v1）

> 已实施完成，留档供后续维护。目标：模型详情/瀑布流支持 C 站视频预览（mp4 + 首帧海报）与全量多图展示。

## 0. images 数组规范（数据真相源）

C 站 `modelVersions[].images` 每项：`{url, type: 'image'|'video', imageUrl(视频首帧图，仅 video 有), width, height, nsfw}`。
`.civitai.info` 的 images 保留以上字段；截断 [:20]。
**`type` 不可信，也不能只信它**：存量 sidecar 里 images 条目常常只有 `{url,width,height,nsfw}`——真实库 479 条实测 `type` 字段 100% 缺失，其中 107 条（22.3%）URL 尾缀是 `.mp4`。v2.6.28 之前"按非 video 兜底"直接把这段视频当静帧交给 `<img>`，是"一张图要 1 分钟甚至 5 分钟"的主因（详见 §1b）。判定一律走 `webui._is_video_entry`：`type=='video'` **或** `url`/`imageUrl` 的 path 尾缀 ∈ `VIDEO_URL_EXTS`。

## 1. Python 侧（已实施：reverse_parse.py + webui.py）

1. `reverse_parse.build_info`：images 保留 `{url,type,width,height,nsfw}` + 条件 `imageUrl`，[:5]→[:20]。
2. `get_model_detail`：video 项 → `{type:'video', url:<本地 file URI 优先，否则 mp4>, poster:<imageUrl，老 info 没有则留空>, prompt, negative, orig_url}`；image → 原结构 + `type:'image'`；mp4 绝不进 get_cover_b64/PIL。**本地 `<base>.images/` 优先于远程 URL**，并按 `image_%02d` 序号抑制同帧重复（封面落在哪个序号，那个序号就不再出远程 URL；只按文件路径去重会穿透，实测踩过）。
3. `download_all_images`：video 项存首帧 imageUrl 为 image_%02d.jpg，**并落 mp4 原片为 video_%02d.mp4**（v2.6.28 起，受 `video_max_mb` 与磁盘余量约束）；静帧落 `image_%02d.<ext>`。已存在即跳过 → 幂等。
4. `_pick_cover_url(imgs)`（webui.py 约 59-74）：封面 URL 选择——**先用 `_is_video_entry` 滤掉视频**，全 video 时退回首帧 poster，老 info 连 poster 也没有则返回 ''（调用方按"无封面"处理）；应用于 `_handle_dl_done` 封面段与 `mm_download_covers`。
5. `get_covers`：find_cover 未命中回退 `<base>.images/` 首图（`_images_dir_first`）。
6. `get_covers_multi(paths, size=220, n=3)`：`{path: [[src, isVideo], ...]}`，**帧顺序=本地优先于远程**：封面 → 按 info 序号依次取帧（该序号本地已落盘就用本地 b64，本地没有才用远程 URL，image 项转 `width=460` 缩略）→ 无序号的本地图片补位。视频项只返回可播源并标 `isVideo=True`，没有 poster 也绝不伪造一个指向 mp4 的"图片 URL"。
7. `get_gallery_image` 对 video 项直接抛/返回中文错误（`image_gallery.preview` 在入口即拒，连下载都不发起）；`ValueError` 的文案原样透传给用户，不再并成"图片读取失败"。

## 1b. 图片加载性能档位（v2.6.29 实测结论，勿凭直觉改）

**CDN 参数能力（对真实库 URL 逐个 HEAD 实测）**

| 请求 | 静帧均值 | 视频均值 | 结论 |
|---|---|---|---|
| `original=true` | 3235 KB | 19429 KB | 查看器/轮播默认的浪费来源 |
| `width=1440` | 609 KB | 5093 KB | 有效 |
| `width=1440,quality=80` | 461 KB | — | **再省 32%**，显示档默认 |
| `width=460` | 102 KB | 1957 KB | 轮播缩略用 |
| `format=jpeg/webp/avif`、`resize=cover` | 与 `width=1440` **字节数完全相同** | 同 | **CDN 忽略，禁止使用** |

- **对 mp4 做 `width=` 只会拿到更小的 mp4，content-type 仍是 `video/mp4`**，`<img>` 依旧解不出来。降级救不了误判，必须先判对类型。
- **防上采样**：`natural_width <= max_width` 时不得加 `width=`（实测一张 682KB 的窄图被 `width=1440` 顶成 1,218,800 字节）。全库 479 条的 `width` 字段 100% 存在，median 1080，**328 条（68%）本来就 ≤1440px**——不防这一条，"降级"对多数图是反向的。
  `natural_width` 由 `image_gallery.generation()`（:289 输出 `width/height/image_id`）提供，`get_model_detail` 用 `c.update(generation(...))` 挂进 covers；**不要在 append 处再手动补一份 width**（独立复核报过「接线断了」，实测把那份重复代码删掉后 `read_bytes` 照样拿到 1200/2048，属于误判）。回归守卫见 `tests/test_gallery_display_tier.py::test_natural_width_actually_reaches_the_guard`，它是从 `get_model_detail` 一路打到真正请求的 URL 的，不是只测 `display_url`。
- URL 只能替换**已存在的 params 段**（四段式 `/{auth}/{hash}/{params}/{file}`）；盲插新段会 404 空帧。非 `image.civitai.com`、无 params 段、视频源 → 一律不改写。

**分档与缓存**

- `image_gallery.display_url(url, max_width, quality, natural_width)` 造显示档；`read_bytes/preview(..., max_width, quality)`。
- 缓存键按档位分档，**默认档（无档位）必须与旧键逐字节一致**，否则已缓存的 68 个文件集体作废、触发一轮全库重下。
- `.civitai.info` 转码会**剥掉 EXIF/UserComment**：显示档字节一律不得用于提取生成参数，只认已缓存的原图字节；取不到就退回 info 的 meta 并置 `metadata_from_original=False`、`downgraded=True`。查看器如实显示"生成参数未从原图读取"。
- `save_gallery_image` / `copy_gallery_image` / `get_gallery_cached` **始终走原图档**，不受显示档影响。

**上限与时限（配置键即口径，两处实现必须算出同一个值）**

- `image_max_mb=20`（静帧单张）、`video_max_mb=100`（视频单个）、`fetch_deadline_s=120`（wall-clock 总时限；socket 级 `timeout` 管不住慢流，实测同批 URL 吞吐在 8.2KB/s~1760KB/s 之间抖，一轮 60s、一轮 3.5s）。
- `gallery_cache_mb` 默认 **8192**、可配到 **65536**。全库原图量级约 3.3GB（静帧 1.19GB + 视频 2.08GB），旧默认 1024MB 必然把"看过一次"的档淘汰掉——这就是"加载一次之后很久都不会变"不成立的直接原因。
- 历史缺陷类别，复查时优先扫：**normalize 允许的大值被运行时的 `min(...)` 静默截断**（曾出现在 `gallery_cache_mb` 8192 与 `_limit_mb` 1024 两处）。新增可配大值时，所有消费点的钳制范围必须一起放开。

**按需补帧到模型目录（`images_dir_autofill=True`）**

- 打开详情后排一次补帧：把 `<名>.images/` 缺的帧补上（静帧 `image_%02d`、视频 `video_%02d.mp4`），全局同时只 1 个任务、任务内串行 1 张、已存在即跳过、可取消。
- 写用户目录的守卫：`_fill_dir_writable` 要求目录解析到模型同层；**符号链接检测必须看 `FILE_ATTRIBUTE_REPARSE_POINT`——`os.path.islink()` 对 Windows junction 实测返回 False**，只查 islink + realpath 同层会被同层 junction 绕过；目标文件级同样拒绝重解析点。磁盘余量不足按帧跳过并写明原因；任何写失败都不影响当前查看。
- 没起来的原因留在 `get_images_autofill_state.reason`，否则"开关关了/磁盘不足/目录被拒"在界面上和"这模型没示例图"长得一样。


## 1c. 远程帧磁盘缓存与视频首帧（v2.6.30 实测结论）

**远程帧必须软件侧留一份，第二次才算"不用再从网上下载"**

- 以前 `get_covers_multi` 只把 CDN 直链交给浏览器，软件侧零持久化：浏览器自己的 HTTP 缓存一失效就从头再拉，重启即归零。现在同一条帧的字节永久落 `APP_DIR/detail_thumb_cache`，命中就直接出 `data:`，**第二次打开零网络**（`_url_thumb_b64` 只读盘、绝不在首屏 RPC 里顺手补一张，首屏延迟不受影响；未命中才出 CDN 档并排一次后台入盘）。
- 代价如实写明：首次打开会多一份我们自己用的字节（实测约 2× 缩略字节，浏览器一份 + 入盘一份），换的是第二次与以后每次。
- **档位只有一个数**：瀑布流 `_WATERFALL_THUMB_W=460`、详情 `_DETAIL_THUMB_W=512`。详情必须用 512，因为它随后会替每帧调一次 `get_cover_b64`（默认 `size=512`）；两处分叉时同一帧被取两次、落两份，第二次打开详情还多跑一趟网络。
- **缓存键 = 归一后的源图 URL + 档位**（`_detail_cache_path`）：`width=512` 与 `original=true` 是同一张图，前端拿到的是后端改写过的缩略串，与各算各的键等于永远不命中。非 C 站 URL 与 `file://` 走原样兜底，不报错。
- 档位夹取只在 `_thumb_size` 一处（64..1024）。历史缺陷类别：**读侧原样拼键、写侧先夹取**，越界的 size 会永远读不到自己刚写下的那张。
- **缩略图的字节闸 `_THUMB_FETCH_CAP`（8MB）与 `config.image_max_mb` 解耦**：后者管的是原图归档预算，能被设到 1MB，拿它当缩略图上限时设置一低就 `len(data) > cap → return ""`，瀑布流与详情的缩略图**整片空白**且没有任何提示。请求的是 `width<=1024` 的转码档，正常回字节几十到几百 KB，8MB 只是"别把失控的响应读进内存"。
- 入盘队列（`_warm_url_thumb` + `_thumb_warm_worker`）：单线程串行、同一 `档位:URL` 只排一次、上界 256 键、`get(timeout=5)` 空闲收工。**`put` 必须在锁内，worker 收工必须在取锁后复查 `queue.empty()`**——否则"worker 刚把 `thread` 置 None 收工、这一单才被挂上"会留下没人取的孤儿，键永久占着 `pending`，攒满 256 后整条入盘链路哑掉且不再自愈。

**视频首帧：谁生产、归谁、落哪儿**

- 真实库 107 条视频帧的 `imageUrl` 实测 **100% 为空**，纯 stdlib + PIL 又解不了 H.264，所以首帧只能由程序自己出像素：`video_thumb.first_frame()` 走 Windows 外壳（`native/CftChrome.cs` 的 `SHCreateItemFromParsingName` + `IShellItemImageFactory::GetImage`，`IIGBF_RESIZETOFIT|IIGBF_THUMBNAILONLY`）取长边 ≤640 的 JPEG，原子独占落盘、**已存在有内容就不覆盖**、目标或目录是重解析点就拒。PowerShell `Add-Type` 拿这个接口实测返回 `E_NOINTERFACE`，必须由 csc 编译的程序集声明 `InterfaceType(ComInterfaceType.InterfaceIsIUnknown)` 且 `GetImage` 排在接口首方法。
- 落点是既有补帧管线的新 kind：`<模型名>.images/image_XX.jpg`，与 `video_XX.mp4` **同序号**——本地优先取用全靠这个约定，`info` 里没有字段可查。
- **`image_XX.jpg` 只要序号落在视频项上，它就归那条视频自己当 poster，不是一张独立图帧**：不占 `local_img_slots`、不被选成详情封面、也不在 `get_covers_multi` 里以静帧身份出第二次。这条是三处判定（占位 / 跳过 / 首帧认领）统一到 `_frame_slot` 一份实现之后的唯一口径；`fn[6:8].isdigit()` 那种旧写法遇三位数序号会静默落空。曾经的 P0 就是"补帧刚落盘 → 位置猜测把视频序号也认领 → 第二次打开详情视频整个消失"，而触发它的是默认开启的首次打开补帧。
- 首帧依赖原片：`kind='poster'` 的源是**本地 mp4 路径**，原片没落成（`_download_video` 原子替换，失败即不留文件）时记"跳过"而不是再记一次"失败"——一次下载故障被计成两个坏消息，文案还会把根因说成"本机取不到首帧"。
- 浏览器 canvas 抓帧回传（`captureModelVideoPoster` → `save_model_video_poster`）**降级为兜底**：服务非 Windows（外壳不可用）与原片尚未落盘的远程视频。窗口模式下 `file://` 页面加载 `file://` 视频会污染画布、`toDataURL` 抛 SecurityError，远程 mp4 无 ACAO 同样污染，所以它不能作为主路径。
- 消费侧三处都必须吃到 poster，缺一处就是用户报的"只有鼠标移过去才看得见画面"：`<video poster>`（详情主图与轮播叠帧）、自动轮播的静帧分支（`_carouselStillFrame`）、详情缩略条的 `data-url`。`data:` 串**绝不发给 `get_cover_b64`**——那个 RPC 只认 http(s)，对 `data:` 返回空串，前端会把本来有图的缩略图整个 `remove()` 掉。
- **本地落盘视频的 src 按运行模式分流**（`_playableMediaUri`）：窗口模式（pywebview 的 `file://` 页面）原样用 `file:///`，同源可用；浏览器兜底模式页面是 http origin，Chromium 一律拒绝 `file:///` 子资源（实测控制台 `Not allowed to load local resource`），必须换成同源 `/api/media?p=<绝对路径>`（只放行模型根目录内、支持 Range）。`_local_video_uri` 的注释当年就写了"由前端降级远程 URL"，但**这条降级从来没实现过**——v2.6.29 的「本地优先」在浏览器模式下等于本地视频不播。URL 解析走 `new URL()`：`pathname` 要 `decodeURIComponent`（带空格/中文的模型名是真实存在的），UNC 补回 `//host`，Drive 去掉前导斜杠（`/L:/x` → `L:/x`）。序号回认（`_videoFrameInfoIndex`）仍然只看数据里的原始 `file:///` 串，不要把改写后的地址写回 `frame[0]`——`%2F` 会让那条正则认不出 `video_XX`。



## 2. 前端（已实施：web/app.js + web/image-viewer.js + style.css + workbench.css）

1. 详情画廊：video 缩略图 = poster b64 + ▶ 角标（.thumb-video-badge/.detail-thumb-box）；主图 = `<video controls preload="metadata" src=mp4 poster>`；切走/关详情 pause（loadB64 分支、closeDetail）。
2. image-viewer：video 项渲染 `.iv-stage-video`（controls autoplay），跳过全部 b64/get_gallery_image 路径；索引切换复用；切回图片/关闭时 pause + 清理残留（修了视频残留播放 bug）。
3. 瀑布流轮播（`_startCarousel`，间隔 `_MM_CAROUSEL_MS=3000`）：hover 启动，从第 2 帧起按 ~3 秒/帧循环回卷；每帧挂载成功即 `rearm()` 重排定时器（驻留均匀，不受预载耗时影响）。帧类型：本地 dataURI 直挂；远程 https 图先 `new Image()` 预载、成功才叠 `.ms-img-alt`（防空白），失败/6s 超时记入会话级 `dead` 集合后续直接跳过（不递归重试）；video 帧静音循环播放、按同样节奏切帧。`_masonryCarousel` 存 `{timer, cur}` 且 `cur` 随挂载回写——移出卡片 `_stopCarousel` 清叠层 + 暂停视频。帧来源 `get_covers_multi`：`[[src, isVideo], [mp4Src, true, posterSrc, infoIndex], ...]`，src 为 dataURI/https/file URL；视频帧第三格是它自己的首帧静图（可为空串），第四格是该帧在 `info.images[]` 里的序号（抓帧回传按它落 `image_XX.jpg`，猜错一位就会写进别的帧）。**卡片 hover 轮播不点击不发新请求（预载除外），点击进详情播放**。
3b. **预取必须绑在真正的滚动容器上**（v2.6.27 后修的哑火缺陷）：页面滚的是 `main.content`——`html`/`body` 与 `#mmMasonry` 自身都不滚（`scrollHeight == clientHeight`），而元素上的 `scroll` 事件**不冒泡**。旧写法把监听挂在 window 冒泡阶段和 `#mmMasonry` 上，两处一次都没触发过：首屏渲染时瀑布流还在折线以下（`_visibleMasonryCards()` 返回 0）→ 一帧都不请求，之后滚动也不补 → `coverCacheMulti` 恒空 → hover 无帧可轮，且预取失败被静默 catch，界面上连报错都没有。现在改成 `window.addEventListener("scroll", _onMmScroll, {passive:true, capture:true})`（捕获阶段能收到任意容器的滚动），并在 hover 命中未预取的卡片时就地补一次请求（`_multiTried` 去重，同一模型一次会话只问一次；请求失败的路径放回待请求，下次还能补）。轮播心跳改在 `_startCarousel` 入口就 `setInterval`，不再只在挂载成功后 `rearm`——旧写法首帧预载一失败就再也没有下一拍，永久停摆。回归用例：`tests/test_masonry_carousel.cjs`（真实 Chromium + mock 桥，验"滚动会驱动预取 / hover 起播 / ~3 秒换帧 / 同时只有一层叠帧 / 移开清理干净"）。
3c. **停播后晚到的预载帧不许挂图**（同一轮在冻结产物上实测到的第二个缺陷）：`_stopCarousel` 只 `clearInterval` 并移除 `st.cur`，**不取消在途的 `new Image()` 预载**；慢网下鼠标已经移开、预载才回来，帧会被挂到已经停播的卡片上，成为没人负责清理的孤儿叠图（实测：一次 hover 后叠层数变 2，移开鼠标后仍剩 1 张，要等下次重渲染才消失）。现在 `mount()` 先做归属检查（`_masonryCarousel.get(card) !== st` 就丢弃并返回 false，视频分支据此不再 `play()`），`rearm()` 同样只在归属仍属于本轮时排下一拍（否则停播后回来的帧会留下无人回收的定时器），`_stopCarousel` 改成先摘登记、再按 DOM 清掉 `.ms-img-wrap` 里的全部 `.ms-img-alt`（顺手治好历史遗留的孤儿）。
3d. **换帧必须是淡入，且自动轮播不靠悬停**（v2.6.28 完善；同时把 §3c 末句"先清空所有叠层再挂新帧"改成交叉淡入的口径）：
    - 淡入以前是**被自己废掉的**——CSS 已有 `.ms-img-alt{opacity:0;transition:opacity .35s}`，但 JS 在 `append` 之前就把 `style.opacity="1"` 写死，元素一插入就是不透明，过渡没有可插手的边界，所以切换生硬。现在改成"以 0 插入 → 下一帧抬到 1"，并且**旧帧压在下面保持不透明，等新帧淡到位（`transitionend` 或 `_MM_FADE_MS+260` 超时）才摘**——两张同时半透明会透出底下的主封面，反而闪一下。`_nextFrame()` 带 48ms 超时兜底：后台标签页与最小化窗口里 `requestAnimationFrame` 不跑，只靠 rAF 会让叠层永远停在 `opacity:0`（看不见又占着卡片）。
    - **v2.6.29 实测到上面这套分两步还不够**：刚 `append` 的元素在这一帧之前没被算过样式，浏览器没有"变化前样式"可比，rAF 里改 `opacity` 被当成初始值直接落到 1，过渡整体跳过（computed opacity 从 `1` 起步）。所以 `append` 之后必须先读一次 `getComputedStyle(el).opacity` 强制那次样式计算，把 `opacity:0` 钉成变化前的值，淡入才真的存在。**回归断言要同时盯住起始值 <0.9 和过程中出现 intermediate 值**，只断言起始值会把"延迟硬切"当成合格淡入放过去。
    - **关掉 `masonry_autoplay` 必须连已挂的叠帧一起收回**：`_mmAutoSetInterval(false)` 原来只 `clearInterval`，而 `_mmAutoTick` 在开关关闭时提前 return，回收用的 `_mmAutoSweep` 再也轮不到——实测关掉后 2 张非悬停卡片各剩 1 层叠帧，界面表现是"关了自动轮播，图停在最后一帧不动"，要等 hover 或重渲染才消失。现在关闭时把 `_autoPlayers` 里的卡片逐个 `_stopCarousel` 并清空登记，设置项即时生效分支（`state.cfg[key]=val` 那一支）直接调 `_mmAutoSetInterval(val!==false)`。
    - 自动轮播 `_mmAutoTick`（`_MM_AUTO_MS=3000`，设置项 `masonry_autoplay` 默认开）：**每拍只推进一张卡片**，按游标在"视口内 + 没被悬停 + 帧数 ≥2"的卡片间轮转，所以整屏是"隔几秒动一处"而不是同时闪；挑不到候选时催一次预取，下一拍就有得换。悬停会把该卡的播放器升级成它自己的 3 秒节拍（`st.auto=false` + 专属 `setInterval`），移出鼠标即 `_stopCarousel` 回到主封面；卡片滚出视口由 `_mmAutoSweep` 停播回收。**自动模式默认不解码视频帧**（一屏多路解码不划算，悬停时才播），但 v2.6.30 起视频帧自带首帧静图，有静图就以静帧身份参与换帧——否则视频多的模型在自动模式下整帧被跳过，卡片看着就是呆的。`document.hidden`、`prefers-reduced-motion: reduce`、瀑布流不在前台时一律不跑，避免后台偷拉流量。
    - 播放器状态从闭包提成 `_masonryCarousel` 里的对象（`{card, wrap, list, idx, cur, loading, seq, dead, timer, auto}`），`_autoPlayers` 另存一份可迭代登记（WeakMap 不能遍历）；`idx` 因此能跨拍续着走，不会每次从第 2 帧重来。
    - 回归：`tests/test_masonry_carousel.cjs` 覆盖"以透明插入并收敛到不透明、交叉期最多两层、鼠标不碰网格也会挂帧换帧、关掉 `masonry_autoplay` 即停"。
3e. **轮播用例自身的四条口径**（v2.6.29 一条条实测出来的，改用例时别退回旧写法）：
    - 先 `switchPage('models')` 再 `mmSetView('masonry')`：`index.html` 默认 active 标签是 download，`#page-models` 处于 `display:none` 时卡片全是零尺寸，`waitForSelector('#mmMasonry .ms-card')` 只会等到超时。
    - 判断"换帧了没有"要取 **DOM 里最后一张** `.ms-img-alt`：新帧是 `append` 上去的，淡入交叉期同时有旧新两张，`querySelector('.ms-img-alt')` 拿到的第一张是还没摘的旧帧——"约 3 秒/帧应至少换过一次帧"此前就是这样误报的（产品其实一直在换帧）。
    - 慢网/孤儿图用例的帧必须是**远程 URL**：`data:` 帧在 `_carouselAdvance` 走"本地 b64 立即可用"的直挂分支，根本不经过 `new Image()` 预载，"预载未回来前不该有叠层"用 `data:` 帧恒不成立；用完把 `coverCacheMulti` 还原回 `data:` 帧，后面的重新起播检查才拿得到帧。
    - 断言"整个网格叠层清零"的检查点必须先关自动轮播（自动节拍会在别的卡片上合法挂帧），用 `_mmAutoSetInterval(false)` 而不是只改 `state.cfg`——前者才是设置项走的同一条路。淡入探针的采样窗口要跨过 0.35s 过渡（≥30 帧），14 帧（≈0.23s）时"收敛到不透明"不可能成立。
4. 瀑布流封面筛选：扫描行带 `has_cover`（webui 扫描时 `find_cover || _images_dir_first` 判定，`get_scan_rows` 透传）；前端 `#mmCoverF` 下拉（全部封面/仅看有图·视频/仅看无图）在 `applyMmFilter` 过滤，`state.coverCache` 已命中路径作为有图兜底（自愈扫描后补下封面的滞后）。
5. 样式：.ms-img-alt、.thumb-video-badge、.iv-stage-video、.ms-img-wrap position:relative。

## 3. 兼容与不动

旧 info（无 type）行为不变；未动 model_manager/civitai_api/downloader/image_gallery；tests 355 + 桥接 43 全绿。

## 4. 存量数据迁移（用户操作）

- 旧 `.civitai.info` 无视频字段：详情面板点「从 C 站同步」重建 info 后才有视频预览与 20 张图。
- 被 mp4 写坏的 `.preview.png`/`image_*.jpg`：点「下载封面」/「下载示例图」重建（新版用首帧）。
- 新版下载的模型自动带全字段，无需手动处理。
