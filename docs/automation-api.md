# 桌面桥自动化接口（AI / 脚本调试用）

> 目的：把「必须点界面/看截图/自己轮询」的事情变成一条 HTTP 调用，减少人工与 token 消耗。
> 全部接口只监听 `127.0.0.1:47531`；无 `Origin` 的本机脚本放行，浏览器来源仍按原白名单校验。
> 实现分两层：语义化端点（本文）与长尾方法（`POST /api/rpc`，160+ 个，见 `/api/rpc/methods`）。

## 0. 先读能力清单

```bash
curl http://127.0.0.1:47531/api/automation
```

返回端点表、可写配置键、任务状态词表、数据目录、RPC 方法数量。**这是给 AI 的自描述入口**，
字段与本文一致；新增端点会同步出现在这里。

### `GET /api/automation/schema`（自描述）

AI 不必猜配置值形状、也不必读源码：

- `config`：每个可写键的 `type`（bool/int/str/path/enum）、`options`、`default`、`current`
  （密钥显示 `***`）、`desc`。
- `task_states` / `task_actions` / `caps`：词表。
- `library_ops`：模型管理动作清单（见 §5）。
- `routing`：`core_folders` 与 `type_to_folder`（Civitai 类型 → ComfyUI 目录，**唯一事实源**
  是 `model_routing`，这里是它的投影）。
- `sidecars`：落点校验期望的附属文件与「算封面的后缀集合」。

## 1. 观测

### `GET /api/automation/state`
版本、关键配置、最近 20 条任务（含 `routed_dir` / `route_reason`）、待选文件、界面状态。

### `GET /api/automation/tasks`
参数：`ids`(csv)、`status`(csv)、`limit`、`since`(ms)、`wait=1`、`timeout`(秒，默认 300，上限 1800)。

- 不传 `wait`：立即返回快照。
- `wait=1` + `ids`：阻塞到这些任务全部进入终态（done/error/canceled）。
- `wait=1` 无 `ids`：阻塞到队列里没有 pending/downloading。
- 返回 `{ok, tasks, count, waited_s, timed_out}` —— `timed_out=true` 表示到点仍未终态，
  不是失败，按需再查。

### `GET /api/automation/extension`
浏览器扩展通道状态：`download_requests`、`archive_files_written`、`archive_done_calls`、
最近一次提交的 `origin/caps/url/age_s`、当前有效 `active_bundle_tokens`。
用来回答「插件到底连没连上、有没有真的提交」。

### `GET /api/automation/config`
全量配置（`api_key` / `baidu_key` / `baidu_appid` 打码）+ 数据目录信息 + 可写键列表。

### `GET /api/automation/logs`
参数：`source=error|events|both`（默认 both）、`tail`（默认 80）。
`error_log` 给 `error.log` 尾部；`events` 是本层最近 200 次调用（方法/路径/状态码/耗时），
用来确认「我的请求到底到没到、耗时多少」。

### `GET /api/automation/events`
增量事件流：`since`（上次返回的 `next_since`）、`limit`。每条事件带自增 `seq`。
比 `logs?source=events` 更适合轮询——只拿新增的那几条，不重复读全量。

### `GET /api/automation/history`
下载历史（已完成/失败/取消，倒序）。参数：`limit`、`status`、`detail=1`。
默认返回精简字段（id/filename/status/dest_dir/routed_dir/file_path/file_exists/total/error/finished_at）；
`detail=1` 返回原始行。用于回答「上次那个下完了吗、文件还在不在」。

### `GET /api/automation/prompts`
参数：`path`（模型文件绝对路径）、`limit`。直接读 `.civitai.info` 的 `images[].meta`，返回
`{index, type, url, width, height, nsfw, prompt, negative_prompt, params:{steps, sampler, seed, ...}}`。
AI 复现示例图时不用再手撸元数据解析。没有 info 时给 `hint` 指向 `library/op {op:"verify"}`。

### `GET /api/automation/connectivity`
连通性体检，一条请求区分「网络问题」与「代码问题」：

- `dns`：`civitai.red`、`b2.civitai.com`（模型文件 CDN）、`image.civitai.com` 的解析结果，
  带 `suspicious`（命中 Facebook 网段 / `face:b00c` 等污染特征）。
- `api`：公开 API 基址与实际请求耗时（跟随 `site_domain`）。
- `proxy`：应用配置的代理；`system_proxy`：Windows 系统代理（WinINET）。
- `warnings`：**系统代理开着但应用没开**、疑似 DNS 污染等可直接行动的提示。

> 实测教训：浏览器走系统代理、Python/urllib 默认不走。系统代理开着而应用没开时，
> 表现是「浏览器能下、桌面端一律 10060 超时」，很容易被误判成代码 bug。

### `GET /api/automation/library`
参数：`q`、`type`、`limit`、`offset`。返回已解析成对象的模型行（path/name/type/ver/verId/modelId…）
与当前扫描状态。底层 `get_scan_rows()` 原本返回 JSON 字符串，这里已解析。

