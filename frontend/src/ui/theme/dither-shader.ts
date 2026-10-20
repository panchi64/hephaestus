import {
  BASE,
  CELL,
  DENSITY_MAX,
  DRIFT,
  GLOW_LIFE_MS,
  GLOW_REACH,
  GLOW_RISE_MS,
  LOAD_CAP,
  MAX_PULSES,
  PER_LOAD,
  PULSE_TONES,
} from "./dither";

/** A GLSL float literal: ES 1.00 has no implicit int-to-float conversion. */
const f = (n: number): string => n.toFixed(4);

export const VERTEX_SHADER = `
attribute vec2 aPos;
void main() { gl_Position = vec4(aPos, 0.0, 1.0); }
`;

/**
 * One fragment per field cell. `uPulses[i]` is (x px, y px, age ms, tone index), with a
 * negative age for an empty slot. Cells are counted from the top left, as the pulses'
 * viewport coordinates are; `gl_FragCoord` counts from the bottom left.
 *
 * A glow never changes which cells are lit. It moves lit cells from `uPalette[0]`
 * toward its tone's colour by `k`: a gaussian around the source, eased in, then faded.
 * `uPalette` already holds each tone at full tint, so `k` scales the tint toward 0.
 *
 * The 4×4 Bayer threshold is built from the 2×2 one (`fract(x/2 + y²·¾)`), because
 * GLSL ES 1.00 has neither constant arrays nor integer bit operations.
 */
export const FRAGMENT_SHADER = `
precision highp float;
uniform vec2 uSize;
uniform float uTime;
uniform float uLoad;
uniform vec4 uPulses[${MAX_PULSES}];
uniform vec3 uPalette[${PULSE_TONES.length}];
uniform vec3 uGround;

float bayer2(vec2 a) {
  a = floor(a);
  return fract(a.x / 2.0 + a.y * a.y * 0.75);
}
float bayer4(vec2 a) { return bayer2(0.5 * a) * 0.25 + bayer2(a); }

void main() {
  vec2 cell = vec2(gl_FragCoord.x - 0.5, uSize.y - gl_FragCoord.y - 0.5);
  float a = sin(cell.x * 0.016 + uTime * 0.07) * sin(cell.y * 0.021 - uTime * 0.05);
  float b = sin((cell.x + cell.y) * 0.0095 + uTime * 0.035);
  float drift = 0.5 + 0.5 * (0.6 * a + 0.4 * b);
  float steps = min(max(floor(uLoad), 0.0), ${f(LOAD_CAP)});
  float v = min(${f(BASE)} + ${f(DRIFT)} * drift + ${f(PER_LOAD)} * steps, ${f(DENSITY_MAX)});

  if (v <= bayer4(cell) + 1.0 / 32.0) {
    gl_FragColor = vec4(uGround, 1.0);
    return;
  }

  vec2 px = cell * ${f(CELL)} + ${f(CELL / 2)};
  float strongest = 0.0;
  float tone = 0.0;
  for (int i = 0; i < ${MAX_PULSES}; i++) {
    vec4 p = uPulses[i];
    if (p.z < 0.0 || p.z > ${f(GLOW_LIFE_MS)}) continue;
    float d = distance(px, p.xy);
    if (d > ${f(GLOW_REACH * 3)}) continue;
    float rise = min(p.z / ${f(GLOW_RISE_MS)}, 1.0);
    float fall = 1.0 - max(p.z - ${f(GLOW_RISE_MS)}, 0.0) / ${f(GLOW_LIFE_MS - GLOW_RISE_MS)};
    float k = rise * fall * exp(-(d * d) / ${f(2 * GLOW_REACH * GLOW_REACH)});
    if (k > strongest) { strongest = k; tone = p.w; }
  }

  vec3 c = uPalette[0];
  for (int t = 1; t < ${PULSE_TONES.length}; t++) {
    if (abs(tone - float(t)) < 0.5) c = uPalette[t];
  }
  gl_FragColor = vec4(mix(uPalette[0], c, strongest), 1.0);
}
`;
