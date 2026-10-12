/* Real Chromium, synthetic models + mocked bridge: 瀑布流多图轮播必须真的起播、换帧、收摊。
   回归的是这个缺陷：真正滚动的是 main.content，而预取监听挂在 window（冒泡）与 #mmMasonry（它自己不滚），
   两处一次都没触发过 → 首屏之后多图帧再也不预取 → hover 无帧可轮，轮播整体哑火。 */
const fs=require('node:fs'),http=require('node:http'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const root=path.resolve(__dirname,'..'),web=path.join(root,'web');
const source=fs.readFileSync(path.join(__dirname,'test_ui_preferences.cjs'),'utf8').replace(/\r\n/g,'\n');
const begin=source.indexOf('await page.addInitScript(')+'await page.addInitScript('.length,
      end=source.indexOf('\n    });\n    const url',begin)+6,init=eval('('+source.slice(begin,end)+')');
const server=http.createServer((req,res)=>{
  const url=new URL(req.url,'http://localhost'),p=path.resolve(web,'.'+decodeURIComponent(url.pathname==='/'?'/index.html':url.pathname));
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
    const s=await page.evaluate(()=>{const c=document.querySelector('#mmMasonry .ms-card:hover');const a=c.querySelector('.ms-img-alt');return a?String(a.src):'';});
    assert(s,'换帧过程中叠层帧不能消失');seen.add(s);}
  assert(seen.size>=2,'约 3 秒/帧应至少换过一次帧');
  ok('hover 期间按 ~3 秒/帧继续换帧（真的在循环，不是挂个静止叠层）');

  await page.mouse.move(8,8);
  await page.waitForFunction(()=>document.querySelectorAll('#mmMasonry .ms-img-alt').length===0,null,{timeout:4000});
  ok('移开鼠标后叠层帧清理干净');

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

  assert.equal(errors.length,0,JSON.stringify(errors));
  ok('全部轮播检查无前端异常');
  console.log('PASS '+passed+' masonry carousel checks');
 }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
