import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import * as THREE from '../../frontend/node_modules/three/build/three.module.js';
import { SKIN_DETAIL, createSkinUniforms, makeSkinMaterial } from '../../frontend/src/body-skin.js';

test('each viewer gets independent uniforms with the original skin parameters and asset paths', () => {
  const uniforms = createSkinUniforms(), other = createSkinUniforms();
  assert.deepEqual(SKIN_DETAIL, {
    normalUrl: '/assets/skin/skin-detail-normal.webp', roughnessUrl: '/assets/skin/skin-detail-roughness.webp',
    repeat: 28, normalStrength: .35, roughnessStrength: 1,
  });
  assert.equal(uniforms.rimColor.value.getHex(), 0xf0c478);
  assert.deepEqual(uniforms.skinWrap.value.toArray(), [.2, .09, .05]);
  assert.deepEqual(uniforms.skinScatterColor.value.toArray(), [1, .38, .24]);
  for (const [key, expected] of Object.entries({
    rimStrength: .1, skinScatter: .2, skinDetailNormal: null, skinDetailRoughness: null,
    skinDetailRepeat: 28, skinDetailNormalStrength: .35, skinDetailRoughnessStrength: 1,
  })) assert.equal(uniforms[key].value, expected);
  for (const key of Object.keys(uniforms)) assert.notEqual(uniforms[key], other[key]);
  uniforms.skinWrap.value.x = 9;
  uniforms.rimColor.value.set(0xffffff);
  assert.equal(other.skinWrap.value.x, .2);
  assert.equal(other.rimColor.value.getHex(), 0xf0c478);
});

test('physical material preserves maps and normal scale without disposing source resources', t => {
  const map = new THREE.Texture(), normalMap = new THREE.Texture(), roughnessMap = new THREE.Texture();
  const source = new THREE.MeshStandardMaterial({ color: 0xc49175, map, normalMap, roughnessMap, roughness: .3 });
  source.normalScale.set(.8, -.6);
  const disposed = t.mock.method(source, 'dispose');
  const material = makeSkinMaterial(source, createSkinUniforms());
  assert.ok(material.isMeshPhysicalMaterial);
  assert.notEqual(material, source);
  for (const key of ['map', 'normalMap', 'roughnessMap']) assert.equal(material[key], source[key]);
  assert.deepEqual(material.normalScale.toArray(), [.8, -.6]);
  assert.notEqual(material.normalScale, source.normalScale);
  assert.ok(material.color.equals(source.color));
  assert.equal(material.roughness, .3);
  assert.equal(material.specularIntensity, .7);
  assert.equal(material.sheen, .35);
  assert.equal(material.sheenRoughness, .55);
  assert.equal(material.sheenColor.getHex(), 0x5a463c);
  assert.deepEqual(material.defines, { STANDARD: '', PHYSICAL: '' });
  assert.equal(material.customProgramCacheKey(), 'healthos-skin-v2');
  assert.equal(disposed.mock.calls.length, 0);
  assert.equal(makeSkinMaterial(new THREE.MeshStandardMaterial({ roughness: .2 }), {}).roughness, .6);
  assert.equal(makeSkinMaterial(new THREE.MeshStandardMaterial({ roughness: .8 }), {}).roughness, .8);
});

test('shader output matches the pre-extraction source and shares live viewer uniforms', () => {
  const uniforms = createSkinUniforms();
  const material = makeSkinMaterial(new THREE.MeshStandardMaterial(), uniforms);
  const existing = { value: 42 };
  const shader = { uniforms: { existing }, fragmentShader: [
    'common', 'lights_physical_pars_fragment', 'roughnessmap_fragment', 'normal_fragment_maps', 'emissivemap_fragment',
  ].map(name => `#include <${name}>`).join('\n') };
  material.onBeforeCompile(shader);
  // Captured from the original body.js with the pinned three.js version. This checks all GLSL
  // calculations (wrap, scatter, Fresnel, whiteout and grazing-detail fade), without a GPU.
  assert.equal(createHash('sha256').update(shader.fragmentShader).digest('hex'),
    '080238e311fcb329ecee769a9b3cb662e36696061004ebecf85887c49b73ee20');
  assert.equal(shader.uniforms.existing, existing);
  for (const key of Object.keys(uniforms)) assert.equal(shader.uniforms[key], uniforms[key]);
  const texture = new THREE.Texture();
  uniforms.skinDetailNormal.value = texture;
  assert.equal(shader.uniforms.skinDetailNormal.value, texture);
});
