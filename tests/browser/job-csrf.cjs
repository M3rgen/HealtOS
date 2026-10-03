// Synthetic fixtures; no live account records.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true,channel:'msedge'});
  try {
    for (const viewport of [{width:1280,height:720},{width:390,height:844}]) {
      const page = await browser.newPage({viewport});
      let started = 0;
      await page.route('**/health', route => route.fulfill({json:{status:'healthy',database:'healthy',
        scheduler:'running',configuration:'healthy',failed_syncs:0}}));
      await page.route('**/api/dashboard?**', route => route.fulfill({json:{profile:{},
        start_date:'2026-10-01',end_date:'2026-10-01',timezone:'UTC',
        days:[{date:'2026-10-01',steps:2000,sleep_seconds:28800,resting_bpm:70,status:'partial',partial:true}],
        charts:{steps:Array(24).fill(10),heart_rate:Array(24).fill(70)}}}));
      await page.route('**/sync/status', route => route.fulfill({json:[]}));
      await page.route('**/sync/all', async route => {
        assert.equal((await route.request().allHeaders())['x-healthos-request'],'1');
        assert.equal(route.request().method(),'POST');
        started++;
        await route.fulfill({status:202,json:{status:'accepted',data_types:['steps']}});
      });
      await page.goto(process.env.HEALTHOS_TEST_URL || 'http://127.0.0.1:5174');
      await page.waitForFunction(()=>document.querySelector('#steps-value').textContent==='2.000');
      await page.locator('#sync-button').click();
      await page.waitForFunction(()=>!document.querySelector('#sync-button').disabled);
      assert.equal(started,1,'Sync button must send the deliberate API request header');
      const overflow = await page.evaluate(()=>({
        x:document.documentElement.scrollWidth>innerWidth,
        y:document.documentElement.scrollHeight>innerHeight,
      }));
      assert.deepEqual(overflow,{x:false,y:false});
      await page.close();
    }
    console.log('PASS: desktop/mobile sync sends CSRF header and page does not overflow');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1)});
