import * as THREE from 'three';

// Tileable skin micro-detail (lane C, public/assets/skin/, see ASSETS.md): OpenGL +Y normal,
// roughness 0.5 = neutral. One tile ≈ 6–7 cm on the body at 28 repeats per UV unit.
// Missing files simply leave the detail off.
export const SKIN_DETAIL = {
  normalUrl: '/assets/skin/skin-detail-normal.webp',
  roughnessUrl: '/assets/skin/skin-detail-roughness.webp',
  repeat: 28, // tiles across the 0–1 body UV atlas
  normalStrength: .35,
  roughnessStrength: 1, // base roughness × (2 × detail): 0.5 is neutral, 0.43–0.57 → ×0.86–1.14
};

export function createSkinUniforms() {
  return {
    rimColor: { value: new THREE.Color(0xf0c478) }, rimStrength: { value: .1 },
    skinWrap: { value: new THREE.Vector3(.2, .09, .05) },
    skinScatterColor: { value: new THREE.Color(1, .38, .24) }, skinScatter: { value: .2 },
    skinDetailNormal: { value: null }, skinDetailRoughness: { value: null },
    skinDetailRepeat: { value: SKIN_DETAIL.repeat },
    skinDetailNormalStrength: { value: SKIN_DETAIL.normalStrength },
    skinDetailRoughnessStrength: { value: SKIN_DETAIL.roughnessStrength },
  };
}

// Cheap single-pass skin approximation layered onto three's physical BRDF:
// - per-channel wrapped diffuse: red light bleeds past the terminator (subsurface softness);
// - back-scatter: light from behind leaks through thin silhouette areas (ears, fingers) red-shifted;
// - built-in sheen for peach fuzz, skin-like F0 (~0.028);
// - tileable micro-detail normal blended over the base map (whiteout); the fat-driven
//   normalScale only scales the base (muscle) map, never the pores;
// - a thin amber Fresnel accent so the UI colour reads on the silhouette without tinting the skin.
export function makeSkinMaterial(source, skinUniforms) {
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
