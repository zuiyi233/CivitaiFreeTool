# CivitaiFreeTool

> 免费的 Civitai / HuggingFace 模型下载与本地模型管理工作台。

![Version](https://img.shields.io/badge/version-2.6.28-blue) ![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-orange) ![CI](https://github.com/ADVICEsama/CivitaiFreeTool/workflows/tests/badge.svg)

[下载最新版](https://github.com/ADVICEsama/CivitaiFreeTool/releases/latest) · [更新日志](CHANGELOG.md) · [问题反馈](https://github.com/ADVICEsama/CivitaiFreeTool/issues) · [Linux / macOS](LINUX.md)

## 当前工作台

- **资源库（v2.6.27）**：把 ComfyUI output 的图片 / 视频索引成本地库，按类型 / 底模 / LoRA / 文件夹 / 标签 / 采样器 / 调度器 / 尺寸 / 时长筛选，可搜提示词、可播放视频、可收藏打标签，支持多选批量（收藏 / 加摘标签 / 送解析 / 删除，服务端分页取全 id、按 500 一片执行），模型引用能直接跳回模型库对应条目。纯 stdlib sqlite 索引 + 纯 Python 读 PNG 文本块、mp4 atom 与 webm/mkv 的 EBML（不依赖 ffmpeg），几万条目不卡；现代自定义节点工作流的提示词 / 底模 / 生成参数按字段名回收并如实标注来源，视频帧率按 `mdhd`+`stts` 计算；增量扫描，schema 升级即迁移不用删库重扫，收藏与标签可导出 JSON 再导入、库目录改名换盘符后自动重连；删除只回收媒体本体不动共用的工作流 `.json`。设置里填输出目录（支持多根与局域网共享路径，有自动探测）。详见 [版本说明](docs/releases/v2.6.27.md)。

- **文件夹显示 / 排除（v2.6.26）**：首次默认显示全部。点父文件夹会统一显示或排除整个子树，覆盖之前的子目录选择；再单独点子目录可覆盖父目录。只想显示 A/B：先排除 A，再显示 B。勾选 A 后排除 B：只排除 B 子树，其它内容保留。只筛选显示，不移动 / 删除文件；选择立即保存。

- **多文件下载安全**：正确列出 Diffusion Model 等权重变体，弹窗可见后才开始 10 秒默认选择；显式 fileId 不退回第一项。下载列表右键按任务 ID 移除，停止写入后清理所属 .part，模型/历史保留，退出后继续未完成清理，不扫除旧历史断点。

- **校验不一致文件手动关联**：下载管理可明确确认后把已完整落盘的文件关联到来源 C站模型，保存名称/版本/封面信息。真实与官方 SHA256 分别保留，任务、历史和详情持续警告；不修改模型、不把关联当作校验成功、不覆盖已有信息。

- **WeiLin 提示词兼容**：工作流分析与图片生成数据按实际采样器连接读取，支持 WeiLin 编辑器和链接文字，忽略禁用旧节点。负向条件清零明确说明；无法还原的外接触发词/随机输出不猜测。旧生成信息缓存自动重解析，已有原图缓存保留。

- **名称显示与可调滚动**：整卡悬浮时 C站名称与本地文件名紧接为一组，在底部资料上方垂直居中。C站名优先换行、放不下才省略；本地名空间够就完整换行，不够才单行滚动。作者、类型、版本及大小置底。外观设置可调速度和首尾停顿，默认 70px/秒、350/250 毫秒。短单行名字不动，屏幕外暂停，不改变卡片和列宽。
- **原生窗口恢复**：首次加载后核实窗口真的可见；重复打开按实例 PID 恢复窗口，不误认同名文件管理器，不擅自改浏览器模式。
- **瀑布流悬浮介绍**：设置 → 外观与布局可选常驻、封面悬停滑出或淡入；覆盖下方或整个卡片。深色半透明底 + 大号文字，亮色主题自动换亮底深字，本地文件名始终单行。
- **透明表格与宽屏适配**：原生 Mica 客户区材质可透过下载表格及斑马纹；更新页宽屏铺满、窄窗按需滚动，菜单说明不会被二级菜单盖住。

- **自由外观**：14 套配色、本地字体名称即预览、五档独立字号与图标尺寸、强调色、密度、圆角和局部画布交互特效；动画上限可输入 15–360，0 跟随屏幕（不保证达到指定帧率）。瀑布流按钮悬停展开竖向图片大小滑条。
- **局域网资产接入**：支持局域网共享/网络路径（SMB 映射盘符或 UNC，如 `\\192.168.1.10\models`），配合 `linux-server-setup/install-samba.sh` 一键搭建；目录不可达时扫描明确提示；缩略图本地缓存；设置页「测试连接」当场校验路径。

## 界面示例

下面是 **当前前端的演示截图**：使用虚构模型、文件路径及程序绘制的风景；不包含用户 API Key、私有模型或真实下载任务。它们用于展示界面，不代表下载或生成结果。

### 瀑布流：只看封面，悬停显示大号介绍

左侧卡片处于悬停状态，右侧只显示封面；截图为“淡入 + 整个卡片”，两种名称相邻成组、在上方剩余区域居中，空间足够时都完整换行；辅助资料置底。支持滑出 / 淡入、部分 / 整卡片覆盖，默认仍为常驻介绍。

![整卡名称空间分配](docs/screenshots/masonry-full-names.png)

![深色悬浮介绍](docs/screenshots/masonry-hover-dark.png)

![浅色悬浮介绍](docs/screenshots/masonry-hover-light.png)

### 工作流分析：大窗三栏 / 小窗报告

按内容区实际宽度切换，同一份结果不重新解析；配色继续跟随主题。节点详情为只读，不编辑工作流。

![工作流三栏](docs/screenshots/workflow-columns.png)

![工作流报告](docs/screenshots/workflow-report.png)

### 工作流完整画布

只读展示实际保存的节点与连接，点击节点查看参数；不是 ComfyUI 执行器。Forge 图片通常只有生成参数，界面明确说明不绘制虚假连线。

![只读节点画布](docs/screenshots/workflow-full.png)

### 下载任务布局与完成项管理

表格利用可用宽度，只有确实超宽才出现横向滚动；“移除已完成”不删除模型或下载历史。

![下载任务示例](docs/screenshots/download-queue-v2617.png)

### 多文件版本选择

默认弹窗选择；无人操作 10 秒选第一个，勾选后停止倒计时。取消不下载。扩展提交后也在软件内选择。

![多文件下载](docs/screenshots/download-file-selection.png)

### 模型管理与常驻详情

![模型管理工作台](docs/screenshots/models-workbench.png)

### 目录显隐与类型筛选

默认按更具体的子目录选择覆盖父目录选择；隐藏父目录也可以显示单独勾选的子目录。

![独立目录选择](docs/screenshots/model-folder-filter.png)

类型优先使用已有元数据，无元数据时只按明确的模型目录判断，不按文件大小猜测。

![模型类型筛选](docs/screenshots/model-type-filter.png)

### 模型列表三档大小

完整（最大）：

![完整列表](docs/screenshots/models-list-size-3.png)

紧凑（无封面与作者）：

![紧凑列表](docs/screenshots/models-list-size-2.png)

极简（精简名称，保留基础、版本等信息列；窄窗可横向滚动）：

![极简列表](docs/screenshots/models-list-size-1.png)

### 下载目录收藏与失效记录管理

右键只清理记录，不删除磁盘文件；胶囊中键取消收藏。

![目录记录菜单](docs/screenshots/folder-picker-records.png)

### 快捷键设置与底部状态胶囊

![对齐的快捷键与状态胶囊](docs/screenshots/shortcuts-aligned.png)

### 大图、半透明背景与生成数据

![图片与生成数据](docs/screenshots/image-generation-viewer.png)

### 下载历史

![独立下载历史](docs/screenshots/download-history.png)

### 外观设置

![外观设置](docs/screenshots/settings-appearance.png)

### 分类规则与使用介绍

![分类规则说明](docs/screenshots/settings-classification.png)

### 页面式引导

先在 civitai.red 申请并填写 API Key；空值不能继续或跳过，Key 先保存。之后保留六步结构中的页面介绍，七个页面都给出说明。点击页面卡片直接体验，引导缩到右下角，可随时继续。不会自动下载。

![开始与模型库](docs/screenshots/onboarding.png)

## 快速开始（Windows）

1. 在 [Releases](https://github.com/ADVICEsama/CivitaiFreeTool/releases/latest) 下载 Windows 便携 ZIP 或版本号 EXE。
2. 解压到个人可写目录，运行 `CivitaiFreeToolWeb.exe`。默认启动软件窗口，失败才按兜底开关打开浏览器。设置“启动界面”为浏览器后，以后直接启动浏览器，不创建软件窗口；选择即保存、重启生效。临时打开浏览器不会改长期模式。
3. 设置下载目录、模型管理目录；如需要，在 C站账户中申请 API Key 后填写。它不是使用本地模型管理的前提。
4. 粘贴 Civitai / HuggingFace 链接进行下载，或扫描已有模型。

**更新已有安装只替换 EXE，保留配置、任务、历史和缓存。** 新版默认把软件信息存放在 EXE 旁，可在「设置 → 目录」迁移到个人目录；不要把配置目录当作临时文件删除。

### 隐私与 Key

- `user_config.json` 可能包含 API Key、翻译密钥或代理信息，**不要上传或分享**。
- 配置、任务/历史 JSON、模型文件和运行缓存不属于发布源码/便携包。
- 示例截图与测试使用虚构数据。遇到问题提供日志时仍需先脱敏。
- 客户端访问 C站使用账户权限；不绕过付费、Early Access、浏览等级或访问限制。

## 下载与模型管理

- 多链接批量解析、断点续传、并发下载、暂停/重试；SHA256 校验，拒绝把 HTML 错误页面当成模型。
- 多目录扫描、关键词/底模/作者/状态/文件夹筛选、列宽调整及多种排序。
- 自定义改名、文件名 → C站名称、文件名翻译；按手动分类、tags 或自定义规则整理。
- 移动模型连同 sidecar/预览图/images 目录；冲突拒绝覆盖。历史路径同步更新，误整理可按日志恢复。
- 更新检测、版本选择、更新白名单；更新下载由用户触发，默认保留旧版。
- SHA256 反向识别模型、简介翻译、触发词复制、封面选择；解析 ComfyUI 工作流中的模型引用和提示词。

## 外观与快捷键

| 操作 | 行为 |
| --- | --- |
| Ctrl + 滚轮 | 全局界面缩放 |
| Alt + 滚轮（瀑布流） | 调整模型图片大小 |
| 图片查看器普通滚轮 | 缩放当前图片 |
| 按住放大后的图片拖动 | 移动查看位置 |
| 图片查看器空白处 / Escape | 关闭大图；有右键菜单时 Escape 先关闭菜单 |
| 双击窗口拖动区 | 最大化 / 还原 |

Mica / Mica Alt 是 Windows 原生**壁纸色调材质**，并不透视其他窗口；失焦、系统关闭透明、高对比度等条件下会回退纯色。内置效果需要支持的 Windows 11；外部模式配合 Mica For Everyone 需要另行安装/配置，框架切换需重启。参见 [微软 Mica 说明](https://learn.microsoft.com/en-us/windows/apps/design/style/mica)。

## v2.6.25：批量标签页与反查指引

- Chrome / Firefox 扩展更新到 **1.3.0**：保留当前页发送，新增所有窗口 / 当前窗口的模型页勾选列表、搜索、全选和批量发送；跨 red/com 重复页合并，不同版本与文件 ID 保留。提交由扩展后台执行，关闭小窗不会中断；仅向本机 CFT 发送勾选的模型链接，不发送其它标签页、Cookie 或 API Key。
- **未填写并保存 Civitai API Key** 时，批量下载、下载管理、检查更新、反向解析显示半透明遮罩及账号配置指引。模型管理中的识别入口与扩展下载入口同样拒绝开始新的在线任务。本地模型浏览、移动和工作流分析可继续使用；不会删除模型或历史，也不强停已经运行的下载。
- 反向解析提供五步教程：**模型管理 → 全选 → 信息处理 → 识别模型信息 → 开始反查**。教程只指导，用户点击才会提交和反查。
- 反查列表支持行勾选、仅显示/反查失败、反查选中和移除选中。筛选后不会误操作隐藏勾选；移除仅清列表，不删磁盘文件，进行中的记录保留。
- 反查与下载历史表格默认随窗口铺满，保留手动调列宽；自定义列宽过大时允许横向滚动。
- 工作流分析识别 **LoRA Manager 的 `loras.__value__`**（含无后缀模型名 / JSON 字符串 / UI 数组）。启用模型加入引用，关闭或旁路模型单独显示，不把注释或旧 UI 状态当作实际引用；本地匹配覆盖全部已配置模型目录。

以下为隔离测试生成的示例界面，不包含用户 Key 或真实下载记录：

![标签页批量发送](docs/screenshots/extension-batch.png)
![API Key 配置指引](docs/screenshots/api-key-gate.png)
![反查列表选择](docs/screenshots/reverse-selection.png)

## Chrome / Firefox 扩展

Release 同时附带两款扩展 ZIP，`manifest.json` 在压缩包根目录。

- **Chrome / Edge**：解压 Chrome 包，在扩展管理页启用开发者模式，加载已解压目录。
- **Firefox**：当前包未签名，通过 `about:debugging` 临时加载；长期安装需要 Mozilla 签名。不能把它当作已签名永久扩展。
- 软件运行后，点击扩展图标，发送当前页，或切换“批量标签页”后全选 / 勾选并发送；当前页不是模型也能提交其它已打开的模型页。

详细说明：[Chrome](chrome-extension/README.md) · [Firefox](firefox-extension/README.md)。

## 源码运行与构建

Windows 使用 Python 3.12 与现有 `requirements.txt`：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python main_web.py --window
# 构建：Spec 包含网页、原生辅助 DLL、托盘及 Tcl/Tk
.\.venv\Scripts\python -m pip install pyinstaller
.\.venv\Scripts\python -m PyInstaller -y CivitaiFreeToolWeb.spec
```

原生辅助代码由 Windows 自带 .NET Framework C# 编译器构建，不需要额外安装完整 Visual Studio。Linux/macOS 使用社区提供的适配，安装依赖与限制见 [LINUX.md](LINUX.md)；本轮原生窗口验证在 Windows 完成，不声称其他平台已复测。

## 测试、贡献与维护

- 后端/桥服务回归与隔离前端测试位于 `tests/`；需要真实窗口的改动另做原生检查。
- GitHub Actions 的状态以实际运行结果为准，版本号/本地测试通过不等于远端 CI 已通过。
- 修复后发布包含 EXE、Windows ZIP、源码 ZIP、两款扩展和 SHA256 的完整附件，见 [发布流程](docs/RELEASING.md)。
- 感谢 [guanhaisen](https://github.com/guanhaisen)、[LckHot](https://github.com/LckHot) 等社区贡献；本次 [Fork 检查](docs/FORKS.md) 说明哪些分支存在独立变更及检查范围。

详细历史请看 [CHANGELOG.md](CHANGELOG.md)，不再用旧版 Emoji/双主题截图代表当前工作台。

## 反馈

[GitHub Issues](https://github.com/ADVICEsama/CivitaiFreeTool/issues) · [作者 B 站](https://space.bilibili.com/273101122) · 粉丝群：909810278

### 校验不一致文件的手动关联

以下为合成文件与虚构模型示例。确认只关联来源信息，不代表该文件与官方模型相同。

![手动关联确认示例](docs/screenshots/download-unverified-binding.png)

### 大模型多格式选择

合成例图：仅勾选 bf16；未显示弹窗时不启动倒计时，勾选后停止自动选择。

![多格式权重选择](docs/screenshots/diffusion-file-choice.png)
