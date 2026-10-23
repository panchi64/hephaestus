import {
  AURORA_MAX,
  AURORA_TONE,
  CELL,
  FLASHES,
  FLASH_MS,
  FLASH_SPREAD_MS,
  LIGHT,
  MAX_STRIKES,
  STRIKE_LIFE_MS,
  STRIKE_TONES,
} from "./dither";

/** A GLSL float literal: ES 1.00 has no implicit int-to-float conversion. */
const f = (n: number): string => n.toFixed(4);

export const VERTEX_SHADER = `
attribute vec2 aPos;
void main() { gl_Position = vec4(aPos, 0.0, 1.0); }
`;

/**
 * One fragment per field cell. Positions are viewport CSS px from the top left;
 * `gl_FragCoord` counts from the bottom left. `uDisc` is the planet (centre x, centre
 * y, radius) from `planetDisc`, `uPhase` its rotation from `advancePhase`, and
 * `uStrikes[i]` is (x, y, age ms, tone index) with a negative age for an empty slot.
 *
 * Every layer yields a density: the turbulent cloud deck on the disc, a thin halo
 * beyond it, an aurora curtain per live sub-agent and a strike's flashes. The densest
 * one decides whether the cell is lit against the Bayer threshold and which palette
 * entry it takes; `uPalette` holds each tone already at `--dither-tint`.
 *
 * The 4×4 Bayer threshold is built from the 2×2 one (`fract(x/2 + y²·¾)`), because
 * GLSL ES 1.00 has neither constant arrays nor integer bit operations.
 */
