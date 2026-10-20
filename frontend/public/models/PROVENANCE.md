# Anatomical figure model (`body.<hash>.glb`)

`body.d736859b.glb` (called `body.glb` below) is a morphable male figure with realistic but non-photographic skin, bundled locally. No remote runtime request is made. It derives from the official MakeHuman data assets, licensed **CC0 1.0 Universal**, plus original procedural work made for HealthOS. Nothing comes from a scan, an AI generator, a photo texture or any non-CC0 source. It is a generic figure, not a reconstruction of any person.

## Sources (all CC0 1.0)

- Project: https://github.com/makehumancommunity/makehuman
- Immutable revision: `a8bc2d54ff0ac92e78ff71431b1023eda42bf482`
- Licence: https://github.com/makehumancommunity/makehuman/blob/a8bc2d54ff0ac92e78ff71431b1023eda42bf482/LICENSE.md. The asset licence explicitly covers base meshes, targets and rigs under CC0; the full legal text is bundled as `LICENSE-CC0.txt`.
- Credit (voluntary under CC0): MakeHuman team / Data Collection AB / Joel Palmius / Jonas Hauquier.

Files used from `makehuman/data/` at that revision:

- `3dobjs/base.obj`: groups `body`, `helper-l-eye`, `helper-r-eye` (UVs as shipped; the eye islands are enlarged into free atlas space).
- `targets/macrodetails/universal-male-young-{min,average,max}muscle-{min,average,max}weight.target`, `{african,asian,caucasian}-male-young.target` (1/3 each), `proportions/male-young-*-idealproportions.target`.
- Head (every shape): `head/head-age-decr`, `head/head-fat-decr`, `head/head-square`, `chin/chin-prominent-incr`, `eyes/{l,r}-eye-bag-decr`, `mouth/mouth-angles-up`.
- Morph extremes only: `stomach/stomach-pregnant-incr`, `neck/neck-double-incr`, `buttocks/buttocks-volume-incr`, `torso/torso-muscle-{pectoral,dorsi}-incr`, `torso/torso-vshape-incr`, `armslegs/{l,r}-{upperarm,lowerarm,upperleg,lowerleg}-{fat,muscle}-{incr,decr}`, `armslegs/{l,r}-upperarm-shoulder-muscle-incr`.
- Build-time posing only (no skeleton is shipped): `rigs/default.mhskel`, `rigs/default_weights.mhw`.

## Contents of `body.glb`

- **Mesh:** one mesh `Body` with a single material. glTF Y-up, feet at y = 0, basis exactly 1.75 m tall, facing +Z, horizontal centre of mass over the origin. 23,271 vertices and 43,970 triangles, with POSITION, NORMAL, TANGENT (MikkTSpace, matching the bake) and TEXCOORD_0. Meshopt-compressed, 1.43 MB (see *Compression* below).
- **Pose:** natural contrapposto, readable from the front, made with linear-blend skinning on the MakeHuman weights:
  - weight on the right leg, which stands straight under the body; the ribcage is shifted over it;
  - left knee clearly bent (about 36°) and turned slightly out, heel lifted;
  - pelvis dropped about 9° toward the relaxed side, shoulders counter-tilted about 5°;
  - head turned and tilted a few degrees toward the standing side, mouth closed with relaxed lips;
  - arms hanging naturally about 9.5° from vertical, soft elbows, hands beside the outer thigh.

  The relaxed foot's toes touch the floor; the standing foot is at y = 0.
- **Basis:** young adult male of a ~70 kg class build at ~20 % body fat (MakeHuman muscle 0.65, weight 0.5, ideal proportions 0.7).
- **Geometry:** large torso, limb and cranium faces are subdivided once (cranium twice). Every vertex lies on the Catmull-Clark limit surface of the cage, as a fixed linear function of it, so the morphs stay fold-free. Muscle masses are sculpted into the geometry so that they shape the silhouette: deltoid cap, biceps, triceps, forearm masses, lats and V-taper, serratus, obliques over the iliac crest, pectorals, trapezius, erectors, glutes, quadriceps with the vastus medialis teardrop, the sartorius line and the gastrocnemius heads.
- **Morph targets:** `fat_high`, `fat_low`, `muscle_high`, `muscle_low`, with POSITION and NORMAL deltas as sparse accessors.
  - They are the same pipeline at other MakeHuman macro values: about 103 kg, 56 kg, 76 kg and 60 kg at the same height.
  - Each target also scales the sculpted muscle masses: `fat_high` keeps 25 % of them, `fat_low` 135 %, `muscle_high` 145 %, `muscle_low` 60 %.
  - `fat_high` and `muscle_high` open the arms (and stance) only as far as their volume needs.
  - Every single target and every pair at weight 1 was checked: there is no interpenetration. Upper arm to torso is at least 8.2 mm, forearms and hands to the body at least 45 mm, thigh to thigh at least 6.5 mm.
