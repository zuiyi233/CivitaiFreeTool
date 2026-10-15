/* 扩展 trpc 取数契约（commentv2.getInfinite / resourceReview.getPaged）。
   匿名 HTTP 永远拿不到非空评论（已实测），所以这里用**官方真实响应形状**的 fixture + 假 fetcher
   做功能验证；每条断言旁边注明它的实测/源码依据（见 chrome-extension/lib/civitai-api.js 文件头 8)/9)）。 */
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.resolve(__dirname, '..');
const SRC = fs.readFileSync(path.join(root, 'chrome-extension/lib/civitai-api.js'), 'utf8');
let passed = 0; const ok = (s) => { passed++; console.log('OK ' + s); };

function freshApi(fetch) {
  const ctx = vm.createContext({ URL, URLSearchParams, AbortController, setTimeout, clearTimeout, console, fetch });
  vm.runInContext(SRC, ctx);
  return ctx.CFT.api;
}
// trpc 的入参在 URL 的 ?input={"json":{…}} 里（buildTrpcUrl），断言必须解析它，别猜
function inputOf(url) { return JSON.parse(decodeURIComponent(new URL(url).searchParams.get('input'))).json; }
function procOf(url) { return new URL(url).pathname.split('/').pop(); }
function isTrpc(url) { return procOf(url).indexOf('.') > 0; }

// 页面通道（带登录 Cookie 的同源代发）假 fetcher：记录每次入参，按 fixture 回话。
// handler 返回 {status?, body}（body 就是发给页面的响应体文本所表示的对象）。
function pageChannel(handler) {
  const calls = [];
  const fetcher = async (url, opts) => {
    const r = handler(url, isTrpc(url) ? inputOf(url) : null, calls.length + 1, opts) || {};
    const status = r.status || 200;
    calls.push({ url, input: isTrpc(url) ? inputOf(url) : null, proc: procOf(url), opts });
    return { ok: status >= 200 && status < 300, status, text: JSON.stringify(r.body) };
  };
  return { calls, fetcher };
}
const env = (json) => ({ result: { data: { json } } }); // 官方信封：{"result":{"data":{"json":…}}}

// commentV2Select 的真实节点形状（selector 里没有 children、顶层也没有 userId）
const cnode = (id, username, content) => ({
  id, createdAt: '2026-10-0' + (id % 9) + 'T00:00:00.000Z', nsfw: false, tosViolation: false,
  content, hidden: false, threadId: 1, pinnedAt: null, reactionCount: 0,
  user: { id: 1000 + id, username, name: username, profilePicture: null }, reactions: [],
});
const PAGE1 = env({
  comments: [cnode(10, 'alice', '<p>First &amp; best</p>'), cnode(20, 'bob', '<p>Second</p>')],
  nextCursor: 10,                      // 数字：上一页最后一条 regular comment 的 id
  targetComment: null,
  replyThreads: [{                     // ReplyThread={id,commentId,locked,depth,commentCount,comments,nextCursor?}
    id: 77, commentId: 10, locked: false, depth: 1, commentCount: 2, nextCursor: 102,
    comments: [cnode(101, 'carol', '<p>reply one</p>'), cnode(102, 'dave', '<p>reply two</p>')],
  }],
  childlessCommentIds: [20],           // 服务端明确说「bob 这条没有回复」
});
const PAGE2 = env({ comments: [cnode(30, 'eve', '<p>Third</p>')], nextCursor: null, targetComment: null, replyThreads: [], childlessCommentIds: [] });
const EMPTY_COMMENTS = env({ comments: [], nextCursor: null, targetComment: null, replyThreads: [], childlessCommentIds: [] });

