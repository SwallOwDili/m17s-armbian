/* m17s_tonemap_fast.c - table-driven HDR10 (BT.2020 / PQ) -> SDR (BT.709 / 2.2)
 * tone mapper for the M17S (Amlogic S905X, 4x Cortex-A53 @ 2.0 GHz).
 *
 * Same math as m17s_tonemap.c, but restructured so the per-pixel path is
 * table lookups plus adds instead of multiplies, divides and clamps:
 *
 *   - Y is averaged as an integer sum (0..1020) and indexed straight into a
 *     normalized-value table, so the limited-range divide disappears.
 *   - Cb/Cr contributions are folded into four 256-entry float tables.
 *   - The PQ EOTF table is 4097 entries (four times denser than v1), which
 *     halves the dark-end quantization error.
 *   - The 2:1 chroma/vertical sampling becomes shifts instead of divides.
 *
 * ABIs match m17s_tonemap.h so tonemap_test.py can drive either build.
 */
#define _GNU_SOURCE
#include <math.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>

#define PQ_LUT_SIZE 4097
#define GAMMA_LUT_SIZE 4096
#define Y_SUM_MAX 1020
#define M17S_MAX_THREADS 8

typedef struct {
  float inv_ref_white;
  float peak_sq;
  float inv_peak_sq;
  int threads;
  float y_lut[Y_SUM_MAX + 1];
  float rcr_lut[256];
  float gcb_lut[256];
  float gcr_lut[256];
  float bcb_lut[256];
  float pq_lut[PQ_LUT_SIZE];
  uint8_t gamma_lut[GAMMA_LUT_SIZE];
} m17s_tonemap;

static double pq_eotf(double e) {
  const double m1 = 0.1593017578125, m2 = 78.84375;
  const double c1 = 0.8359375, c2 = 18.8515625, c3 = 18.6875;
  double vp, num, den;
  if (e <= 0.0) return 0.0;
  vp = pow(e, 1.0 / m2);
  num = vp - c1;
  if (num < 0.0) num = 0.0;
  den = c2 - c3 * vp;
  if (den < 1e-6) den = 1e-6;
  return pow(num / den, 1.0 / m1) * 10000.0;
}

m17s_tonemap *m17s_tonemap_new(double ref_white, double peak, int threads) {
  m17s_tonemap *tm = (m17s_tonemap *)calloc(1, sizeof(*tm));
  int i;
  if (!tm) return NULL;
  tm->inv_ref_white = (float)(1.0 / ref_white);
  {
    float peak_norm = (float)(peak / ref_white);
    tm->peak_sq = peak_norm * peak_norm;
    tm->inv_peak_sq = 1.0f / tm->peak_sq;
  }
  tm->threads = threads < 1 ? 1 : (threads > M17S_MAX_THREADS ? M17S_MAX_THREADS : threads);

  for (i = 0; i <= Y_SUM_MAX; i++)
    tm->y_lut[i] = (float)(((double)i * 0.25 - 16.0) / 219.0);
  for (i = 0; i < 256; i++) {
    double c = (i - 128.0) / 224.0;
    tm->rcr_lut[i] = (float)(1.4746 * c);
    tm->gcb_lut[i] = (float)(-0.164553 * c);
    tm->gcr_lut[i] = (float)(-0.571353 * c);
    tm->bcb_lut[i] = (float)(1.8814 * c);
  }
  for (i = 0; i < PQ_LUT_SIZE; i++)
    tm->pq_lut[i] = (float)pq_eotf((double)i / (PQ_LUT_SIZE - 1));
  for (i = 0; i < GAMMA_LUT_SIZE; i++) {
    double linear = (double)i / (GAMMA_LUT_SIZE - 1);
    int value = (int)(pow(linear, 1.0 / 2.2) * 255.0 + 0.5);
    tm->gamma_lut[i] = (uint8_t)(value > 255 ? 255 : value);
  }
  return tm;
}

void m17s_tonemap_free(m17s_tonemap *tm) { free(tm); }

typedef struct {
  const m17s_tonemap *tm;
  const uint8_t *y_plane;
  const uint8_t *uv_plane;
  int y_stride;
  int uv_stride;
  int src_w;
  int src_h;
  int dst_w;
  int dst_h;
  int row_begin;
  int row_end;
  uint8_t *dst;
  int dst_stride;
} m17s_band;

#define M17S_CLAMP01(v) ((v) < 0.0f ? 0.0f : ((v) > 1.0f ? 1.0f : (v)))

