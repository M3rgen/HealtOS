import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

// Sculpt the continuous source mesh; all landmarks are in its normalized space.
// Broad volumes establish anatomy; narrow valleys separate adjoining muscle groups.
function sculptAthleticBody(geometry) {
  const positions = geometry.attributes.position;
  const bell = (v, center, width) => Math.exp(-(((v - center) / width) ** 2));
  for (let i = 0; i < positions.count; i++) {
    let x = positions.getX(i), y = positions.getY(i), z = positions.getZ(i);
    const ax = Math.abs(x);
    const front = THREE.MathUtils.smoothstep(z, .065, .145);
    const back = 1 - THREE.MathUtils.smoothstep(z, -.065, .015);
    const torso = 1 - THREE.MathUtils.smoothstep(ax, .22, .29);
    const side = Math.sign(x);
    const chest = bell(ax, .12, .082) * bell(y, .705, .095);
    let frontRelief = .082 * chest;
    // Sternum and the gently sloping lower pectoral border.
    frontRelief -= .013 * bell(x, 0, .021) * bell(y, .69, .135);
    frontRelief -= .016 * bell(y, .598 - .1 * ax, .019) * bell(ax, .115, .095);
    for (const [row, amount] of [[.515, .03], [.407, .029], [.299, .026], [.2, .012]]) {
      frontRelief += amount * bell(ax, .061, .051) * bell(y, row, .047);
    }
    frontRelief -= .012 * bell(x, 0, .014) * bell(y, .37, .2);
    frontRelief -= .009 * bell(x, 0, .014) * bell(y, .225, .014);
    // Obliques and three serratus slips merge into the rib cage.
    frontRelief += .021 * bell(ax, .165, .045) * bell(y, .335, .16);
    for (const row of [.55, .485, .42]) {
      frontRelief += .014 * bell(ax, .185, .046) * bell(y, row - .24 * (ax - .15), .021);
    }
    frontRelief -= .012 * bell(y, .08 + .62 * ax, .025) * bell(ax, .135, .085);
    z += front * torso * frontRelief;
    x *= 1 - .095 * bell(y, .37, .19) * torso;
    // Trapezius, scapular masses, lats and paired spinal erectors.
    const backRelief = .038 * bell(ax, .12, .075) * bell(y, .7, .14)
      + .032 * bell(ax, .18, .065) * bell(y, .53, .16)
      + .016 * bell(ax, .052, .028) * bell(y, .38, .22)
      - .009 * bell(x, 0, .022) * bell(y, .57, .26);
    z -= back * torso * backRelief;
    // Deltoid cap, biceps and triceps wrap around the actual arm centers.
    const shoulder = bell(ax, .342, .073) * bell(y, .737, .107);
    const upperArm = bell(ax, .364, .06) * bell(y, .54, .115);
    x += side * (.023 * shoulder + .014 * upperArm);
    z += .027 * shoulder * Math.tanh((z - .02) / .065);
    z += .026 * upperArm * Math.tanh((z - .028) / .052);
    const arm = THREE.MathUtils.smoothstep(ax, .28, .32)
      * THREE.MathUtils.smoothstep(y, -.22, -.12);
    const lowerArm = bell(y, .31, .12) * arm;
    z += .015 * lowerArm * Math.tanh((z - .12) / .055);
    // Relaxed hanging hands: remove the source pose's forward forearm sweep.
    z -= .35 * arm * (1 - THREE.MathUtils.smoothstep(y, .015, .48));
    x -= side * .035 * arm * (1 - THREE.MathUtils.smoothstep(y, .06, .68));
    if (y < .09) {
      const leg = 1 - THREE.MathUtils.smoothstep(y, -.02, .09);
      const thigh = bell(ax, .168, .08) * bell(y, -.235, .235);
      const legFront = THREE.MathUtils.smoothstep(z, .025, .095);
      const legBack = 1 - THREE.MathUtils.smoothstep(z, -.005, .055);
      z += .051 * thigh * legFront * leg;
      z -= .025 * thigh * legBack * leg;
      // Long rectus ridge, lateral quad and teardrop above each kneecap.
      z += legFront * leg * (.024 * bell(ax, .155, .042) * bell(y, -.22, .19)
        + .025 * bell(ax, .222, .052) * bell(y, -.3, .16)
        + .023 * bell(ax, .152, .042) * bell(y, -.47, .065));
      z -= legFront * leg * .009 * bell(ax, .192, .014) * bell(y, -.275, .2);
      const calf = bell(y, -.795, .145);
      const calfCenter = .264;
      x += .2 * (x - side * calfCenter) * calf * leg;
      z += .018 * calf * Math.tanh((z - .005) / .045) * leg;
      z -= .02 * calf * legBack * leg;
      // Bring the feet under the hips without closing the inner thigh gap.
      x -= side * .038 * (1 - THREE.MathUtils.smoothstep(y, -.65, .025));
    }
    // Slightly larger head and subtle head turn match the statue's proportions.
    const head = THREE.MathUtils.smoothstep(y, .955, 1.05);
    x *= 1 + .075 * head;
    y += .055 * (y - .99) * head;
    z = .09 + (z - .09) * (1 + .065 * head);
    const angle = .12 * head;
    const headX = x * Math.cos(angle) + (z - .09) * Math.sin(angle);
    z = .09 - x * Math.sin(angle) + (z - .09) * Math.cos(angle);
    x = headX;
    positions.setXYZ(i, x, y, z);
  }
  positions.needsUpdate = true;
  geometry.computeVertexNormals();
  geometry.computeBoundingSphere();
}

