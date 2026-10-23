/* CftTabIndex: pure payload builder — no page content, no cookies, no non-Civitai tabs. */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');

const root = path.resolve(__dirname, '..');
let passed = 0;
const ok = (s) => { passed++; console.log('OK ' + s); };

const ctx = vm.createContext({ URL, URLSearchParams, console });
vm.runInContext(fs.readFileSync(path.join(root, 'chrome-extension/tabs-common.js'), 'utf8'), ctx);
vm.runInContext(fs.readFileSync(path.join(root, 'chrome-extension/lib/tab-index.js'), 'utf8'), ctx);
const { CftTabIndex } = ctx;

const TABS = [
  { id: 11, windowId: 1, active: true, pinned: true, title: 'Live Wallpaper Style | Civitai',
    url: 'https://civitai.red/models/1264662/live-wallpaper-style?modelVersionId=3351608&token=SECRET&utm_source=x' },
  { id: 12, windowId: 1, title: 'Duplicate | Civitai',
    url: 'https://civitai.com/models/1264662/live-wallpaper-style?modelVersionId=3351608' },
  { id: 13, windowId: 2, discarded: true, title: 'Other version | Civitai',
    url: 'https://civitai.com/models/1264662/live-wallpaper-style?modelVersionId=9999' },
  { id: 14, windowId: 3, title: 'GitHub', url: 'https://github.com/x/y?token=OUTSIDE' },
  { id: 15, windowId: 3, title: 'Image', url: 'https://civitai.com/images/123' },
  { id: 16, windowId: 3, title: 'No version | Civitai', url: 'https://civitai.red/models/777/foo' },
];

const payload = CftTabIndex.buildPayload(TABS, { source: 'chrome-extension' });
assert.equal(payload.v, 1);
assert.equal(payload.source, 'chrome-extension');
assert.equal(payload.ext_version, '');
assert.equal(payload.tabs.length, 3);
assert.deepEqual(JSON.parse(JSON.stringify(payload.counts)),
  { tabs: 6, civitai: 5, models: 3, windows: 3, discarded: 1 });
ok('只保留 C 站模型页并统计全部标签页/窗口');

const versioned = CftTabIndex.buildPayload(TABS, { source: 'chrome-extension', extVersion: '2.6.1' });
assert.equal(versioned.ext_version, '2.6.1');
assert.equal(CftTabIndex.buildPayload(TABS, { extVersion: '2.6.1 <script>' }).ext_version, '');
ok('扩展版本随载荷上报，非法字符被丢弃');

const first = payload.tabs[0];
assert.equal(first.title, 'Live Wallpaper Style');
assert.equal(first.url, 'https://civitai.red/models/1264662/live-wallpaper-style?modelVersionId=3351608');
assert.equal(first.modelId, '1264662');
assert.equal(first.versionId, '3351608');
assert.equal(first.copies, 2);
assert.equal(first.active, true);
assert.equal(first.pinned, true);
assert(!JSON.stringify(payload).includes('SECRET'));
assert(!JSON.stringify(payload).includes('OUTSIDE'));
assert(!JSON.stringify(payload).includes('token'));
ok('同版本跨 red/com 合并，URL 去掉 token/utm，保留 active/pinned');

const other = payload.tabs.find((r) => r.versionId === '9999');
assert(other && other.discarded);
assert.equal(payload.tabs.find((r) => r.versionId === '').modelId, '777');
ok('不同版本/无版本各自保留，discarded 标记不被丢掉');

const limited = CftTabIndex.buildPayload(
  Array.from({ length: CftTabIndex.MAX_ROWS + 5 }, (_, i) => ({
    id: i + 1, windowId: 1, title: 'm' + i, url: 'https://civitai.red/models/' + (i + 1),
  })), { source: 'chrome-extension' });
assert.equal(limited.tabs.length, CftTabIndex.MAX_ROWS);
ok('单次索引严格限制在 2000 行');

const chromeModule = fs.readFileSync(path.join(root, 'chrome-extension/lib/tab-index.js'));
const firefoxModule = fs.readFileSync(path.join(root, 'firefox-extension/lib/tab-index.js'));
assert.deepEqual(firefoxModule, chromeModule);
ok('Chrome/Firefox 共用逐字节相同的 tab-index 模块');

console.log('tab-index: ' + passed + ' checks passed');
