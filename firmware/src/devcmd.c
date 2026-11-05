/*
 * Debug-only command console on the USB CDC ACM port (CONFIG_WX_DEVCMD).
 *
 * Line commands (terminated by CR or LF):
 *   poll   wake the main loop and poll the host now (no reboot)
 *   reset  software reset (no splash refresh; polls right after boot)
 *   boot   reboot into the Adafruit UF2 bootloader (PROMICRO drive)
 *   help   list commands
 * Opening the port at 1200 baud also reboots into the bootloader
 * (Arduino-style "1200 bps touch"), e.g. `stty -F /dev/ttyACM0 1200`.
 *
 * Release builds have no USB stack, so none of this is compiled in.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "devcmd.h"

#include <string.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/sys/reboot.h>
#include <hal/nrf_power.h>

LOG_MODULE_REGISTER(devcmd, LOG_LEVEL_INF);

/* Adafruit nRF52 bootloader: GPREGRET magic that starts UF2 mass-storage DFU. */
#define DFU_MAGIC_UF2_RESET 0x57

static const struct device *const uart = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

static char line[16];
static size_t line_len;
static char pending[sizeof(line)];

static void cmd_work_fn(struct k_work *work);
static K_WORK_DEFINE(cmd_work, cmd_work_fn);

static void reboot_to_bootloader(void)
{
	LOG_WRN("rebooting into UF2 bootloader");
	log_flush();
	k_msleep(100); /* let USB push the line out */
	nrf_power_gpregret_set(NRF_POWER, 0, DFU_MAGIC_UF2_RESET);
	sys_reboot(SYS_REBOOT_COLD);
}

static void cmd_work_fn(struct k_work *work)
{
	ARG_UNUSED(work);

	if (strcmp(pending, "poll") == 0) {
		LOG_INF("poll requested");
		wx_wake_now();
	} else if (strcmp(pending, "reset") == 0) {
		LOG_WRN("software reset");
		log_flush();
		k_msleep(100);
		sys_reboot(SYS_REBOOT_COLD);
	} else if (strcmp(pending, "boot") == 0) {
		reboot_to_bootloader();
	} else if (pending[0] != '\0') {
		LOG_INF("commands: poll | reset | boot | help  (1200-baud open = boot)");
	}
}

static void uart_cb(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);

	while (uart_irq_update(dev) && uart_irq_rx_ready(dev)) {
		uint8_t buf[16];
		int n = uart_fifo_read(dev, buf, sizeof(buf));

		for (int i = 0; i < n; i++) {
			char c = (char)buf[i];

			if (c == '\r' || c == '\n') {
				if (line_len && !k_work_is_pending(&cmd_work)) {
					line[line_len] = '\0';
					memcpy(pending, line, line_len + 1);
					k_work_submit(&cmd_work);
				}
				line_len = 0;
			} else if (line_len < sizeof(line) - 1) {
				line[line_len++] = c;
			}
		}
	}
}

/* 1200-baud touch: no line-coding callback is exposed for the boot-time CDC
 * instance, so check the host-set baud rate periodically (debug build only).
 */
static void baud_check_fn(struct k_timer *t)
{
	uint32_t baud = 0;

	ARG_UNUSED(t);
	if (uart_line_ctrl_get(uart, UART_LINE_CTRL_BAUD_RATE, &baud) == 0 && baud == 1200) {
		nrf_power_gpregret_set(NRF_POWER, 0, DFU_MAGIC_UF2_RESET);
		sys_reboot(SYS_REBOOT_COLD);
	}
}
static K_TIMER_DEFINE(baud_timer, baud_check_fn, NULL);

int devcmd_init(void)
{
	if (!device_is_ready(uart)) {
		return -ENODEV;
	}
	uart_irq_callback_set(uart, uart_cb);
	uart_irq_rx_enable(uart);
	k_timer_start(&baud_timer, K_MSEC(250), K_MSEC(250));
	LOG_INF("dev console: poll | reset | boot | help");
	return 0;
}
