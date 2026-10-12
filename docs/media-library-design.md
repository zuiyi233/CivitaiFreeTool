# ComfyUI 产物资源库（生成媒体库）设计方案 v1

> 目标：把 ComfyUI 产出的图片/视频纳入本程序，形成与模型库并列的资源库——按类型/底模/文件夹分类、可打标签、可搜索，复用现有瀑布流/查看器/解析管线。

## 0. 参考项目（已克隆到 `reference/`，.gitignore 不入库）

| 项目 | 体积 | 参考价值 |
|---|---|---|
| TokenSpender/ComfyUI-Sidebar-Gallery | 2.3M | **SQLite 索引架构**：`media_files`（mtime/missing_since 软删除）+ `paths/vals/facts` EAV 事实索引分面检索；增量扫描对账 |
| LuqP2/Image-MetaHub | 23M | **产品形态最接近**（本地优先 AI 媒体管理器）：库网格 + Explore 分面（checkpoint/LoRA/sampler/scheduler include-exclude）+ 标签/收藏/自动标签 + 文件夹树 + Group By；`ARCHITECTURE.md` 是现成的产品需求 |
| TagStudioDev/TagStudio | 26M | **标签体系**：Tag / Namespace / Alias / TagParent（父子标签）/ Entry（SQLAlchemy models.py） |
| PanicTitan/ComfyUI-Gallery | 21M | 实时画廊节点：视频/GIF/音频/3D 卡片展示细节 |

## 1. 复用现状（不重造轮子）

- `comfy_metadata.extract_prompts / extract_model_references`：已能从 PNG 的 prompt/workflow 提取提示词与模型引用（checkpoint/LoRA 文件名）——底模/LoRA 分面的数据源直接复用。
- `webui.py` 工作流分析管线（~2600 行区域）：PNG/webp 元数据读取、workflow JSON 解析、预览图生成。
- `image_gallery.py` + `gallery_cache`：缓存管线模式。
- 前端：瀑布流卡片/3s 轮播/图片查看器（已支持 mp4 播放）/详情面板，全部可复用到新页面。

## 2. 元数据来源（技术要点）

- 图片：PNG `tEXt/iTXt` 块内嵌 `prompt`/`workflow` JSON（现有管线已解析）。
- 视频：ComfyUI 核心 SaveVideo 与 VHS_VideoCombine 把 `{"prompt":…,"workflow":…}` 写进 mp4 容器/流的 **comment（或 description）标签**，或独立 `prompt`/`workflow` 标签。纯 Python mp4 atom 解析（moov→udta，约百行）即可读取，**不引入 PyAV/ffmpeg**；参考 Sidebar-Gallery `_extract_embedded_container_meta` 的兼容矩阵（comment/description、容器级/流级、合并 JSON/独立标签）。
- 兜底：无内嵌元数据时找同名 `.json` sidecar；再无则标 `no_meta`（仍入库展示，仅不能按底模分类）。

## 3. 数据层（新增 stdlib sqlite，数据根目录 `media_library.db`）

- `items(id, path UNIQUE, root_id, relpath, filename, ext, kind image|video, size, mtime, missing_since, added_at, checkpoint, loras_json, has_meta)`
- `item_meta(item_id, key, value_json)`——prompt/negative/sampler/scheduler/尺寸/时长等低频字段
- `tags(id, name UNIQUE, color, parent_id)` + `item_tags(item_id, tag_id)`——TagStudio 简化版（父子标签保留，别名/命名空间缓行）
- 文件夹分类不建表：按 `relpath` 目录段动态分组 + 排除目录配置（PhotoPrism 目录索引思路）
- 规模假设 1w–10w 文件；关键列（kind/checkpoint）建索引即可，EAV 分面索引（facts 表）留作二期

## 4. 扫描管线

- 配置：ComfyUI output 目录（可多根），默认探测 `ComfyUI/output`；沿用模型扫描根的状态管理模式。
- 增量对账：walk → 与 items 表比对 mtime+size，变化才重解析；新文件解析入库；消失文件标 `missing_since`，确认期后清除。
- 缩略图：图片 PIL 生成（复用 thumb_cache 模式）；视频海报帧由前端 `<video>`+canvas 抓帧回传缓存（避免后端 ffmpeg 依赖）。
- 触发：手动扫描按钮 + 打开页面时后台增量（与 `mm_scan_state` 同模式，不阻塞 UI）。

## 5. 前端（新页面 `data-page="library"` 资源库）

- 网格：复用瀑布流卡片 + 3s 轮播 + 查看器（视频直接播）。
- 筛选侧栏：类型（图/视频）、底模 checkpoint 分面、LoRA 分面、文件夹树、标签、收藏、无元数据。
- 顶栏：关键词（prompt 全文，二期 FTS5）、排序（时间/大小/名称）、分组（日期/底模/文件夹）。
- 卡片操作：打标签、收藏、打开所在文件夹、送工作流分析/逆向解析、删除。
- 详情：大图/视频 + 全部元数据；模型引用可点击跳转模型库对应条目。

## 6. 改动面三案

- **方案 A（推荐）**：独立 sqlite + 独立"资源库"页，不动模型库。新增 `media_library.py`（扫描+DB，一个文件）；`webui.py` 注册 ~10 个 RPC；`app.js` 增 library 段；`index.html` 新页；少量 CSS。模型库零风险，符合单一职责。
- 方案 B：生成物并入模型页瀑布流混排。改动小，但概念混杂、行结构冲突（模型行 vs 媒体行），不推荐。
- 方案 C：独立程序照抄 Image-MetaHub（Electron+React 全套）。工作量最大且与现有 UI/打包割裂，不推荐。

## 7. 分期

- P1：扫描 + 网格 + 类型/文件夹/底模筛选 + 详情/查看器 + 视频播放。
- P2：标签 CRUD + 按标签筛选 + 收藏。
- P3：自动标签（底模/LoRA 即标签）、FTS5 全文、分组视图、卡片跳工作流分析。
