import {
  AURORA_FROM,
  AURORA_TONE,
  CELL,
  HALO,
  LIGHT,
  SHEAR,
  STRIKE_TONES,
  TONE_INDEX,
} from "./dither";
import { FLASH_REACH, MAX_FLASHES } from "./dither-flashes";

/** A GLSL float literal: ES 3.00 has no implicit int-to-float conversion. */
const f = (n: number): string => n.toFixed(4);

/** The cloud deck repeats once around the planet: `q.x` spans `SPAN`. */
const SPAN = 4 * Math.PI;

/** A noise frequency nudged so a whole number of lattice cells fits around the
 *  planet, which keeps the baked texture seamless where it wraps. */
const around = (freq: number): string => f(Math.round(freq * SPAN));

/** A 0–1 hash of a lattice point with no `sin`, whose precision varies by GPU. */
const HASH = `float hash(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}`;

/** The nine storms the bands roll around, placed once here rather than per texel. */
const VORTICES = Array.from({ length: 9 }, (_, k) => {
  const h = (salt: number) => {
    const v = Math.sin(k * 127.1 + salt * 311.7) * 43758.5453;
    return v - Math.floor(v);
  };
  return {
    c: [h(11) * SPAN, -0.4 - 1.8 * h(12)],
    ab: [0.18 + 0.3 * h(13), 0.1 + 0.12 * h(14)],
    spin: (h(15) < 0.5 ? -1 : 1) * (2 + 3 * h(16)),
    bright: h(17) < 0.5 ? -1 : 1,
  };
});

export const VERTEX_SHADER = `#version 300 es
in vec2 aPos;
void main() { gl_Position = vec4(aPos, 0.0, 1.0); }
`;

/**
 * One texel per band-space sample: x is rotated longitude in turns (0–1, repeating),
 * y is latitude from south pole to north. Writes the cloud density in red. Run in
 * row strips over several draws; `uPhase` is the slow evolution of the warp.
 *
 * Noise along x wraps its lattice (`noiseP`) with whole-number periods from `around`.
 */
export const BAKE_SHADER = `#version 300 es
precision highp float;
uniform vec2 uSize;
uniform float uPhase;
out vec4 outColor;

const float SPAN = ${f(SPAN)};

${HASH}
float noiseP(vec2 p, float period) {
  vec2 i = floor(p);
  vec2 t = fract(p);
  vec2 s = t * t * (3.0 - 2.0 * t);
  float x0 = mod(i.x, period);
  float x1 = mod(i.x + 1.0, period);
  float a = hash(vec2(x0, i.y));
  float b = hash(vec2(x1, i.y));
  float c = hash(vec2(x0, i.y + 1.0));
  float d = hash(vec2(x1, i.y + 1.0));
  return mix(mix(a, b, s.x), mix(c, d, s.x), s.y);
}
/** Three octaves at x frequencies n1, n2, n3 per turn; x is in turns. */
float fbmP(float x, float y, vec3 n) {
  return 0.5 * noiseP(vec2(x * n.x, y), n.x)
    + 0.3 * noiseP(vec2(x * n.y, y * 2.1), n.y)
    + 0.2 * noiseP(vec2(x * n.z, y * 4.3), n.z);
}

// Per vortex: centre (x, y), then semi-axes (x, y); spin; whether it's a bright oval.
const vec4 VORTEX[${VORTICES.length}] = vec4[](${VORTICES.map((v) => `vec4(${v.c.map(f).join(", ")}, ${v.ab.map(f).join(", ")})`).join(", ")});
const vec2 VORTEX_TURN[${VORTICES.length}] = vec2[](${VORTICES.map((v) => `vec2(${f(v.spin)}, ${f(v.bright)})`).join(", ")});

float clouds(vec2 q) {
  float oval = 0.0;
  for (int i = 0; i < ${VORTICES.length}; i++) {
    vec2 c = VORTEX[i].xy;
    vec2 d = q - c;
    d.x -= floor(d.x / SPAN + 0.5) * SPAN;
    vec2 e = d / VORTEX[i].zw;
    float r2 = dot(e, e);
    if (r2 > 9.0) continue;
    float a = exp(-r2 * 0.5) * VORTEX_TURN[i].x;
    q = c + vec2(d.x * cos(a) - d.y * sin(a), d.x * sin(a) + d.y * cos(a));
    oval += VORTEX_TURN[i].y * exp(-r2 * 1.2);
  }
  float t = q.x / SPAN;
  float warp = fbmP(t, q.y * 3.0 + uPhase * 2.0,
    vec3(${around(1.2)}, ${around(1.2 * 2.1)}, ${around(1.2 * 4.3)}));
  float belt = sin(q.y * 6.0 + warp * 3.2);
  float band = 0.5 + 0.5 * belt;
  // The warp shifts x by a fraction of a turn, so the lattice still wraps.
  float tw = t + warp * 2.0 / (3.0 * SPAN);
  float turb = fbmP(tw, q.y * 8.0 + warp,
    vec3(${around(3.0)}, ${around(3.0 * 2.1)}, ${around(3.0 * 4.3)}));
  float festoon = abs(belt) < 0.18
    ? 0.25 * fbmP(t, q.y * 9.0, vec3(${around(9.0)}, ${around(9.0 * 2.1)}, ${around(9.0 * 4.3)}))
    : 0.0;
  return clamp(0.18 + 0.32 * band + 0.38 * turb + festoon + 0.25 * oval, 0.0, 1.0);
}

void main() {
  vec2 uv = gl_FragCoord.xy / uSize;
  vec2 q = vec2(uv.x * SPAN, (uv.y * 2.0 - 1.0) * 3.14159265);
  outColor = vec4(clouds(q), 0.0, 0.0, 1.0);
}
`;

