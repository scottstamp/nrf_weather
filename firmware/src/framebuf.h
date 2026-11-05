/*
 * Two-plane framebuffer in panel-native layout (protocol payload format 0),
 * plus a few drawing helpers in *landscape* coordinates for on-device
 * overlays (splash, offline, low battery).
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "epd_ssd1680.h"

#define FB_LAND_W     EPD_HEIGHT /* 296 */
#define FB_LAND_H     EPD_WIDTH  /* 128 */
#define FB_BYTES      (2 * EPD_PLANE_BYTES) /* 9472: BW || RED */

enum fb_color {
	FB_WHITE,
	FB_BLACK,
	FB_RED,
};

/* fb[0..4735] = BW plane (1 = white), fb[4736..] = RED plane (1 = red). */
void fb_clear(uint8_t *fb);
void fb_pixel(uint8_t *fb, int x, int y, enum fb_color c);
void fb_fill_rect(uint8_t *fb, int x, int y, int w, int h, enum fb_color c);
void fb_frame(uint8_t *fb, int thickness, enum fb_color c);
int fb_text_width(const char *s, int scale);
int fb_text_height(int scale);
/* Draws text with its top-left at (x, y). Unknown characters render as space. */
void fb_text(uint8_t *fb, int x, int y, const char *s, enum fb_color c, int scale);

/* Screens/overlays */
void fb_draw_splash(uint8_t *fb, const char *line1, const char *line2);
/* Red frame + "OFFLINE" badge drawn over whatever is in fb. */
void fb_draw_offline(uint8_t *fb);
/* Red "LOW BATTERY" badge. */
void fb_draw_low_batt(uint8_t *fb);
