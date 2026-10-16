import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import './body.css';

// Locally bundled body model (see public/models/PROVENANCE.md). glTF Y-up, feet at y=0,
// facing +Z, ~1.75 m at basis, carrying the morph targets below on every deforming mesh.
const MODEL_URL = '/models/body.glb';
const PEDESTAL_URL = '/assets/3d/pedestal-glow.webp';
const MORPHS = ['fat_high', 'fat_low', 'muscle_high', 'muscle_low'];

const TWEEN_MS = 600;
const BASE_HEIGHT_M = 1.75;
// Height cue: the frame always spans at least a 182 cm figure, so shorter people read shorter
// inside the same frame; beyond that the frame follows the figure so nothing is cropped.
const FRAME_MIN_SCALE = 1.04;
const MARGIN_Y = .07; // fraction of the canvas kept free above the head and below the feet
const MARGIN_X = .03;
const FOV = 20;
const ELEVATION = THREE.MathUtils.degToRad(4); // camera looks very slightly down
const START_TURN = .35; // front three-quarter view (radians)
const SHAPE_BINS = 48;
const SHAPE_SPAN_M = 2.4; // normalised-model height covered by the silhouette bins

// Basis of the model: 175 cm, 70 kg (BMI 22.9), 20 % body fat, ~40 % skeletal muscle.
const BASIS = { heightCm: 175, weightKg: 70, bodyFatPct: 20, musclePct: 40 };
const BASIS_BMI = BASIS.weightKg / (BASIS.heightCm / 100) ** 2; // 22.86
const BASIS_FMI = BASIS_BMI * BASIS.bodyFatPct / 100; // fat mass index, 4.57 kg/m²
const BASIS_FFMI = BASIS_BMI - BASIS_FMI; // fat-free mass index, 18.29 kg/m²

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const ease = (t) => (t < .5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);
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
function parseHealthos(extras) {
  if (typeof extras !== 'string') return extras && typeof extras === 'object' ? extras : null;
  try { return JSON.parse(extras); } catch { return null; }
}
function reliefParams(extras) {
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

function radialTexture(stops) {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 128;
  const ctx = canvas.getContext('2d');
  const gradient = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  for (const [offset, color] of stops) gradient.addColorStop(offset, color);
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 128, 128);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function groundDecal(texture, opacity, blending) {
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({
    map: texture, transparent: true, opacity, depthWrite: false, blending, toneMapped: false,
  }));
  mesh.rotation.x = -Math.PI / 2;
  mesh.renderOrder = 1;
  return mesh;
}

// Tileable skin micro-detail (lane C, public/assets/skin/, see ASSETS.md): OpenGL +Y normal,
// roughness 0.5 = neutral. One tile ≈ 6–7 cm on the body at 28 repeats per UV unit.
// Missing files simply leave the detail off.
const SKIN_DETAIL = {
  normalUrl: '/assets/skin/skin-detail-normal.png',
  roughnessUrl: '/assets/skin/skin-detail-roughness.webp',
  repeat: 28, // tiles across the 0–1 body UV atlas
  normalStrength: .35,
  roughnessStrength: 1, // base roughness × (2 × detail): 0.5 is neutral, 0.43–0.57 → ×0.86–1.14
};