/**
 * One fragment per field cell of the planet's box. `uOrigin` is the box's top-left
 * cell in the viewport, so cells and the Bayer pattern count from the viewport.
 * `uDisc` is the planet from `planetDisc`, `uPhase` its rotation from `advancePhase`.
 *
 * Every layer yields a density: the baked cloud deck (`uCloudOld` giving way to
 * `uCloudNew` as `uFade` goes 0 to 1), a thin halo, an aurora curtain per live sub-agent and the flashes from
 * `writeFlashes`. The densest decides whether the cell is lit against the Bayer
 * threshold and which palette entry it takes; `uPalette` is already at `--dither-tint`.
 * `uBake` is the cloud texture's size, for the mip level.
 *
 * The clouds are fetched only on the lit side, so the fetch takes an explicit level:
 * implicit derivatives are undefined inside a branch.
 */
export const FIELD_SHADER = `#version 300 es
precision highp float;
uniform vec2 uSize;
uniform vec2 uOrigin;
uniform vec3 uDisc;
uniform float uTime;
uniform float uPhase;
uniform float uFade;
uniform float uAurora;
uniform int uFlashCount;
uniform vec4 uFlashes[${MAX_FLASHES}];
uniform vec3 uPalette[${STRIKE_TONES.length}];
uniform vec3 uGround;
uniform sampler2D uCloudOld;
uniform sampler2D uCloudNew;
uniform vec2 uBake;
out vec4 outColor;

const vec3 LIGHT = vec3(${f(LIGHT[0])}, ${f(LIGHT[1])}, ${f(LIGHT[2])});
const float TAU = 6.28318531;

float bayer2(vec2 a) {
  a = floor(a);
  return fract(a.x / 2.0 + a.y * a.y * 0.75);
}
float bayer4(vec2 a) { return bayer2(0.5 * a) * 0.25 + bayer2(a); }

${HASH}
float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 t = fract(p);
  vec2 s = t * t * (3.0 - 2.0 * t);
  float a = hash(i);
  float b = hash(i + vec2(1.0, 0.0));
  float c = hash(i + vec2(0.0, 1.0));
  float d = hash(i + vec2(1.0, 1.0));
  return mix(mix(a, b, s.x), mix(c, d, s.x), s.y);
}

/** The densest aurora curtain at a disc-relative point (unit radius). */
float aurora(vec2 d, float r) {
  // The curtains reach from about -2.1 to -0.9 rad: the top of the limb, either
  // side of the pole, where every one of them is on screen.
  if (uAurora < 0.5 || r < 0.9 || r > 1.16 || d.y > 0.0 || d.x < -0.6) return 0.0;
  float a = atan(d.y, d.x);
  float best = 0.0;
  // Curtains are 0.16 rad apart and reach 0.21, so only the nearest three can show.
  float near = floor((a - ${f(AURORA_FROM)}) / 0.16 + 0.5);
  for (int i = -1; i <= 1; i++) {
    float k = near + float(i);
    if (k < 0.0 || k >= uAurora) continue;
    float da = a - (${f(AURORA_FROM)} + 0.16 * k + 0.03 * sin(uTime * 0.07 + k));
    if (abs(da) > 0.18) continue;
    float span = exp(-(da * da) / 0.0081);
    float lift = r - 1.0 + 0.015 * sin(a * 30.0 + uTime * 0.2 + k);
    float curtain = lift < 0.0 ? exp(lift * 60.0) : exp(-lift * 18.0);
    float rays = 0.55 + 0.45 * sin(a * 260.0 + uTime * 0.6 + 3.0 * noise(vec2(a * 40.0, uTime * 0.1 + k)));
    best = max(best, 0.5 * span * curtain * rays * (0.7 + 0.3 * sin(uTime * 0.15 + k * 2.0)));
  }
  return best;
}

void main() {
  vec2 cell = vec2(gl_FragCoord.x - 0.5, uSize.y - gl_FragCoord.y - 0.5) + uOrigin;
  vec2 px = cell * ${f(CELL)} + ${f(CELL / 2)};
  vec2 d = (px - uDisc.xy) / uDisc.z;
  float r = length(d);

  if (r > ${f(HALO)}) {
    outColor = vec4(uGround, 1.0);
    return;
  }

  float body;
  if (r < 1.0) {
    vec3 n = vec3(d, sqrt(1.0 - r * r));
    float lit = dot(n, LIGHT);
    float shade = clamp(lit * 1.15, 0.0, 1.0);
    float cloud = 0.0;
    if (shade > 0.0) {
      float lat = asin(n.y);
      vec2 uv = vec2(
        (atan(n.x, n.z) + uPhase * (1.0 + ${f(SHEAR)} * sin(lat * 7.0))) / TAU,
        0.5 + lat / 3.14159265);
      // Texels per cell, from the sphere: longitude stretches by 1/z toward the limb,
      // latitude by 1/cos(lat). Analytic, so unlit cells and the halo skip the trig.
      float cells = uDisc.z / ${f(CELL)};
      float ring = sqrt(max(1.0 - n.y * n.y, 1e-4));
      float lod = log2(max(uBake.x / (TAU * cells * max(n.z, 1e-3)), uBake.y / (3.14159265 * cells * ring)));
      // The crossfade is dithered in 8-cell blocks: a block takes one bake or the
      // other, so a GPU wave fetches once. Outside a fade the branch is uniform.
      vec2 block = floor(cell / 8.0);
      if (fract(dot(block, vec2(0.7548777, 0.5698403))) < uFade)
        cloud = textureLod(uCloudNew, uv, lod).r;
      else
        cloud = textureLod(uCloudOld, uv, lod).r;
    }
    body = shade * cloud + (1.0 - n.z) * 0.12 * clamp(lit + 0.4, 0.0, 1.0);
  } else {
    body = 0.4 * exp(-(r - 1.0) * 14.0) * clamp(dot(d, LIGHT.xy) / r + 0.45, 0.0, 1.0);
  }
  float glow = aurora(d, r);

  float flash = 0.0;
  float tone = 0.0;
  for (int i = 0; i < ${MAX_FLASHES}; i++) {
    if (i >= uFlashCount) break;
    vec4 s = uFlashes[i];
    vec2 off = (px - s.xy) / ${f(CELL)};
    float d2 = dot(off, off);
    if (d2 > s.z * ${f(FLASH_REACH)}) continue;
    float v = fract(s.w) * exp(-d2 / s.z);
    if (v > flash) { flash = v; tone = floor(s.w); }
  }
  if (flash > 0.0) flash *= 0.6 + 0.5 * noise(cell * 0.4);

  float v = max(body, max(glow, flash));
  if (v <= bayer4(cell) + 1.0 / 32.0) {
    outColor = vec4(uGround, 1.0);
    return;
  }
  int pick = flash >= v ? int(tone) : glow >= v ? ${TONE_INDEX[AURORA_TONE]} : 0;
  outColor = vec4(uPalette[pick], 1.0);
}
`;