// A locally bundled continuous anatomical mesh. See public/models/PROVENANCE.md.
export function createBody(container, motionEnabled) {
  let renderer;
  const fallback = () => {
    container.innerHTML = '<div class="model-fallback"><svg viewBox="0 0 120 280" aria-hidden="true"><path fill="#8d8b85" d="M60 6c-10 0-17 8-17 20 0 10 4 18 8 22l-2 10c-14 3-23 11-26 26l-9 58-1 29c0 7 5 9 7 3l4-26 10-40 5 43-6 76-1 34-7 8c-3 4 0 7 7 7h10l8-9 3-38 7-45 7 45 3 38 8 9h10c7 0 10-3 7-7l-7-8-1-34-6-76 5-43 10 40 4 26c2 6 7 4 7-3l-1-29-9-58c-3-15-12-23-26-26l-2-10c4-4 8-12 8-22 0-12-7-20-17-20Z"/></svg></div>';
  };
  try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true }); }
  catch { fallback(); return { setMotion() {} }; }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 0);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.08;
  container.append(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-1, 1, 1.48, -1.48, .1, 30);
  camera.position.set(0, .13, 6); camera.lookAt(0, -.01, 0);
  scene.add(new THREE.HemisphereLight(0xe8e5df, 0x292723, .45));
  const key = new THREE.DirectionalLight(0xfff5e7, 3.1);
  key.position.set(-3.3, 3.8, 3.5); key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048); key.shadow.camera.left = -2; key.shadow.camera.right = 2;
  key.shadow.camera.top = 2; key.shadow.camera.bottom = -2; key.shadow.normalBias = .008;
  key.shadow.bias = -.0001;
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xe1e4eb, .62); fill.position.set(2, 1, 3); scene.add(fill);
  const rim = new THREE.DirectionalLight(0xf0e9db, 1.8); rim.position.set(2, 2, -3); scene.add(rim);
  const body = new THREE.Group(); scene.add(body);
  const material = new THREE.MeshStandardMaterial({ color: 0xa5a19a, roughness: .52, metalness: .12 });
  let disposed = false, loaded = false;
  new GLTFLoader().load('/models/athletic-male.glb', ({ scene: model }) => {
    if (disposed) return;
    model.traverse(node => {
      if (node.isMesh) {
        sculptAthleticBody(node.geometry);
        node.material = material; node.castShadow = true; node.receiveShadow = true;
      }
    });
    body.add(model);
    loaded = true;
    container.dataset.modelLoaded = 'true';
  }, undefined, () => {
    if (!disposed) { renderer.setAnimationLoop(null); resize.disconnect(); renderer.dispose(); fallback(); }
  });
  const floor = new THREE.Mesh(new THREE.CircleGeometry(.8, 64), new THREE.ShadowMaterial({ opacity: .28 }));
  floor.rotation.x = -Math.PI / 2; floor.position.y = -1.305; floor.receiveShadow = true; scene.add(floor);
  const resize = new ResizeObserver(() => {
    const w = container.clientWidth, h = container.clientHeight;
    if (w && h) {
      const halfHeight = Math.max(1.48, .76 / (w / h));
      camera.left = -halfHeight * w / h; camera.right = halfHeight * w / h;
      camera.top = halfHeight; camera.bottom = -halfHeight;
      camera.updateProjectionMatrix(); renderer.setSize(w, h, false);
    }
  });
  resize.observe(container);
  let enabled = motionEnabled, elapsed = 0, last = 0;
  renderer.setAnimationLoop(timestamp => {
    const delta = last ? Math.min(timestamp - last, 100) : 0; last = timestamp;
    if (!document.hidden && container.getClientRects().length) {
      if (loaded && enabled) elapsed += delta;
      // Thirty seconds per half turn, with gentle easing before returning.
      body.rotation.y = .35 + Math.PI * (1 - Math.cos(elapsed / 60000 * Math.PI * 2)) / 2;
      container.dataset.rotationDegrees = ((body.rotation.y - .35) * 180 / Math.PI).toFixed(1);
      renderer.render(scene, camera);
    }
  });
  window.addEventListener('pagehide', () => {
    disposed = true; renderer.setAnimationLoop(null); resize.disconnect();
    body.traverse(node => { if (node.isMesh) node.geometry.dispose(); }); material.dispose();
    floor.geometry.dispose(); floor.material.dispose(); renderer.dispose();
  }, { once: true });
  return { setMotion(value) { enabled = value; } };
}
