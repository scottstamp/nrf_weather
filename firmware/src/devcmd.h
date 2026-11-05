/*
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

/* Start the debug command console (CONFIG_WX_DEVCMD). */
int devcmd_init(void);

/* Provided by main.c: end the current sleep and poll immediately. */
void wx_wake_now(void);
