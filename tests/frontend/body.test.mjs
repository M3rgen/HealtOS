import test from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import * as THREE from '../../frontend/node_modules/three/build/three.module.js';
import { GLTFLoader } from '../../frontend/node_modules/three/examples/jsm/loaders/GLTFLoader.js';
import * as metrics from '../../frontend/src/body-metrics.js';

// Run the real viewer/scene/material logic with synthetic I/O, without a WebGL context.
// Only the renderer boundary and CSS import need module hooks; production JS is never rewritten.
const bodyURL = new URL('../../frontend/src/body.js', import.meta.url).href;
const threeURL = new URL('../../frontend/node_modules/three/build/three.module.js', import.meta.url).href;
const rendererKey = Symbol.for('healthos.test.bodyRenderer');
const rendererModule = 'data:text/javascript,' + encodeURIComponent(`
  export * from ${JSON.stringify(threeURL)};
  export class WebGLRenderer {
    constructor(options) { return globalThis[Symbol.for('healthos.test.bodyRenderer')](options); }
  }
`);
const hooks = registerHooks({
  resolve(specifier, context, next) {
    if (specifier === 'three' && context.parentURL === bodyURL) return { url: rendererModule, shortCircuit: true };
    return next(specifier, context);
  },
  load(url, context, next) {
    if (url.endsWith('/body.css')) return { format: 'module', source: '', shortCircuit: true };
    return next(url, context);
  },
});
const body = await import(bodyURL);
hooks.deregister();

function environment(t, { unavailable = false } = {}) {
  const events = () => ({
    listeners: new Map(),
    addEventListener(type, callback) { this.listeners.set(type, callback); },
    removeEventListener(type) { this.listeners.delete(type); },
    emit(type) { this.listeners.get(type)?.(); },
  });
  const win = { ...events(), devicePixelRatio: 2 }, reduced = { ...events(), matches: false };
  const canvas = { getContext: () => ({ createRadialGradient: () => ({ addColorStop() {} }), fillRect() {} }) };
  const doc = { hidden: false, createElement: () => canvas };
  const renderer = {
    domElement: { remove: t.mock.fn() }, shadowMap: {}, capabilities: { getMaxAnisotropy: () => 16 },
    setPixelRatio: t.mock.fn(), setClearColor() {}, setSize: t.mock.fn(), dispose: t.mock.fn(),
    render: t.mock.fn(), setAnimationLoop(callback) { this.frame = callback; },
  };
  let resize, modelRequest;
  const observer = { observe() {}, disconnect: t.mock.fn() };
  const values = {
    window: win, document: doc, matchMedia: () => reduced,
    ResizeObserver: class { constructor(callback) { resize = callback; return observer; } },
    [rendererKey]: () => { if (unavailable) throw new Error('synthetic WebGL unavailable'); return renderer; },
  };
  for (const key of Reflect.ownKeys(values)) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { configurable: true, value: values[key] });
    t.after(() => previous ? Object.defineProperty(globalThis, key, previous) : delete globalThis[key]);
  }
  const envMap = new THREE.Texture(), envDispose = t.mock.method(envMap, 'dispose');
  t.mock.method(THREE.PMREMGenerator.prototype, 'fromScene', () => ({ texture: envMap }));
  t.mock.method(THREE.PMREMGenerator.prototype, 'dispose', () => {});
  const textures = new Map();
  t.mock.method(THREE.TextureLoader.prototype, 'load', (url, complete) => {
    const texture = new THREE.Texture(), dispose = t.mock.method(texture, 'dispose');
    textures.set(url, { texture, complete: () => complete(texture), dispose });
    return texture;
  });
  t.mock.method(GLTFLoader.prototype, 'load', (url, complete, progress, fail) => { modelRequest = { url, complete, fail }; });
  const container = { dataset: {}, clientWidth: 300, clientHeight: 500, append: t.mock.fn(), getClientRects: () => [{}] };
  const controller = body.createBody(container, true);
  t.after(() => controller.dispose());
  return { controller, container, renderer, textures, reduced, win, doc, observer, envDispose,
    get request() { return modelRequest; }, resize: () => resize() };
}

function model(t) {
  const geometry = new THREE.BoxGeometry(.5, 1.75, .3).translate(0, .875, 0);
  geometry.morphTargetsRelative = true;
  geometry.morphAttributes.position = ['fat_high', 'fat_low', 'muscle_high', 'muscle_low'].map((name, index) => {
    const attribute = new THREE.Float32BufferAttribute(new Float32Array(geometry.attributes.position.array.length), 3);
    attribute.name = name;
    attribute.setX(0, .01 * (index + 1));
    return attribute;
  });
  const normalMap = new THREE.Texture(), normalDispose = t.mock.method(normalMap, 'dispose');
  const source = new THREE.MeshStandardMaterial({ normalMap });
  source.normalScale.set(.8, -.6);
  const sourceDispose = t.mock.method(source, 'dispose'), geometryDispose = t.mock.method(geometry, 'dispose');
  const mesh = new THREE.Mesh(geometry, source), scene = new THREE.Group();
  scene.userData.healthos = JSON.stringify({ skinToneHex: '#c49175', suggestedNormalScale: { max: 1.6, min: .1, exponent: 2, fatLowGain: .4 } });
  scene.add(mesh);
  return { scene, mesh, sourceDispose, normalDispose, geometryDispose };
}

