/*
 * Battery voltage via SAADC VDDH/5 (nice!nano runs the nRF52840 in
 * high-voltage mode with the cell on VDDH). One-shot sample per poll.
 * SPDX-License-Identifier: Apache-2.0
 */
#include "battery.h"

#include <errno.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/sys/util.h>

static const struct adc_dt_spec vddh = ADC_DT_SPEC_GET_BY_IDX(DT_PATH(zephyr_user), 0);

int battery_init(void)
{
	if (!adc_is_ready_dt(&vddh)) {
		return -ENODEV;
	}
	return adc_channel_setup_dt(&vddh);
}

int battery_read_mv(void)
{
	int16_t raw = 0;
	struct adc_sequence seq = {
		.buffer = &raw,
		.buffer_size = sizeof(raw),
	};
	int err = adc_sequence_init_dt(&vddh, &seq);

	if (!err) {
		err = adc_read_dt(&vddh, &seq);
	}
	if (err) {
		return err;
	}

	int32_t mv = MAX(raw, 0);

	err = adc_raw_to_millivolts_dt(&vddh, &mv);
	if (err) {
		return err;
	}
	return (int)(mv * 5); /* VDDH/5 input */
}

uint8_t battery_pct(int mv)
{
	/* Resting LiPo discharge curve (approximate). */
	static const struct {
		uint16_t mv;
		uint8_t pct;
	} curve[] = {
		{4200, 100}, {4100, 90}, {4000, 79}, {3900, 66}, {3800, 52}, {3750, 42},
		{3700, 31}, {3650, 21}, {3600, 13}, {3500, 6}, {3400, 3}, {3300, 0},
	};

	if (mv >= curve[0].mv) {
		return 100;
	}
	for (size_t i = 1; i < ARRAY_SIZE(curve); i++) {
		if (mv >= curve[i].mv) {
			int span = curve[i - 1].mv - curve[i].mv;
			int dp = curve[i - 1].pct - curve[i].pct;

			return (uint8_t)(curve[i].pct + (mv - curve[i].mv) * dp / span);
		}
	}
	return 0;
}
