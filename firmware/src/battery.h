/*
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stdint.h>

int battery_init(void);
/* Returns battery (VDDH) voltage in mV, or negative errno. ADC is idle afterwards. */
int battery_read_mv(void);
/* Rough LiPo state of charge from voltage (0..100). */
uint8_t battery_pct(int mv);
