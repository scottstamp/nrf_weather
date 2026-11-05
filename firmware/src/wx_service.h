/*
 * BLE peripheral + GATT service implementing docs/protocol.md (v1).
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stdint.h>

#define WX_PROTO_VERSION 1

#define WX_FLAG_STALE_SHOWN    BIT(0)
#define WX_FLAG_LOW_BATT       BIT(1)
#define WX_FLAG_NO_IMAGE       BIT(2)
#define WX_FLAG_REFRESH_FAILED BIT(3)
#define WX_FLAG_CHARGING       BIT(4)

enum wx_poll_result {
	WX_POLL_NO_CONTACT, /* nobody connected, or session failed */
	WX_POLL_NO_CHANGE,  /* host says the displayed image is current */
	WX_POLL_NEW_IMAGE,  /* a CRC-verified image is ready: wx_take_image() */
};

struct wx_status {
	uint16_t battery_mv;
	uint8_t battery_pct; /* 255 = unknown */
	uint8_t flags;
	uint32_t displayed_crc;
	uint32_t refresh_count;
};

/* Enable Bluetooth and register the service. Does not advertise. */
int wx_service_init(void);

/* Values reported by the Status characteristic during the next poll. */
void wx_service_set_status(const struct wx_status *st);

/*
 * Advertise for up to CONFIG_WX_ADV_WINDOW_S and serve one host session.
 * Blocks until the session finished (device disconnected) or the window
 * elapsed with no successful session.
 */
enum wx_poll_result wx_poll(void);

/* Copy the last committed image (FB_BYTES) into dst; returns its CRC32. */
uint32_t wx_take_image(uint8_t *dst);

/* Poll interval (s), possibly changed by the host via SET_INTERVAL. */
uint32_t wx_poll_interval_s(void);
