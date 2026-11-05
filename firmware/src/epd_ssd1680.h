/*
 * Minimal SSD1680 black/white/red e-paper driver (128x296).
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#define EPD_WIDTH       128 /* source lines (native x) */
#define EPD_HEIGHT      296 /* gate lines (native y) */
#define EPD_ROW_BYTES   (EPD_WIDTH / 8)
#define EPD_PLANE_BYTES (EPD_ROW_BYTES * EPD_HEIGHT) /* 4736 */

/* Configure GPIOs and leave the panel unpowered with all lines disconnected. */
int epd_init(void);

/*
 * Power the panel, write both planes, run a full refresh, wait for BUSY,
 * put the controller into deep sleep and cut power again.
 *
 * bw:  1 = white, 0 = black.  red: 1 = red (inverted if CONFIG_WX_EPD_RED_INVERT).
 * Blocks for the whole refresh (~15-25 s). Returns 0, -ETIMEDOUT or SPI error.
 */
int epd_show(const uint8_t *bw, const uint8_t *red);
