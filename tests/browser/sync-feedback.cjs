// Synthetic fixtures; no live account records.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const URL = process.env.HEALTHOS_TEST_URL || 'http://127.0.0.1:5174';
const TYPES = ['heart-rate', 'sleep', 'steps'];
const SIZES = [{width:1280,height:800},{width:1440,height:900},{width:390,height:844},{width:320,height:700}];

async function openPage(browser, viewport, scenario) {
  // The browser runs in New York while the user picked Brussels: times must follow the selection.
  const context = await browser.newContext({viewport, timezoneId:'America/New_York'});
  await context.addInitScript(() => localStorage.setItem('healtos.timezone.v2', 'Europe/Brussels'));
  const page = await context.newPage();
  const state = {posts:0, dashboards:0, statusCalls:0, finished:false, devices:'ok', ...scenario};
  await page.route('**/health', route => route.fulfill({json:{status:'healthy',database:'healthy',
    scheduler:'running',configuration:'healthy',failed_syncs:state.finished&&state.failures?1:0}}));
  await page.route('**/api/dashboard?**', route => { state.dashboards++; return route.fulfill({json:{profile:{},
    start_date:'2026-10-01',end_date:'2026-10-01',timezone:'Europe/Brussels',
    days:[{date:'2026-10-01',steps:2000,sleep_seconds:28800,resting_bpm:70,status:'partial',partial:true}],
    charts:{steps:Array(24).fill(10),heart_rate:Array(24).fill(70)}}}); });
  await page.route('**/sync/all', async route => {
    assert.equal((await route.request().allHeaders())['x-healthos-request'],'1');
    state.posts++;
    await route.fulfill({status:202,json:{status:'accepted',data_types:TYPES,sequence:40}});
  });
  await page.route('**/sync/status', route => {
    state.statusCalls++;
    // Until the run finishes, every outcome still carries the pre-click baseline sequence (40).
    return route.fulfill({json:TYPES.map((type, index) => {
      const failed = state.finished && (state.failures || []).includes(type);
      return {data_type:type, status:state.finished ? (failed ? 'failed' : 'completed') : 'running',
        records_downloaded:5000, last_error:failed ? `${type} için Google 503 döndürdü.${type === 'steps' ? ' Ayrıntı: yeniden dene.'.repeat(40) : ''}` : null,
        last_run:state.finished
          ? {sequence:41 + index, inserted:(state.inserted || [0,0,0])[index], updated:0, failed}
          : {sequence:40 - index, inserted:99, updated:0, failed:false}};
    })});
  });
  await page.route('**/google-health/devices', route => state.devices === 'ok'
    ? route.fulfill({json:{devices:[
        {name:'Charge 6',device_type:'TRACKER',last_sync_time:'2026-10-17T13:02:00Z',features:[]},
        {name:'Aria',device_type:'SCALE',last_sync_time:'2026-10-12T08:00:00Z',features:[]},
        {name:'Eski',device_type:'TRACKER',last_sync_time:null,features:[]}]}})
    : route.fulfill({status:503,json:{detail:'Google erişilemedi.'}}));
  await page.goto(URL);
  await page.waitForFunction(()=>document.querySelector('#steps-value').textContent==='2.000');
  return {page, state, context};
}

const overflow = page => page.evaluate(()=>({x:document.documentElement.scrollWidth>innerWidth,
  y:document.documentElement.scrollHeight>innerHeight}));
const done = page => page.waitForFunction(()=>!document.querySelector('#sync-button').disabled,null,{timeout:20000});

(async () => {
  const browser = await chromium.launch({headless:true,channel:'msedge'});
  try {
    // 1. Busy state holds while polling and a second start is ignored.
    {
      const {page, state, context} = await openPage(browser, {width:1280,height:800}, {inserted:[10,0,2]});
      const button = page.locator('#sync-button');
      await button.click();
      await page.waitForTimeout(4500);
      assert.ok(state.statusCalls >= 2, 'status must be polled while the run is in progress');
      assert.equal(await button.isDisabled(), true);
      assert.equal(await button.getAttribute('aria-busy'), 'true');
      assert.match(await button.textContent(), /Eşitleniyor…/);
      assert.equal(await button.evaluate(el=>el.classList.contains('syncing')), true);
      assert.notEqual(await button.locator('.button-icon').evaluate(el=>getComputedStyle(el).animationName), 'none');
      await button.evaluate(el=>{el.disabled=false;el.click();el.disabled=true;});
      assert.equal(state.posts, 1, 'a running sync must not be started twice');
      assert.equal(await page.locator('#notice').isVisible(), false);
      const dashboardsBefore = state.dashboards;
      state.finished = true;
      await done(page);
      assert.match(await page.locator('#notice').textContent(), /^Eşitleme tamamlandı · 12 yeni kayıt$/);
      assert.equal(await page.locator('#notice').evaluate(el=>el.classList.contains('error')), false);
      assert.ok(state.dashboards > dashboardsBefore, 'dashboard must refresh after the run');
      assert.match(await button.textContent(), /Şimdi eşitle/);
      assert.equal(await button.getAttribute('aria-busy'), 'false');
      await context.close();
    }
    // 2. No new data: device time is shown in the selected zone (Europe/Brussels, CEST = UTC+2).
    {
      const {page, state, context} = await openPage(browser, {width:1280,height:800}, {finished:true});
      await page.locator('#sync-button').click();
      await done(page);
      assert.match(await page.locator('#notice').textContent(),
        /^Eşitleme tamamlandı · Google[’']da yeni veri yok\. Saatin son eşitlemesi: (17 Eki )?15:02$/);
      state.devices = 'fail';
      await page.locator('#sync-button').click();
      await done(page);
      assert.match(await page.locator('#notice').textContent(), /^Eşitleme tamamlandı · Google[’']da yeni veri yok\.$/);
      await context.close();
    }
    // 3. Failures name the failed data types in an error notice; a long error still causes no page scroll.
    for (const viewport of SIZES) {
      const {page, state, context} = await openPage(browser, viewport, {inserted:[3,0,0]});
      await page.locator('#sync-button').click();
      await page.waitForTimeout(2600);
      assert.deepEqual(await overflow(page), {x:false,y:false}, `busy state overflows at ${viewport.width}x${viewport.height}`);
      state.failures = ['sleep','steps']; state.finished = true;
      await done(page);
      const notice = page.locator('#notice');
      const text = await notice.textContent();
      assert.match(text, /^Eşitleme 2\/3 veri türünde başarısız oldu · /);
      assert.match(text, /sleep: sleep için Google 503 döndürdü\./);
      assert.match(text, /steps: steps için Google 503 döndürdü\./);
      assert.doesNotMatch(text, /heart-rate:/);
      assert.match(text, /· 3 yeni kayıt$/);
      assert.equal(await notice.evaluate(el=>el.classList.contains('error')), true);
      assert.deepEqual(await overflow(page), {x:false,y:false}, `failure notice overflows at ${viewport.width}x${viewport.height}`);
      await context.close();
    }
    console.log('PASS: sync shows busy state until completion, reports new records, no-new-data with Brussels device time, and failures without page scroll');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1)});
