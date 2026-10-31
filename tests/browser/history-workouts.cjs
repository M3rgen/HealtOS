// Synthetic fixtures; no live account records.
const assert = require('node:assert/strict');
const path = require('node:path');
const { BASE, launch, shot, run } = require('./lib.cjs');
const fx = require(path.join(__dirname, '../../frontend/preview/fixtures.cjs'));
run(async () => {
 const browser = await launch();
 try {
  for(const [width,height] of [[1280,800],[390,844],[360,640]]) {
   const page=await browser.newPage({viewport:{width,height},hasTouch:width<=390});
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   const noPageScroll=async()=>assert(await page.evaluate(()=>{
    const root=document.scrollingElement;return root.scrollHeight<=innerHeight&&root.scrollWidth<=innerWidth;
   }), `${width}x${height}: main page must fit`);
   let saved;
   // Collapsing animates the panel (380 ms); measure the handle only once it has settled.
   const settled=()=>page.waitForFunction(()=>!document.querySelector('#history-panel').getAnimations().length);
   await page.route('**/health',route=>route.fulfill({json:fx.health}));
   await page.route('**/api/dashboard?**',route=>route.fulfill({json:fx.dashboard(new URL(route.request().url()).searchParams)}));
   await page.route('**/sync/status',route=>route.fulfill({json:fx.syncStatus}));
   await page.route('**/api/dashboard/workouts**',async route=>{
    if(route.request().method()==='POST') {saved=route.request().postDataJSON();await route.fulfill({status:201,json:{...saved,id:'test',source:'Manuel'}});}
    else await route.fulfill({json:[{id:'test',exercise_type:'Yürüyüş',start_time:'2026-09-29T09:00:00Z',end_time:'2026-09-29T10:00:00Z',duration_seconds:3600,calories_kcal:200,distance_meters:4000,source:'Fitbit'}]});
   });
   await page.goto(BASE);
   await page.waitForFunction(()=>document.querySelectorAll('#history-rows tr').length===7);
   await noPageScroll();
   const steps=await page.locator('#steps-value').textContent();
   await page.locator('#history-toggle').click();
   await page.waitForFunction(()=>document.querySelector('#history-panel').classList.contains('expanded')&&document.querySelector('#history-status').hidden);
   assert.equal(await page.locator('#steps-value').textContent(),steps);
   await page.locator('#history-month').fill('');await page.locator('#history-month').dispatchEvent('change');
   await page.waitForFunction(()=>document.querySelector('#history-status').textContent.includes('Geçerli bir ay'));
   await page.locator('#history-month').fill('2026-09');await page.locator('#history-month').dispatchEvent('change');
   await page.waitForFunction(()=>document.querySelectorAll('#history-rows tr').length===30);
   assert.match(await page.locator('#export-button').getAttribute('href'),/days=30/);
   await page.screenshot({path:shot(`history-${width}.png`),fullPage:false});
   await page.keyboard.press('Escape');await page.waitForFunction(()=>document.querySelectorAll('#history-rows tr').length===7);
   await settled();
   await page.locator('#history-toggle').scrollIntoViewIfNeeded();
   const initial=await page.locator('#history-panel').boundingBox();
   const box=await page.locator('#history-toggle').boundingBox();
   await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await page.mouse.move(box.x+box.width/2,box.y+box.height/2-70,{steps:8});
   const moving=await page.locator('#history-panel').boundingBox();
   await page.mouse.up();
   const released=await page.locator('#history-panel').boundingBox();
   assert(Math.abs(initial.y-70-moving.y)<2, 'History height follows drag distance');
   assert(Math.abs(moving.height-released.height)<2, 'History retains released height');
   await noPageScroll();
   await page.waitForFunction(()=>document.querySelector('#history-panel').classList.contains('expanded'));
   await page.keyboard.press('Escape');
   if(width===390){
    await settled();
    await page.locator('#history-toggle').scrollIntoViewIfNeeded();
    const touchBox=await page.locator('#history-toggle').boundingBox();const x=touchBox.x+touchBox.width/2,y=touchBox.y+touchBox.height/2;
    const cdp=await page.context().newCDPSession(page);
    await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});
    await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x,y:y-65}]});
    await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    await page.waitForFunction(()=>document.querySelector('#history-panel').classList.contains('expanded'));
    await page.keyboard.press('Escape');await cdp.detach();
   }
   await page.locator('[data-page="sports"]').click();
   await page.waitForFunction(()=>document.querySelectorAll('.workout-entry').length===1);
   await noPageScroll();
   await page.locator('#workout-add').click();
   await noPageScroll();
   await page.locator('[name="exercise_type"]').fill('Bisiklet');
   await page.locator('[name="start_time"]').fill('2026-09-29T10:00');await page.locator('[name="end_time"]').fill('2026-09-29T11:00');
   await page.locator('[name="distance_km"]').fill('5');await page.locator('#workout-save').click();
   await page.waitForFunction(()=>!document.querySelector('#workout-editor').open);
   assert.equal(saved.distance_meters,5000);assert.equal(saved.exercise_type,'Bisiklet');
   await page.screenshot({path:shot(`workouts-${width}.png`),fullPage:true});
   await noPageScroll();
   assert.deepEqual(errors,[]);console.log(`PASS ${width}: weekly/month, month selector/CSV, drag/Escape, sports/manual form`);
   await page.close();
  }
 } finally {await browser.close();}
});
