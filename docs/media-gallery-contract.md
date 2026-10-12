# 多图展示 + 视频预览 — 跨模块契约（v1）

> 已实施完成，留档供后续维护。目标：模型详情/瀑布流支持 C 站视频预览（mp4 + 首帧海报）与全量多图展示。

## 0. images 数组规范（数据真相源）

C 站 `modelVersions[].images` 每项：`{url, type: 'image'|'video', imageUrl(视频首帧图，仅 video 有), width, height, nsfw}`。
`.civitai.info` 的 images 保留以上字段（type 缺省为 None，消费方按"非 video"兜底）；截断 [:20]。

## 1. Python 侧（已实施：reverse_parse.py + webui.py）

1. `reverse_parse.build_info`：images 保留 `{url,type,width,height,nsfw}` + 条件 `imageUrl`，[:5]→[:20]。
2. `get_model_detail`：video 项 → `{type:'video', url:<mp4>, poster:<imageUrl>, prompt, negative, orig_url}`；image/未知 → 原结构 + `type:'image'`；mp4 绝不进 get_cover_b64/PIL。
3. `download_all_images`：video 项存首帧 imageUrl 为 image_%02d.jpg，不落 mp4。
4. `_pick_cover_url(imgs)`（新，webui.py 约 59-74）：封面 URL 选择——非 video 优先，全 video 用首帧；应用于 `_handle_dl_done` 封面段与 `mm_download_covers`。
5. `get_covers`：find_cover 未命中回退 `<base>.images/` 首图（`_images_dir_first`）。
6. `get_covers_multi(paths, size=220, n=3)`：`{path: [b64,...]}`，封面 + .images/ 前 n-1 张；与 get_covers 共享缓存（seq0 共享，i>0 内存 key 加序号、磁盘文件名加 _%d）。
7. get_gallery_image 对 video 项防御性返回错误 dict（已确认现有 try/except 覆盖）。

## 2. 前端（已实施：web/app.js + web/image-viewer.js + style.css + workbench.css）

1. 详情画廊：video 缩略图 = poster b64 + ▶ 角标（.thumb-video-badge/.detail-thumb-box）；主图 = `<video controls preload="metadata" src=mp4 poster>`；切走/关详情 pause（loadB64 分支、closeDetail）。
2. image-viewer：video 项渲染 `.iv-stage-video`（controls autoplay），跳过全部 b64/get_gallery_image 路径；索引切换复用；切回图片/关闭时 pause + 清理残留（修了视频残留播放 bug）。
3. 瀑布流轮播（`_startCarousel`，间隔 `_MM_CAROUSEL_MS=3000`）：hover 启动，从第 2 帧起按 ~3 秒/帧循环回卷；每帧挂载成功即 `rearm()` 重排定时器（驻留均匀，不受预载耗时影响）。帧类型：本地 dataURI 直挂；远程 https 图先 `new Image()` 预载、成功才叠 `.ms-img-alt`（防空白），失败/6s 超时记入会话级 `dead` 集合后续直接跳过（不递归重试）；video 帧静音循环播放、按同样节奏切帧。`_masonryCarousel` 存 `{timer, cur}` 且 `cur` 随挂载回写——移出卡片 `_stopCarousel` 清叠层 + 暂停视频。帧来源 `get_covers_multi`：`[[src, isVideo], ...]`，src 为 dataURI/https/file URL。**卡片 hover 轮播不点击不发新请求（预载除外），点击进详情播放**。
3b. **预取必须绑在真正的滚动容器上**（v2.6.27 后修的哑火缺陷）：页面滚的是 `main.content`——`html`/`body` 与 `#mmMasonry` 自身都不滚（`scrollHeight == clientHeight`），而元素上的 `scroll` 事件**不冒泡**。旧写法把监听挂在 window 冒泡阶段和 `#mmMasonry` 上，两处一次都没触发过：首屏渲染时瀑布流还在折线以下（`_visibleMasonryCards()` 返回 0）→ 一帧都不请求，之后滚动也不补 → `coverCacheMulti` 恒空 → hover 无帧可轮，且预取失败被静默 catch，界面上连报错都没有。现在改成 `window.addEventListener("scroll", _onMmScroll, {passive:true, capture:true})`（捕获阶段能收到任意容器的滚动），并在 hover 命中未预取的卡片时就地补一次请求（`_multiTried` 去重，同一模型一次会话只问一次；请求失败的路径放回待请求，下次还能补）。轮播心跳改在 `_startCarousel` 入口就 `setInterval`，不再只在挂载成功后 `rearm`——旧写法首帧预载一失败就再也没有下一拍，永久停摆。回归用例：`tests/test_masonry_carousel.cjs`（真实 Chromium + mock 桥，验"滚动会驱动预取 / hover 起播 / ~3 秒换帧 / 同时只有一层叠帧 / 移开清理干净"）。
4. 瀑布流封面筛选：扫描行带 `has_cover`（webui 扫描时 `find_cover || _images_dir_first` 判定，`get_scan_rows` 透传）；前端 `#mmCoverF` 下拉（全部封面/仅看有图·视频/仅看无图）在 `applyMmFilter` 过滤，`state.coverCache` 已命中路径作为有图兜底（自愈扫描后补下封面的滞后）。
5. 样式：.ms-img-alt、.thumb-video-badge、.iv-stage-video、.ms-img-wrap position:relative。

## 3. 兼容与不动

旧 info（无 type）行为不变；未动 model_manager/civitai_api/downloader/image_gallery；tests 355 + 桥接 43 全绿。

## 4. 存量数据迁移（用户操作）

- 旧 `.civitai.info` 无视频字段：详情面板点「从 C 站同步」重建 info 后才有视频预览与 20 张图。
- 被 mp4 写坏的 `.preview.png`/`image_*.jpg`：点「下载封面」/「下载示例图」重建（新版用首帧）。
- 新版下载的模型自动带全字段，无需手动处理。
