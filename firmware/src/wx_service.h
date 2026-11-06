/*
 * BLE peripheral + GATT service implementing docs/protocol.md (v2).
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stdint.h>

#define WX_PROTO_VERSION 2

/* Image slots = views. Slot 0 is the weather view, slot 1 the tide view. */
#define WX_SLOT_WEATHER 0
#define WX_SLOT_TIDE    1
#define WX_NUM_SLOTS    2

#define WX_FLAG_STALE_SHOWN    BIT(0)
#define WX_FLAG_LOW_BATT       BIT(1)
#define WX_FLAG_NO_IMAGE       BIT(2)
#define WX_FLAG_REFRESH_FAILED BIT(3)
#define WX_FLAG_CHARGING       BIT(4)

enum wx_poll_result {
	WX_POLL_NO_CONTACT, /* nobody connected, or session failed */
	WX_POLL_NO_CHANGE,  /* host contact, no slot changed */
	WX_POLL_NEW_IMAGE,  /* host contact, at least one slot got a CRC-verified image */
};

struct wx_status {
	uint16_t battery_mv;
	uint8_t battery_pct; /* 255 = unknown */
	uint8_t flags;
	uint8_t view; /* slot currently selected for display */
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

/*
 * Stored image of a slot (FB_BYTES) or NULL if the host never sent one.
 * *crc gets its CRC32 (0 if empty). Only call outside wx_poll(): slots are
 * written by the BT thread during a session.
 */
const uint8_t *wx_slot(int slot, uint32_t *crc);

/* Poll interval (s), possibly changed by the host via SET_INTERVAL. */
uint32_t wx_poll_interval_s(void);