test('body.js retains exactly its public exports, including the original calculation references', () => {
  assert.deepEqual(Object.keys(body).sort(), ['DEFAULT_NORMAL_RELIEF', 'createBody', 'metricsToMorphs', 'normalReliefScale']);
  for (const key of ['metricsToMorphs', 'DEFAULT_NORMAL_RELIEF', 'normalReliefScale']) assert.equal(body[key], metrics[key]);
  assert.deepEqual(body.metricsToMorphs({ bodyFatPct: 35, musclePct: 32 }), { fat: 1, muscle: -1, scale: 1 });
});

test('viewer applies pending metrics, metadata relief, material conversion, tween and motion controls', t => {
  const env = environment(t), asset = model(t);
  const { controller, container, renderer, reduced } = env;
  assert.deepEqual(Object.keys(controller).sort(), ['dispose', 'setBodyMetrics', 'setMotion']);
  assert.equal(env.request.url, '/models/body.d736859b.glb');
  assert.equal(renderer.setPixelRatio.mock.calls[0].arguments[0], 1.75);
  assert.deepEqual([...env.textures.keys()], [
    '/assets/3d/pedestal-glow.webp', '/assets/skin/skin-detail-normal.webp', '/assets/skin/skin-detail-roughness.webp',
  ]);
  controller.setBodyMetrics({ bodyFatPct: 27.5, musclePct: 44, heightCm: 210 });
  env.request.complete({ scene: asset.scene });
  assert.equal(container.dataset.modelLoaded, 'true');
  assert.equal(container.dataset.morphTargets, 'fat_high,fat_low,muscle_high,muscle_low');
  assert.deepEqual(asset.mesh.morphTargetInfluences, [.5, 0, .5, 0]);
  assert.equal(container.dataset.heightScale, '1.200');
  assert.equal(container.dataset.normalScale, '0.250');
  assert.deepEqual(asset.mesh.material.normalScale.toArray(), [.2, -.15]);
  assert.equal(asset.mesh.material.color.getHex(), 0xc49175);
  assert.equal(asset.sourceDispose.mock.calls.length, 1);
  const detail = env.textures.get('/assets/skin/skin-detail-normal.webp');
  detail.complete();
  assert.equal(asset.mesh.material.defines.SKIN_DETAIL_NORMAL, '');
  assert.equal(detail.texture.wrapS, THREE.RepeatWrapping);
  assert.equal(detail.texture.colorSpace, THREE.NoColorSpace);
  assert.equal(detail.texture.anisotropy, 8);
  env.resize();
  assert.deepEqual(renderer.setSize.mock.calls.at(-1).arguments, [300, 500, false]);
  for (const timestamp of [100, 200, 300, 400, 500, 600]) renderer.frame(timestamp);
  const [, camera] = renderer.render.mock.calls.at(-1).arguments;
  assert.equal(camera.aspect, .6);
  assert.ok(Number.isFinite(camera.position.z) && camera.position.z > 0);
  assert.notEqual(container.dataset.rotationDegrees, '0.0');
  controller.setMotion(false);
  const rotation = container.dataset.rotationDegrees;
  controller.setBodyMetrics({ bodyFatPct: 20, musclePct: 40 });
  renderer.frame(1000); renderer.frame(1300);
  assert.equal(container.dataset.morphFat, '0.250');
  assert.equal(container.dataset.rotationDegrees, rotation);
  renderer.frame(1600);
  assert.equal(container.dataset.morphFat, '0.000');
  reduced.matches = true; reduced.emit('change');
  controller.setMotion(true);
  controller.setBodyMetrics({ bodyFatPct: 8, musclePct: 32 });
  assert.deepEqual(asset.mesh.morphTargetInfluences, [0, 1, 0, 1]);
  renderer.frame(1700);
  assert.equal(container.dataset.rotationDegrees, '0.0');
  controller.dispose(); controller.dispose();
  assert.equal(renderer.frame, null);
  for (const fn of [renderer.dispose, renderer.domElement.remove, env.observer.disconnect, env.envDispose,
    asset.normalDispose, asset.geometryDispose, detail.dispose]) assert.equal(fn.mock.calls.length, 1);
  assert.equal(env.win.listeners.size + reduced.listeners.size, 0);
  const late = env.textures.get('/assets/skin/skin-detail-roughness.webp');
  late.complete();
  assert.equal(late.dispose.mock.calls.length, 1);
  assert.equal(asset.mesh.material.defines.SKIN_DETAIL_ROUGHNESS, undefined);
});

test('pagehide disposes a pending viewer and releases a model arriving after disposal', t => {
  const env = environment(t), asset = model(t);
  env.win.emit('pagehide');
  env.request.complete({ scene: asset.scene });
  assert.equal(env.renderer.frame, null);
  assert.equal(env.renderer.dispose.mock.calls.length, 1);
  assert.equal(asset.geometryDispose.mock.calls.length, 1);
  assert.equal(asset.normalDispose.mock.calls.length, 1);
  assert.equal(asset.sourceDispose.mock.calls.length, 1);
  assert.equal(env.container.dataset.modelLoaded, undefined);
});

test('model load failure cleans up and shows the existing static silhouette', t => {
  const env = environment(t);
  env.request.fail();
  assert.equal(env.renderer.dispose.mock.calls.length, 1);
  assert.equal(env.observer.disconnect.mock.calls.length, 1);
  assert.equal(env.renderer.frame, null);
  assert.match(env.container.innerHTML, /class="model-fallback".*<svg.*fill="#8d8b85"/);
});

test('unavailable WebGL keeps all controller methods callable on the static silhouette', t => {
  const env = environment(t, { unavailable: true });
  assert.match(env.container.innerHTML, /class="model-fallback"/);
  env.controller.setBodyMetrics({ weightKg: 90 });
  env.controller.setMotion(false);
  env.controller.dispose(); env.controller.dispose();
  assert.equal(env.container.append.mock.calls.length, 0);
});