const rnode = (id, username, rating, details) => ({
  id, modelId: 4201, modelVersionId: 501240, rating, recommended: rating >= 4, details,
  createdAt: '2026-10-14T06:06:10.854Z', nsfw: false, exclude: false, metadata: null,
  commentCount: 0, imageCount: 0, quality: 0, user: { id: 900 + id, username, profilePicture: null },
});
const REVIEW_P1 = env({ items: [rnode(1, 'rev1', 5, '<p>Loved it</p>'), rnode(2, 'rev2', 4, null)], totalItems: 120, currentPage: 1, pageSize: 50, totalPages: 2 });
const REVIEW_P2 = env({ items: [rnode(3, 'rev3', 2, '<p>Bad</p>')], totalItems: 120, currentPage: 2, pageSize: 50, totalPages: 2 });

const count = (text, re) => (String(text).match(re) || []).length;

(async () => {
  // ---- 1. 评论入参 + 解析（页面通道） ----
  {
    const api = freshApi(async () => { throw new Error('不该走直连'); });
    const ch = pageChannel((url, inp) => ({ body: inp.cursor === 10 ? PAGE2 : PAGE1 }));
    api.setFetcher(ch.fetcher);
    const text = await api.fetchCivitaiComments(4201, null, 'https://civitai.com/models/4201/x?modelVersionId=501240');
    const first = ch.calls[0];
    assert.equal(ch.calls.length, 2);
    assert.equal(first.proc, 'commentv2.getInfinite');
    // 依据：commentConnectorSchema 的 entityType 枚举是小写 'model'（传 'Model' → 400 invalid_value）
    assert.equal(first.input.entityType, 'model');
    // 依据：getCommentsInfiniteSchema.entityId = z.number()（传 {modelId} → 400 expected number received undefined）
    assert.equal(first.input.entityId, 4201);
    assert.equal(typeof first.input.entityId, 'number');
    // 依据：cursor = z.number().optional()，cursor:null → 400「expected number, received null」→ 第一页必须整个不带这个键
    assert.ok(!('cursor' in first.input), '首屏不能带 cursor 键');
    // 依据：服务端 `repliesDepth ? getReplyThreads(…) : {threads:[]}` —— 不显式给就一条回复都拿不到
    assert.ok(first.input.repliesDepth >= 1, '必须显式带 repliesDepth');
    assert.ok(first.input.repliesLimit >= 1);
    assert.ok(!('authed' in first.input), 'authed 不在 schema 里，不能继续发');
    assert.ok(!('modelId' in first.input), '评论接口的入参键是 entityId，不是 modelId');
    // 翻页：nextCursor 是数字 id，下一页 = Object.assign({}, base, {cursor})
    assert.equal(ch.calls[1].input.cursor, 10);
    assert.equal(typeof ch.calls[1].input.cursor, 'number');
    assert.equal(ch.calls[1].input.repliesDepth, first.input.repliesDepth, '翻页要沿用同一组入参');
    assert.equal(first.opts.timeoutMs, 12000, '评论请求显式带 12s 超时（先于调用方的请求-答复放弃）');
    // 解析：主列表 comments[]（旧代码读 data.items → 200 也恒定 0 条），回复在 replyThreads[].comments[]
    assert.equal(count(text, /^【/gm), 3, '三条顶层评论：alice/bob/eve');
    assert.equal(count(text, /^  ↳ 回复 【/gm), 2, '两条回复：carol/dave');
    assert.match(text, /【alice】\(.*\nFirst & best/);          // 正文是消毒后的 HTML → 必须转纯文本
    assert.ok(text.indexOf('<p>') < 0, '不能把原始 HTML 落盘');
    assert.match(text, /  ↳ 回复 【carol】.*〔回复 @alice〕/);   // 回复标出父评论关系
    assert.ok(text.indexOf('carol') > text.indexOf('alice') && text.indexOf('dave') < text.indexOf('bob'),
      '回复紧跟在它的父评论之后，按父评论在 comments 里的位置排');
    assert.ok(text.indexOf('[通道] 数据来自页面通道') >= 0);
    assert.ok(text.indexOf('接口已被站点关闭') < 0, '页面通道拿到了数据，绝不写"接口已关闭"');
    // childlessCommentIds 里的 bob 不该触发「回复状态未知」的说明
    assert.ok(text.indexOf('无回复') < 0 && text.indexOf('既没返回回复线程') < 0);
    ok('评论：入参符合站点当前 schema，回复按父评论位置解析成 ↳ 行');
  }

  // ---- 2. 负例：result.data.json = null 走「无评论区」分支，不是报错分支 ----
  {
    const api = freshApi(async () => { throw new Error('不该走直连'); });
    const ch = pageChannel(() => ({ status: 200, body: { result: { data: { json: null } } } }));
    api.setFetcher(ch.fetcher);
    const text = await api.fetchCivitaiComments(4201, null, 'https://civitai.com/models/4201/x');
    assert.equal(ch.calls.length, 1, '服务端已经明确回答 null：不再换参数重试');
    assert.match(text, /没有评论区/);                     // commentsv2.service.ts: if (!mainThread) return null
    assert.ok(text.indexOf('接口已被站点关闭') < 0);        // 这不是 401，不能写降级/关闭
    assert.ok(text.indexOf('未能保存任何评论') < 0);
    assert.equal(count(text, /^【/gm), 0);
    ok('评论：json:null 认定为「该模型没有评论区」，不写成抓取失败，也不再重试');
  }

  // ---- 3. 空列表的文案必须交代当次通道（页面 = 没有评论；匿名直连 = 不能下这个结论） ----
  {
    const pageApi = freshApi(async () => { throw new Error('不该走直连'); });
    const ch = pageChannel(() => ({ status: 200, body: EMPTY_COMMENTS }));
    pageApi.setFetcher(ch.fetcher);
    const pageText = await pageApi.fetchCivitaiComments(4201, null, 'https://civitai.com/models/4201/x');
    assert.match(pageText, /这个模型目前没有可保存的评论/);
    assert.ok(pageText.indexOf('匿名') < 0, '页面通道拿到的空不能写成匿名结论');
    assert.equal(ch.calls.length, 1);

    const directApi = freshApi(async (url) => ({ ok: true, status: 200, text: async () => JSON.stringify(EMPTY_COMMENTS) }));
    const directText = await directApi.fetchCivitaiComments(4201, null, 'https://civitai.com/models/4201/x');
    assert.match(directText, /扩展匿名直连/);
    assert.match(directText, /不是「这个模型没有评论」/);
    assert.match(directText, /\[通道\] 数据来自扩展直连通道/);
    assert.ok(pageText !== directText, '两种通道的空结果文案必须不同');
    ok('评论：页面通道的空 = 没有评论；匿名直连的空 = 只能交代通道，不能下结论');
  }

  // ---- 4. 主入参被 400 拒时降级到最小形状（而不是继续堆玄学候选） ----
  {
    const seen = [];
    const api = freshApi(async (url) => {
      const inp = inputOf(url);
      seen.push(inp);
      if ('repliesDepth' in inp) {
        return { ok: false, status: 400, text: async () => JSON.stringify({ error: { json: { message: '[{"code":"invalid_type","path":["repliesDepth"],"message":"Invalid input: expected number"}]', code: -32600, data: { code: 'BAD_REQUEST', httpStatus: 400 } } } }) };
      }
      return { ok: true, status: 200, text: async () => {
        const one = JSON.parse(JSON.stringify(PAGE1));       // 最小形状那组：只回一页，别再造 nextCursor
        one.result.data.json.nextCursor = null;
        return JSON.stringify(one);
      } };
    });
    const text = await api.fetchCivitaiComments(4201, null, 'https://civitai.com/models/4201/x');
    assert.equal(seen.length, 2, '只有 HTTP 400（入参被拒）才值得换第二组候选');
    assert.ok('repliesDepth' in seen[0] && !('repliesDepth' in seen[1]));
    assert.equal(seen[1].entityType, 'model');
    assert.match(text, /【alice】/);                       // 主列表仍然保住
    assert.match(text, /入参被站点拒绝/);                   // 并且如实说明契约变了
    ok('评论：400 = 契约漂移 → 用最小入参保住主列表并写明真因');
  }

  // ---- 5. 评价入参：modelId（必填校验）+ modelVersionId（过滤）+ page（分页） ----
  {
    const api = freshApi(async () => { throw new Error('不该走直连'); });
    const ch = pageChannel((url, inp) => (inp && inp.page === 2 ? { body: REVIEW_P2 } : { body: REVIEW_P1 }));
    api.setFetcher(ch.fetcher);
    const text = await api.fetchCivitaiReviews(501240, 'https://civitai.com/models/4201/pony?modelVersionId=501240', 4201);
    const trpcCalls = ch.calls.filter((c) => isTrpc(c.url));
    assert.equal(trpcCalls.length, 2, '按 page 翻到 totalPages 才收（旧代码发 cursor 被 Zod 丢弃，永远停在第 1 页）');
    for (const c of trpcCalls) {
      assert.equal(c.proc, 'resourceReview.getPaged');
      assert.equal(c.input.modelId, 4201, 'resourceReviewPagedSchema 的 modelId 是必填 numericString：缺了 400');
      assert.equal(typeof c.input.modelId, 'number');
      assert.equal(c.input.modelVersionId, 501240, 'SQL 只按 modelVersionId 过滤：只给 modelId 会得到 totalItems:0 的空壳');
      assert.ok(!('cursor' in c.input), 'paginationSchema 只有 {limit,page}，没有 cursor 这个键');
    }
    assert.deepEqual(trpcCalls.map((c) => c.input.page), [1, 2]);
    assert.equal(count(text, /^【/gm), 3);
    assert.match(text, /【rev1】 评分:5 推荐:是.*\nLoved it/);
    assert.match(text, /\(只打了分，没有写文字评价\)/);      // details:null 的条目不能写成空正文，也不能丢
    assert.match(text, /模型ID: 4201/);
    assert.ok(text.indexOf('按版本过滤') >= 0 && text.indexOf('含其它版本') < 0, '取数口径按版本过滤，标题不能谎报');
    assert.ok(text.indexOf('接口已被站点关闭') < 0);
    ok('评价：modelId+modelVersionId+page 三件套，按 currentPage/totalPages 收口');
  }

  // ---- 6. modelId 的补齐顺序：页面 URL → 公开 API（调用方只给 versionId 的真实情形） ----
  {
    assert.equal(freshApi().modelIdFromPageUrl('https://civitai.red/models/4201/pony?modelVersionId=1'), 4201);
    assert.ok(!Number.isFinite(freshApi().modelIdFromPageUrl('civitai.com')));
    const api = freshApi(async () => { throw new Error('不该走直连'); });
    const ch = pageChannel((url, inp) => {
      if (!isTrpc(url)) return { status: 200, body: { id: 501240, modelId: 4201, name: 'V5.1' } }; // /api/v1/model-versions/501240
      return { body: (inp.page === 2 ? REVIEW_P2 : REVIEW_P1) };
    });
    api.setFetcher(ch.fetcher);
    const t6 = await api.fetchCivitaiReviews(501240, 'civitai.com');   // 裸 host：没有 /models/<id> 可解析
    assert.equal(ch.calls[0].url.indexOf('/api/v1/model-versions/501240') > 0, true, '缺 modelId 时先用公开 API 补，一次请求');
    assert.equal(ch.calls[1].input.modelId, 4201);
    assert.equal(ch.calls[1].input.modelVersionId, 501240);
    assert.equal(count(t6, /^【/gm), 3, '补齐 modelId 后照常按 page 翻完两页');
    ok('评价：调用方只给 versionId 时按 URL/公开 API 补出 modelId，绝不发必然 400 的请求');
  }

  // ---- 7. 评价空结果 / 翻页上限的文案 ----
  {
    const api = freshApi(async () => { throw new Error('不该走直连'); });
    const ch = pageChannel(() => ({ status: 200, body: env({ items: [], totalItems: 0, currentPage: 1, pageSize: 50, totalPages: 1 }) }));
    api.setFetcher(ch.fetcher);
    const t = await api.fetchCivitaiReviews(501240, 'https://civitai.com/models/4201/x?modelVersionId=501240', 4201);
    assert.equal(ch.calls.length, 2, '版本级空 → 才用模型级 getInfinite 兜底探一次');
    assert.equal(ch.calls[0].proc, 'resourceReview.getPaged', '主通道必须是版本级 getPaged');
    assert.equal(ch.calls[0].input.modelId, 4201);
    assert.equal(ch.calls[0].input.modelVersionId, 501240);
    assert.equal(ch.calls[0].input.page, 1);
    assert.equal(ch.calls[1].proc, 'resourceReview.getInfinite');
    assert.match(t, /items 空列表且 totalItems=0：这个版本没有评价/);
    assert.ok(t.indexOf('接口已被站点关闭') < 0);
    assert.ok(t.indexOf('来自模型级') < 0, '兜底也没拿到内容时不能谎称有模型级数据');

    const many = freshApi(async () => { throw new Error('不该走直连'); });
    const big = { items: [rnode(7, 'rev7', 5, null)], totalItems: 12706, pageSize: 50, totalPages: 255 };
    const ch2 = pageChannel((url, inp) => ({ status: 200, body: env(Object.assign({}, big, { currentPage: inp.page })) }));
    many.setFetcher(ch2.fetcher);
    const t2 = await many.fetchCivitaiReviews(501240, 'https://civitai.com/models/4201/x?modelVersionId=501240', 4201);
    assert.equal(ch2.calls.length, 5, '到 MAX_PAGES(5) 就收手，不再打第 6 次');
    assert.match(t2, /评价翻页到本文件上限 5 页/);
    assert.match(t2, /totalItems=12706/);
    ok('评价：空结果按通道说话；拿不完时如实写明只保存了前 5 页');
  }

  // ---- 8. 「不完整」必须说出来：翻页封顶 + 站点回复预算缺口 ----
  {
    const api = freshApi(async () => { throw new Error('不该走直连'); });
    const ch = pageChannel(() => ({
      body: env({
        comments: [cnode(1, 'alice', '<p>a</p>'), cnode(2, 'bob', '<p>b</p>'), cnode(3, 'cara', '<p>c</p>')],
        nextCursor: 900,                      // 每页都给 nextCursor → 逼出 MAX_PAGES 上限
        targetComment: null,
        replyThreads: [{
          id: 9, commentId: 1, locked: false, depth: 1, commentCount: 1,
          comments: [cnode(11, 'dave', '<p>r</p>')],
        }],
        childlessCommentIds: [],              // 站点没交「无回复」名单 → bob/cara 的回复状态未知
      }),
    }));
    api.setFetcher(ch.fetcher);
    const t = await api.fetchCivitaiComments(4201, null, 'https://civitai.com/models/4201/x');
    assert.equal(ch.calls.length, 5, 'MAX_PAGES=5 封顶，绝不无限翻');
    assert.equal(count(t, /^【/gm), 3, '同一批评论跨页要去重');
    assert.equal(count(t, /^  ↳ 回复 【/gm), 1);
    assert.match(t, /评论翻页到本文件上限 5 页/);
    assert.match(t, /有 2 条顶层评论服务端既没返回回复线程/);   // 按评论 id 计，不是 2×5 页
    ok('评论：翻页封顶与回复预算缺口如实写进产物，且跨页去重');
  }

  console.log('PASSED ' + passed);})().catch((e) => { console.error(e); process.exitCode = 1; });
