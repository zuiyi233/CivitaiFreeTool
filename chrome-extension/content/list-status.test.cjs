/* content/list-status.js 纯函数自测：node chrome-extension/content/list-status.test.cjs
 * 只测页面判定与链接解析；DOM 贴徽章由 tests/test_model_status_ui.cjs 用真 Chromium 覆盖。
 */
const assert = require('assert/strict');
const path = require('path');

globalThis.location = { origin: 'https://civitai.red', pathname: '/models' };
const LS = require(path.join(__dirname, 'list-status.js'));

let passed = 0;
const ok = (s) => { passed++; console.log('OK ' + s); };

// ---- 页面判定：只接管列表页与图片/帖子页；模型详情页归 model-page.js ----
assert.equal(LS.pageKind('/models'), 'list');
assert.equal(LS.pageKind('/models/'), 'list');
assert.equal(LS.pageKind('/models/101'), '', '模型详情页由胶囊负责，不能重复贴');
assert.equal(LS.pageKind('/models/101/alpha'), '');
assert.equal(LS.pageKind('/images/123'), 'media');
assert.equal(LS.pageKind('/images/123?width=450'), 'media');
assert.equal(LS.pageKind('/posts/456'), 'media');
assert.equal(LS.pageKind('/posts/456/comment/1'), 'media');
assert.equal(LS.pageKind('/images'), '');
assert.equal(LS.pageKind('/user/abc'), '');
assert.equal(LS.pageKind(''), '');
ok('pageKind：只认 /models 列表与 /images|/posts 详情，不接管模型详情页');

// ---- 链接解析：同站模型链接、版本 ID 校验、key 与缓存身份一致 ----
let l = LS.parseModelLink('/models/101?modelVersionId=1001');
assert.equal(l.modelId, '101');
assert.equal(l.versionId, '1001');
assert.equal(l.key, 'version:1001');
assert.equal(l.url, 'https://civitai.red/models/101?modelVersionId=1001');
l = LS.parseModelLink('https://civitai.com/models/202');
assert.equal(l.key, 'model:202');
assert.equal(l.versionId, '');
l = LS.parseModelLink('https://www.civitai.red/models/303/alpha?modelVersionId=3003&fileId=9');
assert.equal(l.key, 'version:3003');
assert.equal(l.modelId, '303');
assert.equal(l.url.includes('fileId=9'), true, '同站查询参数保留（只影响 URL，不影响 key）');
ok('parseModelLink：相对/绝对链接、有无版本、key=version:<vid>/model:<mid>');

for (const bad of [
  'https://example.com/models/404?modelVersionId=1',
  'https://civitai.red:8443/models/101',
  'https://civitai.red/api/download/models/101?modelVersionId=1001',
  'https://civitai.red/images/101',
  'https://civitai.red/models/101?modelVersionId=abc',
  'https://civitai.red/models/101?modelVersionId=0',
  'javascript:alert(1)',
  '',
  null,
]) {
  assert.equal(LS.parseModelLink(bad), null, '不应接受：' + bad);
}
ok('parseModelLink：站外/带端口/下载链接/非法版本/非 http 一律拒绝');

console.log(`\n${passed} passed`);