### `GET /api/automation/model?path=…`
单个模型的完整详情（info + 画廊，底层 `get_model_detail` 的 JSON 字符串已解析）+
该文件/目录的附属文件清单（`.civitai.info`/`.json`/`.preview.png`/`_评论.txt`/`.images`/
扩展产物）。文件不存在返回 404。

### `POST /api/automation/search`
C 站公开模型搜索（只读，`limit` 夹在 1–20，站点根跟随 `site_domain`）。

```json
{"query": "rezero", "types": "LORA", "base_models": "PonyXL", "sort": "Highest Rated",
 "period": "Month", "nsfw": false, "limit": 10}
```

返回 `{ok, total, items:[{id,name,type,nsfw,stats,versions:[{id,name,baseModel}]}]}`。
用于「先找到模型再决定下哪个」，不必让 AI 自己拼 C 站 URL。

## 2. 计划（干跑，不写入）

### `POST /api/automation/plan`

```json
{"url": "https://civitai.red/models/2949009?modelVersionId=3398520", "file_id": null, "probe": false}
```

回答：模型/版本信息、每个文件的名称与体积、**会落进哪个目录**、分流原因、
`exists`（目标文件已在盘上）、`queued`（已在下载队列）、`in_library`（模型库已有该版本）。
`probe=true` 才做 safetensors 头嗅探（两次 Range 请求），用于 Checkpoint/Model 这类
`checkpoints` 与 `diffusion_models` 分不清的情况。

**不建目录、不入队、不下载。**

### `POST /api/automation/resolve`
参数：`url` 或 `model_id`(+`version_id`)。比 `plan` 多返回 `versions`（全部版本的 id/名称/底模/
发布时间/文件数/主文件体积），用来**先挑版本再下载**。其余字段与 `plan` 相同，同样只读。

## 3. 执行与等待

### `POST /api/automation/download`

```json
{"url": "...", "dest_subdir": "", "caps": ["bundle"], "wait": true, "timeout": 900, "verify": true}
```

也接受这些等价写法：

- `{"model_id": 2949009, "version_id": 3398520}`：由 `site_domain` 拼出模型页链接。
- `{"url": "...", "file_ids": [1, 2]}`：同一版本只下指定文件（每个文件一条 URL，带 `fileId=`）。
- `{"urls": ["...", "..."]}`：批量提交（`file_ids` 不能与 `urls` 同用）。

- `caps` 缺省就是 `["bundle"]`：自动化默认开启类型分流，本体与附属文件才会进同一个目录。
- `wait=true`（默认）：阻塞到本次提交产生的**全部**新任务进入终态，返回 `task`（单条）或
  `items`（多条，每条含 `submit`/`task`/`verify`）；`timed_out=true` 表示到点未完成。
- `verify=true`（默认）：完成后再跑一次落点校验（文件/附属文件）。

单条返回 `{ok, submit, task, verify, waited_s, timed_out}`，批量返回
`{ok, items, count, waited_s, timed_out}`。**一次调用覆盖「提交→等完→校验」。**

### `POST /api/automation/tasks`
批量控制：`{"action": "pause|resume|retry|start|remove|archive", "ids": ["..."], "all": false}`。
逐项返回 `{id, ok, result|error}`；`all:true` 只支持 pause/resume/start/remove。

### `POST /api/automation/file-choice`
回答多文件选择（自动化最常见的阻塞点）：

```json
{"identity": "state.pending_file_choice.id", "indices": [0], "pause": false}
```

`indices` 按 `pending_file_choice.files[].index` 取；传 `{"ack": true}` 表示「界面已经展示过」，
用于启动默认超时。没有待选时返回 400 并说明。

### `POST /api/automation/library/scan`
触发模型扫描，`wait=true`（默认）等到扫描结束，返回扫描状态。

### `POST /api/automation/library/op`
模型管理动作的统一入口，`op` 取以下值（`GET /api/automation/schema` 的 `library_ops` 同源）：

| op | 作用 | 备注 |
| --- | --- | --- |
| `verify` | 校验哈希 + 补全 C 站元数据 | 会发网络请求 |
| `dedupe` | SHA256 查重（只算不删） | 结果在 `progress.result` |
| `dedupe-delete` | 重复文件移入回收站 | **必须 `confirm:true` 且给 `paths`** |
| `localize` | 汉化文件名 | 需要百度翻译密钥 |
| `gen-json` | 生成 WebUI 可读 json | `overwrite:true` 覆盖已有 |
| `download-covers` | 下载缺失封面 | |
| `check-updates` | 检查 C 站更新 | `force:true` 绕过 24h 缓存 |
| `update` | 把「有新版」的模型加入下载队列 | 先跑 `check-updates` |
| `sync-local` | 按本地文件清理失效更新记录 | 同步执行，最长约 2 分钟 |

参数：`paths`（模型文件绝对路径数组，省略=全部）、`wait=1`（默认，阻塞到 `running=false`）、
`timeout`。返回 `{ok, op, start, progress, waited_s, timed_out}`。
`wait=0` 时立即返回，用 `GET /api/automation/library/op?op=verify` 轮询进度。

