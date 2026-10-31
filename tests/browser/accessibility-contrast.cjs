// Use the production cascade and the actual painted backdrop, not palette constants.
const assert = require('node:assert/strict');
const { BASE, launch, run } = require('./lib.cjs');
const { mock, tabTo, contained, scrollKey } = require('./helpers/accessibility.cjs');
const { auditText, auditFocus, auditBorders } = require('./helpers/contrast.cjs');

async function activate(page, selector) { await tabTo(page, selector); await page.keyboard.press('Enter'); }
async function check(page, label) { await contained(page); await auditText(page, label); }
async function close(page, id) {
  await page.keyboard.press('Escape');
  await page.waitForFunction(id => !document.getElementById(id).open, id);
}

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [{width:1280,height:800}, {width:390,height:844}, {width:360,height:640}]) {
      const page = await browser.newPage({viewport, reducedMotion:'reduce', deviceScaleFactor:1});
      const state = await mock(page);
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7 && 'appearanceSynced' in document.documentElement.dataset);
      const name = `${viewport.width}×${viewport.height}`;
      await check(page, `${name} overview`);
      // Worst-case user background: a completely white image behind the interface.
      await page.evaluate(() => {
        const style = document.documentElement.style;
        style.setProperty('--bg-tile', 'url("data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'8\' height=\'8\'%3E%3Cpath fill=\'white\' d=\'M0 0h8v8H0z\'/%3E%3C/svg%3E")');
        style.setProperty('--bg-vignette', 'none'); style.setProperty('--bg-dim', 'transparent');
      });
      await tabTo(page, '#profile-weight');
      await auditFocus(page, `${name} profile`);
      await check(page, `${name} image behind overview`);
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.querySelector('#measurement-dialog').open);
      await auditFocus(page, `${name} measurement value`);
      await auditBorders(page, '#measurement-value, #measurement-metric, #measurement-date');
      state.failSave = true;
      await activate(page, '#measurement-save');
      await page.waitForFunction(() => document.querySelector('#form-error').textContent.includes('Sentetik'));
      await auditFocus(page, `${name} primary action`);
      await check(page, `${name} measurement API error`);
      await close(page, 'measurement-dialog');
      await tabTo(page, '#sync-button');
      await auditFocus(page, `${name} outline action`);
      await auditBorders(page, '#sync-button');
      await check(page, `${name} connection and metric cards`);
      await activate(page, '[data-history-metric="steps"]');
      await page.waitForFunction(() => !document.querySelector('#metric-history-content').hidden);
      await check(page, `${name} metric history`);
      await tabTo(page, '#metric-history-content');
      await auditFocus(page, `${name} scrollport`);
      await scrollKey(page, '#metric-history-content', 'End');
      await check(page, `${name} metric history end`);
      await close(page, 'metric-history-dialog');
      await activate(page, '[data-page="measurements"]');
      await page.waitForFunction(() => document.querySelectorAll('#measurement-rows tr').length === 45);
      await tabTo(page, '[data-flag-id="synthetic-0"]');
      await auditFocus(page, `${name} table action`);
      await auditBorders(page, '[data-flag-id="synthetic-0"]');
      await check(page, `${name} measurements`);
      await tabTo(page, '[data-flag-id="synthetic-44"]');
      await auditFocus(page, `${name} final row clear of launcher`);
      await tabTo(page, '[data-page="system"]', true);
      await page.keyboard.press('Enter');
      // The panels load asynchronously and reflow the page; audit the settled layout.
      await page.waitForFunction(() => document.querySelectorAll('#sync-rows tr').length === 80 &&
        !['coverage-summary', 'webhook-status-summary'].some(id => document.getElementById(id).textContent.includes('yükleniyor')));
      await check(page, `${name} system`);
      await activate(page, '#coverage-open');
      await check(page, `${name} coverage`);
      await tabTo(page, '.coverage-dialog-body');
      await auditFocus(page, `${name} coverage scrollport`);
      await scrollKey(page, '.coverage-dialog-body', 'End');
      await check(page, `${name} coverage end`);
      await close(page, 'coverage-dialog');
      await activate(page, '#webhook-status-open');
      await check(page, `${name} webhook`);
      await tabTo(page, '.webhook-status-body');
      await auditFocus(page, `${name} webhook scrollport`);
      await scrollKey(page, '.webhook-status-body', 'End');
      await check(page, `${name} webhook end`);
      await close(page, 'webhook-status-dialog');
      await activate(page, '#appearance-open');
      await auditFocus(page, `${name} preset radio`);
      await auditBorders(page, '.preset-tile:has(input:focus-visible)');
      await check(page, `${name} appearance`);
      await close(page, 'appearance-dialog');
      await activate(page, '[data-page="sports"]');
      await page.waitForFunction(() => document.querySelectorAll('.workout-entry').length === 60);
      await check(page, `${name} sports`);
      await activate(page, '#workout-add');
      await auditBorders(page, '[name="exercise_type"]');
      await check(page, `${name} workout form and placeholder`);
      await activate(page, '#assistant-open');
      assert.equal(await page.locator('#assistant-input').isDisabled(), true);
      await auditFocus(page, `${name} offline Umay close`);
      await check(page, `${name} Umay`);
      await page.keyboard.press('Escape');
      await contained(page);
      await page.close();
    }
  } finally { await browser.close(); }
});
