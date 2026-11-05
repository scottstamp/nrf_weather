/*
 * SPDX-License-Identifier: Apache-2.0
 */
#include "framebuf.h"

#include <string.h>
#include <zephyr/sys/util.h>

#include "font_gen.h"

BUILD_ASSERT(CONFIG_WX_ROTATION == 90 || CONFIG_WX_ROTATION == 270,
	     "CONFIG_WX_ROTATION must be 90 or 270");

/*
 * Landscape (x in 0..295, y in 0..127) -> native (nx in 0..127, ny in 0..295).
 * Must match host/wxd/render.py to_planes():
 *   rotate=90  (landscape rotated 90 deg CCW into portrait): nx = y,       ny = 295 - x
 *   rotate=270 (90 deg CW):                                 nx = 127 - y, ny = x
 */
static inline void land_to_native(int x, int y, int *nx, int *ny)
{
#if CONFIG_WX_ROTATION == 90
	*nx = y;
	*ny = (EPD_HEIGHT - 1) - x;
#else
	*nx = (EPD_WIDTH - 1) - y;
	*ny = x;
#endif
}

void fb_clear(uint8_t *fb)
{
	memset(fb, 0xff, EPD_PLANE_BYTES);                  /* all white */
	memset(fb + EPD_PLANE_BYTES, 0x00, EPD_PLANE_BYTES); /* no red */
}

void fb_pixel(uint8_t *fb, int x, int y, enum fb_color c)
{
	int nx, ny;

	if (x < 0 || y < 0 || x >= FB_LAND_W || y >= FB_LAND_H) {
		return;
	}
	land_to_native(x, y, &nx, &ny);

	size_t idx = (size_t)ny * EPD_ROW_BYTES + (size_t)(nx / 8);
	uint8_t mask = 0x80 >> (nx % 8);
	uint8_t *bw = fb;
	uint8_t *red = fb + EPD_PLANE_BYTES;

	switch (c) {
	case FB_WHITE:
		bw[idx] |= mask;
		red[idx] &= ~mask;
		break;
	case FB_BLACK:
		bw[idx] &= ~mask;
		red[idx] &= ~mask;
		break;
	case FB_RED:
		bw[idx] |= mask; /* white underneath; red wins anyway */
		red[idx] |= mask;
		break;
	}
}

void fb_fill_rect(uint8_t *fb, int x, int y, int w, int h, enum fb_color c)
{
	for (int j = y; j < y + h; j++) {
		for (int i = x; i < x + w; i++) {
			fb_pixel(fb, i, j, c);
		}
	}
}

void fb_frame(uint8_t *fb, int t, enum fb_color c)
{
	fb_fill_rect(fb, 0, 0, FB_LAND_W, t, c);
	fb_fill_rect(fb, 0, FB_LAND_H - t, FB_LAND_W, t, c);
	fb_fill_rect(fb, 0, 0, t, FB_LAND_H, c);
	fb_fill_rect(fb, FB_LAND_W - t, 0, t, FB_LAND_H, c);
}

static int glyph_index(char ch)
{
	static const char chars[] = FONT_CHARS;

	if (ch >= 'a' && ch <= 'z') {
		ch = (char)(ch - 'a' + 'A');
	}
	for (int i = 0; chars[i]; i++) {
		if (chars[i] == ch) {
			return i;
		}
	}
	return 0; /* space */
}

int fb_text_width(const char *s, int scale)
{
	int w = 0;

	for (; *s; s++) {
		w += font_glyphs[glyph_index(*s)].width * scale;
	}
	return w;
}

int fb_text_height(int scale)
{
	return FONT_HEIGHT * scale;
}

void fb_text(uint8_t *fb, int x, int y, const char *s, enum fb_color c, int scale)
{
	for (; *s; s++) {
		const struct font_glyph *g = &font_glyphs[glyph_index(*s)];
		const uint8_t *bm = &font_bitmap[g->offset];
		int rb = (g->width + 7) / 8;

		for (int gy = 0; gy < FONT_HEIGHT; gy++) {
			for (int gx = 0; gx < g->width; gx++) {
				if (bm[gy * rb + gx / 8] & (0x80 >> (gx % 8))) {
					fb_fill_rect(fb, x + gx * scale, y + gy * scale, scale,
						     scale, c);
				}
			}
		}
		x += g->width * scale;
	}
}

void fb_draw_splash(uint8_t *fb, const char *line1, const char *line2)
{
	fb_clear(fb);
	fb_fill_rect(fb, 0, 0, FB_LAND_W, 8, FB_RED);

	int w1 = fb_text_width(line1, 2);
	int w2 = fb_text_width(line2, 1);

	fb_text(fb, (FB_LAND_W - w1) / 2, 22, line1, FB_BLACK, 2);
	fb_text(fb, (FB_LAND_W - w2) / 2, 22 + fb_text_height(2) + 10, line2, FB_BLACK, 1);
}

/*
 * Red badge with white text, anchored to the bottom-right corner: in the host
 * layout (docs/layout.md) that is the observation time + battery, which is
 * exactly the information an OFFLINE / LOW BATTERY badge supersedes.
 */
static void badge(uint8_t *fb, const char *text)
{
	const int pad = 4;
	int w = fb_text_width(text, 1) + 2 * pad;
	int h = fb_text_height(1) + 2;

	fb_fill_rect(fb, FB_LAND_W - w, FB_LAND_H - h, w, h, FB_RED);
	fb_text(fb, FB_LAND_W - w + pad, FB_LAND_H - h + 1, text, FB_WHITE, 1);
}

void fb_draw_offline(uint8_t *fb)
{
	fb_frame(fb, 3, FB_RED);
	badge(fb, "OFFLINE");
}

void fb_draw_low_batt(uint8_t *fb)
{
	fb_frame(fb, 3, FB_RED);
	badge(fb, "LOW BATTERY");
}
