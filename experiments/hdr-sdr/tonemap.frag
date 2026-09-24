// HDR10 (BT.2020 / PQ) to SDR (BT.709 / gamma 2.2) tone mapping for M17S.
//
// Input : RGBA texture produced by glcolorconvert from the 8-bit NV12 frame.
//         Channel values are PQ-encoded (SMPTE ST 2084) R'G'B'.
// Output: SDR BT.709 with a 2.2 display gamma, reference white = 100 cd/m2.
precision highp float;
varying vec2 v_texcoord;
uniform sampler2D tex;

#define PQ_M1 0.1593017578125
#define PQ_M2 78.84375
#define PQ_C1 0.8359375
#define PQ_C2 18.8515625
#define PQ_C3 18.6875
#define REF_WHITE 100.0
#define TONE_PEAK 1000.0

vec3 pq_eotf_nits(vec3 e) {
  vec3 v = clamp(e, 0.0, 1.0);
  vec3 vp = pow(v, vec3(1.0 / PQ_M2));
  vec3 num = max(vp - PQ_C1, 0.0);
  vec3 den = max(PQ_C2 - PQ_C3 * vp, 1e-6);
  return pow(num / den, vec3(1.0 / PQ_M1)) * 10000.0;
}

void main() {
  vec3 nits = pq_eotf_nits(texture2D(tex, v_texcoord).rgb);

  // Luminance-preserving Reinhard-with-white-point tone curve.
  float lum = max(dot(nits, vec3(0.2627, 0.6780, 0.0593)), 0.0);
  float x = lum / REF_WHITE;
  float peak = TONE_PEAK / REF_WHITE;
  float mapped = x * (1.0 + x / (peak * peak)) / (1.0 + x);
  float scale = x > 1e-6 ? mapped / x : 1.0;

  // BT.2020 primaries to BT.709 primaries in linear light.
  vec3 linear = (nits / REF_WHITE) * scale;
  vec3 bt709 = vec3(
      dot(linear, vec3( 1.6605, -0.5876, -0.0728)),
      dot(linear, vec3(-0.1246,  1.1329, -0.0083)),
      dot(linear, vec3(-0.0182, -0.1006,  1.1187)));

  gl_FragColor = vec4(pow(clamp(bt709, 0.0, 1.0), vec3(1.0 / 2.2)), 1.0);
}
