// 3D warehouse diorama for the landing page. Pure presentation: no data is read or written here.
// `render(p, t)` is a pure function of scroll progress p (0..1) and time t, so scrubbing works both ways.
import * as THREE from 'three';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { LOAD_SPAN, LOAD_START, LOAD_STEP as STEP, TOTAL_LOAD } from './timeline';

export type HoverInfo = { kind: 'risk' | 'fixed' | 'ok'; x: number; y: number } | null;
export type AnchorId = 'drone' | 'forklift' | 'risk' | 'scan' | 'truck';
export type AnchorPoint = { x: number; y: number; visible: boolean };

export type SceneOptions = {
  host: HTMLElement;
  atRisk: number;
  reduceMotion: boolean;
  onHover: (info: HoverInfo) => void;
};

export type WarehouseScene = {
  render: (p: number, t: number) => void;
  project: (id: AnchorId) => AnchorPoint;
  arrive: () => void;
  dispose: () => void;
};

const WALL_AT = [0.81, 0.85] as const;
const ROOF_AT = [0.83, 0.87] as const;
const DEPART_AT = 0.87;
const FIX_AT = 0.54;

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const sstep = (a: number, b: number, v: number) => {
  const t = clamp01((v - a) / (b - a));
  return t * t * (3 - 2 * t);
};
const easeOut = (t: number) => 1 - Math.pow(1 - t, 3);

