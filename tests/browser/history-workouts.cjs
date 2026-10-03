// Synthetic fixtures; no live account records.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
 const browser = await chromium.launch({headless:true,channel:'msedge'});
 try {
  for(const width of [1280,390]) {
   const page=await browser.newPage({viewport:{width,height:900},hasTouch:width===390});
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   let saved;
   await page.route('**/api/dashboard/workouts**',async route=>{
    if(route.request().method()==='POST') {saved=route.request().postDataJSON();await route.fulfill({status:201,json:{...saved,id:'test',source:'Manuel'}});}
    else await route.fulfill({json:[{id:'test',exercise_type:'Yürüyüş',start_time:'2026-09-29T09:00:00Z',end_time:'2026-09-29T10:00:00Z',duration_seconds:3600,calories_kcal:200,distance_meters:4000,source:'Fitbit'}]});
   });
   await page.goto('http://127.0.0.1:5174');
   await page.waitForFunction(()=>document.querySelectorAll('#history-rows tr').length===7);
   const steps=await page.locator('#steps-value').textContent();
   await page.locator('#history-toggle').click();
   await page.waitForFunction(()=>document.querySelector('#history-panel').classList.contains('expanded')&&document.querySelector('#history-status').hidden);
   assert.equal(await page.locator('#steps-value').textContent(),steps);
   await page.locator('#history-month').fill('');await page.locator('#history-month').dispatchEvent('change');
   await page.waitForFunction(()=>document.querySelector('#history-status').textContent.includes('Geçerli bir ay'));
   await page.locator('#history-month').fill('2026-09');await page.locator('#history-month').dispatchEvent('change');
   await page.waitForFunction(()=>document.querySelectorAll('#history-rows tr').length===30);
   assert.match(await page.locator('#export-button').getAttribute('href'),/days=30/);
   await page.screenshot({path:`.test-tmp-history-${width}.png`,fullPage:false});
   await page.keyboard.press('Escape');await page.waitForFunction(()=>document.querySelectorAll('#history-rows tr').length===7);
   await page.locator('#history-toggle').scrollIntoViewIfNeeded();
   const box=await page.locator('#history-toggle').boundingBox();
   await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await page.mouse.move(box.x+box.width/2,box.y+box.height/2-70,{steps:8});await page.mouse.up();
   await page.waitForFunction(()=>document.querySelector('#history-panel').classList.contains('expanded'));
   await page.keyboard.press('Escape');
   if(width===390){
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
   await page.waitForFunction(()=>document.querySelectorAll('#workout-rows tr').length===1);
   await page.locator('#workout-add').click();
   await page.locator('[name="exercise_type"]').fill('Bisiklet');
   await page.locator('[name="start_time"]').fill('2026-09-29T10:00');await page.locator('[name="end_time"]').fill('2026-09-29T11:00');
   await page.locator('[name="distance_km"]').fill('5');await page.locator('#workout-save').click();
   await page.waitForFunction(()=>!document.querySelector('#workout-editor').open);
   assert.equal(saved.distance_meters,5000);assert.equal(saved.exercise_type,'Bisiklet');
   await page.screenshot({path:`.test-tmp-workouts-${width}.png`,fullPage:true});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   assert.deepEqual(errors,[]);console.log(`PASS ${width}: weekly/month, month selector/CSV, drag/Escape, sports/manual form`);
   await page.close();
  }
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
