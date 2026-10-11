# Chrome 扩展「归档助手」跨模块契约（v1）

> 所有参与开发的代理必须遵守本契约。 Phase 1-4（脚本功能吸收进扩展）统一按此实现。
> 参考源：`D:\油猴脚本\Civitai一键下载模型并归档.user.js`（下称"脚本"）。

## 0. 目录结构

```
chrome-extension/
  manifest.json            # D 负责：权限 MV3
  background.js            # D 负责：SW 消息路由 + offscreen 管理 + 写盘通道 + bridge 客户端
  popup.html/.js/.css      # D 负责：保留现有"发软件下载" + 新增一键归档入口
  offscreen/
    archive-worker.html    # D 负责（壳）
    archive-worker.js      # B 负责：归档编排主流程
  content/
    model-page.js          # C 负责：模型页交互（按钮/弹窗/DOM 提取）
    image-badge.js         # C 负责：图片流/帖子页 SAVE 徽章（Phase 3）
    content.css            # C 负责：徽章/按钮样式
  lib/                     # A 负责：纯逻辑，从脚本搬运适配
    util.js  civitai-api.js  scheduler.js  idb.js  history.js  config.js  log.js
```

## 1. 模块加载方式（无构建，硬性约束）

- `manifest.json`：`"background": {"service_worker": "background.js", "type": "module"}`。
- offscreen：`archive-worker.html` 内 `<script type="module" src="archive-worker.js">`。
- **lib/*.js 一律禁止 `export` / `import` 语句**（content script 无法用 module）。所有 lib
  文件把公开 API 挂到 `globalThis.CFT.<ns>`（如 `CFT.util.safeFsName`）。
  module 环境（SW / offscreen）通过 `import './lib/util.js'` 的副作用获得全局 `CFT`。
- content scripts 在 manifest 中按序注入 `lib/util.js` → `content/xxx.js`，直接使用 `CFT`。

## 2. 消息协议（chrome.runtime.sendMessage）

信封：`{__cft: true, to: 'bg'|'offscreen'|'content', type: string, payload: object}`。
所有 onMessage 监听器必须先检查 `to` 字段，非本模块的消息直接 `return`（不占用 sendResponse）。
异步响应必须 `return true` 保持通道。路由规则：
- `to:'offscreen'` 的消息由 offscreen archive-worker 处理（bg 不转发，runtime 消息本身广播）；
- `to:'bg'` 由 background 处理；`to:'content'` 由 content 处理。

### 消息清单

| from → to | type | payload → 响应 | 阶段 |
|---|---|---|---|
| popup/content → bg | `startArchive` | `{tabId}` → 无（bg 指挥 content 采集） | P1 |
| bg → content | collectPage | bg 经 tabs.sendMessage 主动采集，envelope 应答 `{modelId, modelVersionId, aboutText, pageUrl}` | P1 |
| bg → offscreen | `archive` | `{modelId, modelVersionId, aboutText, pageUrl, tabId}` → 流式进度 | P1 |
| offscreen → bg | `writeFile` | `{relPath, dataB64, overwrite}` → `{ok, saved, channel, msg}` | P1 |
| offscreen → bg | `getArchiveConfig` | → `{softwareOnline, archiveRoot, channel}` | P1 |
| offscreen → bg | `sendModelToSoftware` | `{downloadUrl, pageUrl, destSubdir}` → `{ok, msg}`（POST 软件桥 /api/download，body `{url, dest_subdir}`；dest_subdir=归档子目录，模型文件落 <archive_root>/<destSubdir>/） | P1 |
| offscreen → content | `progress` | `{percent, text, level}` | P1 |
| offscreen → content | `archiveDone` | `{ok, dirName, msg, errors:[]}` | P1 |
| offscreen → content | `askFileSelection` | `{files:[{name,size,type}]}` → `{fileName}` | P2 |
| offscreen → content | `askArchiveConfirm` | `{record}` → `{proceed}` | P3 |
| offscreen → content | `requestDomComments` | → `{results:[{user,content,createdAt,reply,fromDom}]}`（结构化数组，不做文本拼接） | P2 |
| offscreen → bg | `checkLibrary` | `{versionId}` → `{found, paths}` | P4 |
| offscreen → bg | `notifyArchiveDone` | `{dirName}` → `{ok}`（通知软件重扫） | P4 |
| content → offscreen | `userSelection` | `{fileName}`（对 askFileSelection 的答复） | P2 |
| content → offscreen | `archiveConfirm` | `{proceed}` | P3 |
| content → offscreen | `domComments` | `{cid, results:[...]}`（同上结构；offscreen 并入 trpc 结果走统一 formatter） | P2 |
| content → bg | `saveSingleImage` | `{imageId, originalUrl, ext}` → `{ok}` | P3 |
| offscreen → content | `savedImageDone` | `{ok, imageId}`（bg 经 tabs.sendMessage 广播到所有 images/posts 页，刷新徽章 SAVED） | P3 |

注意：content 页面可能被刷新/关闭，offscreen 对 content 的每个"请求-答复"消息都要有
超时兜底（15s），超时按用户取消处理。

## 3. 写盘通道（background 统一实现，offscreen 只发 writeFile）

目录结构（相对归档根）：

```
<safeFsName(模型名_版本)>/
  模型名.txt          # 模型信息（访问地址/关于此版本/介绍/其他参数）
  example.json        # {triggerWord, samples:[{prompt,negativePrompt,sampler,cfgScale,steps,Size}]}   [P2]
  评论.txt            # fetchCivitaiComments + fetchCivitaiReviews 拼接                        [P2]
  <模型名_版本>.jpg|mp4   # 封面（视频封面用 .mp4）                                              [P1]
  配图_N.jpg / 视频_N.mp4  #                                                                    [P2]
Images/<文件前缀>_<imageId>.jpg|.json   # SAVE 徽章单图归档                                     [P3]
_civitai_archive_history.json           # 历史镜像（导入导出格式）                               [P3]
```

通道选择（bg 的 writeFile 实现顺序）：
1. 软件在线且 `archive_root` 非空 → `POST http://127.0.0.1:47531/api/archive/file`
   `{rel_path, data_b64}`（原始大小 ≤ 32MB）。
2. 否则降级 `chrome.downloads.download({url: dataUrl, filename: 'CivitaiArchive/'+relPath,
   saveAs:false, conflictAction:'overwrite'})`；原始大小 > 20MB 拒绝，返回
   `{ok:false, msg:'文件过大且软件未在线'}`。
3. 响应统一 `{ok, saved: 'software'|'downloads'|'', channel, msg}`（`saved:''` 仅出现在失败响应；无 'skipped' 状态）。
4. 模型文件本体不经 writeFile：offscreen 发 `sendModelToSoftware {downloadUrl, pageUrl, destSubdir}`，
   bg POST `/api/download` body `{url, dest_subdir}`，由软件下载到 `<archive_root>/<dest_subdir>/`，
   与小文件同一归档目录闭环。

## 4. Python 侧（browser_bridge.py + webui.py + web/app.js）

- `GET /api/archive/config` → `{ok, archive_root, download_dir}`（archive_root = cfg 的
  `archive_root`，未设置则回落 `download_dir`；两者都空则 `archive_root=""`）。
- `POST /api/archive/file` body `{rel_path, data_b64}` → `{ok, path}`。安全：rel_path 用
  os.path.normpath 校验不得逃逸根目录（拒绝 `..`、绝对路径、盘符）；根目录未配置返回
  `{ok:false, msg:'归档根目录未设置'}`。
- `POST /api/archive/done` body `{dir_name}` → `{ok}`：起 daemon 线程触发模型库后台重扫（立即返回，
  不阻塞请求线程；尽力而为）。
- `GET /api/library/check?version_id=` → `{ok, found, paths:[...]}`：在模型库扫描缓存/目录中
  匹配 `.civitai.info`/`.json` 的 `version_id` 字段。
- browser_bridge.py 保持既有风格：handler 注册制（新增
  `set_archive_handlers(config_fn, write_fn, done_fn, check_fn)`），bridge 自身不 import webui；
  静默失败、CORS 白名单、origin 校验不变；Content-Length 上限放宽到 128MB（仅
  /api/archive/file）。
- 设置页（web/app.js 数据驱动设置项，见 2079-2160 区段）新增「归档根目录」行，key=
  `archive_root`，用现有 pick_dir 对话框模式。
- 不改动：downloader.py / civitai_api.py / gui.py / 模型管理其余功能。

## 5. 归档历史（Phase 3，lib/history.js）

- IndexedDB：库名 `civitai_archive_assistant`，store `history`，keyPath 无（key =
  `"${modelId}@${modelVersionId}"` 手动指定）。记录字段：
  `{modelId, modelVersionId, modelName, modelVer, dirName, archiveTime(ISO),
  modelFile, commentCount, imgCount}`。
- API：`CFT.history.all()` / `.get(modelId, versionId)` / `.put(record)` /
  `.delete(key)` / `.search(keyword)` / `.exportJson()` / `.importJson(json, merge)` /
  `.mirrorFile(writeFileFn)`（把全量历史写 `_civitai_archive_history.json`）。
- 已归档检测：`findArchivedRecord(records, modelId, modelVersionId)` 逻辑与脚本一致。

## 6. 插件配置（lib/config.js）

```js
CONFIG_DEFAULTS = {
  coverSaveMode: 'video',     // 'image' | 'video'
  downloadImages: true,
  downloadVideos: true,
  sendModelToSoftware: true,  // 模型文件本体转发软件下载（永远建议 true）
  maxPics: 100,
  badgeEnabled: true,
  globalConcurrency: 4,       // 写盘通道并发（chrome.downloads / 软件写盘）
  fallbackDirPrefix: 'CivitaiArchive'  // 降级 downloads 时的子目录前缀
}
```
存取走 `chrome.storage.local`，API：`CFT.config.load()` / `.save(patch)` / `.get()`。

## 7. 验证基线

- 所有 JS：`node --check <file>` 通过。
- Python：`python -m py_compile` 通过；不改坏现有单测（`python -m pytest` / unittest）。
- 提交前每个代理自检自己文件；跨文件集成问题由主控统一修。
