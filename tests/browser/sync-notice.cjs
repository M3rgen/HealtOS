// Synthetic fixtures; no live account records.
const assert = require('node:assert/strict');
const { BASE, launch, run } = require('./lib.cjs');

run(async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({viewport:{width:1280,height:720}});
    let rejectStart = false, failed = false, runs = 0;
    await page.route('**/health', route => route.fulfill({json:{status:'healthy',database:'healthy',
      scheduler:'running',configuration:'healthy',failed_syncs:failed?1:0}}));
    await page.route('**/api/dashboard?**', route => route.fulfill({json:{profile:{},
      start_date:'2026-10-01',end_date:'2026-10-01',timezone:'UTC',
      days:[{date:'2026-10-01',steps:2000,sleep_seconds:28800,resting_bpm:70,status:'partial',partial:true}],
      charts:{steps:Array(24).fill(10),heart_rate:Array(24).fill(70)}}}));
    await page.route('**/sync/all', route => route.fulfill(rejectStart
      ? {status:500,json:{detail:'Google bağlantısı kurulamadı.'}}
      : {status:202,json:{status:'accepted',data_types:['steps'],sequence:runs++}}));
    await page.route('**/sync/status', route => route.fulfill({json:[{data_type:'steps',
      status:failed?'failed':'completed',last_error:failed?'Google erişim izni reddedildi.':null,
      last_run:{sequence:runs,inserted:0,updated:0,failed}}]}));
    await page.goto(BASE);
    await page.waitForFunction(()=>document.querySelector('#steps-value').textContent==='2.000');
    const button=page.locator('#sync-button'), notice=page.locator('#notice');
    const finished=()=>page.waitForFunction(()=>!document.querySelector('#sync-button').disabled,null,{timeout:20000});
    await button.click();
    await finished();
    assert.match(await notice.textContent(),/^Eşitleme tamamlandı/,'Finished sync must report its result');
    rejectStart=true;
    await button.click();
    await notice.waitFor({state:'visible'});
    assert.match(await notice.textContent(),/Google bağlantısı kurulamadı/);
    rejectStart=false;
    await button.click();
    await notice.waitFor({state:'hidden'});
    await finished();
    failed=true;
    await button.click();
    await finished();
    assert.match(await notice.textContent(),/Google erişim izni reddedildi/);
    assert.equal(await notice.evaluate(el=>el.classList.contains('error')),true);
    failed=false;
    await button.click();
    await notice.waitFor({state:'hidden'});
    await finished();
    assert.match(await notice.textContent(),/^Eşitleme tamamlandı/);
    assert.equal(await notice.evaluate(el=>el.classList.contains('error')),false);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollHeight>innerHeight),false);
    console.log('PASS: finished sync reports its result, start/run errors show details, successful retry clears the error');
  } finally { await browser.close(); }
});
