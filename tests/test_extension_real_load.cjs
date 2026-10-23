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

    // 免点击这一路的真 Chrome 环节：不点图标也有胶囊、心跳真的在册、状态能推给已打开的页面、
    // 重复注入不会长出第二个胶囊。
    // 为什么不在这里跑 chrome.runtime.reload()：实测在这套 Playwright 临时 profile 里，reload() 之后
    // 扩展 SW 再也不回来（40 秒内 SW 列表与 serviceworker 事件都是空的，开新标签页事件也叫不醒），
    // 复现不了"真浏览器重载那一刻"；那一段由 tests/test_extension_batch.cjs 的冷启动用例钉住。
    {
      const MODEL_URL = 'https://civitai.red/models/2998224/mmh3-anime-shift?modelVersionId=3400711';
      await context.route('**://civitai.red/models/**', (route) => route.fulfill({
        status: 200, contentType: 'text/html',
        body: '<html><head><title>[MMH3] Anime Shift | Civitai</title></head>'
          + '<body><div id="root">模型页替身：只放一个 body，胶囊自己会挂上来</div></body></html>',
      }));
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      await page.goto(MODEL_URL);
      await page.waitForFunction(() => {
        const c = document.getElementById('cft-status-chip');
        return !!c && /已下载|有更新|下载中|其他版本|未下载|未知/.test(c.textContent);
      }, null, { timeout: 30000 });
      const chipFirst = (await page.textContent('#cft-status-chip') || '').trim();
      ok('真扩展：没点图标、没开弹层，只加载页面就自己有状态胶囊（' + chipFirst + '）');

      const swInfo = await sw.evaluate(async () => {
        const alarms = (await chrome.alarms.getAll()).map((a) => [a.name, a.periodInMinutes]);
        const st = await chrome.storage.local.get(['cftBootWakeAtV1', 'cftModelStatusV1']);
        const tabs = await chrome.tabs.query({ url: 'https://civitai.red/models/*' });
        return { alarms, bootAt: Number(st.cftBootWakeAtV1) || 0,
          cacheEntries: Object.keys((st.cftModelStatusV1 && st.cftModelStatusV1.entries) || {}),
          tabIds: tabs.map((t) => t.id) };
      });
      assert(swInfo.alarms.some((a) => a[0] === 'cft-status-heartbeat' && a[1] === 2),
        '2 分钟状态心跳要在真 Chrome 里真的登记：' + JSON.stringify(swInfo.alarms));
      assert(swInfo.bootAt > 0, '冷启动补回要记下时刻（SW 每次醒都自查，不靠 onInstalled 复查）');
      assert.equal(swInfo.tabIds.length, 1, '模型页标签应只有一个：' + JSON.stringify(swInfo.tabIds));
      ok('真扩展：状态心跳在册，冷启动补回已跑过（storage 里有 cftBootWakeAtV1）');

      // 后台把状态推给"已经打开的那一页"：不刷新、不点图标，胶囊必须自己变过来
      await page.evaluate(() => {
        window.__cftProbeMarker = 'alive';
        document.getElementById('cft-status-chip').textContent = '__STALE__';
      });
      const pushed = await sw.evaluate(async (tabId) => {
        try {
          return await chrome.tabs.sendMessage(tabId, {
            __cft: true, to: 'content', type: 'modelStatusPush',
            payload: { entry: { state: 'downloaded', modelId: '2998224', versionId: '3400711',
              local: { source: 'library', version_name: 'v1.0', model_name: '[MMH3] Anime Shift',
                path: 'D:\\models\\a.safetensors', exists: true } } },
          });
        } catch (e) { return { error: String((e && e.message) || e) }; }
      }, swInfo.tabIds[0]);
      assert(!pushed || !pushed.error, '把状态推给页面失败：' + JSON.stringify(pushed));
      await page.waitForFunction(() => {
        const c = document.getElementById('cft-status-chip');
        return !!c && c.textContent !== '__STALE__';
      }, null, { timeout: 15000 });
      const chipPushed = (await page.textContent('#cft-status-chip') || '').trim();
      assert(chipPushed.includes('已下载') && chipPushed.includes('v1.0'), '推送后胶囊文案：' + chipPushed);
      assert.equal(await page.evaluate(() => window.__cftProbeMarker || ''), 'alive',
        '页面被重新加载了 —— 这条验收的前提是"不刷新"');
      ok('真扩展：后台一推，已打开的页面不刷新也不点图标就把胶囊改成 ' + chipPushed);

      // 重载补脚本走的正是这条注入路径：manifest 声明的文件再注一遍，不许长出第二个胶囊
      const manifest = JSON.parse(fs.readFileSync(path.join(extDir, 'manifest.json'), 'utf8'));
      await sw.evaluate(async (files) => {
        const t = (await chrome.tabs.query({ url: 'https://civitai.red/models/*' }))[0];
        await chrome.scripting.executeScript({ target: { tabId: t.id }, files });
      }, manifest.content_scripts[0].js);
      assert.equal(await page.locator('#cft-status-chip').count(), 1, '重复注入后页面只剩一个胶囊才叫幂等');
      assert.equal(errors.length, 0, '页面脚本错误：' + errors.join('\n'));
      ok('真扩展：manifest 声明的文件重复注入后仍只有一个胶囊，且没有页面脚本错误');
      await page.close();
    }

    console.log('\n' + passed + ' passed');
  } finally {
    await context.close();
    try { fs.rmSync(userDataDir, { recursive: true, force: true }); } catch (_) {}
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