// Cheap single-pass skin approximation layered onto three's physical BRDF:
// - per-channel wrapped diffuse: red light bleeds past the terminator (subsurface softness);
// - back-scatter: light from behind leaks through thin silhouette areas (ears, fingers) red-shifted;
// - built-in sheen for peach fuzz, skin-like F0 (~0.028);
// - tileable micro-detail normal blended over the base map (whiteout); the fat-driven
//   normalScale only scales the base (muscle) map, never the pores;
// - a thin amber Fresnel accent so the UI colour reads on the silhouette without tinting the skin.
function makeSkinMaterial(source, skinUniforms) {
  const material = new THREE.MeshPhysicalMaterial();
  THREE.MeshStandardMaterial.prototype.copy.call(material, source);
  material.defines = { STANDARD: '', PHYSICAL: '' };
  material.specularIntensity = .7;
  material.sheen = .35;
  material.sheenRoughness = .55;
  material.sheenColor.set(0x5a463c);
  if (!material.roughnessMap && material.roughness < .6) material.roughness = .6; // untextured skin reads as plastic below this
  material.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, skinUniforms);
    let lights = THREE.ShaderChunk.lights_physical_pars_fragment;
    const diffuseLine = 'reflectedLight.directDiffuse += irradiance * BRDF_Lambert( material.diffuseContribution ) * ( 1.0 - F );';
    if (lights.includes(diffuseLine)) {
      lights = lights.replace(diffuseLine, `
        float skinNL = dot( geometryNormal, directLight.direction );
        vec3 skinWrapped = saturate( ( vec3( skinNL ) + skinWrap ) / ( 1.0 + skinWrap ) );
        reflectedLight.directDiffuse += skinWrapped * directLight.color * BRDF_Lambert( material.diffuseContribution ) * ( 1.0 - F );
        // Back-scatter uses the smooth surface normal (mapped detail would make it noisy) and is clamped.
        vec3 skinBackDir = normalize( directLight.direction + skinSmoothNormal * 0.35 );
        float skinBack = min( pow( saturate( dot( geometryViewDir, - skinBackDir ) ), 4.0 ) * skinThin, 0.35 );
        reflectedLight.directDiffuse += directLight.color * skinBack * skinScatter * skinScatterColor * material.diffuseContribution;`);
    }
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', `#include <common>
        uniform vec3 rimColor; uniform float rimStrength;
        uniform vec3 skinWrap; uniform vec3 skinScatterColor; uniform float skinScatter;
        uniform sampler2D skinDetailNormal; uniform sampler2D skinDetailRoughness;
        uniform float skinDetailRepeat; uniform float skinDetailNormalStrength; uniform float skinDetailRoughnessStrength;
        float skinThin = 0.0;
        vec3 skinSmoothNormal = vec3( 0.0, 0.0, 1.0 );`)
      .replace('#include <lights_physical_pars_fragment>', lights)
      .replace('#include <roughnessmap_fragment>', `#include <roughnessmap_fragment>
        #if defined( SKIN_DETAIL_ROUGHNESS ) && defined( USE_NORMALMAP )
          roughnessFactor = clamp( roughnessFactor * mix( 1.0, 2.0 * texture2D( skinDetailRoughness, vNormalMapUv * skinDetailRepeat ).g, skinDetailRoughnessStrength ), 0.05, 1.0 );
        #endif`)
      .replace('#include <normal_fragment_maps>', `
        #if defined( SKIN_DETAIL_NORMAL ) && defined( USE_NORMALMAP_TANGENTSPACE )
          vec3 mapN = texture2D( normalMap, vNormalMapUv ).xyz * 2.0 - 1.0;
          mapN.xy *= normalScale;
          vec2 detailUv = vNormalMapUv * skinDetailRepeat;
          vec3 detailN = texture2D( skinDetailNormal, detailUv ).xyz * 2.0 - 1.0;
          // Pores alias into a comb pattern where the surface turns away (grazing, strongly anisotropic
          // minification) or where one pixel spans a large part of a tile: fade the detail out there.
          float detailFacing = smoothstep( 0.12, 0.45, abs( dot( nonPerturbedNormal, normalize( vViewPosition ) ) ) );
          float detailFootprint = max( length( dFdx( detailUv ) ), length( dFdy( detailUv ) ) );
          float detailFade = detailFacing * ( 1.0 - smoothstep( 0.08, 0.25, detailFootprint ) );
          detailN.xy *= skinDetailNormalStrength * detailFade * vec2( 1.0, sign( normalScale.y ) );
          normal = normalize( tbn * normalize( vec3( mapN.xy + detailN.xy, mapN.z * detailN.z ) ) );
        #else
          #include <normal_fragment_maps>
        #endif`)
      .replace('#include <emissivemap_fragment>', `#include <emissivemap_fragment>
        // Silhouette terms use the interpolated surface normal so normal-map creases don't light up.
        float rimFacing = 1.0 - saturate( dot( nonPerturbedNormal, normalize( vViewPosition ) ) );
        skinThin = rimFacing * rimFacing;
        skinSmoothNormal = nonPerturbedNormal;
        totalEmissiveRadiance += rimColor * rimStrength * pow( rimFacing, 4.0 );`);
  };
  material.customProgramCacheKey = () => 'healthos-skin-v2';
  return material;
}

