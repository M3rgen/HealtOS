// Basis of the model: 175 cm, 70 kg (BMI 22.9), 20 % body fat, ~40 % skeletal muscle.
const BASIS = { heightCm: 175, weightKg: 70, bodyFatPct: 20, musclePct: 40 };
const BASIS_BMI = BASIS.weightKg / (BASIS.heightCm / 100) ** 2; // 22.86
const BASIS_FMI = BASIS_BMI * BASIS.bodyFatPct / 100; // fat mass index, 4.57 kg/m²
const BASIS_FFMI = BASIS_BMI - BASIS_FMI; // fat-free mass index, 18.29 kg/m²

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
function plausible(value, lo, hi) {
  const n = value == null || value === '' ? NaN : Number(value);
  return Number.isFinite(n) && n >= lo && n <= hi ? n : null;
}
// Piecewise-linear signed axis: 0 at basis, ±1 at the chosen "full morph" points.
const axis = (value, basis, upSpan, downSpan) => clamp((value - basis) / (value >= basis ? upSpan : downSpan), -1, 1);

/**
 * Map profile metrics to two signed morph axes plus a height scale.
 * Missing or implausible values fall back to the basis, so `{}` yields the untouched model.
 *
 * fat axis (−1 … +1 → fat_low … fat_high):
 *   with weight: fat mass index FMI = BMI × fat% (fat% defaults to 20 when unknown);
 *     axis = (FMI − 4.57) / 6.5 above basis (≈ 11 kg/m², e.g. 175 cm · 100 kg · 35 %),
 *            (FMI − 4.57) / 3.0 below basis (≈ 1.6 kg/m², e.g. 175 cm · 60 kg · 6 %).
 *   fat% only: axis = (fat% − 20) / 15 above, / 12 below (35 % → +1, 8 % → −1).
 * muscle axis (−1 … +1 → muscle_low … muscle_high):
 *   muscle% term: (muscle% − 40) / 8   (48 % → +1, 32 % → −1).
 *   lean-mass term (needs weight): fat-free mass index FFMI = BMI × (1 − fat%);
 *     (FFMI − 18.3) / 5 above (≈ 23.3, very muscular), / 3 below (≈ 15.3, frail).
 *   both known → 0.6 × muscle% term + 0.4 × lean-mass term; otherwise whichever exists.
 * scale: height / 175 cm clamped to 0.82 … 1.2; applied as (√s, s, √s) because at a
 *   constant BMI body volume ∝ h², so girth grows with √h while the morphs carry BMI.
 * Height (default 175 cm) is used for BMI so height and weight are always read together.
 */
export function metricsToMorphs({ heightCm, weightKg, bodyFatPct, musclePct } = {}) {
  const height = plausible(heightCm, 100, 250);
  const weight = plausible(weightKg, 25, 350);
  const fatPct = plausible(bodyFatPct, 2, 70);
  const musclePctValue = plausible(musclePct, 10, 75);
  const meters = (height ?? BASIS.heightCm) / 100;
  let fat = 0, muscle = 0;
  if (weight != null) {
    const bmi = weight / (meters * meters);
    const fmi = bmi * (fatPct ?? BASIS.bodyFatPct) / 100;
    fat = axis(fmi, BASIS_FMI, 6.5, 3);
    const lean = axis(bmi - fmi, BASIS_FFMI, 5, 3);
    muscle = musclePctValue == null ? lean : .6 * axis(musclePctValue, BASIS.musclePct, 8, 8) + .4 * lean;
  } else {
    if (fatPct != null) fat = axis(fatPct, BASIS.bodyFatPct, 15, 12);
    if (musclePctValue != null) muscle = axis(musclePctValue, BASIS.musclePct, 8, 8);
  }
  const scale = height == null ? 1 : clamp(height / BASIS.heightCm, .82, 1.2);
  return { fat: clamp(fat, -1, 1), muscle: clamp(muscle, -1, 1), scale };
}

/**
 * Normal-map strength for the current fat influences. All muscle relief lives in body.glb's
 * normal map and must fade under adipose tissue (lane A's recommendation):
 *   normalScale = min(max, max(min, (1 − fat_high)^exponent) · (1 + fatLowGain · fat_low))
 *   defaults: max 1.25, min 0.05, exponent 1.8, fatLowGain 0.25
 *   → 1.0 at fat_high 0, 0.53 @ 0.3, 0.29 @ 0.5, 0.095 @ 0.73, 0.05 @ 1; 1.25 at fat_low 1.
 * Parameters are read from body.glb `extras.healthos.suggestedNormalScale` when present, either as
 * numeric fields {max, min, exponent, fatLowGain} or parsed from its `formula` string of exactly
 * this shape; the defaults below apply otherwise. The formula string is never evaluated.
 */
export const DEFAULT_NORMAL_RELIEF = { max: 1.25, min: .05, exponent: 1.8, fatLowGain: .25 };
export function normalReliefScale(fatHigh, fatLow, params = DEFAULT_NORMAL_RELIEF) {
  const { max, min, exponent, fatLowGain } = params;
  return Math.min(max, Math.max(min, Math.pow(Math.max(0, 1 - fatHigh), exponent)) * (1 + fatLowGain * fatLow));
}
const RELIEF_FORMULA = /min\(\s*([\d.]+)\s*,\s*max\(\s*([\d.]+)\s*,\s*pow\(\s*1\s*-\s*fat_high\s*,\s*([\d.]+)\s*\)\s*\)\s*\*\s*\(\s*1\s*\+\s*([\d.]+)\s*\*\s*fat_low\s*\)\s*\)/;
export function parseHealthos(extras) {
  if (typeof extras !== 'string') return extras && typeof extras === 'object' ? extras : null;
  try { return JSON.parse(extras); } catch { return null; }
}
export function reliefParams(extras) {
  const healthos = parseHealthos(extras);
  const suggested = healthos?.suggestedNormalScale;
  if (!suggested || typeof suggested !== 'object') return DEFAULT_NORMAL_RELIEF;
  const parsed = typeof suggested.formula === 'string' ? RELIEF_FORMULA.exec(suggested.formula) : null;
  const fromFormula = parsed ? { max: parsed[1], min: parsed[2], exponent: parsed[3], fatLowGain: parsed[4] } : {};
  const pick = (key) => {
    for (const value of [suggested[key], fromFormula[key]]) {
      const n = value == null || value === '' ? NaN : Number(value);
      if (Number.isFinite(n)) return n;
    }
    return DEFAULT_NORMAL_RELIEF[key];
  };
  return { max: pick('max'), min: pick('min'), exponent: pick('exponent'), fatLowGain: pick('fatLowGain') };
}