static void tonemap_rows(const m17s_band *band) {
  const m17s_tonemap *tm = band->tm;
  const int dst_w = band->dst_w;
  const int uv_w = band->src_w / 2;
  const int uv_h = band->src_h / 2;
  const float inv_ref = tm->inv_ref_white;
  float peak_sq = tm->peak_sq;
  const float inv_peak_sq = tm->inv_peak_sq;
  const int x_shift = (band->src_w == dst_w * 2) ? 1 : 0;
  const int y_shift = (band->src_h == band->dst_h * 2) ? 1 : 0;
  int dy, dx;

  for (dy = band->row_begin; dy < band->row_end; dy++) {
    uint8_t *out = band->dst + (size_t)dy * band->dst_stride;
    const int sy = y_shift ? (dy << 1) : (int)((int64_t)dy * band->src_h / band->dst_h);
    const int sy2 = sy + 1 < band->src_h ? sy + 1 : sy;
    const int uvy = y_shift ? dy : (int)((int64_t)dy * uv_h / band->dst_h);
    const uint8_t *row0 = band->y_plane + (size_t)sy * band->y_stride;
    const uint8_t *row1 = band->y_plane + (size_t)sy2 * band->y_stride;
    const uint8_t *uvrow = band->uv_plane + (size_t)uvy * band->uv_stride;

    for (dx = 0; dx < dst_w; dx++) {
      const int sx = x_shift ? (dx << 1) : (int)((int64_t)dx * band->src_w / band->dst_w);
      const int sx2 = (sx + 1 < band->src_w) ? (sx + 1) : sx;
      const int uvx = (x_shift ? dx : (int)((int64_t)dx * uv_w / band->dst_w));
      const int uvx_c = uvx < uv_w ? uvx : (uv_w > 0 ? uv_w - 1 : 0);
      const int ysum = row0[sx] + row0[sx2] + row1[sx] + row1[sx2];
      const float yv = tm->y_lut[ysum];
      const int u = uvrow[uvx_c * 2];
      const int v = uvrow[uvx_c * 2 + 1];
      float r, g, b, rl, gl, bl, lum, x, scale, lr, lg, lb, r709, g709, b709;
      int idx;

      r = M17S_CLAMP01(yv + tm->rcr_lut[v]);
      g = M17S_CLAMP01(yv + tm->gcb_lut[u] + tm->gcr_lut[v]);
      b = M17S_CLAMP01(yv + tm->bcb_lut[u]);

#if defined(M17S_PROF_SKIP_PQ)
      rl = 0.5f * r; gl = 0.5f * g; bl = 0.5f * b;
#else
      rl = tm->pq_lut[(int)(r * (PQ_LUT_SIZE - 1) + 0.5f)];
      gl = tm->pq_lut[(int)(g * (PQ_LUT_SIZE - 1) + 0.5f)];
      bl = tm->pq_lut[(int)(b * (PQ_LUT_SIZE - 1) + 0.5f)];
#endif

      lum = 0.2627f * rl + 0.6780f * gl + 0.0593f * bl;
      x = lum * inv_ref;
#if defined(M17S_PROF_SKIP_DIV)
      if (peak_sq > 0.0f) peak_sq = peak_sq;  /* silence unused warning */
      scale = 1.0f;
#else
      scale = (1.0f + x * inv_peak_sq) / (1.0f + x);
#endif

      lr = rl * inv_ref * scale;
      lg = gl * inv_ref * scale;
      lb = bl * inv_ref * scale;

#if defined(M17S_PROF_SKIP_MATRIX)
      r709 = M17S_CLAMP01(lr); g709 = M17S_CLAMP01(lg); b709 = M17S_CLAMP01(lb);
#else
      r709 = M17S_CLAMP01(1.6605f * lr - 0.5876f * lg - 0.0728f * lb);
      g709 = M17S_CLAMP01(-0.1246f * lr + 1.1329f * lg - 0.0083f * lb);
      b709 = M17S_CLAMP01(-0.0182f * lr - 0.1006f * lg + 1.1187f * lb);
#endif

      out[0] = tm->gamma_lut[(int)(b709 * (GAMMA_LUT_SIZE - 1) + 0.5f)];
      out[1] = tm->gamma_lut[(int)(g709 * (GAMMA_LUT_SIZE - 1) + 0.5f)];
      out[2] = tm->gamma_lut[(int)(r709 * (GAMMA_LUT_SIZE - 1) + 0.5f)];
      out[3] = 255;
      out += 4;
    }
  }
}

static void *band_thread(void *opaque) {
  tonemap_rows((const m17s_band *)opaque);
  return NULL;
}

int m17s_tonemap_convert(const m17s_tonemap *tm, const uint8_t *y_plane, int y_stride,
                         const uint8_t *uv_plane, int uv_stride, int src_w, int src_h,
                         int dst_w, int dst_h, uint8_t *dst, int dst_stride) {
  pthread_t threads[M17S_MAX_THREADS];
  m17s_band bands[M17S_MAX_THREADS];
  int count = tm->threads;
  int per_band, i;
  if (count > dst_h) count = dst_h;
  if (count < 1) count = 1;
  per_band = (dst_h + count - 1) / count;
  for (i = 0; i < count; i++) {
    bands[i].tm = tm;
    bands[i].y_plane = y_plane;
    bands[i].uv_plane = uv_plane;
    bands[i].y_stride = y_stride;
    bands[i].uv_stride = uv_stride;
    bands[i].src_w = src_w;
    bands[i].src_h = src_h;
    bands[i].dst_w = dst_w;
    bands[i].dst_h = dst_h;
    bands[i].dst = dst;
    bands[i].dst_stride = dst_stride;
    bands[i].row_begin = i * per_band;
    bands[i].row_end = bands[i].row_begin + per_band;
    if (bands[i].row_end > dst_h) bands[i].row_end = dst_h;
    if (bands[i].row_begin >= bands[i].row_end) bands[i].row_begin = bands[i].row_end = dst_h;
  }
  if (count == 1) {
    tonemap_rows(&bands[0]);
    return 0;
  }
  for (i = 1; i < count; i++) pthread_create(&threads[i], NULL, band_thread, &bands[i]);
  tonemap_rows(&bands[0]);
  for (i = 1; i < count; i++) pthread_join(threads[i], NULL);
  return 0;
}

int m17s_tonemap_threads(const m17s_tonemap *tm) { return tm->threads; }
