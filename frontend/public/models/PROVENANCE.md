# Anatomical reference model

`athletic-male.glb` is a locally bundled derivative of the official MakeHuman data assets, licensed **CC0 1.0 Universal**. No remote runtime request is made.

Source project: https://github.com/makehumancommunity/makehuman
Immutable source revision: `a8bc2d54ff0ac92e78ff71431b1023eda42bf482`

Source asset paths at that revision:
- `makehuman/data/3dobjs/base.obj`
- `makehuman/data/targets/macrodetails/caucasian-male-young.target` (weight 1)
- `makehuman/data/targets/macrodetails/universal-male-young-maxmuscle-averageweight.target` (weight 1)
- `makehuman/data/targets/macrodetails/proportions/male-young-maxmuscle-averageweight-idealproportions.target` (weight 0.6)

Official license: https://github.com/makehumancommunity/makehuman/blob/a8bc2d54ff0ac92e78ff71431b1023eda42bf482/LICENSE.md
The project's asset license explicitly covers base meshes and morph targets under CC0. Full legal text is bundled as `LICENSE-CC0.txt`.

Changes: applied male, athletic and ideal-proportion targets; removed helper meshes; lowered arms from the A pose with a smooth shoulder blend; performed one Catmull-Clark subdivision; computed smooth vertex normals; normalized height; exported a static untextured GLB. 53,514 vertices / 107,024 triangles, approximately 2.5 MB.

This is a generic anatomical reference, not a scan or reconstruction of the account owner. Its face and muscle definition are artist-authored by MakeHuman and do not reproduce the generated design reference exactly. Source assets credited to the MakeHuman team / Data Collection AB / Joel Palmius / Jonas Hauquier; attribution is voluntarily preserved despite CC0 not requiring it.

Presentation refinement: `src/body.js` sculpts the continuous source geometry with pectoral borders, paired abdominal volumes, serratus and oblique relief, deltoids, upper arms, quadriceps, calves, scapular volumes, lats and spinal erectors. The waist tapers, the feet move closer under the hips, the forearms relax from their forward sweep, and the slightly enlarged head turns toward screen-right. Normals are recomputed after deformation. A warm silver material, directional studio lighting, self-shadowing and a soft ground shadow emphasize the sculpture. The model starts in a front three-quarter view and automatically oscillates over exactly 180 degrees, taking 30 seconds in each direction; the System motion control pauses it. These are original procedural presentation changes; the source GLB remains unchanged.
