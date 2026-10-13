/* Real Chromium, synthetic models + mocked bridge: 瀑布流多图轮播必须真的起播、换帧、收摊。
   回归的是这个缺陷：真正滚动的是 main.content，而预取监听挂在 window（冒泡）与 #mmMasonry（它自己不滚），
   两处一次都没触发过 → 首屏之后多图帧再也不预取 → hover 无帧可轮，轮播整体哑火。 */
const fs=require('node:fs'),http=require('node:http'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const root=path.resolve(__dirname,'..'),web=path.join(root,'web');
const source=fs.readFileSync(path.join(__dirname,'test_ui_preferences.cjs'),'utf8').replace(/\r\n/g,'\n');
const begin=source.indexOf('await page.addInitScript(')+'await page.addInitScript('.length,
      end=source.indexOf('\n    });\n    const url',begin)+6,init=eval('('+source.slice(begin,end)+')');
const hangSockets=new Set();
const server=http.createServer((req,res)=>{
  const url=new URL(req.url,'http://localhost');
  // /hang.mp4：视频请求吊住不返回。Chromium 因此既不解码也不报错，<video> 会带着 poster 一直留在 DOM 上，
  // 这正是「鼠标移过去之前」的真实状态；换成会立刻失败的 URL 元素会被 onerror 摘掉，poster 就没处可查。
  if(url.pathname==='/hang.mp4'){const s=res.socket;s.setTimeout(0);hangSockets.add(s);s.on('close',()=>hangSockets.delete(s));return;}
  const p=path.resolve(web,'.'+decodeURIComponent(url.pathname==='/'?'/index.html':url.pathname));
  if(!p.startsWith(web+path.sep)){res.writeHead(403).end();return;}
  fs.readFile(p,(e,b)=>{if(e){res.writeHead(404).end();return;}
    res.setHeader('Content-Type',p.endsWith('.js')?'application/javascript':p.endsWith('.css')?'text/css':'text/html; charset=utf-8');res.end(b);});
});
let passed=0;function ok(s){console.log('OK '+s);passed++;}
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:720}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(init);
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.waitForFunction(()=>window.__ready&&document.querySelector('#settingsForm [data-key=show_file_paths]'));
  await page.evaluate(()=>{
    window.fixtureCfg.default_view='waterfall'; state.cfg.default_view='waterfall'; applyUiAppearance&&applyUiAppearance();
    const shot=(color,label)=>{const c=document.createElement('canvas');c.width=300;c.height=420;const g=c.getContext('2d');
      g.fillStyle=color;g.fillRect(0,0,300,420);g.fillStyle='#fff';g.font='64px sans-serif';g.fillText(label,24,220);return c.toDataURL('image/jpeg');};
    const rows=[],multi={};
    for(let i=0;i<8;i++){const p='D:/Demo/models/m'+i+'.safetensors';
      rows.push({path:p,name:'m'+i+'.safetensors',civitai_name:'模型 '+i,modelId:i,ver:'v1',base:'Anima',type:'LORA',size:1048576});
      multi[p]=[[shot('#123456','A'+i),false],[shot('#654321','B'+i),false],[shot('#228822','C'+i),false]];
      state.coverCache.set(p,multi[p][0][0]);}
    window.fixtureMultiCovers=multi; window.fixturePaths=rows.map(r=>r.path);
    window.fixtureLastFrame=multi[rows[7].path][2][0];
    state.models=rows; window.fixtureScanRows=rows;
    // 必须先切到「模型管理」页：index.html 的默认 active 标签是 download，
    // #page-models 处于 display:none 时卡片全是零尺寸，waitForSelector 只等到超时。
    switchPage('models');
    mmSetView('masonry'); state.mmBaseF=''; state.mmTypeF=''; applyMmFilter();
    document.querySelector('.content').scrollTop=0;
  });
  await page.waitForSelector('#mmMasonry .ms-card');
  const calls=()=>page.evaluate(()=>window.fixtureCalls.filter(c=>c.method==='get_covers_multi').map(c=>c.args[0]));
  const firstCalls=await calls();
  assert(firstCalls.flat().length>0,'首屏应预取可见卡片的多图帧');
  assert(!firstCalls.flat().includes(await page.evaluate(()=>window.fixturePaths[7])),'首屏外的卡片此时不该被请求');
  ok('首屏只预取视口内的卡片');

  // 滚动到最底部：真正的滚动容器是 main.content，冒泡监听收不到——回归点
  await page.evaluate(()=>{const s=document.querySelector('.content');s.scrollTop=s.scrollHeight;});
  await page.waitForFunction(p=>window.fixtureCalls.some(c=>c.method==='get_covers_multi'&&c.args[0].includes(p)),
                             await page.evaluate(()=>window.fixturePaths[7]),{timeout:5000});
  ok('滚出首屏的卡片：滚动会驱动多图预取（capture 监听生效）');

  const lastCard=page.locator('.ms-card').nth(7);
  await lastCard.hover({position:{x:60,y:60}});
  await page.waitForFunction(()=>document.querySelectorAll('.ms-card:nth-last-child(1) .ms-img-alt').length>0,null,{timeout:5000});
  ok('hover 起播：卡片上挂出叠层帧');

  const shape=await page.evaluate(()=>{const c=document.querySelector('#mmMasonry .ms-card:hover');
    const alts=[...c.querySelectorAll('.ms-img-alt')];return {n:alts.length,src:alts[0]?String(alts[0].src):'',cover:String(c.querySelector('.ms-img:not(.ms-img-alt)').src)};});
  assert.equal(shape.n,1,'同一时刻只应有一层叠帧');
  assert(shape.src!==shape.cover,'轮播应从第 2 帧起，不能一直显示主封面');
  ok('叠层帧不叠罗汉，且从主封面之外的帧开始');

  const seen=new Set([shape.src]);
  for(let i=0;i<2;i++){await page.waitForTimeout(3400);
    // 采样必须取 DOM 里最后一张叠帧：新帧是 append 上去的，淡入交叉期 alts[0] 还是没摘的旧帧，
    // 读第一张会把"已经换帧"采样成"没换"（这条检查点此前就是这么误报的）。
    const s=await page.evaluate(()=>{const c=document.querySelector('#mmMasonry .ms-card:hover');const alts=[...(c?c.querySelectorAll('.ms-img-alt'):[])];
      return alts.length?String(alts[alts.length-1].src):'';});
    assert(s,'换帧过程中叠层帧不能消失');seen.add(s);}
  assert(seen.size>=2,'约 3 秒/帧应至少换过一次帧');
  ok('hover 期间按 ~3 秒/帧继续换帧（真的在循环，不是挂个静止叠层）');

  // 回归点：关掉自动轮播原来只 clearInterval，停下一次心跳，但自动播放器已经挂上的叠帧永远留在卡片上
  // （实测关设置后 2 张非悬停卡片各剩 1 层叠帧，界面表现是"关了自动轮播，图停在最后一帧不动"，
  // 要等 hover 或重渲染才消失）。现在关的时候连 DOM 一起收。
  await page.mouse.move(8,8);
  const seeded=await page.evaluate(()=>{document.querySelector('.content').scrollTop=0;
    const c=[...document.querySelectorAll('#mmMasonry .ms-card')].find(x=>!x.matches(':hover')&&_carouselHasFrames(state.coverCacheMulti.get(x.dataset.path)));
    if(!c)return null; const st=_carouselPlayer(c,c.dataset.path,true);
    if(st){st.auto=true;_autoPlayers.add(c);_carouselAdvance(st);} return c.dataset.path;});
  assert(seeded,'需要一个未悬停且有多图帧的卡片来复现');
  await page.waitForFunction(()=>document.querySelectorAll('#mmMasonry .ms-img-alt').length>0,null,{timeout:3000});
  await page.evaluate(()=>{state.cfg.masonry_autoplay=false;_mmAutoSetInterval(false);});
  await page.waitForFunction(()=>document.querySelectorAll('#mmMasonry .ms-img-alt').length===0,null,{timeout:4000});
  ok('关掉自动轮播后，自动播放器挂上的叠帧立即收回');

  // 以下检查点都断言"整个网格叠层清零"，而自动轮播会在别的卡片上合法挂帧，所以保持节拍关闭
  await page.mouse.move(8,8);
  await page.waitForFunction(()=>document.querySelectorAll('#mmMasonry .ms-img-alt').length===0,null,{timeout:4000});
  ok('移开鼠标后叠层帧清理干净');

  // 慢网：预载帧在鼠标移开之后才回来。旧写法 _stopCarousel 只清当前那张叠层，不取消在途预载，
  // 晚到的帧会挂到已经停播的卡片上变成永久孤儿图（实测叠层数 2→移开后仍剩 1）。
  await page.evaluate(()=>{const Real=window.Image;
    window.Image=function(){const el=new Real();let s='';
      Object.defineProperty(el,'src',{set(v){s=v;setTimeout(()=>el.dispatchEvent(new Event('load')),1200);},get(){return s;}});return el;};
    window.__restoreImage=()=>{window.Image=Real;};});
  // 帧必须走远程 URL：data: 帧在 _carouselAdvance 里直接挂载（本地 b64 立即可用），
  // 那样慢网预载这条路径根本不会被经过，"预载未回来前不该有叠层"也就无从成立。
  const slowPath=await page.evaluate(()=>{const p=window.fixturePaths[0];
    state.coverCacheMulti.set(p,[['https://cdn.example/slow0.jpg',false],['https://cdn.example/slow1.jpg',false]]);return p;});
  await page.evaluate(()=>{document.querySelector('.content').scrollTop=0;});
  await page.locator('.ms-card').first().hover({position:{x:60,y:60}});
  await page.waitForTimeout(250);
  assert.equal(await page.evaluate(()=>document.querySelectorAll('#mmMasonry .ms-img-alt').length),0,'预载未回来前不该有叠层');
  await page.mouse.move(8,8);
  await page.waitForTimeout(2400);
  assert.equal(await page.evaluate(()=>document.querySelectorAll('#mmMasonry .ms-img-alt').length),0,'移开后晚到的预载帧挂成了孤儿叠图');
  await page.evaluate(p=>{window.__restoreImage();state.coverCacheMulti.set(p,window.fixtureMultiCovers[p]);},slowPath);
  await page.locator('.ms-card').first().hover({position:{x:60,y:60}});
  await page.waitForFunction(()=>document.querySelectorAll('#mmMasonry .ms-card:first-child .ms-img-alt').length===1,null,{timeout:6000});
  await page.mouse.move(8,8);
  await page.waitForFunction(()=>document.querySelectorAll('#mmMasonry .ms-img-alt').length===0,null,{timeout:4000});
  ok('移开鼠标后晚到的预载帧不挂图，之后重新 hover 仍能正常起播收摊');

  // hover 兜底：缓存里没这帧（例如还没滚到就先 hover）时，当场补一次请求并起播
  await page.evaluate(p=>{state.coverCacheMulti.delete(p);_multiTried.delete(p);document.querySelector('.content').scrollTop=0;},
                      await page.evaluate(()=>window.fixturePaths[0]));
  await page.locator('.ms-card').first().hover({position:{x:60,y:60}});
  await page.waitForFunction(()=>document.querySelectorAll('#mmMasonry .ms-card:first-child .ms-img-alt').length>0,null,{timeout:6000});
  ok('未预取过的卡片：hover 当场补请求并起播');

  // 无多图模型的重复 hover 不该反复问后端
  await page.evaluate(()=>{const p=window.fixturePaths[1];delete window.fixtureMultiCovers[p];
    state.coverCacheMulti.delete(p);_multiTried.delete(p);document.querySelector('.content').scrollTop=0;});
  const target=await page.evaluate(()=>window.fixturePaths[1]);
  const asked=async()=>(await calls()).flat().filter(x=>x===target).length;
  const askedBefore=await asked();
  for(let i=0;i<2;i++){await page.mouse.move(8,8);await page.locator('.ms-card').nth(1).hover({position:{x:60,y:60}});await page.waitForTimeout(700);}
  assert.equal(await asked(),askedBefore+1,'没有多图帧的模型，重复 hover 只该问一次');
  ok('无多图模型重复 hover 不重复请求（会话内记一次）');

  // 淡入淡出：新帧必须以 opacity:0 插入、下一帧才抬到 1（旧写法在 append 前就写死 style.opacity="1"，等于自己把过渡废掉，切换很生硬）
  const fade=await page.evaluate(async()=>{
    const c=[...document.querySelectorAll('#mmMasonry .ms-card')].find(x=>{const l=state.coverCacheMulti.get(x.dataset.path);return Array.isArray(l)&&l.length>=2;});
    _stopCarousel(c);
    const st=_carouselPlayer(c,c.dataset.path,true);
    _carouselAdvance(st);
    const seen=[];
    for(let i=0;i<30;i++){ await new Promise(r=>requestAnimationFrame(r));   // 30 帧≈0.5s，跨过 0.35s 的过渡才谈得上"收敛"
      const a=[...c.querySelectorAll('.ms-img-alt')];
      if(a.length) seen.push({n:a.length,op:a.map(x=>Number(getComputedStyle(x).opacity))}); }
    return {first:seen[0]||null,last:seen[seen.length-1]||null,maxN:Math.max(0,...seen.map(s=>s.n)),
            mid:seen.some(s=>s.op.some(v=>v>0.05&&v<0.95))};
  });
  assert(fade.first,'淡入探针没抓到叠层帧');
  assert(fade.first.op[0]<0.9,'新帧不是从透明淡入的（computed opacity 起始 '+fade.first.op[0]+'）');
  assert(fade.mid,'过渡没有 intermediate 值：等于插入后延迟硬切，不是淡入');
  assert(fade.last.op[fade.last.op.length-1]>0.9,'淡入没收敛到不透明');
  assert(fade.maxN<=2,'交叉期最多旧新两层，实测 '+fade.maxN);
  ok('换帧是淡入：以透明插入、下一帧抬起，交叉期最多两层');

  // 自动轮播：鼠标完全不碰网格，也要自己换帧
  await page.mouse.move(4,4);
  await page.evaluate(()=>{state.cfg.masonry_autoplay=true;_mmAutoSetInterval(true);
    document.querySelector('.content').scrollTop=0;
    [...document.querySelectorAll('#mmMasonry .ms-card')].forEach(c=>_stopCarousel(c));});
  await page.waitForFunction(()=>document.querySelectorAll('#mmMasonry .ms-img-alt').length>0,null,{timeout:9000});
  const autoA=await page.evaluate(()=>[...document.querySelectorAll('#mmMasonry .ms-img-alt')].map(a=>String(a.src)));
  await page.waitForFunction(prev=>{const now=[...document.querySelectorAll('#mmMasonry .ms-img-alt')].map(a=>String(a.src));
    return now.some(s=>!prev.includes(s));},autoA,{timeout:15000});
  ok('不悬停也自动轮播：无鼠标介入时自动挂帧并继续换到新的帧');

  const perCard=await page.evaluate(()=>Math.max(...[...document.querySelectorAll('#mmMasonry .ms-card')].map(c=>c.querySelectorAll('.ms-img-alt').length)));
  assert(perCard<=2,'自动轮播下每张卡最多旧新两层，实测 '+perCard);
  ok('自动轮播按卡片各自一层（交叉期两层），不会叠罗汉');

  // ===== 视频帧的首帧静图（用户报的「模型播的是视频，首帧直接空白，鼠标移动过去才会播」） =====
  // 以下检查点全部手动驱动播放器，且保持自动节拍关闭：这些用例断言「整个网格叠层清零」，
  // 自动轮播会在别的卡片上合法挂帧。
  await page.mouse.move(4,4);
  await page.evaluate(()=>{state.cfg.masonry_autoplay=false;_mmAutoSetInterval(false);
    [...document.querySelectorAll('#mmMasonry .ms-card')].forEach(c=>_stopCarousel(c));});
  const posterDu=await page.evaluate(()=>{const c=document.createElement('canvas');c.width=240;c.height=180;
    const g=c.getContext('2d');g.fillStyle='#ff2d95';g.fillRect(0,0,240,180);return c.toDataURL('image/jpeg');});
  const vidPath=await page.evaluate(()=>window.fixturePaths[0]);
  // 吊住不返回的视频请求：Chromium 既解不出帧也不报错，<video> 会一直留在 DOM 上显示 poster——
  // 这正是「起播前」的真实状态，用 https://cdn.example 之类的话 DNS 失败会把元素直接摘掉。
  const hangSrc='http://127.0.0.1:'+server.address().port+'/hang.mp4';

  await page.evaluate(([p,poster,src])=>{
    // 走真实链路：后端原样回三元组 → prefetchMultiForCards 的映射 → 缓存 → 挂 <video>
    window.fixtureMultiCovers[p]=[window.fixtureMultiCovers[p][0],[src,true,poster],window.fixtureMultiCovers[p][2]];
    state.coverCacheMulti.delete(p); _multiTried.delete(p);
    prefetchMultiForCards([...document.querySelectorAll('#mmMasonry .ms-card')].filter(x=>x.dataset.path===p));
  },[vidPath,posterDu,hangSrc]);
  await page.waitForFunction(([p,poster])=>{const l=state.coverCacheMulti.get(p)||[];
    return l.length===3&&String((l[1]||[])[2]||'')===poster;},[vidPath,posterDu],{timeout:6000});
  const mounted=await page.evaluate(([p,poster,src])=>{
    const c=[...document.querySelectorAll('#mmMasonry .ms-card')].find(x=>x.dataset.path===p);
    _stopCarousel(c);
    const cached=(state.coverCacheMulti.get(p)||[])[1]||[];
    const st=_carouselPlayer(c,p,false);            // 悬停路径：视频帧必须挂 <video>
    _carouselAdvance(st);
    const el=c.querySelector('video.ms-img-alt');
    return {cachedPoster:String(cached[2]||''),cachedIsVideo:!!cached[1],
      attr:el?String(el.getAttribute('poster')||''):null,idl:el?String(el.poster||''):null,
      videoWidth:Number(el&&el.videoWidth||0)};
  },[vidPath,posterDu,hangSrc]);
  assert.equal(mounted.cachedIsVideo,true);
  assert.equal(mounted.cachedPoster,posterDu,'预取映射把帧三元组的第三格（首帧静图）丢掉了');
  assert(mounted.attr,'视频帧没挂出 <video>');
  assert(mounted.attr,'挂出来的 <video> 没有 poster 属性（起播前就是一块空白）');
  assert.equal(mounted.attr,posterDu,'<video> 的 poster 必须就是后端给的这一帧首帧静图');
  assert.equal(mounted.idl,posterDu);
  await page.waitForTimeout(500);
  const whilePending=await page.evaluate(([p,poster])=>{const c=[...document.querySelectorAll('#mmMasonry .ms-card')].find(x=>x.dataset.path===p);
    const el=c&&c.querySelector('video.ms-img-alt');
    return el?{poster:String(el.getAttribute('poster')||''),alt:c.querySelectorAll('.ms-img-alt').length,videoWidth:Number(el.videoWidth||0)}:null;},[vidPath,posterDu]);
  assert(whilePending,'解码中的 <video> 被摘掉了，poster 顶不住空白期');
  assert.equal(whilePending.poster,posterDu,'解码前 poster 丢了');
  assert.equal(whilePending.videoWidth,0,'这条路径本来就没解出任何视频帧（poster 是唯一有画面的东西）');
  assert.equal(whilePending.alt,1,'同一时刻只应有一层叠帧');
  ok('视频帧的 <video> 带首帧静图：三元组第三格经预取映射仍在缓存里，poster 实测 '+posterDu.length+' 字节 data URL（开头 '+mounted.attr.slice(0,22)+'…），解码前元素留在卡片上（'+whilePending.alt+' 层叠帧）');
  await page.evaluate(()=>[...document.querySelectorAll('#mmMasonry .ms-card')].forEach(c=>_stopCarousel(c)));

  // 本地落盘的视频在浏览器兜底模式下必须换同源 /api/media：页面是 http origin 时 Chromium 拒绝
  // file:/// 子资源（实测控制台 "Not allowed to load local resource"），v2.6.29 的「本地优先」
  // 在这条模式下等于视频整个不播。窗口模式（__browserMode 不置位）里 file:/// 同源可用，不能动。
  // 本用例的桥是 mock 的 pywebview（app.js 因此不会自己置 __browserMode），这里按两种模式分别钉。
  const uriMap=await page.evaluate(()=>{
    const saved=window.__browserMode;
    window.__browserMode=true;
    const http=_playableMediaUri('file:///D:/models/m.images/video_02.mp4');
    const spaced=_playableMediaUri('file:///D:/m%20a/%E6%A8%A1%E5%9E%8B.images/video_01.mp4');
    const unc=_playableMediaUri('file://server/share/m.images/video_03.mp4');
    const remote=_playableMediaUri('https://image.civitai.com/a/h1/original=true/clip1.mp4');
    const empty=_playableMediaUri('');
    window.__browserMode=false;
    const win=_playableMediaUri('file:///D:/models/m.images/video_02.mp4');
    window.__browserMode=saved;
    return {http,spaced,unc,remote,empty,win};
  });
  assert.equal(uriMap.http,'/api/media?p='+encodeURIComponent('D:/models/m.images/video_02.mp4'),
    '浏览器模式没把本地 mp4 交给 /api/media：'+uriMap.http);
  assert.equal(uriMap.spaced,'/api/media?p='+encodeURIComponent('D:/m a/模型.images/video_01.mp4'),
    '百分号编码的路径没还原成真实文件系统路径：'+uriMap.spaced);
  assert.equal(uriMap.unc,'/api/media?p='+encodeURIComponent('//server/share/m.images/video_03.mp4'),
    'UNC 丢了前导双斜杠：'+uriMap.unc);
  assert.equal(uriMap.remote,'https://image.civitai.com/a/h1/original=true/clip1.mp4','远程直链不该被改写');
  assert.equal(uriMap.empty,'','空 src 不喂给 media 元素');
  assert.equal(uriMap.win,'file:///D:/models/m.images/video_02.mp4','窗口模式必须原样交给 file:///（同源可用）');
  ok('本地视频取用通道：浏览器模式走 /api/media（含带空格/中文与 UNC），窗口模式保持 file:///');

  // 自动轮播遇到视频帧：有首帧静图就以静帧身份参与（后端已不再把 poster 单独占一帧，跳过就等于卡片变呆）
  const autoPoster=await page.evaluate(([p,poster,src])=>{
    state.coverCacheMulti.set(p,[window.fixtureMultiCovers[p][0],[src,true,poster],window.fixtureMultiCovers[p][2]]);
    const c=[...document.querySelectorAll('#mmMasonry .ms-card')].find(x=>x.dataset.path===p);
    _stopCarousel(c);
    const st=_carouselPlayer(c,p,true); st.auto=true;
    _carouselAdvance(st);
    const alts=[...c.querySelectorAll('.ms-img-alt')];
    return {tags:alts.map(a=>a.tagName),src:alts.length?String(alts[alts.length-1].src):'',idx:st.idx,videoOnly:st.videoOnly};
  },[vidPath,posterDu,hangSrc]);
  assert.equal(autoPoster.videoOnly,false,'这条 fixture 必须是「静帧+视频」混合帧，自动轮播才会走跳过/静图分支');
  assert.deepEqual(autoPoster.tags,['IMG'],'自动轮播下带 poster 的视频帧必须以静图挂载');
  assert.equal(autoPoster.src,posterDu,'自动轮播挂出来的静图就是这一帧的 poster');
  assert.equal(autoPoster.idx,2,'视频帧以静图参与后游标要前进，不能卡在视频帧');
  ok('自动轮播：有首帧静图的视频帧以静图参与轮播（挂出 IMG，src=poster，游标 '+autoPoster.idx+'）');

  // 没有首帧静图的老帧仍然跳过：一屏多路解码不划算，这条语义不能被顺手改掉
  const autoBare=await page.evaluate(([p,src])=>{
    state.coverCacheMulti.set(p,[window.fixtureMultiCovers[p][0],[src,true,''],window.fixtureMultiCovers[p][2]]);
    const c=[...document.querySelectorAll('#mmMasonry .ms-card')].find(x=>x.dataset.path===p);
    _stopCarousel(c);
    const st=_carouselPlayer(c,p,true); st.auto=true;
    _carouselAdvance(st);
    const alts=[...c.querySelectorAll('.ms-img-alt')];
    return {tags:alts.map(a=>a.tagName),src:alts.length?String(alts[alts.length-1].src):''};
  },[vidPath,hangSrc]);
  assert.deepEqual(autoBare.tags,['IMG']);
  assert.equal(autoBare.src,await page.evaluate(p=>window.fixtureMultiCovers[p][2][0],vidPath),'没有 poster 的视频帧在自动轮播里仍该跳过，换下一张静帧');
  ok('自动轮播：没有首帧静图的视频帧仍按老语义跳过（不解码）');

  // 抓帧入口的接线：只在没有首帧静图时抓，序号取自本地落盘名 video_XX.mp4（帧三元组里没有 info 序号）
  const spy=await page.evaluate(([p,poster])=>{
    const calls=[]; const real=window.captureModelVideoPoster;
    window.captureModelVideoPoster=(v,path,idx)=>{calls.push([path,idx]);return Promise.resolve('');};
    const c=[...document.querySelectorAll('#mmMasonry .ms-card')].find(x=>x.dataset.path===p);
    _stopCarousel(c);
    state.coverCacheMulti.set(p,[window.fixtureMultiCovers[p][0],['http://127.0.0.1:1/a.mp4',true,poster]]);
    let st=_carouselPlayer(c,p,false); _carouselAdvance(st); _stopCarousel(c);           // 有 poster：不该抓
    state.coverCacheMulti.set(p,[window.fixtureMultiCovers[p][0],['file:///D:/Demo/models/m0.safetensors.images/video_03.mp4',true,'']]);
    st=_carouselPlayer(c,p,false); _carouselAdvance(st); _stopCarousel(c);               // 无 poster：抓，序号 3-1
    window.captureModelVideoPoster=real;
    return calls;
  },[vidPath,posterDu]);
  assert.deepEqual(spy,[[vidPath,2]],'瀑布流视频帧的抓帧触发/序号推导不对');
  ok('瀑布流 hover 播起来的无首帧视频才触发抓帧，序号取自 video_03.mp4');

  const idxChecks=await page.evaluate(()=>({
    local:_videoFrameInfoIndex('file:///D:/Demo/models/m0.safetensors.images/video_03.mp4'),
    upper:_videoFrameInfoIndex('file:///D:/Demo/models/m0.safetensors.images/video_07.MP4'),
    remote:_videoFrameInfoIndex('https://image.civitai.com/x/abc/hash/original=true/gen9f8e.mp4'),
    coverLocal:_coverVideoInfoIndex({images:[]},{url:'file:///D:/Demo/models/x.safetensors.images/video_12.mp4'}),
    coverRemote:_coverVideoInfoIndex({images:[{url:'https://cdn/a.mp4'},{url:'https://cdn/b.jpg'}]},{url:'https://cdn/a.mp4',orig_url:'https://cdn/a.mp4'}),
  }));
  assert.deepEqual(idxChecks,{local:2,upper:6,remote:-1,coverLocal:11,coverRemote:0},'首帧落盘序号推导错了会写进别的帧');
  ok('首帧序号推导：本地 video_XX 认得出，远程 mp4 直链认不出（返回 -1，不猜）');

  // 抓帧本体：readyState>=2 → 离屏 canvas（长边 640 / JPEG q0.82）→ save_model_video_poster(path, infoIndex, dataURL)
  const cap=await page.evaluate(async ([p])=>{
    const realCtx=HTMLCanvasElement.prototype.getContext, realApi=window.pywebview.api, calls=[];
    HTMLCanvasElement.prototype.getContext=function(t){return t==='2d'?{drawImage(){}}:realCtx.apply(this,arguments);};
    window.pywebview={api:new Proxy({},{get:(_,m)=>{
      if(typeof m!=='string')return undefined;
      return async(...a)=>{ if(m==='save_model_video_poster'){calls.push(a);return {ok:true,msg:'fixture 首帧已保存',path:'x'};} return realApi[m](...a); };
    }})};
    const vid=document.createElement('video'); vid.src='https://cdn.example/clip.mp4';
    Object.defineProperty(vid,'readyState',{configurable:true,get:()=>2});   // 首帧已解出（真视频播起来才到 2）
    Object.defineProperty(vid,'videoWidth',{configurable:true,get:()=>1280});
    Object.defineProperty(vid,'videoHeight',{configurable:true,get:()=>720});
    const du=String(await captureModelVideoPoster(vid,p,1));
    const again=String(await captureModelVideoPoster(vid,p,1));               // 同 path#index 会话内只回传一次
    const nope=String(await captureModelVideoPoster(vid,p,-1));               // 认不出序号就不抓，绝不猜
    HTMLCanvasElement.prototype.getContext=realCtx; window.pywebview={api:realApi};
    return {du,again,nope,calls:calls.map(c=>[c[0],c[1],String(c[2]).slice(0,15),String(c[2]).length]),n:calls.length};
  },[vidPath]);
  assert(cap.du.startsWith('data:image/jpeg'),'抓帧没有产出 JPEG data URL：'+cap.du.slice(0,40));
  assert.equal(cap.n,1,'一次会话同一 path#index 只该回传一次（抓不到也算试过，不能逐帧刷爆 RPC）');
  assert.deepEqual(cap.calls[0].slice(0,2),[vidPath,1],'save_model_video_poster 收到的 path/序号不对');
  assert(cap.calls[0][2].startsWith('data:image/jpeg'));
  assert.equal(cap.again,'');assert.equal(cap.nope,'');
  ok('抓帧回传：'+cap.calls[0][3]+' 字节 JPEG 交给 save_model_video_poster(path, 1, dataURL)，重复调用与未知序号都不发 RPC');

  await page.evaluate(()=>{state.coverCacheMulti.set(window.fixturePaths[0],window.fixtureMultiCovers[window.fixturePaths[0]]);});
  await page.evaluate(()=>{state.cfg.masonry_autoplay=true;});

  // 关掉设置里的自动轮播后，不悬停就不该再动
  await page.evaluate(()=>{state.cfg.masonry_autoplay=false;
    [...document.querySelectorAll('#mmMasonry .ms-card')].forEach(c=>_stopCarousel(c));});
  await page.waitForTimeout(4200);
  assert.equal(await page.evaluate(()=>document.querySelectorAll('#mmMasonry .ms-img-alt').length),0,'关闭自动轮播后仍在挂帧');
  await page.evaluate(()=>{state.cfg.masonry_autoplay=true;});
  ok('设置项 masonry_autoplay 关掉即停，悬停轮播不受影响');

 console.log('  判别性证据：视频帧 poster 属性 = '+mounted.idl.slice(0,24)+'…（'+mounted.idl.length+' 字符），自动轮播静帧 src = '+autoPoster.src.slice(0,24)+'…');
  assert.equal(errors.length,0,JSON.stringify(errors));
  ok('全部轮播检查无前端异常');
  console.log('PASS '+passed+' masonry carousel checks');
 }finally{await browser.close();[...hangSockets].forEach((s)=>s.destroy());server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
