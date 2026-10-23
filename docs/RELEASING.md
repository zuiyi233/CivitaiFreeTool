# 修复后的发布流程

维护约定：每次修复后，检查并提交当前源码到 GitHub，然后发布对应版本 Release，附带 Chrome 与 Firefox 扩展。

1. 在隔离目录运行后端、桥服务与前端回归检查；涉及原生窗口时另做实际窗口验证。
2. 同步版本号、更新日志与 `docs/releases/v<版本>.md`。保留现有下载任务、历史及模型数据。
3. Windows 使用 Python 3.12，安装 `requirements.txt` 与 PyInstaller，执行 `python -m PyInstaller -y CivitaiFreeToolWeb.spec`。Spec 自动用 Windows 自带 .NET Framework 编译器编译 `native/CftChrome.cs`。
4. 校验 EXE 中的页面、后端版本、原生 DLL、托盘与 Tcl/Tk。备份旧 EXE 后替换本机安装，确认原生窗口启动。
5. 只提交应用源码、长期测试与发布文档；**不能上传 API Key、用户配置、任务/历史 JSON、图片缓存或用户模型/素材目录**。
6. 正常推送 main，不强推。附件清单须绑定这次提交的完整 SHA。
7. Release 附件为版本号 EXE、Windows 便携 ZIP、Chrome 扩展 ZIP、Firefox 扩展 ZIP、源码 ZIP、SHA256SUMS.txt；扩展 ZIP 的 manifest.json 必须位于根目录，不嵌套旧 ZIP。
   - **两份扩展 ZIP 的文件名固定不带版本号**（`CivitaiFreeTool-ChromeExtension.zip` / `CivitaiFreeTool-FirefoxExtension.zip`，版本只在 Release tag、`manifest.json` 的 `version` 和更新说明里）。原因：Windows"全部提取"用 ZIP 文件名建目录；没有 `key` 的未打包扩展会按路径推导 ID，而同一 ID 不能并存两份。名字固定后解压就是原地覆盖，扩展管理页点一次"重新加载"即可，`chrome.storage` 与 IndexedDB 里的提交记录/历史不会丢。
   - `chrome-extension/manifest.json` 的 `"key"`（公钥 SPKI DER 的 base64，非机密）**不得删除**：它把 ID 从路径解绑，换目录、换机器、从源码树加载都仍是同一个 ID（`cljlgpakghbofcdjbimchcmefdfnfdii`），存储和权限授权因此不再丢。同一浏览器里同 ID 不能并存两个未打包扩展，所以从旧版（无 `key`）迁移过去的那一次仍要先移除旧条目再加载新目录，此后不再需要。
   - 便携 ZIP 用 `CivitaiFreeTool/` 单层根目录，内含 `CivitaiFreeToolWeb.exe`、`更新说明.md`、`使用说明.txt`、`chrome-extension/**`、`firefox-extension/**`；只放文件不放目录项，全部 deflate。两份说明**不在仓库里**：`更新说明.md` 由 `docs/releases/v<版本>.md` 精简成用户视角，`使用说明.txt` 只把版本号换掉、其余文案沿用。源码 ZIP 用 `git archive HEAD`（不加前缀，根级就是 `.github/`、`CHANGELOG.md`）。`SHA256SUMS.txt` 为 5 行 `<64位哈希>␣␣<文件名>`、按文件名排序、CRLF、无 BOM。
8. `python scripts/release_github.py <版本> docs/releases/v<版本>.md --assets-dir <附件目录>` 只发布完整、哈希匹配的附件：先草稿，上传并验证远端 SHA256/大小，全部通过后才公开。出错保持草稿，不把“建了 Release”当作完成。
9. 发到二开节点时用 `--repo <owner>/<name> --remote origin`，脚本会强制“发布目标与源码推送的仓库是同一个”，对不上就在取凭据之前中止；默认仍指官方仓库。发到 fork 时不要动 `upstream`。
10. 发布后逐项核对，别把“脚本退出 0”当成完成：Release 的 `tag/draft/published_at/target_commitish` 与资产数、远端重新下载的 `SHA256SUMS.txt` 与本地逐字节一致、目标仓库上这次提交的 Actions 是否真的跑过。**fork 的 push 事件不产生工作流运行**（推 130 个提交后运行数仍是 0，同期主仓库 220 次），要核对就 `gh workflow run tests --repo <owner>/<name> --ref main` 手动触发一次；工作流只跑 `test_browser_bridge`（43 项），绿灯不等于全量门禁，全量仍是本机 `discover -s tests -t tests`（651 项）加根目录 43 项 = 694 项。不能拿主仓库的历史绿灯代替发布仓库的结果。

认证使用环境变量或 Git Credential Manager，认证信息不写日志、源码、清单或聊天。Firefox 包未签名，仅支持临时加载；永久安装需要 Mozilla 签名。
