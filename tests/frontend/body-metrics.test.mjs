import test from 'node:test';
import assert from 'node:assert/strict';
import { metricsToMorphs, DEFAULT_NORMAL_RELIEF, normalReliefScale, parseHealthos, reliefParams } from '../../frontend/src/body-metrics.js';

const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-12, `${actual} ≠ ${expected}`);
// Recorded from the original body.js before extraction; values are synthetic profiles.
const profiles = [
  [{}, { fat: 0, muscle: 0, scale: 1 }],
  [{ heightCm: 175, weightKg: 70, bodyFatPct: 20, musclePct: 40 }, { fat: 0, muscle: 0, scale: 1 }],
  [{ heightCm: 180, weightKg: 90, bodyFatPct: 30, musclePct: 45 },
    { fat: .5787545787545785, muscle: .46769841269841267, scale: 1.0285714285714285 }],
  [{ weightKg: 90 }, { fat: .20094191522762947, muscle: 1, scale: 1 }],
  [{ weightKg: 50 }, { fat: -.43537414965986415, muscle: -1, scale: 1 }],
  [{ heightCm: 160, weightKg: 55, bodyFatPct: 12, musclePct: 38 },
    { fat: -.6644345238095242, muscle: -.10035714285714306, scale: .9142857142857143 }],
  [{ bodyFatPct: 35, musclePct: 48 }, { fat: 1, muscle: 1, scale: 1 }],
  [{ bodyFatPct: 8, musclePct: 32 }, { fat: -1, muscle: -1, scale: 1 }],
  [{ heightCm: 100, weightKg: 350, bodyFatPct: 70, musclePct: 75 }, { fat: 1, muscle: 1, scale: .82 }],
  [{ heightCm: 250, weightKg: 25, bodyFatPct: 2, musclePct: 10 }, { fat: -1, muscle: -1, scale: 1.2 }],
];

for (const [profile, expected] of profiles) {
  test(`morph results remain unchanged for ${JSON.stringify(profile)}`, () => {
    const input = Object.freeze(profile);
    const actual = metricsToMorphs(input);
    assert.deepEqual(Object.keys(actual), ['fat', 'muscle', 'scale']);
    for (const key of Object.keys(expected)) close(actual[key], expected[key]);
    const strings = Object.fromEntries(Object.entries(input).map(([key, value]) => [key, String(value)]));
    assert.deepEqual(metricsToMorphs(strings), actual);
  });
}

test('missing, nonfinite and implausible values independently fall back to the basis', () => {
  const basis = { fat: 0, muscle: 0, scale: 1 };
  assert.deepEqual(metricsToMorphs(), basis);
  assert.throws(() => metricsToMorphs(null), TypeError);
  const bounds = { heightCm: [100, 250], weightKg: [25, 350], bodyFatPct: [2, 70], musclePct: [10, 75] };
  for (const [key, [lo, hi]] of Object.entries(bounds)) {
    for (const value of [undefined, null, '', 'invalid', NaN, Infinity, -Infinity, lo - .01, hi + .01]) {
      assert.deepEqual(metricsToMorphs({ [key]: value }), basis, `${key}: ${value}`);
    }
  }
  assert.deepEqual(metricsToMorphs({ heightCm: 'invalid', weightKg: 90 }), metricsToMorphs({ weightKg: 90 }));
  assert.deepEqual(metricsToMorphs({ weightKg: 0, bodyFatPct: 35, musclePct: 32 }), { fat: 1, muscle: -1, scale: 1 });
});

test('normal relief keeps its original curve, floor, low-fat gain and cap', () => {
  assert.deepEqual(DEFAULT_NORMAL_RELIEF, { max: 1.25, min: .05, exponent: 1.8, fatLowGain: .25 });
  for (const [high, low, expected] of [
    [0, 0, 1], [.3, 0, .5262310526550318], [.5, 0, .2871745887492587],
    [.73, 0, .0947228541342634], [1, 0, .05], [0, 1, 1.25], [.5, .5, .32307141234291603], [2, 0, .05],
  ]) close(normalReliefScale(high, low), expected);
  const custom = Object.freeze({ max: 1.6, min: .1, exponent: 2, fatLowGain: .4 });
  close(normalReliefScale(.5, .5, custom), .3);
  close(normalReliefScale(1, 1, custom), .14);
  close(normalReliefScale(0, 2, custom), 1.6);
});

test('model metadata accepts object/JSON and defaults without evaluating formula strings', () => {
  for (const value of [undefined, null, false, 42, '', '{broken']) {
    assert.equal(parseHealthos(value), null);
    assert.equal(reliefParams(value), DEFAULT_NORMAL_RELIEF);
  }
  const metadata = { skinToneHex: '#c49175', suggestedNormalScale: { max: '1.6', exponent: 2 } };
  assert.equal(parseHealthos(metadata), metadata);
  assert.deepEqual(parseHealthos(JSON.stringify(metadata)), metadata);
  const expected = { max: 1.6, min: .05, exponent: 2, fatLowGain: .25 };
  assert.deepEqual(reliefParams(metadata), expected);
  assert.deepEqual(reliefParams(JSON.stringify(metadata)), expected);
  assert.equal(reliefParams({ suggestedNormalScale: 'invalid' }), DEFAULT_NORMAL_RELIEF);
  assert.deepEqual(reliefParams({ suggestedNormalScale: { formula: 'throw new Error("must not execute")' } }), DEFAULT_NORMAL_RELIEF);
});

test('numeric metadata overrides formula fields, invalid fields use formula then defaults', () => {
  const formula = 'min( 1.6, max( 0.1, pow(1 - fat_high, 2) ) * (1 + 0.4 * fat_low))';
  assert.deepEqual(reliefParams({ suggestedNormalScale: { formula } }), { max: 1.6, min: .1, exponent: 2, fatLowGain: .4 });
  assert.deepEqual(reliefParams({ suggestedNormalScale: { formula, max: '1.8', min: '', exponent: Infinity, fatLowGain: 'bad' } }),
    { max: 1.8, min: .1, exponent: 2, fatLowGain: .4 });
  assert.deepEqual(reliefParams({ suggestedNormalScale: { max: '', min: null, exponent: NaN, fatLowGain: 'bad' } }), DEFAULT_NORMAL_RELIEF);
});
