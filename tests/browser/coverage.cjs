// Coverage panel: synthetic HTTP responses, keyboard access and viewport containment.
const assert = require('node:assert/strict');
const path = require('node:path');
const { BASE, launch, run, until, shot } = require('./lib.cjs');
const fx = require(path.join(__dirname, '../../frontend/preview/fixtures.cjs'));

const overflow = page => page.evaluate(() => ({
  x: document.documentElement.scrollWidth > innerWidth,
  y: document.documentElement.scrollHeight > innerHeight,
  bodyX: document.body.scrollWidth > innerWidth,
  bodyY: document.body.scrollHeight > innerHeight,
}));
const sizes = [{width:1440,height:900}, {width:1280,height:720},
  {width:390,height:844}, {width:320,height:700}];
const names = ['heart-rate', 'sleep', 'steps', 'weight', 'exercise'];
const days = Array.from({length:31}, (_, i) => ({date:`2026-10-${String(i + 1).padStart(2,'0')}`,
  closed:true, has_value:i%3 === 0, retrieval:['completed_with_records','completed_empty','unknown','incomplete'][i%4],
  returned_records:i%4===0?2:i%4===1?0:null, reason:null}));

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of sizes) {
      const page = await browser.newPage({viewport});
      const state = {fail:false, pause:false, requests:[], releases:[]};
      const errors=[]; page.on('pageerror', e=>errors.push(e.message));
      await page.route('**/health', route=>route.fulfill({json:fx.health}));
      await page.route('**/api/dashboard?**', route=>route.fulfill({json:fx.dashboard(new URL(route.request().url()).searchParams)}));
      await page.route('**/sync/status', route=>route.fulfill({json:fx.syncStatus}));
      await page.route('**/api/sync/coverage?**', async route=>{
        const query = new URL(route.request().url()).searchParams;
        state.requests.push(Object.fromEntries(query));
        if(state.pause) await new Promise(resolve=>state.releases.push(resolve));
        if(state.fail) return route.fulfill({status:503,json:{detail:'Sentetik bağlantı hatası'}});
        const offset=Number(query.get('offset')||0);
        const first=Date.parse(`${query.get('start_date')}T12:00:00Z`);
        const count=Math.round((Date.parse(`${query.get('end_date')}T12:00:00Z`)-first)/86400000)+1;
        const requestedDays=Array.from({length:count},(_,i)=>({...days[i],
          date:new Date(first+i*86400000).toISOString().slice(0,10)}));
        const limit=Math.min(4,Math.floor(62/count));
        const selected=query.get('data_type')?[query.get('data_type')]:names.slice(offset,offset+limit);
        return route.fulfill({json:{supported_data_types:names,
          types:selected.map(name=>({data_type:name,presence_basis:'metric_value',days:requestedDays,
            source_regimes:[{source:'kaynak-örnek',value_days:10,record_count:20}],exclusions:{quarantined:1}})),
          summary:{days_without_value:20*selected.length,unknown_days:8*selected.length,incomplete_days:7*selected.length},
          next_offset:!query.get('data_type')&&offset+limit<names.length?offset+limit:null}});
      });
      await page.goto(BASE);
      await page.waitForFunction(()=>document.querySelectorAll('#history-rows tr').length===7);
      state.pause=true;
      await page.locator('[data-page="system"]').click();
      await until(()=>state.requests.length===1,'coverage request');
      assert.match(await page.locator('#coverage-summary').textContent(),/yükleniyor/);
      await page.locator('[data-page="system"]').click();
      assert.deepEqual(await overflow(page),{x:false,y:false,bodyX:false,bodyY:false});
      state.pause=false; state.releases.splice(0).forEach(resolve=>resolve());
      await page.waitForFunction(()=>document.querySelector('#coverage-summary').textContent.includes('bilinmeyen'));
      assert.equal(state.requests.length,1,'a repeated refresh must not restart an in-flight report');
      // Native dialog keyboard behavior: Enter opens, Escape closes and restores focus.
      await page.locator('#coverage-open').focus(); await page.keyboard.press('Enter');
      assert.equal(await page.locator('#coverage-dialog').evaluate(el=>el.open),true);
      assert.equal(await page.locator('.coverage-days li').count(),7);
      await page.locator('#coverage-start').fill('2026-10-01');
      await page.locator('#coverage-end').fill('2026-10-31');
      await page.locator('#coverage-form button').click();
      await page.waitForFunction(()=>document.querySelectorAll('.coverage-days li').length===31);
      assert.match(await page.locator('#coverage-results').textContent(),/Değer yok/);
      assert.match(await page.locator('#coverage-results').textContent(),/Google boş döndü/);
      assert.match(await page.locator('#coverage-results').textContent(),/Çekimin tamamlandığı bilinmiyor/);
      await page.locator('.coverage-dialog-body').focus();
      await page.keyboard.press('End');
      await page.locator('.coverage-days li').last().scrollIntoViewIfNeeded();
      assert.equal(await page.locator('.coverage-dialog-body').evaluate(el=>el.scrollTop>0),true);
      assert.deepEqual(await overflow(page),{x:false,y:false,bodyX:false,bodyY:false});
      const bounds=await page.locator('#coverage-dialog').boundingBox();
      assert.ok(bounds.x>=0&&bounds.y>=0&&bounds.x+bounds.width<=viewport.width+1&&bounds.y+bounds.height<=viewport.height+1);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#coverage-open').evaluate(el=>el===document.activeElement),true);
      await page.keyboard.press('Enter');
      await page.locator('#coverage-start').fill('2026-10-01');
      await page.locator('#coverage-end').fill('2026-10-31');
      await page.locator('#coverage-type').selectOption('');
      await page.locator('#coverage-form button').click();
      await page.waitForFunction(()=>document.querySelectorAll('.coverage-type').length===5);
      assert.ok(state.requests.some(query=>query.offset==='4'),'all types must follow pagination');
      state.fail=true;
      await page.locator('#coverage-form button').click();
      await page.locator('#coverage-retry').waitFor({state:'visible'});
      assert.match(await page.locator('#coverage-status').textContent(),/Kapsama alınamadı/);
      state.fail=false;
      await page.locator('#coverage-retry').focus(); await page.keyboard.press('Enter');
      await page.waitForFunction(()=>document.querySelectorAll('.coverage-type').length===5);
      assert.equal(await page.locator('#coverage-retry').isVisible(),false);
      await page.screenshot({path:shot(`coverage-${viewport.width}.png`)});
      await page.locator('#coverage-close').click();
      await page.locator('[data-page="overview"]').click();
      // The original history drag remains proportional and retains its released height.
      const handle=page.locator('#history-toggle');
      const box=await handle.boundingBox();
      const x=box.x+box.width/2,y=box.y+box.height/2;
      await page.mouse.move(x,y);await page.mouse.down();await page.mouse.move(x,y-75,{steps:10});
      const during=await page.locator('#history-panel').evaluate(el=>el.getBoundingClientRect().height);
      await page.mouse.up();
      await page.waitForFunction(()=>!document.querySelector('#history-panel').getAnimations().length);
      const released=await page.locator('#history-panel').evaluate(el=>el.getBoundingClientRect().height);
      assert.ok(Math.abs(during-released)<4,'history must retain the drag height');
      assert.deepEqual(await overflow(page),{x:false,y:false,bodyX:false,bodyY:false});
      assert.deepEqual(errors,[]);
      await page.close();
    }
  } finally { await browser.close(); }
});