function rngFrom(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function canvasTex(w: number, h: number, draw: (g: CanvasRenderingContext2D) => void, repeat = false) {
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  draw(c.getContext('2d')!);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 8;
  if (repeat) tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  return tex;
}

const std = (color: number, extra: THREE.MeshStandardMaterialParameters = {}) =>
  new THREE.MeshStandardMaterial({ color, roughness: 0.78, metalness: 0.02, ...extra });

// Camera path. Cubic Hermite through these keys keeps the motion continuous (no stop-and-go).
type Cam = { p: number; pos: [number, number, number]; tgt: [number, number, number] };
const CAMS: Cam[] = [
  { p: 0.0, pos: [26.5, 16.5, 35], tgt: [6, 2.5, 0.5] },
  { p: 0.12, pos: [25.5, 15.8, 33.5], tgt: [6, 2.5, 0.5] },
  { p: 0.3, pos: [-3, 8.5, 17], tgt: [-4, 3.2, -3] },
  { p: 0.37, pos: [-13.5, 6.4, 7.5], tgt: [-3, 3.4, -4] },
  { p: 0.5, pos: [-2, 19, 13], tgt: [-2, 3, -3.5] },
  { p: 0.58, pos: [16, 12, 26], tgt: [7, 2.6, 3.5] },
  { p: 0.78, pos: [19, 10.5, 27], tgt: [10, 2.8, 3.5] },
  { p: 0.87, pos: [34, 9, 28], tgt: [18.5, 2.6, 3.5] },
  { p: 1.0, pos: [52, 8, 30], tgt: [40, 2.8, 3.5] },
];

function camAt(p: number, pos: THREE.Vector3, tgt: THREE.Vector3) {
  let i = 0;
  while (i < CAMS.length - 2 && p > CAMS[i + 1].p) i++;
  const k0 = CAMS[Math.max(0, i - 1)], k1 = CAMS[i], k2 = CAMS[i + 1], k3 = CAMS[Math.min(CAMS.length - 1, i + 2)];
  const dt = k2.p - k1.p;
  const t = clamp01((p - k1.p) / dt);
  const t2 = t * t, t3 = t2 * t;
  const h00 = 2 * t3 - 3 * t2 + 1, h10 = t3 - 2 * t2 + t, h01 = -2 * t3 + 3 * t2, h11 = t3 - t2;
  const TEN = 0.9;
  const one = (a0: number, a1: number, a2: number, a3: number) => {
    const m1 = ((a2 - a0) / (k2.p - k0.p)) * TEN;
    const m2 = ((a3 - a1) / (k3.p - k1.p)) * TEN;
    return h00 * a1 + h10 * dt * m1 + h01 * a2 + h11 * dt * m2;
  };
  for (let c = 0; c < 3; c++) {
    pos.setComponent(c, one(k0.pos[c], k1.pos[c], k2.pos[c], k3.pos[c]));
    tgt.setComponent(c, one(k0.tgt[c], k1.tgt[c], k2.tgt[c], k3.tgt[c]));
  }
}

// Light only: soft morning -> bright midday -> warm golden hour. It never gets dark.
type Look = { top: THREE.Color; hor: THREE.Color; hSky: THREE.Color; hGnd: THREE.Color; sun: THREE.Color; hInt: number; sInt: number; env: number };
const C = (h: number) => new THREE.Color(h);
const LOOKS: { p: number; look: Look }[] = [
  { p: 0.0, look: { top: C(0x3f7fd6), hor: C(0xbcd4f0), hSky: C(0xdde9fb), hGnd: C(0x8497b5), sun: C(0xfff0d6), hInt: 0.7, sInt: 2.3, env: 0.4 } },
  { p: 0.5, look: { top: C(0x3573d0), hor: C(0xb3cdee), hSky: C(0xdde9fb), hGnd: C(0x8093b2), sun: C(0xfff7e8), hInt: 0.75, sInt: 2.3, env: 0.42 } },
  { p: 1.0, look: { top: C(0x4f78cc), hor: C(0xe8cfb8), hSky: C(0xfae6d3), hGnd: C(0x9a96ad), sun: C(0xffd6a6), hInt: 0.7, sInt: 2.4, env: 0.38 } },
];

function mixLook(out: Look, p: number) {
  let i = 0;
  while (i < LOOKS.length - 2 && p > LOOKS[i + 1].p) i++;
  const a = LOOKS[i], b = LOOKS[i + 1];
  const t = sstep(0, 1, (p - a.p) / (b.p - a.p));
  out.top.lerpColors(a.look.top, b.look.top, t);
  out.hor.lerpColors(a.look.hor, b.look.hor, t);
  out.hSky.lerpColors(a.look.hSky, b.look.hSky, t);
  out.hGnd.lerpColors(a.look.hGnd, b.look.hGnd, t);
  out.sun.lerpColors(a.look.sun, b.look.sun, t);
  out.hInt = lerp(a.look.hInt, b.look.hInt, t);
  out.sInt = lerp(a.look.sInt, b.look.sInt, t);
  out.env = lerp(a.look.env, b.look.env, t);
}

export function createWarehouse(opts: SceneOptions): WarehouseScene {
  const { host, onHover } = opts;
  const reduce = opts.reduceMotion;

  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 0.95;
  renderer.domElement.style.cssText = 'display:block;width:100%;height:100%';
  host.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.fog = new THREE.Fog(0xfdf1e6, 150, 700);
  const camera = new THREE.PerspectiveCamera(36, 1, 0.5, 2000);

  const pmrem = new THREE.PMREMGenerator(renderer);
  const envTex = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environment = envTex;

  const hemi = new THREE.HemisphereLight(0xf4f8ff, 0xe9e5de, 1.4);
  scene.add(hemi);
  const sun = new THREE.DirectionalLight(0xfff1de, 2.2);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, { left: -34, right: 34, top: 30, bottom: -30, near: 1, far: 160 });
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.04;
  sun.shadow.radius = 4;
  scene.add(sun, sun.target);

  // ---------- sky ----------
  const skyMat = new THREE.ShaderMaterial({
    side: THREE.BackSide,
    depthWrite: false,
    fog: false,
    uniforms: { top: { value: new THREE.Color() }, horizon: { value: new THREE.Color() } },
    vertexShader: 'varying vec3 vDir; void main(){ vDir = normalize(position); gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
    fragmentShader: [
      'uniform vec3 top; uniform vec3 horizon; varying vec3 vDir;',
      'void main(){',
      '  float h = clamp(vDir.y, 0.0, 1.0);',
      '  gl_FragColor = vec4(mix(horizon, top, pow(h, 0.6)), 1.0);',
      '  #include <colorspace_fragment>',
      '}',
    ].join('\n'),
  });
  const sky = new THREE.Mesh(new THREE.SphereGeometry(900, 24, 16), skyMat);
  sky.frustumCulled = false;
  sky.renderOrder = -10;
  scene.add(sky);

  // ---------- ground, yard, road ----------
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(3000, 3000), std(0x93a8c6, { roughness: 1 }));
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);

  const pad = new THREE.Mesh(new THREE.PlaneGeometry(96, 50), std(0xc4d1e4, { roughness: 0.95 }));
  pad.rotation.x = -Math.PI / 2;
  pad.position.set(4, 0.02, -1);
  pad.receiveShadow = true;
  scene.add(pad);

  const road = new THREE.Mesh(new THREE.PlaneGeometry(1200, 7.6), std(0x4a566b, { roughness: 0.95 }));
  road.rotation.x = -Math.PI / 2;
  road.position.set(560, 0.04, 3.5);
  road.receiveShadow = true;
  scene.add(road);

  const dashes = new THREE.InstancedMesh(new THREE.BoxGeometry(3, 0.02, 0.18), new THREE.MeshBasicMaterial({ color: 0xffffff }), 110);
  {
    const m = new THREE.Matrix4();
    for (let i = 0; i < 110; i++) dashes.setMatrixAt(i, m.makeTranslation(14 + i * 6, 0.06, 3.5));
  }
  dashes.frustumCulled = false;
  scene.add(dashes);

  // ---------- warehouse shell ----------
  const X0 = -12, X1 = 8, Z0 = -9, Z1 = 7.5;
  const floorTex = canvasTex(1600, 1320, (g) => {
    const px = (x: number) => (x - X0) * 80;
    const py = (z: number) => (z - Z0) * 80;
    g.fillStyle = '#eef2f8';
    g.fillRect(0, 0, 1600, 1320);
    g.strokeStyle = '#d9e1ed';
    g.lineWidth = 2;
    for (let x = X0; x <= X1; x += 2) { g.beginPath(); g.moveTo(px(x), 0); g.lineTo(px(x), 1320); g.stroke(); }
    for (let z = Z0; z <= Z1; z += 2) { g.beginPath(); g.moveTo(0, py(z)); g.lineTo(1600, py(z)); g.stroke(); }
    g.strokeStyle = '#8fa5c9';
    g.lineWidth = 6;
    g.setLineDash([46, 30]);
    for (const z of [-3.5, -1.3]) { g.beginPath(); g.moveTo(px(-11.6), py(z)); g.lineTo(px(7.6), py(z)); g.stroke(); }
    g.setLineDash([]);
    g.fillStyle = 'rgba(37,99,235,0.10)';
    g.fillRect(px(4), py(1.2), 80 * 3.8, 80 * 4.6);
    g.strokeStyle = 'rgba(37,99,235,0.55)';
    g.strokeRect(px(4), py(1.2), 80 * 3.8, 80 * 4.6);
  });
  const slab = new THREE.Mesh(new THREE.BoxGeometry(X1 - X0 + 0.8, 1, Z1 - Z0 + 0.8), std(0xaebbd3));
  slab.position.set((X0 + X1) / 2, 0.5, (Z0 + Z1) / 2);
  slab.receiveShadow = true;
  scene.add(slab);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(X1 - X0, Z1 - Z0), new THREE.MeshStandardMaterial({ map: floorTex, roughness: 0.5, metalness: 0.03 }));
  floor.rotation.x = -Math.PI / 2;
  floor.position.set((X0 + X1) / 2, 1.005, (Z0 + Z1) / 2);
  floor.receiveShadow = true;
  scene.add(floor);

  const wallMat = std(0xffffff, { roughness: 0.9 });
  const bandMat = std(0x24407a);
  const windowMat = std(0xcfe8ff, { emissive: 0xbfe0ff, emissiveIntensity: 0.3, roughness: 0.3 });
  const lampMat = std(0xffffff, { emissive: 0xfff4d6, emissiveIntensity: 0.5, transparent: true });

  const add = (geo: THREE.BufferGeometry, mat: THREE.Material, x: number, y: number, z: number, cast = true, parent: THREE.Object3D = scene) => {
    const m = new THREE.Mesh(geo, mat);
    m.position.set(x, y, z);
    m.castShadow = cast;
    m.receiveShadow = true;
    parent.add(m);
    return m;
  };
  const B = (w: number, h: number, d: number) => new THREE.BoxGeometry(w, h, d);

  add(B(20.8, 8, 0.4), wallMat, -2, 5, -9.2);
  add(B(20.8, 1.2, 0.46), bandMat, -2, 1.6, -9.2);
  add(B(0.4, 8, 17), wallMat, -12.2, 5, -0.7);
  add(B(0.46, 1.2, 17), bandMat, -12.2, 1.6, -0.7);
  add(B(0.4, 8, 10.7), wallMat, 8.2, 5, -3.85);
  add(B(0.46, 1.2, 10.7), bandMat, 8.2, 1.6, -3.85);
  add(B(0.4, 8, 2.3), wallMat, 8.2, 5, 6.65);
  add(B(0.46, 1.2, 2.3), bandMat, 8.2, 1.6, 6.65);
  add(B(0.4, 2, 4), wallMat, 8.2, 8, 3.5);
  add(B(0.5, 0.3, 4.4), std(0xf59e0b), 8.2, 7.1, 3.5);
  for (let z = -8; z <= -1; z += 2.3) add(B(0.06, 2.6, 1.7), windowMat, -12.0, 6.7, z, false);
  for (let x = -10; x <= 6; x += 3.2) add(B(2.2, 1.7, 0.06), windowMat, x, 7.9, -8.98, false);

  const brandTex = canvasTex(1600, 260, (g) => {
    g.fillStyle = '#ffffff';
    g.fillRect(0, 0, 1600, 260);
    const gr = g.createLinearGradient(0, 0, 1600, 0);
    gr.addColorStop(0, '#1e3a8a');
    gr.addColorStop(0.55, '#2563eb');
    gr.addColorStop(1, '#38bdf8');
    g.fillStyle = gr;
    g.fillRect(0, 236, 1600, 24);
    g.fillStyle = '#0f172a';
    g.font = '800 140px Inter, Segoe UI, Arial, sans-serif';
    g.textBaseline = 'middle';
    g.fillText('INVENTORY RESCUE', 130, 122);
    g.fillStyle = '#1e3a8a';
    g.beginPath(); g.roundRect(24, 52, 78, 78, 18); g.fill();
    g.fillStyle = '#fff';
    g.font = '800 40px Inter, Segoe UI, Arial, sans-serif';
    g.fillText('IR', 40, 92);
  });
  const sign = new THREE.Mesh(new THREE.PlaneGeometry(9.6, 1.56), new THREE.MeshStandardMaterial({ map: brandTex, roughness: 0.6 }));
  sign.position.set(-2.4, 7.7, -8.97);
  scene.add(sign);

  const trussMat = std(0xb4bdd0, { metalness: 0.25, transparent: true });
  for (let x = -12; x <= 8; x += 4) add(B(0.22, 0.28, 17), trussMat, x, 9.1, -0.7, false);
  for (const z of [-8.6, -3, 2.6, 7.2]) add(B(20.8, 0.18, 0.2), trussMat, -2, 9.0, z, false);
  for (let x = -9; x <= 5; x += 4.67) for (const z of [-5.5, 0, 5.2]) add(B(1.8, 0.14, 0.5), lampMat, x, 8.4, z, false);

  add(B(1.6, 0.08, 4), std(0x94a3b8), 8.7, 1.08, 3.5, false);
  for (const z of [2, 5]) add(B(0.4, 0.8, 0.5), std(0x334155), 8.5, 1.5, z, false);

  // ---------- racks ----------
  const upMat = std(0x24407a, { metalness: 0.2 });
  const beamMat = std(0xf59e0b, { metalness: 0.2 });
  const deckMat = std(0xe2e8f0);
  const ROWS = [-7.6, -4.4, -0.4];
  const BAYS = 6, BAY_W = 2.4, RX0 = -11.4;
  const DECKS = [1.35, 3.0, 4.65];
  const upI = new THREE.InstancedMesh(B(0.12, 5.4, 0.12), upMat, ROWS.length * (BAYS + 1) * 2);
  const beamI = new THREE.InstancedMesh(B(BAY_W - 0.06, 0.14, 0.1), beamMat, ROWS.length * BAYS * 3 * 2);
  const deckI = new THREE.InstancedMesh(B(BAY_W - 0.12, 0.04, 1.16), deckMat, ROWS.length * BAYS * 3);
  {
    const m = new THREE.Matrix4();
    let u = 0, b = 0, d = 0;
    for (const rz of ROWS) {
      for (let k = 0; k <= BAYS; k++) for (const f of [-0.62, 0.62]) upI.setMatrixAt(u++, m.makeTranslation(RX0 + k * BAY_W, 3.7, rz + f));
      for (let bay = 0; bay < BAYS; bay++) {
        const cx = RX0 + (bay + 0.5) * BAY_W;
        for (const top of DECKS) {
          for (const f of [-0.62, 0.62]) beamI.setMatrixAt(b++, m.makeTranslation(cx, top - 0.12, rz + f));
          deckI.setMatrixAt(d++, m.makeTranslation(cx, top - 0.02, rz));
        }
      }
    }
  }
  for (const im of [upI, beamI, deckI]) {
    im.castShadow = true;
    im.receiveShadow = true;
    scene.add(im);
  }

  // ---------- boxes (one instanced mesh: shelf stock, pallets, loader stream, belt) ----------
  const boxTex = canvasTex(128, 128, (g) => {
    g.fillStyle = '#f4f1ec';
    g.fillRect(0, 0, 128, 128);
    g.fillStyle = 'rgba(0,0,0,0.05)';
    g.fillRect(50, 0, 28, 128);
    g.fillStyle = '#fff';
    g.fillRect(8, 88, 34, 22);
    g.fillStyle = '#475569';
    for (let i = 0; i < 9; i++) g.fillRect(11 + i * 3.4, 92, 1 + (i % 3), 12);
    g.strokeStyle = 'rgba(0,0,0,0.12)';
    g.lineWidth = 3;
    g.strokeRect(1.5, 1.5, 125, 125);
  });
  const boxMat = new THREE.MeshStandardMaterial({ map: boxTex, roughness: 0.85 });

  type Spec = { x: number; y: number; z: number; sx: number; sy: number; sz: number; ry: number; tint: number };
  const statics: Spec[] = [];
  const rackIdx: { id: number; row: number }[] = [];
  // mostly kraft and cream, with a little white and cool grey
  const TINTS = [0xe8cda4, 0xf0e2c8, 0xe8cda4, 0xffffff, 0xdcc3a0, 0xf0e2c8, 0xd6dfea, 0xe8cda4, 0xffffff, 0xf0e2c8];
  const rnd = rngFrom(7);
  for (let row = 0; row < ROWS.length; row++) {
    for (let bay = 0; bay < BAYS; bay++) {
      for (const top of DECKS) {
        const n = rnd() < 0.5 ? 2 : 3;
        const slot = (BAY_W - 0.2) / n;
        for (let i = 0; i < n; i++) {
          if (rnd() < 0.12) continue;
          const sx = slot * (0.78 + rnd() * 0.16);
          const sy = 0.5 + rnd() * 0.65;
          const sz = 0.8 + rnd() * 0.25;
          const x = RX0 + bay * BAY_W + 0.1 + slot * (i + 0.5);
          const base = { x, z: ROWS[row] + (rnd() - 0.5) * 0.05, sx, sz, ry: (rnd() - 0.5) * 0.08, tint: Math.floor(rnd() * TINTS.length) };
          rackIdx.push({ id: statics.length, row });
          statics.push({ ...base, y: top + sy / 2, sy });
          if (sy < 0.72 && rnd() < 0.4) {
            const sy2 = 0.4 + rnd() * 0.3;
            statics.push({ ...base, y: top + sy + sy2 / 2, sy: sy2, sx: sx * 0.9, sz: sz * 0.92, tint: Math.floor(rnd() * TINTS.length) });
          }
        }
      }
    }
  }
  const palletSpecs: { x: number; z: number }[] = [];
  for (const x of [-10.6, -8.9, -7.2]) for (const z of [1.9, 3.7, 5.5]) palletSpecs.push({ x, z });
  for (const ps of palletSpecs) {
    const layers = 2 + Math.floor(rnd() * 3);
    let y = 1.16;
    for (let l = 0; l < layers; l++) {
      const h = 0.5 + rnd() * 0.18;
      for (const dx of [-0.34, 0.34]) for (const dz of [-0.34, 0.34]) {
        if (l === layers - 1 && rnd() < 0.3) continue;
        statics.push({ x: ps.x + dx, y: y + h / 2, z: ps.z + dz, sx: 0.64, sy: h, sz: 0.64, ry: (rnd() - 0.5) * 0.1, tint: Math.floor(rnd() * TINTS.length) });
      }
      y += h;
    }
  }
  const N_STATIC = statics.length;
  const N_BELT = 9;
  const N_TOTAL = N_STATIC + TOTAL_LOAD + N_BELT;
  const boxes = new THREE.InstancedMesh(B(1, 1, 1), boxMat, N_TOTAL);
  boxes.castShadow = true;
  boxes.receiveShadow = true;
  boxes.frustumCulled = false;
  boxes.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(N_TOTAL * 3), 3);
  scene.add(boxes);

  const dummy = new THREE.Object3D();
  const tmpC = new THREE.Color();
  const setBox = (i: number, x: number, y: number, z: number, sx: number, sy: number, sz: number, ry: number) => {
    dummy.position.set(x, y, z);
    dummy.rotation.set(0, ry, 0);
    dummy.scale.set(sx, sy, sz);
    dummy.updateMatrix();
    boxes.setMatrixAt(i, dummy.matrix);
  };
  statics.forEach((s, i) => {
    setBox(i, s.x, s.y, s.z, s.sx, s.sy, s.sz, s.ry);
    boxes.setColorAt(i, tmpC.setHex(TINTS[s.tint]));
  });
  for (let i = N_STATIC; i < N_TOTAL; i++) boxes.setColorAt(i, tmpC.setHex(TINTS[i % TINTS.length]));

  const palletI = new THREE.InstancedMesh(B(1.4, 0.16, 1.4), std(0xd9c3a5), palletSpecs.length);
  palletSpecs.forEach((ps, i) => palletI.setMatrixAt(i, new THREE.Matrix4().makeTranslation(ps.x, 1.08, ps.z)));
  palletI.castShadow = true;
  palletI.receiveShadow = true;
  scene.add(palletI);

  const rr = rngFrom(23);
  const riskCount = Math.max(3, Math.min(9, opts.atRisk || 5));
  const pool = rackIdx.filter((r) => r.row >= 1);
  const riskIds: number[] = [];
  while (riskIds.length < riskCount && pool.length) riskIds.push(pool.splice(Math.floor(rr() * pool.length), 1)[0].id);
  const riskSet = new Set(riskIds);
  const RISK_TINT = new THREE.Color(0xff8a1f);

  const badge = (symbol: string, bg: string) =>
    canvasTex(128, 128, (g) => {
      g.fillStyle = 'rgba(15,23,42,0.16)';
      g.beginPath(); g.arc(64, 70, 50, 0, 7); g.fill();
      g.fillStyle = bg;
      g.beginPath(); g.arc(64, 64, 50, 0, 7); g.fill();
      g.strokeStyle = '#fff';
      g.lineWidth = 8;
      g.stroke();
      g.fillStyle = '#fff';
      g.font = '800 70px Inter, Segoe UI, Arial, sans-serif';
      g.textAlign = 'center';
      g.textBaseline = 'middle';
      g.fillText(symbol, 64, 68);
    });
  const warnTex = badge('!', '#f59e0b');
  const okTex = badge('✓', '#16a34a');
  const sprites = riskIds.map(() => {
    const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: warnTex, transparent: true, depthTest: false, fog: false, toneMapped: false }));
    s.renderOrder = 20;
    s.scale.setScalar(0.001);
    scene.add(s);
    return s;
  });

  // ---------- conveyor + scanner ----------
  const beltTex = canvasTex(64, 64, (g) => {
    g.fillStyle = '#475569';
    g.fillRect(0, 0, 64, 64);
    g.fillStyle = '#5b6b82';
    g.fillRect(0, 0, 64, 6);
    g.fillRect(0, 32, 64, 6);
  }, true);
  beltTex.repeat.set(1, 15);
  const beltTop = new THREE.Mesh(new THREE.PlaneGeometry(1.4, 13.6), new THREE.MeshStandardMaterial({ map: beltTex, roughness: 0.9 }));
  beltTop.rotation.x = -Math.PI / 2;
  beltTop.rotation.z = Math.PI / 2;
  beltTop.position.set(2, 1.82, 3.5);
  beltTop.receiveShadow = true;
  scene.add(beltTop);
  add(B(13.6, 0.8, 1.5), std(0x334155), 2, 1.4, 3.5);
  for (const z of [2.72, 4.28]) add(B(13.6, 0.18, 0.12), std(0x2563eb), 2, 1.9, z);
  for (let x = -4; x <= 8; x += 3) for (const z of [2.9, 4.1]) add(B(0.12, 0.4, 0.12), std(0x94a3b8), x, 1.2, z, false);

  const gateMat = std(0x38bdf8, { emissive: 0x38bdf8, emissiveIntensity: 1.2, transparent: true, opacity: 0.22, depthWrite: false });
  for (const z of [2.6, 4.4]) add(B(0.2, 2.6, 0.2), std(0x24407a), -3, 3.1, z);
  add(B(0.28, 0.28, 2.1), std(0x24407a), -3, 4.4, 3.5);
  const gate = new THREE.Mesh(new THREE.PlaneGeometry(1.8, 2.5), gateMat);
  gate.rotation.y = Math.PI / 2;
  gate.position.set(-3, 3.1, 3.5);
  scene.add(gate);
  const scanLine = new THREE.Mesh(B(0.04, 0.05, 1.8), new THREE.MeshBasicMaterial({ color: 0x38bdf8 }));
  scanLine.position.set(-3, 2, 3.5);
  scene.add(scanLine);

  // ---------- forklift ----------
  const fork = new THREE.Group();
  {
    const body = std(0xf5a524), accent = std(0x1f2937), dark = std(0x334155), mast = std(0x24407a);
    add(B(1.5, 0.5, 0.95), body, -0.1, 0.62, 0, true, fork);
    add(B(0.55, 0.62, 0.95), accent, -0.78, 0.72, 0, true, fork);
    for (const z of [-0.4, 0.4]) { add(B(0.07, 1.3, 0.07), dark, 0.05, 1.5, z, true, fork); add(B(0.07, 1.3, 0.07), dark, -0.55, 1.5, z, true, fork); }
    add(B(0.78, 0.07, 0.95), dark, -0.25, 2.18, 0, true, fork);
    add(B(0.45, 0.3, 0.6), std(0x94a3b8), -0.25, 1.0, 0, true, fork);
    for (const z of [-0.3, 0.3]) add(B(0.1, 2.3, 0.1), mast, 0.78, 1.2, z, true, fork);
    add(B(0.1, 0.1, 0.8), mast, 0.78, 2.1, 0, true, fork);
    for (const z of [-0.28, 0.28]) add(B(1.0, 0.06, 0.12), dark, 1.28, 0.25, z, true, fork);
    add(B(0.5, 0.5, 0.6), boxMat, 1.2, 0.58, 0, true, fork);
    const tire = new THREE.CylinderGeometry(0.3, 0.3, 0.22, 14);
    for (const x of [-0.6, 0.45]) for (const z of [-0.5, 0.5]) {
      const w = new THREE.Mesh(tire, dark);
      w.rotation.x = Math.PI / 2;
      w.position.set(x, 0.3, z);
      w.castShadow = true;
      fork.add(w);
    }
    fork.scale.setScalar(1.05);
    fork.position.y = 1.0;
    scene.add(fork);
  }

  // ---------- inventory drone ----------
  const drone = new THREE.Group();
  const rotors: THREE.Mesh[] = [];
  {
    add(B(0.8, 0.22, 0.8), std(0xffffff), 0, 0, 0, true, drone);
    add(B(0.3, 0.1, 0.3), std(0x2563eb, { emissive: 0x2563eb, emissiveIntensity: 0.6 }), 0, -0.16, 0, false, drone);
    const rg = new THREE.CylinderGeometry(0.34, 0.34, 0.02, 20);
    const rm = new THREE.MeshBasicMaterial({ color: 0x475569, transparent: true, opacity: 0.3 });
    for (const [x, z] of [[0.5, 0.5], [-0.5, 0.5], [0.5, -0.5], [-0.5, -0.5]]) {
      add(B(0.5, 0.05, 0.05), std(0x94a3b8), x / 2, 0.05, z / 2, false, drone).rotation.y = Math.atan2(-z, x);
      const r = new THREE.Mesh(rg, rm);
      r.position.set(x, 0.12, z);
      drone.add(r);
      rotors.push(r);
    }
    const beam = new THREE.Mesh(new THREE.ConeGeometry(1.3, 4, 24, 1, true), new THREE.MeshBasicMaterial({ color: 0x60a5fa, transparent: true, opacity: 0.16, depthWrite: false, side: THREE.DoubleSide }));
    beam.position.y = -2.2;
    drone.add(beam);
    scene.add(drone);
  }

  // ---------- truck ----------
  const truck = new THREE.Group();
  truck.position.z = 3.5;
  scene.add(truck);
  const tWhite = std(0xffffff), tTint = std(0xe4eaf4), tIndigo = std(0x24407a, { roughness: 0.5 }), tDark = std(0x334155), tGlass = std(0x1e293b, { roughness: 0.2, metalness: 0.5 });
  const T = (w: number, h: number, d: number, m: THREE.Material, x: number, y: number, z: number) => add(B(w, h, d), m, x, y, z, true, truck);
  T(10.6, 0.28, 3.0, tDark, 14.5, 1.0, 0);
  T(0.2, 2.9, 3.0, tTint, 19.7, 2.575, 0);
  T(10.6, 2.9, 0.12, tTint, 14.5, 2.575, -1.44);
  for (const [x, z] of [[9.3, -1.45], [9.3, 1.45], [19.6, -1.45], [19.6, 1.45]]) T(0.2, 2.95, 0.2, tIndigo, x, 2.6, z);
  T(10.6, 0.14, 0.14, tIndigo, 14.5, 4.03, 1.46);
  T(0.3, 0.5, 3.1, tDark, 9.1, 0.8, 0);
  for (const z of [-1.4, 1.4]) T(0.1, 0.25, 0.3, std(0xef4444, { emissive: 0xef4444, emissiveIntensity: 0.6 }), 9.05, 1.3, z);
  T(5.0, 0.5, 2.4, tDark, 20.4, 0.95, 0);
  T(2.0, 2.3, 2.8, tWhite, 21.0, 2.2, 0);
  T(2.05, 0.5, 2.85, tIndigo, 21.0, 1.35, 0);
  T(1.5, 1.1, 2.6, tWhite, 22.75, 1.55, 0);
  T(1.55, 0.4, 2.65, tIndigo, 22.75, 1.15, 0);
  T(0.12, 0.8, 2.0, tDark, 23.55, 1.45, 0);
  T(0.06, 1.0, 2.5, tGlass, 22.03, 2.75, 0).rotation.z = -0.25;
  for (const z of [-1.41, 1.41]) T(1.2, 0.9, 0.04, tGlass, 21.0, 2.75, z);
  for (const z of [-0.9, 0.9]) T(0.1, 0.28, 0.4, std(0xfff7c2, { emissive: 0xfff2a8, emissiveIntensity: 1.0 }), 23.58, 1.7, z);
  for (const z of [-0.95, 0.95]) T(0.2, 2.0, 0.2, std(0xcbd5e1, { metalness: 0.7, roughness: 0.3 }), 19.9, 3.1, z);
  const brandTruck = canvasTex(1024, 290, (g) => {
    const gr = g.createLinearGradient(0, 0, 1024, 0);
    gr.addColorStop(0, '#1e3a8a');
    gr.addColorStop(0.6, '#2563eb');
    gr.addColorStop(1, '#3b82f6');
    g.fillStyle = gr;
    g.fillRect(0, 0, 1024, 290);
    g.fillStyle = '#f59e0b';
    g.fillRect(0, 250, 1024, 40);
    g.fillStyle = '#fff';
    g.beginPath(); g.roundRect(48, 56, 130, 130, 30); g.fill();
    g.fillStyle = '#1e3a8a';
    g.font = '800 66px Inter, Segoe UI, Arial, sans-serif';
    g.textBaseline = 'middle';
    g.fillText('IR', 78, 124);
    g.fillStyle = '#fff';
    g.font = '800 104px Inter, Segoe UI, Arial, sans-serif';
    g.fillText('Inventory Rescue', 212, 108);
    g.font = '600 44px Inter, Segoe UI, Arial, sans-serif';
    g.fillText('Right stock. Right time.', 216, 196);
  });
  const brandMat = new THREE.MeshStandardMaterial({ map: brandTruck, roughness: 0.6 });
  const wallGeo = B(10.6, 2.9, 0.12);
  wallGeo.translate(0, 1.45, 0);
  const nearWall = new THREE.Mesh(wallGeo, [tWhite, tWhite, tWhite, tWhite, brandMat, tWhite]);
  nearWall.position.set(14.5, 1.14, 1.44);
  nearWall.castShadow = true;
  nearWall.scale.y = 0.001;
  truck.add(nearWall);
  const roofPivot = new THREE.Group();
  roofPivot.position.set(19.8, 4.1, 0);
  const roof = new THREE.Mesh(B(10.6, 0.14, 3.1), tWhite);
  roof.position.x = -5.3;
  roof.castShadow = true;
  roofPivot.add(roof);
  roofPivot.scale.x = 0.001;
  truck.add(roofPivot);

  const wheelGeo = new THREE.CylinderGeometry(0.55, 0.55, 0.42, 22);
  const hubGeo = new THREE.CylinderGeometry(0.28, 0.28, 0.46, 12);
  const lugGeo = B(0.1, 0.1, 0.5);
  const wheels: THREE.Group[] = [];
  for (const x of [10.9, 12.2, 18.4, 21.8]) for (const z of [-1.3, 1.3]) {
    const g = new THREE.Group();
    g.position.set(x, 0.55, z);
    const tire = new THREE.Mesh(wheelGeo, std(0x1e293b));
    tire.rotation.x = Math.PI / 2;
    tire.castShadow = true;
    const hub = new THREE.Mesh(hubGeo, std(0xe2e8f0, { metalness: 0.6, roughness: 0.3 }));
    hub.rotation.x = Math.PI / 2;
    const lug = new THREE.Mesh(lugGeo, tIndigo);
    lug.position.set(0.2, 0, 0);
    g.add(tire, hub, lug);
    truck.add(g);
    wheels.push(g);
  }

  // ---------- scenery: soft shrubs, hills, clouds ----------
  const shrubI = new THREE.InstancedMesh(new THREE.IcosahedronGeometry(1, 1), std(0xffffff, { roughness: 0.95 }), 110);
  {
    const r = rngFrom(5);
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const greens = [0x7fae92, 0x6f9f86, 0x8fbb9f, 0x78a58a];
    let n = 0;
    const put = (x: number, z: number, s: number) => {
      if (n >= 110) return;
      m.compose(new THREE.Vector3(x, s * 0.8, z), q, new THREE.Vector3(s * (1.1 + r() * 0.5), s * 0.85, s * (1.1 + r() * 0.5)));
      shrubI.setMatrixAt(n, m);
      shrubI.setColorAt(n, tmpC.setHex(greens[Math.floor(r() * greens.length)]));
      n++;
    };
    for (let x = -30; x < 30; x += 6 + r() * 3) put(x, -15 - r() * 3, 1.3 + r() * 1.0);
    for (let x = 40; x < 560; x += 11 + r() * 8) { put(x, 13 + r() * 5, 1.1 + r() * 0.9); put(x + 5, -1.5 - r() * 3, 1 + r() * 0.8); }
    for (let i = n; i < 110; i++) { m.makeScale(0, 0, 0); shrubI.setMatrixAt(i, m); shrubI.setColorAt(i, tmpC.setHex(0xffffff)); }
  }
  shrubI.castShadow = true;
  shrubI.frustumCulled = false;
  scene.add(shrubI);

  const hills: { m: THREE.Mesh; x: number }[] = [];
  {
    const r = rngFrom(31);
    const cols = [0xa9bdd8, 0xb6c7de, 0x9fb4d2, 0xb1c2da];
    for (let i = 0; i < 9; i++) {
      const m = new THREE.Mesh(new THREE.SphereGeometry(1, 24, 12), new THREE.MeshBasicMaterial({ color: cols[i % cols.length] }));
      m.scale.set(150 + r() * 120, 30 + r() * 45, 80 + r() * 60);
      m.position.set(0, -4, -430 - r() * 90);
      scene.add(m);
      hills.push({ m, x: (i - 4) * 150 + r() * 40 });
    }
  }

  const cloudMat = new THREE.MeshLambertMaterial({ color: 0xffffff, transparent: true, opacity: 0.85, fog: false });
  const clouds: { g: THREE.Group; x: number; sp: number }[] = [];
  {
    const r = rngFrom(3);
    const ico = new THREE.IcosahedronGeometry(1, 1);
    for (let i = 0; i < 9; i++) {
      const g = new THREE.Group();
      for (let k = 0; k < 5; k++) {
        const s = new THREE.Mesh(ico, cloudMat);
        s.position.set(k * 3.2 - 6, r() * 1.4, r() * 2 - 1);
        s.scale.set(3 + r() * 2.5, 2 + r() * 1.5, 2.6 + r() * 1.5);
        g.add(s);
      }
      g.position.set(0, 55 + r() * 25, -110 - r() * 80);
      scene.add(g);
      clouds.push({ g, x: (i / 9) * 420 - 210, sp: 1.2 + r() * 1.4 });
    }
  }

  // ---------- interaction ----------
  const ray = new THREE.Raycaster();
  const ptr = new THREE.Vector2(9, 9);
  const ptrSmooth = new THREE.Vector2();
  let ptrClient = { x: 0, y: 0 };
  let hoverId = -1;
  let ptrDirty = false;
  const lift = new Float32Array(N_STATIC);
  const popAt = new Float32Array(N_STATIC).fill(-10);
  const active = new Set<number>();
  let pCur = 0;
  let tCur = 0;

  const onMove = (e: PointerEvent) => {
    const r = renderer.domElement.getBoundingClientRect();
    ptr.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ptrClient = { x: e.clientX, y: e.clientY };
    ptrDirty = true;
  };
  const onLeave = () => {
    ptr.set(9, 9);
    ptrDirty = true;
  };
  const onClick = () => {
    if (hoverId >= 0 && hoverId < N_STATIC) {
      popAt[hoverId] = tCur;
      active.add(hoverId);
    }
  };
  host.addEventListener('pointermove', onMove);
  host.addEventListener('pointerleave', onLeave);
  host.addEventListener('click', onClick);

  const pick = () => {
    ptrDirty = false;
    if (Math.abs(ptr.x) > 1) { hoverId = -1; onHover(null); host.style.cursor = ''; return; }
    ray.setFromCamera(ptr, camera);
    const hit = ray.intersectObject(boxes, false).find((h) => (h.instanceId ?? N_STATIC) < N_STATIC);
    const id = hit?.instanceId ?? -1;
    if (id !== hoverId) {
      if (hoverId >= 0) active.add(hoverId);
      hoverId = id;
      if (id >= 0) active.add(id);
    }
    host.style.cursor = id >= 0 ? 'pointer' : '';
    if (id < 0) onHover(null);
    else onHover({ kind: riskSet.has(id) ? (pCur >= FIX_AT ? 'fixed' : 'risk') : 'ok', x: ptrClient.x, y: ptrClient.y });
  };

  // ---------- sizing ----------
  const size = { w: 1, h: 1 };
  const resize = () => {
    const w = Math.max(1, host.clientWidth), h = Math.max(1, host.clientHeight);
    size.w = w;
    size.h = h;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  const ro = new ResizeObserver(resize);
  ro.observe(host);
  resize();

  // ---------- per-frame ----------
  const look: Look = { top: new THREE.Color(), hor: new THREE.Color(), hSky: new THREE.Color(), hGnd: new THREE.Color(), sun: new THREE.Color(), hInt: 0, sInt: 0, env: 0 };
  const posA = new THREE.Vector3(), tgtA = new THREE.Vector3(), tmpV = new THREE.Vector3(), tmpV2 = new THREE.Vector2();
  const slotPos = (i: number) => {
    const col = Math.floor(i / 6), k = i % 6;
    return { x: 19.2 - col * 1.1, y: 1.6 + Math.floor(k / 3) * 0.92, z: (k % 3 - 1) * 0.95 };
  };
  let arriveAt = reduce ? -100 : Infinity;
  const BELT_X0 = -4.8, BELT_X1 = 8.6;

  const render = (p: number, t: number) => {
    pCur = p;
    tCur = t;
    const it = reduce ? 0 : t;

    mixLook(look, p);
    skyMat.uniforms.top.value.copy(look.top);
    skyMat.uniforms.horizon.value.copy(look.hor);
    (scene.fog as THREE.Fog).color.copy(look.hor);
    hemi.color.copy(look.hSky);
    hemi.groundColor.copy(look.hGnd);
    hemi.intensity = look.hInt;
    sun.color.copy(look.sun);
    sun.intensity = look.sInt;
    scene.environmentIntensity = look.env;
    gateMat.opacity = 0.16 + 0.1 * Math.sin(it * 3);
    // roof beams would sit in front of the lens while the camera is inside the building
    trussMat.opacity = lampMat.opacity = 1 - sstep(0.14, 0.24, p) * (1 - sstep(0.57, 0.65, p));

    // truck: arrives once the page opens, departs at the end
    const arrive = 1 - easeOut(clamp01((t - arriveAt) / 2.8));
    const dep = 330 * Math.pow(clamp01((p - DEPART_AT) / (1 - DEPART_AT)), 2.2);
    const tx = (Number.isFinite(arriveAt) ? arrive * 60 : 60) + dep;
    truck.position.x = tx;
    const spin = -tx / 0.55;
    for (const w of wheels) w.rotation.z = spin;
    nearWall.scale.y = Math.max(0.001, sstep(WALL_AT[0], WALL_AT[1], p));
    roofPivot.scale.x = Math.max(0.001, sstep(ROOF_AT[0], ROOF_AT[1], p));
    truck.rotation.z = Math.sin(it * 9) * 0.002 * clamp01(dep / 20);

    // camera
    camAt(p, posA, tgtA);
    const follow = sstep(0.86, 0.94, p);
    tgtA.x = lerp(tgtA.x, tx + 15, follow * 0.92);
    posA.x += dep * 0.88 * follow;
    const aspect = size.w / size.h;
    const fit = Math.min(2.6, Math.max(1, Math.pow(1.78 / aspect, 0.95)));
    posA.sub(tgtA).multiplyScalar(fit).add(tgtA);
    ptrSmooth.lerp(Math.abs(ptr.x) > 1 ? tmpV2.set(0, 0) : ptr, 0.05);
    camera.position.copy(posA);
    camera.position.x += reduce ? 0 : ptrSmooth.x * 1.6;
    camera.position.y += reduce ? 0 : ptrSmooth.y * 0.8;
    camera.lookAt(tgtA);
    const wide = aspect > 1.15;
    const shiftX = wide ? size.w * 0.12 * (1 - sstep(0.84, 0.95, p)) : 0;
    const shiftY = wide ? 0 : size.h * 0.14;
    camera.setViewOffset(size.w, size.h, -shiftX, shiftY, size.w, size.h);

    sun.position.set(tgtA.x - 26, 40, tgtA.z + 22);
    sun.target.position.copy(tgtA);
    sun.target.position.y = 0;
    sky.position.copy(camera.position);

    for (const c of clouds) {
      const x = ((c.x + it * c.sp + 400) % 800) - 400;
      c.g.position.x = camera.position.x * 0.8 + x;
    }
    for (const h of hills) h.m.position.x = camera.position.x * 0.92 + h.x;
    drone.position.set(-4 + Math.cos(it * 0.35) * 7, 8.1 + Math.sin(it * 1.4) * 0.12, -4 + Math.sin(it * 0.5) * 3);
    drone.rotation.z = Math.sin(it * 0.35) * 0.12;
    for (const r of rotors) r.rotation.y = it * 40;
    {
      const L = 11, Tn = 1.4, v = 1.9;
      const s = (it * v) % (2 * (L + Tn));
      let x: number, h: number;
      if (s < L) { x = -10.5 + s; h = 0; }
      else if (s < L + Tn) { x = -10.5 + L; h = ((s - L) / Tn) * Math.PI; }
      else if (s < 2 * L + Tn) { x = -10.5 + L - (s - L - Tn); h = Math.PI; }
      else { x = -10.5; h = Math.PI + ((s - 2 * L - Tn) / Tn) * Math.PI; }
      fork.position.x = x;
      fork.position.z = -2.4;
      fork.rotation.y = -h;
    }
    beltTex.offset.y = -(it * 0.5 + p * 12) % 1;
    scanLine.position.y = 2 + (Math.sin(it * 4) * 0.5 + 0.5) * 2.1;

    // idle belt stream (before the plan is approved)
    const idleScale = 1 - sstep(LOAD_START - 0.04, LOAD_START, p);
    for (let k = 0; k < N_BELT; k++) {
      const len = BELT_X1 - BELT_X0;
      const rel = (it * 1.5 + k * (len / N_BELT)) % len;
      const s = Math.min(1, rel / 0.7, (len - rel) / 0.7) * idleScale * (p < LOAD_START ? 1 : 0);
      const sc = Math.max(0.0001, 0.8 * s);
      setBox(N_STATIC + TOTAL_LOAD + k, BELT_X0 + rel, 1.82 + 0.4 * sc / 0.8, 3.5, sc, sc * 0.85, sc, 0);
    }

    // loader stream: scroll-driven
    for (let k = 0; k < TOTAL_LOAD; k++) {
      const idx = N_STATIC + k;
      const uK = clamp01((p - (LOAD_START + k * STEP)) / LOAD_SPAN);
      if (uK <= 0) { setBox(idx, 0, -20, 0, 0.0001, 0.0001, 0.0001, 0); continue; }
      const slot = slotPos(k);
      const sx = tx + slot.x;
      const pop = Math.min(1, uK / 0.08);
      const BEAT = 0.55;
      let x: number, y: number, z: number;
      if (uK < BEAT) {
        x = lerp(BELT_X0, 8.8, uK / BEAT);
        y = 2.27;
        z = 3.5;
      } else {
        const e = sstep(0, 1, (uK - BEAT) / (1 - BEAT));
        x = lerp(8.8, sx, e);
        y = lerp(2.27, slot.y, e) + Math.sin(Math.PI * e) * 1.4;
        z = lerp(3.5, 3.5 + slot.z, e);
      }
      const s = 0.86 * pop;
      setBox(idx, x, y, z, s, s * 0.95, s, uK >= 1 ? 0 : (1 - uK) * 0.5);
    }
    boxes.instanceMatrix.needsUpdate = true;

    // risk beacons: on while the story talks about trouble, green once the plan is approved
    const showRisk = sstep(0.33, 0.37, p) * (1 - sstep(0.6, 0.64, p));
    const fixed = p >= FIX_AT;
    let colorsDirty = false;
    riskIds.forEach((id, n) => {
      const s = statics[id];
      const bob = reduce ? 0 : Math.sin(it * 2.4 + n) * 0.12;
      const sp = sprites[n];
      sp.position.set(s.x, s.y + s.sy / 2 + 0.95 + bob, s.z);
      sp.scale.setScalar(Math.max(0.001, showRisk * (1.05 + (reduce ? 0 : Math.sin(it * 5 + n) * 0.06))));
      (sp.material as THREE.SpriteMaterial).map = fixed ? okTex : warnTex;
      if (!fixed && showRisk > 0) {
        const pulse = 0.5 + 0.5 * Math.sin(it * 4 + n);
        tmpC.setHex(TINTS[s.tint]).lerp(RISK_TINT, showRisk * (0.55 + 0.4 * pulse));
        boxes.setColorAt(id, tmpC);
        colorsDirty = true;
      } else if (colorsDirty || fixed || showRisk === 0) {
        boxes.setColorAt(id, tmpC.setHex(TINTS[s.tint]));
        colorsDirty = true;
      }
    });
    if (colorsDirty && boxes.instanceColor) boxes.instanceColor.needsUpdate = true;

    // hover lift and click bounce
    if (ptrDirty) pick();
    if (active.size) {
      for (const id of active) {
        const target = id === hoverId ? 0.32 : 0;
        lift[id] += (target - lift[id]) * 0.2;
        const age = t - popAt[id];
        const bounce = age >= 0 && age < 0.7 ? Math.abs(Math.sin(age * 9)) * 0.5 * (1 - age / 0.7) : 0;
        const s = statics[id];
        setBox(id, s.x, s.y + lift[id] + bounce, s.z, s.sx, s.sy, s.sz, s.ry);
        if (id !== hoverId && lift[id] < 0.002 && !(age >= 0 && age < 0.7)) {
          lift[id] = 0;
          setBox(id, s.x, s.y, s.z, s.sx, s.sy, s.sz, s.ry);
          active.delete(id);
        }
      }
    }

    renderer.render(scene, camera);
  };

  // Screen position of a point of interest, so the page can pin labels to the 3D objects.
  const project = (id: AnchorId): AnchorPoint => {
    switch (id) {
      case 'drone': tmpV.copy(drone.position).y += 0.9; break;
      case 'forklift': tmpV.copy(fork.position).y += 2.9; break;
      case 'risk': {
        const s = statics[riskIds[0]];
        tmpV.set(s.x, s.y + s.sy / 2 + 1.8, s.z);
        break;
      }
      case 'scan': tmpV.set(-3, 5.1, 3.5); break;
      case 'truck': tmpV.set(14.5, 4.6, 1.5).add(truck.position); break;
    }
    tmpV.project(camera);
    return { x: (tmpV.x * 0.5 + 0.5) * size.w, y: (-tmpV.y * 0.5 + 0.5) * size.h, visible: tmpV.z < 1 && Math.abs(tmpV.x) < 1.1 && Math.abs(tmpV.y) < 1.1 };
  };

  return {
    render,
    project,
    arrive: () => {
      if (!reduce) arriveAt = tCur;
    },
    dispose: () => {
      ro.disconnect();
      host.removeEventListener('pointermove', onMove);
      host.removeEventListener('pointerleave', onLeave);
      host.removeEventListener('click', onClick);
      scene.traverse((o) => {
        const m = o as THREE.Mesh;
        if (m.geometry) m.geometry.dispose();
        const mat = m.material as THREE.Material | THREE.Material[] | undefined;
        for (const x of Array.isArray(mat) ? mat : mat ? [mat] : []) {
          (x as THREE.MeshStandardMaterial).map?.dispose();
          x.dispose();
        }
      });
      envTex.dispose();
      pmrem.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
