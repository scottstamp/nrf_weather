/*
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include "wx_service.h"

/* Views map 1:1 to the image slots of the BLE protocol (docs/protocol.md). */
#define WX_VIEW_WEATHER WX_SLOT_WEATHER
#define WX_VIEW_TIDE    WX_SLOT_TIDE

/* Provided by main.c: end the current sleep and poll the host immediately.
 * Safe from ISR and thread context. A request made while a poll/refresh is
 * running is kept and triggers the next poll right after it.
 */
void wx_wake_now(void);

/* Provided by main.c: show the given view (redraws the panel from the stored
 * slot image, no host contact needed). Same context/latching rules as above.
 */
void wx_request_view(int view);
