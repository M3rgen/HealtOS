// Synthetic fixtures; no live account records.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true,channel:'msedge'});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:720}});
    let rejectStart = false, failed = false;
    await page.route('**/health', route => route.fulfill({json:{status:'healthy',database:'healthy',
      scheduler:'running',configuration:'healthy',failed_syncs:failed?1:0}}));
    await page.route('**/api/dashboard?**', route => route.fulfill({json:{profile:{},
      start_date:'2026-10-01',end_date:'2026-10-01',timezone:'UTC',
      days:[{date:'2026-10-01',steps:2000,sleep_seconds:28800,resting_bpm:70,status:'partial',partial:true}],
      charts:{steps:Array(24).fill(10),heart_rate:Array(24).fill(70)}}}));
    await page.route('**/sync/all', route => route.fulfill(rejectStart
      ? {status:500,json:{detail:'Google bağlantısı kurulamadı.'}}
      : {status:202,json:{status:'accepted',data_types:['steps']}}));
    await page.route('**/sync/status', route => route.fulfill({json:[{data_type:'steps',
      status:failed?'failed':'completed',last_error:failed?'Google erişim izni reddedildi.':null}]}));
    await page.goto('http://127.0.0.1:5174');
    await page.waitForFunction(()=>document.querySelector('#steps-value').textContent==='2.000');
    const button=page.locator('#sync-button'), notice=page.locator('#notice');
    await button.click();
    await page.waitForFunction(()=>!document.querySelector('#sync-button').disabled);
    assert.equal(await notice.isVisible(),false,'Accepted sync must not leave a success banner');
    rejectStart=true;
    await button.click();
    await notice.waitFor({state:'visible'});
    assert.match(await notice.textContent(),/Google bağlantısı kurulamadı/);
    rejectStart=false;
    await button.click();
    await notice.waitFor({state:'hidden'});
    failed=true;
    await notice.waitFor({state:'visible',timeout:10000});
    assert.match(await notice.textContent(),/Google erişim izni reddedildi/);
    failed=false;
    await button.click();
    await notice.waitFor({state:'hidden'});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollHeight>innerHeight),false);
    console.log('PASS: success stays quiet, start/background errors show details, successful retry clears the error');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1)});
