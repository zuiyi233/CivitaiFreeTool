/* Background and popup use mocked tabs/bridge; never send users' browsing data. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict'),http=require('http');
const root=path.resolve(__dirname,'..');let passed=0;const ok=s=>{passed++;console.log('OK '+s);};
// 扩展的 lib/*.js 用 ESM 的 side-effect import 串依赖（顶层不触碰 chrome/document，见契约 §1）。
// vm 只能跑 classic script，所以这里按依赖序加载，并把正文包进 IIFE 模拟模块的独立顶层作用域
// —— 不包的话两个文件里的同名 const 会在同一 context 里撞车。
const IMPORT_RE=/^import\s+['"]([^'"]+)['"]\s*;?\s*$/gm;
function loadScript(file,ctx,seen){
 const abs=path.resolve(file);if(seen.has(abs))return;seen.add(abs);
 const src=fs.readFileSync(abs,'utf8');
 for(const m of src.matchAll(IMPORT_RE))loadScript(path.resolve(path.dirname(abs),m[1]),ctx,seen);
 vm.runInContext('(function(){\n'+src.replace(IMPORT_RE,'')+'\n})();',ctx,{filename:path.relative(root,abs)});
}
const loadIn=(ctx,rel)=>loadScript(path.join(root,rel),ctx,new Set());
const common=fs.readFileSync(path.join(root,'chrome-extension/tabs-common.js'),'utf8');
// 后台要按 manifest 自己声明的 content_scripts 决定注入清单，测试也照真清单读，不写死第二份
const MANIFEST=JSON.parse(fs.readFileSync(path.join(root,'chrome-extension/manifest.json'),'utf8'));
function worker(saved=null,hold=null,options={}){
 const stored={lastBatch:saved};if(options.stored)Object.assign(stored,options.stored);
 const calls=[],requests=[],events=[],senders={},reloads=[];
 // 未列出的 chrome.* 命名空间一律自动桩成"返回 Promise 的函数"，onXxx 自动收集监听器：
 // background.js 会随能力增长调用新的 API（offscreen/sidePanel/alarms…），逐条手写 mock 会让
 // 这个基座每次加功能都坏一次（它就是因此从 v2.1.0 起一直跑不起来的）。
 const mkNs=(name,over={})=>{const t=Object.assign({},over);return new Proxy(t,{
  get(o,k){const key=String(k);
   if(key in o)return o[key];
   if(/^on[A-Z]/.test(key)){o[key]={addListener:f=>{(senders[name+'.'+key]=senders[name+'.'+key]||[]).push(f);return true;},removeListener:()=>{}};return o[key];}
   o[key]=()=>Promise.resolve(undefined);return o[key];},
  set(o,k,v){o[k]=v;return true;}});};
 const clone=(v)=>JSON.parse(JSON.stringify(v));
 const ext={runtime:mkNs('runtime',{id:'test-id',
   sendMessage:async(m)=>{if(m&&m.__cft===true)events.push(m);return undefined;},
   getManifest:()=>Object.assign({},MANIFEST,{version:options.extVersion||MANIFEST.version}),
   reload:()=>{reloads.push(Date.now());},
   getContexts:async()=>[],getURL:(p)=>'chrome-extension://test-id/'+p,lastError:undefined}),
  storage:{local:{get:async()=>clone(stored),set:async v=>Object.assign(stored,clone(v)),remove:async k=>{delete stored[k];}}},
  action:mkNs('action'),tabs:mkNs('tabs',{query:async()=>[],
   sendMessage:async()=>{const e=new Error('Could not establish connection. Receiving end does not exist.');throw e;}}),
  offscreen:mkNs('offscreen'),downloads:mkNs('downloads'),contextMenus:mkNs('contextMenus'),
  windows:mkNs('windows'),sidePanel:mkNs('sidePanel'),alarms:mkNs('alarms'),notifications:mkNs('notifications')};
 // 冷启动补回链路要在模块级就跑，所以标签页清单与闹钟状态必须在加载前就给到位
 if(options.tabs) ext.tabs.query=async()=>options.tabs.map((t)=>Object.assign({},t));
 if(options.alarmsAll) ext.alarms.getAll=async()=>options.alarmsAll.map((n)=>({name:n}));
 if(options.scripting) ext.scripting=options.scripting;
 const ctx=vm.createContext({chrome:ext,URL,URLSearchParams,AbortController,setTimeout,clearTimeout,
  setInterval,clearInterval,queueMicrotask,console,TextEncoder,TextDecoder,
  structuredClone:(v)=>clone(v),crypto:{randomUUID:()=>'00000000-0000-4000-8000-000000000000'},
  fetch:async(url,o)=>{const body=o&&o.body?JSON.parse(o.body):{};requests.push({url:String(url),body});if(String(url).includes('/api/download'))calls.push(body.url);if(hold)await hold;
   const payload=Object.assign({ok:true},String(url).includes('/api/ext/tabs')?(options.tabIndexData||{}):{});
   return {ok:true,json:async()=>payload};}});
 vm.runInContext(common,ctx);loadIn(ctx,'chrome-extension/background.js');
 // MV3 语义：处理器可以（a）同步返回答复、（b）返回 Promise、（c）返回 true 之后异步 sendResponse。
 // background.js 走的是 (c)，所以三条都要接住，否则断言只会拿到 undefined。
 const message=(type,urls,sender={id:'test-id'})=>new Promise((resolve,reject)=>{
  let done=false;const finish=(v)=>{if(!done){done=true;clearTimeout(timer);resolve(v);}};
  const timer=setTimeout(()=>{if(!done){done=true;reject(new Error('消息无答复（sendResponse 未调用）: '+type));}},3000);
  let ret;
  for(const f of (senders['runtime.onMessage']||[])){
   ret=f({type,urls},sender,finish);
   if(ret!==false&&ret!==undefined)break;}
  if(ret&&typeof ret.then==='function')ret.then(finish,(e)=>finish(null));
  else if(ret!==true)finish(ret===false?null:(ret||null));});
 const listenerCount=()=>(senders['runtime.onMessage']||[]).length;
 return {ctx,calls,requests,stored,message,events,senders,listenerCount,reloads};
}
const wait=async f=>{for(let i=0;i<200;i++){if(await f())return;await new Promise(r=>setTimeout(r,10));}throw Error('timeout');};
(async()=>{
 const w=worker(),T=w.ctx.CftTabs;
 assert.equal(T.modelLink('https://evil.example/models/1'),null);assert.equal(T.modelLink('https://civitai.red.evil.example/models/1'),null);assert.equal(T.modelLink('https://civitai.red/models/bad'),null);assert.equal(T.modelLink('https://civitai.red/models/1?fileId=2&fileId=3'),null);ok('只接受合法 C 站模型 URL');
 const links=T.candidates([{id:1,url:'https://civitai.red/models/1/a?modelVersionId=11#x',title:'demo | Civitai'},{id:2,url:'https://www.civitai.com/models/1/b?modelVersionId=11',title:'duplicate'},{id:3,url:'https://civitai.red/models/1/a?modelVersionId=12',title:'different version'},{id:4,url:'https://civitai.red/models/1?modelVersionId=11&fileId=9'},{id:5,url:'https://example.invalid/private',title:'private'}]);
 assert.equal(links.length,3);assert.equal(links[0].copies,2);assert.equal(links[0].title,'demo');ok('跨 red/com 合并重复页，保留不同版本和 fileId，排除无关标签');
 assert(!T.modelLink('https://civitai.red/models/1?token=secret&utm_source=test').url.includes('secret'));ok('不发送 URL 中的 token 或追踪参数');
 assert.equal(await w.message('cft-batch-start',['https://civitai.red/models/1'],{id:'other'}),null);assert.equal(w.calls.length,0);ok('拒绝其它来源的扩展消息');
 assert.equal((await w.message('cft-batch-start',['https://civitai.red/models/1','https://evil.example/models/2'])).ok,false);assert.equal(w.calls.length,0);ok('批量含无效链接时零发送');
 let release;const hold=new Promise(r=>release=r),running=worker(null,hold);
 assert((await running.message('cft-batch-start',['https://civitai.red/models/1','https://civitai.red/models/2'])).ok);assert.equal((await running.message('cft-batch-start',['https://civitai.red/models/3'])).ok,false);ok('批量提交互斥，不重复启动');
 release();await wait(()=>!running.stored.lastBatch?.running);assert.equal(running.calls.length,2);assert(running.stored.lastBatch.items.every(r=>r.status==='sent'));ok('无需 popup 存活，后台继续按顺序提交全部勾选链接');
 assert.equal((await running.message('cft-batch-start',['https://civitai.red/models/1'])).ok,false);assert((await running.message('cft-batch-clear')).ok);assert((await running.message('cft-batch-start',['https://civitai.red/models/1'])).ok);await wait(()=>!running.stored.lastBatch?.running);ok('重复发送必须显式清除记录，CFT 任务不删除');
 const interrupted=worker({id:'old',running:true,items:[{key:'a',status:'sending'},{key:'b',status:'waiting'},{key:'c',status:'sent'}]});const status=await interrupted.message('cft-batch-status');assert.equal(status.job.items[0].status,'uncertain');assert.equal(status.job.items[1].status,'failed');assert.equal(interrupted.calls.length,0);ok('浏览器重启后中断记录可见，绝不自动重试未知请求');
 // 标签页变化 → 后台把 C 站模型页索引推到本机桥；token 与站外页不得进入载荷
 const ti=worker();
 const modelUrl='https://civitai.red/models/1264662/live-wallpaper-style?modelVersionId=3351608&token=SECRET';
 ti.ctx.chrome.tabs.query=async()=>[{id:1,windowId:1,title:'Live Wallpaper Style | Civitai',url:modelUrl}];
 const tiTab={id:1,windowId:1,title:'Live Wallpaper Style | Civitai',url:modelUrl};
 for(const f of (ti.senders['tabs.onUpdated']||[])) f(1,{url:modelUrl},tiTab);
 await wait(()=>ti.requests.some(r=>r.url.includes('/api/ext/tabs')&&r.body&&r.body.tabs&&r.body.tabs.length));
 const tiReq=[...ti.requests].reverse().find(r=>r.url.includes('/api/ext/tabs')&&r.body&&r.body.tabs&&r.body.tabs.length);
 assert.equal(tiReq.body.v,1);assert.equal(tiReq.body.source,'chrome-extension');assert.equal(tiReq.body.ext_version,MANIFEST.version);assert.equal(tiReq.body.tabs[0].modelId,'1264662');assert.equal(tiReq.body.tabs[0].versionId,'3351608');assert(!JSON.stringify(tiReq.body).includes('SECRET'));ok('后台在标签页变化后把模型页索引推到桥（不含 token，带扩展版本）');
 // 开发态：桥回传新的 dev_reload_at → 扩展 runtime.reload()；同一时间戳只触发一次（storage 防循环）
 const dr=worker(null,null,{tabIndexData:{dev_reload_at:123456}});
 dr.ctx.chrome.tabs.query=async()=>[tiTab];
 const fireAlarm=(w)=>{for(const f of (w.senders['alarms.onAlarm']||[])) f({name:'cftTabIndex'});};
 fireAlarm(dr);await wait(()=>dr.reloads.length===1);
 assert.equal(dr.stored.cftDevReloadSeenV1,123456);
 fireAlarm(dr);await new Promise(r=>setTimeout(r,80));
 assert.equal(dr.reloads.length,1);ok('开发态重载标记只生效一次，不会因心跳反复重载');
 // 注入面是隐私声明的一部分，必须钉住：Chrome 只允许在 C 站的 models/images/posts 注入，
 // Firefox 侧目前根本没有 content script（它没有 offscreen，能力集不同，见任务#4）。
 const cm=JSON.parse(fs.readFileSync(path.join(root,'chrome-extension/manifest.json'),'utf8'));
 const fm=JSON.parse(fs.readFileSync(path.join(root,'firefox-extension/manifest.json'),'utf8'));
 for(const [n,m] of [['chrome',cm],['firefox',fm]]){assert(m.permissions.includes('storage'),n+' 需要 storage');assert(!/<all_urls>/.test(JSON.stringify(m)),n+' 不得申请 <all_urls>');}
 // scripting 只允许一个用途：把 manifest 自己声明过的文件注回已匹配的 C 站页面（免刷新补胶囊）。
 // 见 docs/extension-status-autoshow.md §3。Firefox 不注入网页，因此连权限都不许申请。
 assert(cm.permissions.includes('scripting'),'Chrome 需要 scripting 才能免刷新补回状态胶囊');
 assert(!fm.permissions.includes('scripting'),'Firefox 不注入网页，不该申请 scripting');
 const bgSrc=fs.readFileSync(path.join(root,'chrome-extension/background.js'),'utf8');
 assert(!/executeScript\s*\(\s*\{[^}]*\b(func|code)\s*:/.test(bgSrc),'scripting 只能注入自家文件，不得执行任意代码');
 const cs=(cm.content_scripts||[]);
 // /models* 含列表页（阶段二卡片徽章），/models/* 是详情页；images/posts 仍只注入详情页
 assert(cs.length>0&&cs.every(c=>c.matches.every(u=>/^https:\/\/(www\.)?civitai\.(com|red)\/(models(\*|\/\*)|images\/\*|posts\/\*)$/.test(u))),
   'Chrome 注入范围只能是 C 站 models 列表/详情、images、posts');
 assert(!fs.readFileSync(path.join(root,'chrome-extension/README.md'),'utf8').includes('不注入网页'),
   'Chrome README 不得再声明"不注入网页"');
 assert(!fm.content_scripts||fm.content_scripts.length===0,'Firefox 侧仍未注入网页');
 // 会被"补回注入"的文件顶层只能 var/function：同一个隔离世界里重复声明 const/let 直接抛
 // SyntaxError，整批脚本静默失效（真机踩过：lib/util.js 顶层 const _utf8Len，胶囊再也不回来）。
 for(const c of cs)for(const f of (c.js||[])){
  const src=fs.readFileSync(path.join(root,'chrome-extension',f),'utf8');
  const bad=(src.match(/^(?:const|let|class)\s+\w+/gm)||[]);
  assert.equal(bad.length,0,f+' 顶层不能有 const/let/class（会被重复注入同一个隔离世界）：'+bad.join(' | '));}
 ok('注入面文件顶层只用 var/function，重复注入不会再声明同名 const');
 // manifest 只是个索引：引用的文件缺一个，加载时整条 content script / 侧栏就静默失效
 for(const f of ['sidepanel.html','sidepanel.js',...cs.flatMap(c=>[...(c.js||[]),...(c.css||[])])]){
  assert(fs.existsSync(path.join(root,'chrome-extension',f)),'Chrome manifest 引用的文件必须存在：'+f);}
 // MV3 扩展页面 CSP 禁止内联脚本：写内联 = 那段逻辑在真扩展里静默不执行（HTTP 测试发现不了）
 for(const f of ['popup.html','sidepanel.html']){
  const html=fs.readFileSync(path.join(root,'chrome-extension',f),'utf8');
  assert(!/<script(?![^>]*\bsrc=)[^>]*>[\s\S]*?<\/script>/i.test(html),f+' 不允许内联脚本（会被 MV3 CSP 拦截）');}
 assert(fs.readFileSync(path.join(root,'chrome-extension/sidepanel.js'),'utf8').includes('__CFT_SIDE_PANEL__'),
  'sidepanel.js 必须设置侧栏标记');
 assert.equal(fs.readFileSync(path.join(root,'firefox-extension/tabs-common.js'),'utf8'),common,'共享的 tabs-common 两边必须逐字节同步');
 for(const [n,dir] of [['chrome','chrome-extension'],['firefox','firefox-extension']]){
  const v=JSON.parse(fs.readFileSync(path.join(root,dir,'manifest.json'),'utf8')).version;
  const first=fs.readFileSync(path.join(root,dir,'README.md'),'utf8').split('\n')[0];
  assert(first.includes(v),n+' README 标题里的版本号必须与 manifest 一致（当前 '+first+' / '+v+'）');}
 ok('权限与注入面符合声明（Chrome 2.x 注入 C 站页面，Firefox 未注入）');
 // Playwright 装在本机 .tools/ 里（不进仓库依赖），路径能从仓库布局推出来就别让人每次现配
 const pwPath=process.env.PLAYWRIGHT_PATH||path.join(root,'.tools','node_modules','playwright');
 process.env.PLAYWRIGHT_BROWSERS_PATH=process.env.PLAYWRIGHT_BROWSERS_PATH||path.join(root,'.tools','ms-playwright');
 let chromium=null;
 try{({chromium}=require(pwPath));}catch(e){console.log('SKIP 浏览器部分：playwright 不可用（'+pwPath+'）：'+e.message);}
 if(chromium){
 const server=http.createServer((req,res)=>{const p=path.join(root,'chrome-extension',new URL(req.url,'http://localhost').pathname);fs.readFile(p,(e,b)=>{if(e){res.writeHead(404).end();return;}res.setHeader('Content-Type',p.endsWith('.js')?'application/javascript':p.endsWith('.css')?'text/css':'text/html');res.end(b);});});await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
   window.sent=[];window.job=null;
   const all=[{id:1,windowId:1,title:'Demo A | Civitai',url:'https://civitai.red/models/1/a?modelVersionId=11'},{id:2,windowId:1,title:'Demo A duplicate',url:'https://civitai.com/models/1/b?modelVersionId=11'},{id:3,windowId:2,title:'Demo B | Civitai',url:'https://civitai.red/models/2/b'},{id:4,windowId:1,title:'Not a model',url:'https://example.invalid/private'}];
   window.chrome={tabs:{query:async q=>q.active?[all[3]]:q.currentWindow?all.filter(t=>t.windowId===1):all},runtime:{sendMessage:async m=>{if(m.type==='cft-batch-start'){window.sent.push(m.urls);window.job={id:'job',running:false,items:m.urls.map(url=>({...CftTabs.modelLink(url),status:'sent',msg:'accepted'}))};}if(m.type==='cft-batch-clear')window.job=null;return {ok:true,job:window.job};}}};
   // 假桥必须回**真的 CFT 健康体**（ok + app + version≥MIN_APP_VERSION）：popup 的发送按钮
   // 现在按 cftHealth 的判定禁用，随便回 {ok:true} 会被判成"不是 CFT"，测的就是按钮禁用了。
   // 假 Response 还要同时给 json() 和 text()（lib 的 fetchJson 读的是 resp.text()），并按真实
   // 字段形状回 C 站公开接口——否则整批富化"取数失败"，界面截图就成了假的故障现场。
   // 模型名沿用行标题（Demo A / Demo B），免得下面按 hasText 过滤的断言漂走。
   window.fetch=async(u)=>{
     const url=String(u), NAMES={1:'Demo A',2:'Demo B'};
     let body;
     if(url.indexOf('/api/v1/models/')>=0){
       const id=String(url.split('/').pop());
       body={id:Number(id)||1,name:NAMES[id]||('Demo Model '+id),type:'LORA',tags:['demo'],nsfw:false,
         stats:{downloadCount:1234,ratingCount:56},
         modelVersions:[{id:11,name:'v1.0',baseModel:'SD 1.5',trainedWords:['demo'],
           files:[{name:'demo.safetensors',sizeKB:102400}],
           images:[{url:'https://image.civitai.com/x/demo/original=true/1.jpeg',width:1024,height:1024,nsfwLevel:1}]}]};
     }else if(url.indexOf('/api/v1/model-versions/mini/')>=0){
       body={id:11,modelId:1,name:'v1.0',baseModel:'SD 1.5',type:'LORA'};
     }else{
       body={ok:true,app:'CivitaiFreeTool',version:'2.6.30',mode:'browser',window:false};
     }
     return {ok:true,status:200,json:async()=>body,text:async()=>JSON.stringify(body)};
   };
  });
  await page.goto('http://127.0.0.1:'+server.address().port+'/popup.html');await page.waitForFunction(()=>document.querySelectorAll('.tab-row').length===2);assert(await page.locator('#batchPanel').isVisible());ok('当前页非模型时仍可批量其它已打开模型页');await page.setViewportSize({width:420,height:640});await page.screenshot({path:path.join(root,'docs/screenshots/extension-batch.png')});
  assert.equal(await page.locator('.tab-row input:checked').count(),2);await page.locator('.tab-row').filter({hasText:'Demo B'}).locator('input').uncheck();await page.locator('#sendBatch').click();await page.waitForFunction(()=>window.sent.length===1);assert.deepEqual(await page.evaluate(()=>window.sent),[['https://civitai.red/models/1/a?modelVersionId=11']]);ok('按勾选精确发送，不包含未选或其它网页');
  assert.equal(await page.locator('.tab-row input:disabled').count(),1);ok('已提交页面保留状态，避免再次误发');
  await page.locator('#scope').selectOption('current');await page.waitForFunction(()=>document.querySelectorAll('.tab-row').length===1);ok('范围可切换当前窗口 / 所有窗口');
  await page.locator('#modeCurrent').click();assert(await page.locator('#sendCurrent').isDisabled());assert.equal(errors.length,0);ok('非模型当前页不可误提交，界面无脚本错误');
 }finally{await browser.close();await new Promise(r=>server.close(r));}
 }
 // 免点击自动显示：开机/更新/心跳都让后台自己查一遍已打开的模型页并点亮徽章
 // 这三个世界专门测"生命周期事件"这一路，所以把冷启动那一轮关掉（闹钟还在 + 刚补过 → bootWake 自己跳过），
 // 否则 vm 里异步链与测试改写 stub 的先后会漂，注入次数就不是 1 轮了。冷启动那一路单独在下面两组里钉。
 const booted=(extra)=>Object.assign({alarmsAll:['cftTabIndex','cft-status-heartbeat'],
   stored:{cftBootWakeAtV1:Date.now()}},extra||{});
 {
  const auto=worker(null,null,booted());
  const tabs=[{id:1,url:'https://civitai.red/models/11/x?token=SECRET'},{id:2,url:'https://civitai.red/models/22/y?modelVersionId=33'},
              {id:3,url:'https://civitai.red/models/44',discarded:true},{id:4,url:'https://evil.example/models/55'}];
  auto.ctx.chrome.tabs.query=async()=>tabs;
  const badges=[],alarmsCreated=[],injected=[];
  auto.ctx.chrome.scripting={executeScript:async(o)=>{injected.push(o);},insertCSS:async()=>{}};
  auto.ctx.chrome.action.setBadgeText=async(o)=>{badges.push(o);};
  auto.ctx.chrome.alarms.create=(name,info)=>{alarmsCreated.push([name,info]);};
  for(const f of (auto.senders['runtime.onStartup']||[])) await f();
  await wait(()=>auto.requests.some(r=>r.url.includes('/api/model/status')));
  const req=auto.requests.find((r)=>r.url.includes('/api/model/status'));
  assert.equal(req.body.items.length,2,'只发已打开且匹配的模型页，跳过休眠标签与站外页');
  assert(!JSON.stringify(req.body).includes('SECRET'),'发的是清洗过的链接，标签页原始 URL 里的 token 不带');
  assert.deepEqual(injected.map((i)=>i.target.tabId),[1,2],'探针无回音才注入（休眠页与站外页零注入）');
  assert.deepEqual(injected[0].files,MANIFEST.content_scripts[0].js,'注入清单取自 manifest 本身，不写第二份');
  assert.ok(alarmsCreated.some((a)=>a[0]==='cft-status-heartbeat'&&a[1].periodInMinutes===2),'2 分钟状态心跳已登记');
  await wait(()=>badges.filter((b)=>Number.isFinite(b.tabId)).length>=2);
  ok('无需点图标：后台补回脚本、批量查状态并点亮逐标签徽章');
  const fired=auto.requests.length;
  for(const f of (auto.senders['runtime.onInstalled']||[])) await f();
  await new Promise((r)=>setTimeout(r,80));
  assert.equal(auto.requests.length,fired,'刚查过就不重复打一轮');
  ok('安装/更新与启动共用同一轮在途查询，不叠加请求');
 }
 {
  const alive=worker(null,null,booted());
  alive.ctx.chrome.tabs.query=async()=>[{id:7,url:'https://civitai.red/models/77'}];
  let injects=0; alive.ctx.chrome.scripting={executeScript:async()=>{injects++;},insertCSS:async()=>{}};
  alive.ctx.chrome.tabs.sendMessage=async()=>({ok:true});   // 页面里的脚本还在
  for(const f of (alive.senders['runtime.onInstalled']||[])) await f();
  await wait(()=>alive.requests.some(r=>r.url.includes('/api/model/status')));
  assert.equal(injects,0,'有接收者就不重复注入，避免第二个胶囊');
  ok('已注入过的页面不被二次注入');
 }
 {
  const many=worker(null,null,booted());
  many.ctx.chrome.tabs.query=async()=>Array.from({length:80},(_,i)=>({id:i+1,url:'https://civitai.red/models/'+(100+i)}));
  for(const f of (many.senders['runtime.onStartup']||[])) await f();
  await wait(()=>many.requests.some(r=>r.url.includes('/api/model/status')));
  assert.equal(many.requests.find((r)=>r.url.includes('/api/model/status')).body.items.length,60,'一次最多 60 个标签，不把 686 个标签的请求打爆');
  ok('批量查询有上限');
 }
 {
  // 冷启动补回：chrome.runtime.reload() 之后 Chrome 不一定再把 onInstalled 交给新的 SW，
  // 只等事件把 SW 唤醒。若唤醒只挂在 onInstalled/onStartup 上，"重载后免刷新"这条链就永远不跑。
  const injects=[],badges=[];
  const boot=worker(null,null,{
    tabs:[{id:5,url:'https://civitai.red/models/55/x'}],
    alarmsAll:['cftTabIndex'],                       // 状态心跳不见了＝刚被重载/更新过
    scripting:{executeScript:async(o)=>{injects.push(o);},insertCSS:async()=>{}},
  });
  boot.ctx.chrome.action.setBadgeText=async(o)=>{badges.push(o);};
  await wait(()=>boot.requests.some(r=>r.url.includes('/api/model/status')));
  assert.deepEqual(injects.map((i)=>i.target.tabId),[5],'SW 一醒就把脚本补回已匹配的页面，不需要任何生命周期事件');
  assert.deepEqual(injects[0].files,MANIFEST.content_scripts[0].js,'补回的清单仍取自 manifest');
  assert.equal(boot.stored.cftBootWakeAtV1>0,true,'冷启动时刻落库，供下次判断"是不是又白醒了一次"');
  await wait(()=>badges.filter((b)=>Number.isFinite(b.tabId)).length>=1);
  ok('免点击链路不依赖 onInstalled：SW 冷启动即补脚本、查状态、点亮徽章');
  // 反过来也要成立：闹钟还在、刚补过，空闲重启不许再打一轮
  const bootedAt=Date.now();
  const quiet=worker(null,null,{
    tabs:[{id:6,url:'https://civitai.red/models/66/x'}],
    alarmsAll:['cftTabIndex','cft-status-heartbeat'],
    stored:{cftBootWakeAtV1:bootedAt},
    scripting:{executeScript:async()=>{},insertCSS:async()=>{}},
  });
  await new Promise((r)=>setTimeout(r,150));
  assert.equal(quiet.requests.filter((r)=>r.url.includes('/api/model/status')).length,0,'刚补过就不再重复查');
  assert.equal(quiet.stored.cftBootWakeAtV1,bootedAt,'跳过时不覆盖时间戳');
  ok('SW 空闲重启不会把本机桥再打一遍（闹钟还在 + 90 秒内补过 → 安静跳过）');
 }
 console.log('PASSED '+passed);
})().catch(e=>{console.error(e);process.exitCode=1;});
