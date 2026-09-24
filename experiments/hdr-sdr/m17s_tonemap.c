/* m17s_tonemap.c - HDR10 (BT.2020 / PQ) to SDR (BT.709 / gamma 2.2) tone mapper.
 *
 * Input : NV12, 4:2:0, limited range, BT.2020 non-constant luminance, PQ coded
 *         (this is what the M17S VPU emits for the HDR10 test film).
 * Output: BGRA 8-bit, BT.709 primaries, 2.2 display gamma, reference white
 *         100 cd/m2, luminance-preserving Reinhard-with-white-point curve.
 *
 * The math matches docs/MULTIMEDIA.md and reference_render.py.
 */
#define _GNU_SOURCE
#include <math.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>

#define PQ_LUT_BITS 11
#define PQ_LUT_SIZE (1 << PQ_LUT_BITS)
#define GAMMA_LUT_BITS 12
#define GAMMA_LUT_SIZE (1 << GAMMA_LUT_BITS)
#define M17S_MAX_THREADS 8

typedef struct {
  float ref_white;
  float inv_ref_white;
  float peak_sq;
  int threads;
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
  tm->ref_white = (float)ref_white;
  tm->inv_ref_white = (float)(1.0 / ref_white);
  {
    float peak_norm = (float)(peak / ref_white);
    tm->peak_sq = peak_norm * peak_norm;
  }
  tm->threads = threads < 1 ? 1 : (threads > M17S_MAX_THREADS ? M17S_MAX_THREADS : threads);
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

static void tonemap_rows(const m17s_band *band) {
  const m17s_tonemap *tm = band->tm;
  const int dst_w = band->dst_w;
  const int uv_w = band->src_w / 2;
  const int uv_h = band->src_h / 2;
  int dy, dx;
  for (dy = band->row_begin; dy < band->row_end; dy++) {
    uint8_t *out = band->dst + (size_t)dy * band->dst_stride;
    const int sy = (int)((int64_t)dy * band->src_h / band->dst_h);
    const int sy2 = sy + 1 < band->src_h ? sy + 1 : sy;
    const int uvy = (int)((int64_t)dy * uv_h / band->dst_h);
    const uint8_t *row0 = band->y_plane + (size_t)sy * band->y_stride;
    const uint8_t *row1 = band->y_plane + (size_t)sy2 * band->y_stride;
    const uint8_t *uvrow = band->uv_plane + (size_t)uvy * band->uv_stride;
    for (dx = 0; dx < dst_w; dx++) {
      const int sx = (int)((int64_t)dx * band->src_w / band->dst_w);
      const int sx2 = sx + 1 < band->src_w ? sx + 1 : sx;
      const int uvx = (int)((int64_t)dx * uv_w / band->dst_w);
      float yv, cb, cr, r, g, b, rl, gl, bl, lum, x, scale;
      float lr, lg, lb, r709, g709, b709;
      int idx;
      yv = (float)(row0[sx] + row0[sx2] + row1[sx] + row1[sx2]) * 0.25f;
      yv = (yv - 16.0f) * (1.0f / 219.0f);
      cb = ((float)uvrow[uvx * 2] - 128.0f) * (1.0f / 224.0f);
      cr = ((float)uvrow[uvx * 2 + 1] - 128.0f) * (1.0f / 224.0f);
      r = yv + 1.4746f * cr;
      g = yv - 0.164553f * cb - 0.571353f * cr;
      b = yv + 1.8814f * cb;
      r = r < 0.0f ? 0.0f : (r > 1.0f ? 1.0f : r);
      g = g < 0.0f ? 0.0f : (g > 1.0f ? 1.0f : g);
      b = b < 0.0f ? 0.0f : (b > 1.0f ? 1.0f : b);
      rl = tm->pq_lut[(int)(r * (PQ_LUT_SIZE - 1) + 0.5f)];
      gl = tm->pq_lut[(int)(g * (PQ_LUT_SIZE - 1) + 0.5f)];
      bl = tm->pq_lut[(int)(b * (PQ_LUT_SIZE - 1) + 0.5f)];
      lum = 0.2627f * rl + 0.6780f * gl + 0.0593f * bl;
      x = lum * tm->inv_ref_white;
      if (x > 0.0f) {
        scale = (1.0f + x / tm->peak_sq) / (1.0f + x);
      } else {
        scale = 1.0f;
      }
      lr = rl * tm->inv_ref_white * scale;
      lg = gl * tm->inv_ref_white * scale;
      lb = bl * tm->inv_ref_white * scale;
      r709 = 1.6605f * lr - 0.5876f * lg - 0.0728f * lb;
      g709 = -0.1246f * lr + 1.1329f * lg - 0.0083f * lb;
      b709 = -0.0182f * lr - 0.1006f * lg + 1.1187f * lb;
      r709 = r709 < 0.0f ? 0.0f : (r709 > 1.0f ? 1.0f : r709);
      g709 = g709 < 0.0f ? 0.0f : (g709 > 1.0f ? 1.0f : g709);
      b709 = b709 < 0.0f ? 0.0f : (b709 > 1.0f ? 1.0f : b709);
      idx = (int)(b709 * (GAMMA_LUT_SIZE - 1) + 0.5f);
      out[0] = tm->gamma_lut[idx];
      idx = (int)(g709 * (GAMMA_LUT_SIZE - 1) + 0.5f);
      out[1] = tm->gamma_lut[idx];
      idx = (int)(r709 * (GAMMA_LUT_SIZE - 1) + 0.5f);
      out[2] = tm->gamma_lut[idx];
      out[3] = 255;
      out += 4;
    }
  }
}

static void *band_thread(void *opaque) {
  tonemap_rows((const m17s_band *)opaque);
  return NULL;
}

/* Returns 0 on success. Strides are in bytes, dst_stride covers one dst row. */
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