function staticFallback(container) {
  container.innerHTML = '<div class="model-fallback"><svg viewBox="0 0 120 280" aria-hidden="true"><path fill="#8d8b85" d="M60 6c-10 0-17 8-17 20 0 10 4 18 8 22l-2 10c-14 3-23 11-26 26l-9 58-1 29c0 7 5 9 7 3l4-26 10-40 5 43-6 76-1 34-7 8c-3 4 0 7 7 7h10l8-9 3-38 7-45 7 45 3 38 8 9h10c7 0 10-3 7-7l-7-8-1-34-6-76 5-43 10 40 4 26c2 6 7 4 7-3l-1-29-9-58c-3-15-12-23-26-26l-2-10c4-4 8-12 8-22 0-12-7-20-17-20Z"/></svg></div>';
  return { setMotion() {}, setBodyMetrics() {}, dispose() {} };
}

export function createBody(container, motionEnabled) {
  let renderer;
  try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' }); }
  catch { return staticFallback(container); }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  // Khronos PBR Neutral keeps the painted skin hue (ACES pushed it orange); exposure keeps forehead and
  // shoulder highlights below clipping while the shadow side stays lifted off the #181814 page.
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = .88;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.VSMShadowMap; // smooth penumbrae; jittered PCF combed grazing skin
  container.append(renderer.domElement);

  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  const room = new RoomEnvironment();
  const envMap = pmrem.fromScene(room, .04).texture;
  room.dispose(); pmrem.dispose();
  scene.environment = envMap;
  scene.environmentIntensity = .14;

  // Lights are fixed in world space; the figure turns beneath them.
  const camera = new THREE.PerspectiveCamera(FOV, 1, .1, 60);
  scene.add(new THREE.HemisphereLight(0xeee9df, 0x221f1a, .12));
  const key = new THREE.DirectionalLight(0xfff5ea, 2.9);
  key.position.set(-2.6, 4.4, 3.2);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  Object.assign(key.shadow.camera, { left: -1.4, right: 1.4, top: 1.6, bottom: -1.6, near: .5, far: 12 });
  // VSM radius 5: a wider blur bleeds light into blocky steps on the shoulders.
  key.shadow.radius = 5; key.shadow.blurSamples = 12; key.shadow.bias = -.0005; key.shadow.normalBias = .03;
  key.target.position.set(0, .9, 0);
  scene.add(key, key.target);
  const fill = new THREE.DirectionalLight(0xd9e0ea, .32);
  fill.position.set(3, 1.4, 2.6);
  // Rim lights are warm but desaturated so skin never turns orange; the UI amber (#f0c478) is
  // carried only by the thin Fresnel accent in the skin shader.
  const rim = new THREE.DirectionalLight(0xf4dcba, 3.6);
  rim.position.set(2.8, 2.6, -3.2);
  const rimLeft = new THREE.DirectionalLight(0xf4dcba, 1.6);
  rimLeft.position.set(-3.2, 2, -2.4);
  scene.add(fill, rim, rimLeft);

  // Ground: the amber pedestal glow and the key light's cast shadow stay put; one soft contact
  // shadow per foot turns with the figure (placed from the model's foot vertices in prepare()).
  const ground = new THREE.Group();
  const contactTexture = radialTexture([[0, 'rgba(0,0,0,.7)'], [.5, 'rgba(0,0,0,.32)'], [1, 'rgba(0,0,0,0)']]);
  const contacts = [0, 1].map(() => {
    const decal = groundDecal(contactTexture, 1, THREE.NormalBlending);
    decal.position.y = .002;
    decal.visible = false;
    return decal;
  });
  // The pedestal image is already drawn as a screen-space ellipse (2:1 canvas), so its ground depth
  // is stretched in fitCamera() to cancel the camera's foreshortening and keep that 2:1 on screen.
  const pedestalTexture = new THREE.TextureLoader().load(PEDESTAL_URL, () => { dirty = true; });
  pedestalTexture.colorSpace = THREE.SRGBColorSpace;
  const pedestal = groundDecal(pedestalTexture, .6, THREE.AdditiveBlending);
  pedestal.position.y = .001;
  const PEDESTAL_WIDTH = 1.6;
  const castShadow = new THREE.Mesh(new THREE.CircleGeometry(1.1, 48), new THREE.ShadowMaterial({ opacity: .24, depthWrite: false }));
  castShadow.rotation.x = -Math.PI / 2;
  castShadow.position.y = .003;
  castShadow.receiveShadow = true;
  ground.add(pedestal, castShadow);
  scene.add(ground);

  // turntable (rotation) → figure (height scale) → normalizer (fit source to contract) → model
  const turntable = new THREE.Group();
  const figure = new THREE.Group();
  const normalizer = new THREE.Group();
  figure.add(normalizer, ...contacts); turntable.add(figure); scene.add(turntable);

  // Shared skin shader uniforms (see makeSkinMaterial); detail maps arrive asynchronously.
  const skinUniforms = {
    rimColor: { value: new THREE.Color(0xf0c478) }, rimStrength: { value: .1 },
    skinWrap: { value: new THREE.Vector3(.2, .09, .05) },
    skinScatterColor: { value: new THREE.Color(1, .38, .24) }, skinScatter: { value: .2 },
    skinDetailNormal: { value: null }, skinDetailRoughness: { value: null },
    skinDetailRepeat: { value: SKIN_DETAIL.repeat },
    skinDetailNormalStrength: { value: SKIN_DETAIL.normalStrength },
    skinDetailRoughnessStrength: { value: SKIN_DETAIL.roughnessStrength },
  };
  const skinMaterials = [];
  const detailTextures = [];
  function loadDetail(url, uniform, define) {
    new THREE.TextureLoader().load(url, (texture) => {
      if (disposed) { texture.dispose(); return; }
      texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
      texture.colorSpace = THREE.NoColorSpace;
      texture.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
      skinUniforms[uniform].value = texture;
      detailTextures.push(texture);
      for (const material of skinMaterials) { material.defines[define] = ''; material.needsUpdate = true; }
      container.dataset.skinDetail = [container.dataset.skinDetail, define].filter(Boolean).join(',');
      dirty = true;
    }, undefined, () => { /* optional asset: render without micro-detail */ });
  }
  loadDetail(SKIN_DETAIL.normalUrl, 'skinDetailNormal', 'SKIN_DETAIL_NORMAL');
  loadDetail(SKIN_DETAIL.roughnessUrl, 'skinDetailRoughness', 'SKIN_DETAIL_ROUGHNESS');

  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const bindings = [];
  const reliefMaterials = []; // [material, base normalScale]
  let relief = DEFAULT_NORMAL_RELIEF;
  // Normalised-space vertex data used to measure the silhouette of the current morph state.
  const shapeMeshes = []; // { base: Float32Array, deltas: [[morphName, Float32Array]] }
  const shape = { rho: new Float32Array(SHAPE_BINS), lo: new Float32Array(SHAPE_BINS), hi: new Float32Array(SHAPE_BINS), top: BASE_HEIGHT_M };
  let shapeKey = '';
  let disposed = false, loaded = false, dirty = true;
  let enabled = motionEnabled, elapsed = 0, last = 0;
  const current = { fat: 0, muscle: 0, scale: 1 };
  let from = { ...current }, to = { ...current }, tweenStart = -1;

  const influences = () => ({
    fat_high: Math.max(current.fat, 0), fat_low: Math.max(-current.fat, 0),
    muscle_high: Math.max(current.muscle, 0), muscle_low: Math.max(-current.muscle, 0),
  });

  // Per height band: the widest horizontal reach (radius from the turn axis) of the morphed mesh.
  function measureShape(values) {
    const key = `${values.fat_high},${values.fat_low},${values.muscle_high},${values.muscle_low}`;
    if (key === shapeKey || !shapeMeshes.length) return;
    shapeKey = key;
    shape.rho.fill(-1); shape.lo.fill(Infinity); shape.hi.fill(-Infinity);
    let top = 0;
    const binHeight = SHAPE_SPAN_M / SHAPE_BINS;
    for (const { base, deltas } of shapeMeshes) {
      const active = deltas.filter(([name]) => values[name] > 0).map(([name, delta]) => [values[name], delta]);
      for (let i = 0; i < base.length; i += 3) {
        let x = base[i], y = base[i + 1], z = base[i + 2];
        for (const [w, delta] of active) { x += w * delta[i]; y += w * delta[i + 1]; z += w * delta[i + 2]; }
        const bin = clamp(Math.floor(y / binHeight), 0, SHAPE_BINS - 1);
        const rho = Math.hypot(x, z);
        if (rho > shape.rho[bin]) shape.rho[bin] = rho;
        if (y < shape.lo[bin]) shape.lo[bin] = y;
        if (y > shape.hi[bin]) shape.hi[bin] = y;
        if (y > top) top = y;
      }
    }
    shape.top = top;
  }

  function applyMorphs() {
    const values = influences();
    for (const { mesh, indices } of bindings) {
      for (const [name, index] of indices) mesh.morphTargetInfluences[index] = values[name];
    }
    const strength = normalReliefScale(values.fat_high, values.fat_low, relief);
    for (const [material, base] of reliefMaterials) material.normalScale.copy(base).multiplyScalar(strength);
    measureShape(values);
    const girth = Math.sqrt(current.scale);
    figure.scale.set(girth, current.scale, girth);
    ground.scale.setScalar(girth);
    container.dataset.morphFat = current.fat.toFixed(3);
    container.dataset.morphMuscle = current.muscle.toFixed(3);
    container.dataset.heightScale = current.scale.toFixed(3);
    container.dataset.normalScale = strength.toFixed(3);
    dirty = true;
  }

  // Fit the camera to the current silhouette: every point of every height band, at any turn angle,
  // must land inside the canvas minus the margins. Solved by bisection on camera distance.
  const fitView = { forward: new THREE.Vector3(), up: new THREE.Vector3() };
  function fitCamera() {
    const w = container.clientWidth, h = container.clientHeight;
    if (!w || !h) return;
    const aspect = w / h;
    const tanY = Math.tan(THREE.MathUtils.degToRad(FOV / 2)) * (1 - 2 * MARGIN_Y);
    const tanX = Math.tan(THREE.MathUtils.degToRad(FOV / 2)) * aspect * (1 - 2 * MARGIN_X);
    const s = current.scale, girth = Math.sqrt(s);
    const figureTop = shape.top * s;
    const frameTop = Math.max(figureTop, BASE_HEIGHT_M * FRAME_MIN_SCALE);
    const centerY = frameTop / 2;
    const points = [[0, 0], [0, frameTop]]; // [radius, height]; frame corners keep the height cue
    for (let b = 0; b < SHAPE_BINS; b++) {
      if (shape.rho[b] < 0) continue;
      points.push([shape.rho[b] * girth, shape.lo[b] * s], [shape.rho[b] * girth, shape.hi[b] * s]);
    }
    const { forward, up } = fitView;
    forward.set(0, -Math.sin(ELEVATION), -Math.cos(ELEVATION));
    up.set(0, Math.cos(ELEVATION), -Math.sin(ELEVATION));
    const fits = (distance) => {
      const camY = centerY + distance * Math.sin(ELEVATION), camZ = distance * Math.cos(ELEVATION);
      for (const [rho, y] of points) {
        const dy = y - camY;
        // Worst case over the turn: the point may sit anywhere on its circle of radius rho.
        for (let k = 0; k < 16; k++) {
          const angle = k / 16 * Math.PI * 2;
          const px = rho * Math.cos(angle), pz = rho * Math.sin(angle) - camZ;
          const depth = dy * forward.y + pz * forward.z;
          if (depth <= .05) return false;
          if (Math.abs(px) > tanX * depth || Math.abs(dy * up.y + pz * up.z) > tanY * depth) return false;
        }
      }
      return true;
    };
    let near = .5, far = 40;
    for (let i = 0; i < 28; i++) { const mid = (near + far) / 2; if (fits(mid)) far = mid; else near = mid; }
    camera.aspect = aspect;
    camera.position.set(0, centerY + far * Math.sin(ELEVATION), far * Math.cos(ELEVATION));
    camera.lookAt(0, centerY, 0);
    camera.updateProjectionMatrix();
    // Keep the pedestal's 2:1 screen ellipse: depth on the ground = width / (2 · sin(view angle)).
    const viewAngle = Math.max(Math.atan2(camera.position.y, camera.position.z), .05);
    pedestal.scale.set(PEDESTAL_WIDTH, Math.min(PEDESTAL_WIDTH / (2 * Math.sin(viewAngle)), far * 1.2), 1);
    dirty = true;
  }

  // Contact shadows from the lowest vertices, split at the stance midline. The standing foot (most
  // vertices on the floor) gets the full shadow; a raised heel (contrapposto) only a faint one.
  function placeContacts() {
    const low = [];
    for (const { base } of shapeMeshes) for (let i = 0; i < base.length; i += 3) if (base[i + 1] < .045) low.push(base[i], base[i + 2], base[i + 1]);
    if (!low.length) return;
    let minX = Infinity, maxX = -Infinity;
    for (let i = 0; i < low.length; i += 3) { minX = Math.min(minX, low[i]); maxX = Math.max(maxX, low[i]); }
    const mid = (minX + maxX) / 2;
    const planted = [0, 0];
    for (let i = 0; i < low.length; i += 3) if (low[i + 2] < .006) planted[low[i] < mid ? 0 : 1]++;
    const standing = planted[0] >= planted[1] ? 0 : 1;
    contacts.forEach((decal, side) => {
      const box = [Infinity, -Infinity, Infinity, -Infinity];
      for (let i = 0; i < low.length; i += 3) {
        if ((low[i] < mid) !== (side === 0)) continue;
        box[0] = Math.min(box[0], low[i]); box[1] = Math.max(box[1], low[i]);
        box[2] = Math.min(box[2], low[i + 1]); box[3] = Math.max(box[3], low[i + 1]);
      }
      if (!Number.isFinite(box[0])) return;
      decal.position.set((box[0] + box[1]) / 2, .002, (box[2] + box[3]) / 2);
      decal.scale.set(box[1] - box[0] + .22, box[3] - box[2] + .22, 1);
      decal.material.opacity = side === standing ? 1 : .45;
      decal.visible = true;
    });
  }

  function prepare(model) {
    model.updateMatrixWorld(true);
    const box = new THREE.Box3();
    const point = new THREE.Vector3(), delta = new THREE.Vector3();
    model.traverse(node => {
      if (!node.isMesh) return;
      const position = node.geometry.attributes.position;
      for (let i = 0; i < position.count; i++) box.expandByPoint(point.fromBufferAttribute(position, i).applyMatrix4(node.matrixWorld));
    });
    const size = box.getSize(new THREE.Vector3());
    // Contract says 1.75 m with feet at y=0; normalise anyway so any source unit still frames correctly.
    const unit = size.y > 1.4 && size.y < 2.1 ? 1 : BASE_HEIGHT_M / size.y;
    normalizer.scale.setScalar(unit);
    // A compliant model (feet at y=0, centre of mass on the origin) turns about its own origin, which
    // matters for asymmetric poses; only off-contract sources are re-centred on their bounding box.
    const compliant = unit === 1 && Math.abs(box.min.y) < .05 && Math.hypot((box.min.x + box.max.x) / 2, (box.min.z + box.max.z) / 2) < .25;
    if (compliant) normalizer.position.set(0, -box.min.y, 0);
    else normalizer.position.set(-(box.min.x + box.max.x) / 2 * unit, -box.min.y * unit, -(box.min.z + box.max.z) / 2 * unit);
    normalizer.updateMatrix();
    const found = new Set();
    const converted = new Map();
    let skinTone = null;
    model.traverse(node => {
      if (node.userData?.healthos) {
        const healthos = parseHealthos(node.userData.healthos);
        if (relief === DEFAULT_NORMAL_RELIEF) relief = reliefParams(healthos);
        if (/^#?[0-9a-f]{6}$/i.test(healthos?.skinToneHex ?? '')) skinTone = new THREE.Color(`#${healthos.skinToneHex.replace('#', '')}`);
      }
      if (!node.isMesh) return;
      node.castShadow = true;
      node.receiveShadow = true;
      const swap = (source) => {
        if (!converted.has(source)) {
          const material = makeSkinMaterial(source, skinUniforms);
          if (skinTone && !material.map) material.color.copy(skinTone);
          for (const [define, uniform] of [['SKIN_DETAIL_NORMAL', 'skinDetailNormal'], ['SKIN_DETAIL_ROUGHNESS', 'skinDetailRoughness']]) {
            if (skinUniforms[uniform].value) material.defines[define] = '';
          }
          if (material.normalMap) reliefMaterials.push([material, material.normalScale.clone()]);
          skinMaterials.push(material);
          converted.set(source, material);
          source.dispose(); // textures are now owned by the physical material
        }
        return converted.get(source);
      };
      node.material = Array.isArray(node.material) ? node.material.map(swap) : swap(node.material);
      const dict = node.morphTargetDictionary || {};
      const indices = MORPHS.filter(name => name in dict).map(name => [name, dict[name]]);
      indices.forEach(([name]) => found.add(name));
      if (indices.length) bindings.push({ mesh: node, indices });
      // Copy positions and morph deltas into normalised figure space for silhouette measurement.
      const toFigure = new THREE.Matrix4().multiplyMatrices(normalizer.matrix, node.matrixWorld);
      const linear = new THREE.Matrix3().setFromMatrix4(toFigure);
      const position = node.geometry.attributes.position;
      const targets = node.geometry.morphAttributes.position || [];
      const base = new Float32Array(position.count * 3);
      for (let i = 0; i < position.count; i++) point.fromBufferAttribute(position, i).applyMatrix4(toFigure).toArray(base, i * 3);
      const deltas = indices.map(([name, index]) => {
        const out = new Float32Array(position.count * 3);
        for (let i = 0; i < position.count; i++) {
          delta.fromBufferAttribute(targets[index], i);
          if (!node.geometry.morphTargetsRelative) delta.sub(point.fromBufferAttribute(position, i));
          delta.applyMatrix3(linear).toArray(out, i * 3);
        }
        return [name, out];
      });
      shapeMeshes.push({ base, deltas });
    });
    placeContacts();
    container.dataset.model = 'body';
    container.dataset.morphTargets = [...found].join(',');
    if (found.size !== MORPHS.length) {
      console.warn(`body.glb is missing morph targets: ${MORPHS.filter(name => !found.has(name)).join(', ')}`);
    }
  }

  new GLTFLoader().load(MODEL_URL, ({ scene: model }) => {
    if (disposed) { disposeTree(model); return; }
    prepare(model);
    normalizer.add(model);
    loaded = true;
    applyMorphs();
    fitCamera();
    container.dataset.modelLoaded = 'true';
  }, undefined, () => {
    // Missing or invalid model: show the static silhouette instead (morph updates become no-ops).
    if (disposed) return;
    dispose();
    staticFallback(container);
  });

  const resize = new ResizeObserver(() => {
    const w = container.clientWidth, h = container.clientHeight;
    if (w && h) { renderer.setSize(w, h, false); fitCamera(); }
  });
  resize.observe(container);

  // Auto-rotation runs only when the System-page toggle is on and the OS does not ask for reduced
  // motion; otherwise the figure rests in the front three-quarter view.
  const rotates = () => loaded && enabled && !reducedMotion.matches;
  function onReducedMotion() {
    if (reducedMotion.matches) { elapsed = 0; turntable.rotation.y = START_TURN; container.dataset.rotationDegrees = '0.0'; }
    dirty = true;
  }
  reducedMotion.addEventListener('change', onReducedMotion);

  renderer.setAnimationLoop(timestamp => {
    const delta = last ? Math.min(timestamp - last, 100) : 0; last = timestamp;
    if (document.hidden || !container.getClientRects().length) return;
    if (tweenStart >= 0) {
      const k = ease(Math.min(1, (timestamp - tweenStart) / TWEEN_MS));
      for (const key of ['fat', 'muscle', 'scale']) current[key] = from[key] + (to[key] - from[key]) * k;
      if (k >= 1) tweenStart = -1;
      applyMorphs();
      fitCamera();
    } else if (tweenStart === -2) tweenStart = timestamp;
    const turning = rotates();
    if (turning) {
      elapsed += delta;
      // Thirty seconds per half turn from a front three-quarter view, easing before returning.
      turntable.rotation.y = START_TURN + Math.PI * (1 - Math.cos(elapsed / 60000 * Math.PI * 2)) / 2;
      container.dataset.rotationDegrees = ((turntable.rotation.y - START_TURN) * 180 / Math.PI).toFixed(1);
    }
    if (turning || dirty) { renderer.render(scene, camera); dirty = false; }
  });
  turntable.rotation.y = START_TURN;
  container.dataset.rotationDegrees = '0.0';

  function disposeTree(root) {
    root.traverse(node => {
      if (!node.isMesh) return;
      node.geometry.dispose();
      for (const material of [node.material].flat()) {
        for (const value of Object.values(material)) if (value?.isTexture) value.dispose();
        material.dispose();
      }
    });
  }
  function dispose() {
    if (disposed) return;
    disposed = true;
    renderer.setAnimationLoop(null);
    resize.disconnect();
    reducedMotion.removeEventListener('change', onReducedMotion);
    disposeTree(turntable);
    disposeTree(ground);
    envMap.dispose();
    detailTextures.forEach(texture => texture.dispose());
    renderer.dispose();
    renderer.domElement.remove();
    window.removeEventListener('pagehide', dispose);
  }
  window.addEventListener('pagehide', dispose);

  return {
    setMotion(value) { enabled = value; dirty = true; },
    /** Accepts {heightCm, weightKg, bodyFatPct, musclePct}; missing values keep the basis. */
    setBodyMetrics(metrics) {
      to = metricsToMorphs(metrics);
      if (to.fat === current.fat && to.muscle === current.muscle && to.scale === current.scale) return;
      if (!loaded || reducedMotion.matches) {
        Object.assign(current, to); tweenStart = -1; applyMorphs(); fitCamera();
        return;
      }
      from = { ...current };
      tweenStart = -2; // starts on the next animation frame's timestamp
    },
    dispose,
  };
}
