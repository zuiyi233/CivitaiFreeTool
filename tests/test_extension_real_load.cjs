/* 真实加载未打包扩展（chrome-extension:// 原点）跑一遍侧栏与弹层。
 *
 * 为什么单独要这个：其余界面回归把扩展目录当普通 HTTP 站点伺服，浏览器不施加扩展 CSP，
 * 于是「内联脚本被 MV3 CSP 拦掉」这类问题在假原点下永远测不出来 —— 侧栏标记就是这样
 * 静默失效的（HTTP 页绿、真扩展里 SIDE=undefined）。这里只验三件事：
 *   1) 扩展能被 Chrome 装载，service worker 起来；
 *   2) chrome-extension:// 原点下 sidepanel.html 真的设置了侧栏标记、样式生效；
 *   3) 弹层页在真扩展里显示「侧栏」入口（chrome.sidePanel.open 确实存在）。
 * 不发真实网络、不点任何下载按钮；桥如果恰好开着，也只会被读一次 /api/health。
 *
 * 跑法：node tests/test_extension_real_load.cjs
 *       headed 版（Chrome 无头模式不暴露扩展 service worker）：CFT_EXT_REAL=1 node tests/test_extension_real_load.cjs
 */
const path = require('path'), assert = require('assert/strict'), os = require('os'), fs = require('fs');
const root = path.resolve(__dirname, '..');
const extDir = path.join(root, 'chrome-extension');
const pwPath = process.env.PLAYWRIGHT_PATH || path.join(root, '.tools', 'node_modules', 'playwright');
process.env.PLAYWRIGHT_BROWSERS_PATH = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(root, '.tools', 'ms-playwright');
const headed = process.env.CFT_EXT_REAL === '1';

let chromium;
try { ({ chromium } = require(pwPath)); }
catch (e) { console.log('SKIP 真扩展：playwright 不可用（' + pwPath + '）：' + e.message); process.exit(0); }

(async () => {
  // 扩展只在持久化上下文里可用（launch() 的匿名上下文不装载 --load-extension）
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cft-ext-real-'));
  const context = await chromium.launchPersistentContext(userDataDir, {
    headless: !headed,
    args: ['--disable-extensions-except=' + extDir, '--load-extension=' + extDir],
  });
  try {
    let sw = null;
    for (let i = 0; i < 60 && !sw; i++) {
      sw = context.serviceWorkers().find((w) => w.url().startsWith('chrome-extension://')) || null;
      if (!sw) await new Promise((r) => setTimeout(r, 250));
    }
    if (!sw) {
      console.log('SKIP 真扩展：没拿到扩展 service worker。Chrome 无头模式不装载扩展；'
        + '要真验 sidepanel 的 CSP 行为请用 CFT_EXT_REAL=1（会开一个临时 Chromium 窗口，用完即关、目录即删）');
      return;
    }
    const id = new URL(sw.url()).host;
    const base = 'chrome-extension://' + id;
    let passed = 0;
    const ok = (s) => { passed++; console.log('OK ' + s); };

    {
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      await page.goto(base + '/sidepanel.html');
      await page.waitForFunction(() => globalThis.__CFT_SIDE_PANEL__ === true, null, { timeout: 15000 });
      await page.waitForFunction(() => document.body.classList.contains('side-panel'));
      assert.equal(await page.locator('#openSidePanel').isHidden(), true, '侧栏页不该显示“侧栏”入口');
      assert.equal(errors.length, 0, '侧栏页脚本错误：' + errors.join('\n'));
      ok('真扩展：sidepanel.html 在 chrome-extension:// 原点加载，CSP 放行外部 sidepanel.js，side-panel 生效');
      await page.close();
    }

    {
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      await page.goto(base + '/popup.html');
      await page.waitForFunction(() => typeof globalThis.__CFT_SIDE_PANEL__ === 'undefined'
        && document.querySelector('#tabList'), null, { timeout: 15000 });
      const hasOpenApi = await page.evaluate(() => !!(globalThis.chrome && chrome.sidePanel
        && typeof chrome.sidePanel.open === 'function'));
      assert.equal(hasOpenApi, true, 'manifest 的 sidePanel 权限没生效？chrome.sidePanel.open 不存在');
      assert.equal(await page.locator('#openSidePanel').isVisible(), true, '弹层要真的显示侧栏入口');
      assert.equal(errors.length, 0, '弹层页脚本错误：' + errors.join('\n'));
      ok('真扩展：popup.html 不带侧栏标记，chrome.sidePanel.open 可用，入口可见');
      await page.close();
    }

    console.log('\n' + passed + ' passed');
  } finally {
    await context.close();
    try { fs.rmSync(userDataDir, { recursive: true, force: true }); } catch (_) {}
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