export const FRAGMENT_SHADER = `
precision highp float;
uniform vec2 uSize;
uniform vec3 uDisc;
uniform float uTime;
uniform float uPhase;
uniform float uAurora;
uniform vec4 uStrikes[${MAX_STRIKES}];
uniform vec3 uPalette[${STRIKE_TONES.length}];
uniform vec3 uGround;

const vec3 LIGHT = vec3(${f(LIGHT[0])}, ${f(LIGHT[1])}, ${f(LIGHT[2])});
const float SPAN = 12.5664;

float bayer2(vec2 a) {
  a = floor(a);
  return fract(a.x / 2.0 + a.y * a.y * 0.75);
}
float bayer4(vec2 a) { return bayer2(0.5 * a) * 0.25 + bayer2(a); }

float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
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
float fbm(vec2 p) {
  return 0.5 * noise(p) + 0.3 * noise(p * 2.1) + 0.2 * noise(p * 4.3);
}

/** The cloud deck at a disc normal: banded, warped, rolled around nine vortices. */
float clouds(vec3 n) {
  float lat = asin(n.y);
  float zonal = sin(lat * 7.0);
  vec2 q = vec2((atan(n.x, n.z) + uPhase * (1.0 + 0.3 * zonal)) * 2.0, lat * 2.0);
  float oval = 0.0;
  for (int i = 0; i < 9; i++) {
    float k = float(i);
    vec2 c = vec2(hash(vec2(k, 11.0)) * SPAN, -0.4 - 1.8 * hash(vec2(k, 12.0)));
    vec2 ab = vec2(0.18 + 0.3 * hash(vec2(k, 13.0)), 0.1 + 0.12 * hash(vec2(k, 14.0)));
    float spin = (hash(vec2(k, 15.0)) < 0.5 ? -1.0 : 1.0) * (2.0 + 3.0 * hash(vec2(k, 16.0)));
    vec2 d = q - c;
    d.x -= floor(d.x / SPAN + 0.5) * SPAN;
    vec2 e = d / ab;
    float r2 = dot(e, e);
    if (r2 > 9.0) continue;
    float a = exp(-r2 * 0.5) * spin;
    q = c + vec2(d.x * cos(a) - d.y * sin(a), d.x * sin(a) + d.y * cos(a));
    oval += (hash(vec2(k, 17.0)) < 0.5 ? -1.0 : 1.0) * exp(-r2 * 1.2);
  }
  float warp = fbm(vec2(q.x * 1.2, q.y * 3.0 + uPhase * 2.0));
  float belt = sin(q.y * 6.0 + warp * 3.2);
  float band = 0.5 + 0.5 * belt;
  float turb = fbm(vec2(q.x * 3.0 + warp * 2.0, q.y * 8.0 + warp));
  float festoon = abs(belt) < 0.18 ? 0.25 * fbm(q * 9.0) : 0.0;
  return clamp(0.18 + 0.32 * band + 0.38 * turb + festoon + 0.25 * oval, 0.0, 1.0);
}

/** The densest aurora curtain at a disc-relative point (unit radius). */
float aurora(vec2 d, float r) {
  if (r < 0.9 || r > 1.16) return 0.0;
  float a = atan(d.y, d.x);
  float best = 0.0;
  for (int i = 0; i < ${AURORA_MAX}; i++) {
    float k = float(i);
    if (k >= uAurora) break;
    float da = a - (-1.5 + 0.16 * k + 0.03 * sin(uTime * 0.07 + k));
    float span = exp(-(da * da) / 0.0081);
    if (span < 0.02) continue;
    float lift = r - 1.0 + 0.015 * sin(a * 30.0 + uTime * 0.2 + k);
    float curtain = lift < 0.0 ? exp(lift * 60.0) : exp(-lift * 18.0);
    float rays = 0.55 + 0.45 * sin(a * 260.0 + uTime * 0.6 + 3.0 * noise(vec2(a * 40.0, uTime * 0.1 + k)));
    best = max(best, 0.5 * span * curtain * rays * (0.7 + 0.3 * sin(uTime * 0.15 + k * 2.0)));
  }
  return best;
}

void main() {
  vec2 cell = vec2(gl_FragCoord.x - 0.5, uSize.y - gl_FragCoord.y - 0.5);
  vec2 px = cell * ${f(CELL)} + ${f(CELL / 2)};
  vec2 d = (px - uDisc.xy) / uDisc.z;
  float r = length(d);
  if (r > 1.2) {
    gl_FragColor = vec4(uGround, 1.0);
    return;
  }

  float body;
  if (r < 1.0) {
    vec3 n = vec3(d, sqrt(1.0 - r * r));
    float lit = dot(n, LIGHT);
    body = clamp(lit * 1.15, 0.0, 1.0) * clouds(n) + (1.0 - n.z) * 0.12 * clamp(lit + 0.4, 0.0, 1.0);
  } else {
    body = 0.4 * exp(-(r - 1.0) * 14.0) * clamp(dot(d, LIGHT.xy) / r + 0.45, 0.0, 1.0);
  }
  float glow = aurora(d, r);

  float flash = 0.0;
  float tone = 0.0;
  for (int i = 0; i < ${MAX_STRIKES}; i++) {
    vec4 s = uStrikes[i];
    if (s.z < 0.0 || s.z > ${f(STRIKE_LIFE_MS)}) continue;
    float seed = s.x * 0.37 + s.y * 0.91;
    for (int j = 0; j < ${FLASHES}; j++) {
      float k = float(j);
      float age = s.z - hash(vec2(seed, k)) * ${f(FLASH_SPREAD_MS)};
      if (age < 0.0 || age > ${f(FLASH_MS)}) continue;
      vec2 at = s.xy + (vec2(hash(vec2(seed, k + 10.0)), hash(vec2(seed, k + 20.0))) - 0.5) * 80.0;
      vec2 off = (px - at) / ${f(CELL)};
      float spread = 60.0 + 140.0 * hash(vec2(seed, k + 30.0));
      float beat = floor(age / 100.0);
      float flicker = (beat == 0.0 || beat == 2.0 || beat == 5.0 || beat == 9.0) ? 1.0 : 0.25;
      float v = flicker * (1.0 - age / ${f(FLASH_MS)}) * 0.85 * exp(-dot(off, off) / spread)
        * (0.6 + 0.5 * noise(cell * 0.4 + seed));
      if (v > flash) { flash = v; tone = s.w; }
    }
  }

  float v = max(body, max(glow, flash));
  if (v <= bayer4(cell) + 1.0 / 32.0) {
    gl_FragColor = vec4(uGround, 1.0);
    return;
  }
  float pick = flash >= v ? tone : glow >= v ? ${f(STRIKE_TONES.indexOf(AURORA_TONE))} : 0.0;
  vec3 c = uPalette[0];
  for (int t = 1; t < ${STRIKE_TONES.length}; t++) {
    if (abs(pick - float(t)) < 0.5) c = uPalette[t];
  }
  gl_FragColor = vec4(c, 1.0);
}
`;
