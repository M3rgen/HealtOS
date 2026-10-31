// Synthetic webhook diagnostics: loading, failure, silence states and keyboard access.
const assert = require('node:assert/strict');
const path = require('node:path');
const { BASE, launch, run, until, shot } = require('./lib.cjs');
const fx = require(path.join(__dirname, '../../frontend/preview/fixtures.cjs'));

const sizes = [{width:1440,height:900}, {width:1280,height:720},
  {width:390,height:844}, {width:320,height:700}];
const contained = async page => assert.deepEqual(await page.evaluate(() => ({
  x:document.documentElement.scrollWidth > innerWidth,
  y:document.documentElement.scrollHeight > innerHeight,
  bodyX:document.body.scrollWidth > innerWidth, bodyY:document.body.scrollHeight > innerHeight,
})), {x:false,y:false,bodyX:false,bodyY:false});

function snapshot(state) {
  return {
    checked_at:'2026-10-25T12:00:00Z', receiver:{configured:state !== 'unconfigured'},
    last_notification_at:state === 'receiving' ? '2026-10-25T10:00:00Z' : null,
    silence:{state,warning:state === 'silent',never_received:state !== 'receiving',
      since:'2026-10-01T00:00:00Z',threshold_seconds:604800},
    counters:{scope:'persistent_diagnostic_events',since:'2026-10-01T00:00:00Z',
      survives_restart:true,valid:12,invalid:3,expired:1,
      reasons:{authorization_rejected:1,malformed_signature:1,invalid_signature:1,
        key_download_failed:2,challenge:1,duplicate:1,processing_failed:1}},
    subscription:{status:'unknown',renewal_status:'unknown',last_renewed_at:null},
    last_error:{code:'key_download_failed',stage:'validation',at:'2026-10-25T11:00:00Z'},
  };
}

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of sizes) {
      const page = await browser.newPage({viewport});
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      const state = {value:'silent',fail:false,pause:true,requests:0,releases:[]};
      await page.route('**/health', route => route.fulfill({json:fx.health}));
      await page.route('**/api/dashboard?**', route => route.fulfill({json:fx.dashboard(new URL(route.request().url()).searchParams)}));
      await page.route('**/sync/status', route => route.fulfill({json:fx.syncStatus}));
      await page.route('**/api/sync/coverage?**', route => route.fulfill({json:{
        supported_data_types:['steps'],types:[],summary:{days_without_value:0,unknown_days:0,incomplete_days:0},next_offset:null,
      }}));
      await page.route('**/api/webhooks/status', async route => {
        state.requests++;
        if (state.pause) await new Promise(resolve => state.releases.push(resolve));
        return state.fail ? route.fulfill({status:503,json:{detail:'private upstream error'}})
          : route.fulfill({json:snapshot(state.value)});
      });
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      const writeText = await page.locator('#webhook-info').textContent();
      assert.match(writeText,/Son veri güncellemesi/);
      assert.doesNotMatch(writeText,/teslim|bildirim/i);
      await page.getByRole('button', {name:'Sistem',exact:true}).click();
      await until(() => state.requests === 1, 'status request starts');
      assert.match(await page.locator('#webhook-status-summary').textContent(), /yükleniyor/);
      const open = page.getByRole('button',{name:'Bildirim ayrıntıları',exact:true});
      await open.focus(); await page.keyboard.press('Enter');
      assert.equal(await page.locator('#webhook-status-dialog').evaluate(el => el.open),true);
      assert.match(await page.locator('#webhook-status-message').textContent(), /yükleniyor/);
      assert.equal(await page.locator('#webhook-status-retry').isDisabled(),true);
      await contained(page);
      state.pause=false; state.releases.splice(0).forEach(resolve => resolve());
      await page.waitForFunction(() => document.querySelector('#webhook-status-summary').textContent.includes('Bildirim gelmiyor'));
      assert.match(await page.locator('#webhook-status-details').textContent(),/Bilinmiyor/);
      assert.match(await page.locator('#webhook-status-details').textContent(),/İmzada süre sonu alanı yok/);
      assert.match(await page.locator('#webhook-status-details').textContent(),/İmza anahtarları alınamadı/);
      // The normal full detail is longer than every supported viewport and stays reachable.
      const body = page.getByRole('region',{name:'Bildirim tanılama içeriği'});
      await body.focus(); await page.keyboard.press('End');
      await page.locator('#webhook-status-details > div').last().scrollIntoViewIfNeeded();
      assert.equal(await body.evaluate(el => el.scrollTop > 0 && el.scrollHeight > el.clientHeight),true);
      const box = await page.locator('#webhook-status-dialog').boundingBox();
      assert.ok(box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 1 && box.y + box.height <= viewport.height + 1);
      await contained(page);
      await page.keyboard.press('Escape');
      assert.equal(await open.evaluate(el => el === document.activeElement),true);
      await page.keyboard.press('Enter');
      await page.getByRole('button',{name:'Bildirim ayrıntılarını kapat',exact:true}).click();

      // All service states have different copy; loading/error never claim silence.
      for (const [value, expected] of [
        ['unconfigured',/alıcı.*yapılandırılmamış/],
        ['never_received',/Henüz doğrulanmış bildirim alınmadı; izleme sürüyor/],
        ['receiving',/Son doğrulanmış bildirim:/],
      ]) {
        state.value=value;
        const before=state.requests;
        await page.getByRole('button',{name:'Durumu yenile',exact:true}).click();
        await until(() => state.requests > before, 'refresh status');
        await page.waitForFunction(() => document.querySelector('#webhook-status-details').getAttribute('aria-busy') === 'false');
        assert.match(await page.locator('#webhook-status-summary').textContent(),expected);
        assert.equal(await page.locator('#webhook-status-summary').evaluate(el => el.classList.contains('warning')),false);
        await contained(page);
      }
      state.fail=true;
      await page.getByRole('button',{name:'Durumu yenile',exact:true}).click();
      await page.waitForFunction(() => document.querySelector('#webhook-status-summary').textContent.includes('alınamadı'));
      await open.focus(); await page.keyboard.press('Enter');
      assert.equal(await page.locator('#webhook-status-details').textContent(),'');
      assert.doesNotMatch(await page.locator('#webhook-status-message').textContent(),/private|gelmiyor/);
      await contained(page);
      state.fail=false;
      await page.getByRole('button',{name:'Bildirim durumunu yeniden al',exact:true}).focus();
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.querySelector('#webhook-status-message').textContent.includes('Son doğrulanmış'));
      await page.screenshot({path:shot(`webhook-status-${viewport.width}.png`)});
      await page.keyboard.press('Escape');
      await page.getByRole('button',{name:'Genel bakış',exact:true}).click();
      const handle=await page.locator('#history-toggle').boundingBox();
      const x=handle.x+handle.width/2,y=handle.y+handle.height/2;
      await page.mouse.move(x,y); await page.mouse.down(); await page.mouse.move(x,y-75,{steps:10});
      const during=await page.locator('#history-panel').evaluate(el => el.getBoundingClientRect().height);
      await page.mouse.up();
      await page.waitForFunction(() => !document.querySelector('#history-panel').getAnimations().length);
      const after=await page.locator('#history-panel').evaluate(el => el.getBoundingClientRect().height);
      assert.ok(Math.abs(after-during)<4,'history retains its released drag height');
      await contained(page);
      assert.deepEqual(errors,[]);
      await page.close();
    }
  } finally { await browser.close(); }
});