- **Normal map (2048², lossless WebP since 2026-10-20, 7 bits per channel):** procedural relief at muscle, vein and wrinkle scale. Rounded muscle bellies with soft, wide transitions: shallow wide abdominal intersections, a soft lower-pectoral fold, serratus bellies. Also superficial veins on the forearms, biceps and lower abdomen, hand and foot tendons and knuckles, fabric thickness at the hem, the hem stitch and waistband ribbing. There is no pore detail; a tiling micro-detail layer is applied separately.
- **Base colour (sRGB JPEG 2048), procedurally painted:**
  - skin on a single tunable base tone, `skinToneHex` **#ad8f7e** (medium, warm, mildly desaturated), with subtle low-frequency mottling;
  - cooler tones under the jaw and on the inner arms; warmer knees, elbows, knuckles, cheeks, ears and nose tip; lighter palms and soles; nails;
  - very short buzz-cut hair, light stubble with soft edges, and soft hair-like eyebrows, all as shading only;
  - lips with a hint of colour;
  - non-photoreal eyes: slightly off-white sclera, soft hazel-brown iris and pupil, with an upper-lid shadow and a lash line;
  - dark fitted training shorts (#2a2824) with a waistband, a hem angled along the thigh and a dashed hem stitch, as a texture region of the same mesh.
- **ORM (JPEG 1024):**
  - R: body-scale ambient occlusion baked from the smooth surface without the fine relief, so fat still softens definition;
  - G: roughness, oilier on the T-zone and lips, rougher on the scalp, soles and fabric;
  - B: metallic 0.
- **Material:** single-sided PBR (`baseColorTexture`, `metallicRoughnessTexture` + `occlusionTexture` sharing the ORM image, `normalTexture`).
- **Extras:** `extras.healthos` carries `skinToneHex` and `suggestedNormalScale`, the formula `normalScale = min(1.25, max(0.05, pow(1 - fat_high, 1.8)) * (1 + 0.25 * fat_low))` with the numeric fields `max` 1.25, `min` 0.05, `exponent` 1.8 and `fatLowGain` 0.25.

The build scripts and editable `.blend` are kept outside the repository; they are not needed at runtime.

## Compression (2026-10-20, plan item 4.3)

The Blender export (5.07 MB, all float attributes, PNG normal map) was compressed to 1.43 MB. The file name carries the first 8 hex digits of the file's SHA-256, so `/models` can be served with an immutable cache header; a changed model needs a new name and an update of `MODEL_URL` in `src/body.js`.

1. **Normal map:** the 2048² PNG (1.23 MB) was rounded to 7 bits per channel and stored as lossless WebP (625 KB, `EXT_texture_webp`) by a small Node script on `@gltf-transform/core` 4.5.1 and sharp 0.35.5 (`webp({ lossless: true, effort: 6 })`). Angular error is at most 0.9° (mean 0.27°). Lossy WebP was rejected: its 4:2:0 chroma subsampling bends normals by up to 60° at block edges. The base colour and ORM JPEGs are kept byte for byte.
2. **Geometry:** `npx @gltf-transform/cli@4.5.1 meshopt in.glb out.glb --quantize-texcoord 14` (meshoptimizer 1.2.0; level `high`: vertex reorder, `KHR_mesh_quantization` with 14-bit positions and morph deltas, 14-bit UVs, 8-bit octahedral normals and tangents, 8-bit morph normal deltas, `EXT_meshopt_compression` with filters). The quantisation scale/offset is a node transform on `Body`; `src/body.js` measures in world space, so it is unaffected. The browser decodes with three's bundled `three/addons/libs/meshopt_decoder.module.js` (WebAssembly, needs `'wasm-unsafe-eval'` in the CSP).

Checked against the export with a vertex-matching diff: names, `extras.healthos` (byte-identical string), `targetNames`, material and sampler settings unchanged; same 23,271 vertices and 43,970 triangles (identical topology after the reorder remap). Maximum errors: position 0.11 mm, morph position delta 0.10 mm, UV 0.11 texel at 2048², normal 1.1°, tangent 1.1° with no handedness flips. Morph normal deltas have a mean error of 0.002; seven deltas on four crease vertices (armpits and crotch) exceed the ±1 range that quantised morph normals can store and are clamped (six by more than 0.05). The high-fat and lean renders show no visible difference there; keeping morph normals as floats would have cost 0.78 MB. Before/after dashboard renders (basis, high fat, lean/muscular) differ by a mean of 0.11–0.12 of 255 per pixel. The `extras.healthos.textures` text still says "PNG 2048" for the normal map; it is descriptive only and was left unchanged on purpose so that the extras stay byte-identical.

## Historical note

The earlier `athletic-male.glb` (same MakeHuman CC0 sources, static and sculpted at runtime by `src/body.js`) was superseded by `body.glb` and has been removed from the repository.