### `POST /api/automation/images/download`
把模型的示例图批量下载到 `<名>.images/`。参数：`path`、`wait=1`、`timeout`。
后台线程执行（绝不把请求线程挂死），`wait=1` 时轮询进度到 `done>=total` 或超时，
返回 `{ok, started, state:{total,done,downloaded,failed}, waited_s, timed_out}`。
`GET /api/automation/images/state?path=…` 同时给出批量下载与后台补帧的进度。

### `POST /api/automation/workflow/analyze`
解析 ComfyUI 工作流（`.json` / 内嵌 PNG/WebP）或 Forge/A1111 生成参数。
参数：`path`、`full=0|1`。默认**丢掉** `preview_b64` 与边表，只留
`node_count` / `models` / `pos_prompt` / `neg_prompt` / `prompt_notes` / `generation_parameters`
与精简节点表；`full=1` 才返回 `edges`。若解析出模型引用，会自动附 `local_matches`
（本地库是否有同名文件 + 路径 + SHA256 前缀）。

### `GET /api/automation/storage`
落点与磁盘：`download_dir` / `download_target_dir` / `models_dir` / `comfyui_models_root`
的存在性、可写性、剩余空间；`type_dirs` 列出 ComfyUI 核心类型目录哪些已存在。
用于回答「为什么没分流」——分流只写已存在的类型目录，不新建。

## 4. 校验

### `GET /api/automation/verify`
参数：`task_id` 或 `path`，可选 `hash=1` 重算 SHA256。

校验项：模型文件存在、体积、SHA256（与任务记录的期望值比对）、
`.civitai.info` / `.json` / `_评论.txt`、封面、`.images/` 目录与帧数、
扩展产物 `评论.txt` / `example.json`。
`path` 是目录时自动挑最大的权重文件当本体。

封面判定：`<stem>.preview.png|.preview.jpg|.preview.webp|.cover.png|.cover.jpg` 与
`<stem>.preview.mp4|.preview.webm|<stem>.mp4|.webm` **任一存在即算通过**，命中的文件名放在
`preview` 字段。封面本身是视频的模型（如 REZERO H3）桌面端写不出 `.preview.png`，
旧规则会误报缺失——以视频封面为准是正确语义。

## 5. 配置写入（白名单）

### `POST /api/automation/config`

```json
{"config": {"route_by_type": true, "ask_move_after_download": false}}
```

允许键：`route_by_type`、`comfyui_models_root`、`ask_move_after_download`、`multi_file_download`、
`download_target_dir`、`download_dir`、`models_dirs`、`site_domain`、`max_images_per_model`、
`download_cover`、`proxy_enabled`、`proxy_address`、`ssl_verify`、
`download_name_mode`、`filename_include_version`、`rename_clean_rules`、
`max_concurrent_downloads`、`download_timeout`、`download_retry`、
`gen_metadata`、`save_raw_json`、`save_translated_json`、`auto_translate`、
`target_env`、`organize_mode`、`archive_root`。

出现白名单外的键 → 整条 400，**不做部分生效**。写入走 `Api.save_config`（与设置页同一条路径，
含多目录规范化与下载器重绑）。

## 6. 长尾方法

`GET /api/rpc/methods` 列出全部可调用方法及参数名；`POST /api/rpc {"method": "...", "args": [...]}`
调用。系统选择框类方法（`pick_dir` / `pick_file` / `pick_files`）永远不暴露。

常用：`respond_download_file_choice`、`get_model_detail`、`mm_organize`、`mm_verify`、
`mm_check_updates`、`lib_list`、`rp_start`、`fetch_model_comments`。

## 7. 一键验证脚本

```bash
python scripts/verify_e2e.py --url "<模型页>" --json      # 提交→等待→校验，一行 JSON
python scripts/verify_e2e.py --url "<模型页>" --plan      # 只干跑
python scripts/verify_e2e.py --task <task_id> --hash      # 只校验已有任务
```

退出码 0 = 全部符合预期。

## 8. 免交互开关（浏览器扩展侧）

`chrome.storage.local` 的 `cft_config_v1.autoConfirmDuplicate = true` 时，
「该版本已归档过」不再弹原生 `confirm`（无头/自动化环境会默认 dismiss 并取消归档），
直接按「继续」处理。默认 `false`，保持人工确认。

## 9. 已知边界

- 「是否移动到分类文件夹」是**前端弹窗**，不属于后端接口；自动化请设
  `ask_move_after_download=false`，或让任务走分流（分流任务本就不会弹）。
- 扩展 offscreen 文档只有 `chrome.runtime`（无 `chrome.tabs` / `chrome.storage`），
  所有 content 消息经 background 代发；归档日志由 offscreen 转交 bg 落盘。
- `plan` 的 `probe=true` 会走网络；默认关闭以免干跑变慢。
- 自动化端点与 `/api/rpc` 同源放行：本机任何进程都能调用，**不要在没有本机信任的前提下暴露端口**。
