/*
 * Command console on the USB CDC ACM port (CONFIG_WX_DEVCMD), present in
 * production builds. USB is powered only while a cable is attached.
 *
 * Line commands (terminated by CR or LF):
 *   poll   wake the main loop and poll the host now (no reboot)
 *   reset  software reset (no splash refresh; polls right after boot)
 *   boot   reboot into the Adafruit UF2 bootloader (PROMICRO drive)
 *   help   list commands
 * Opening the port at 1200 baud also reboots into the bootloader
 * (Arduino-style "1200 bps touch"), e.g. `stty -F /dev/ttyACM0 1200`.
 *
 * USB power: the board's CDC ACM helper registers the device but does not
 * enable it (CONFIG_CDC_ACM_SERIAL_ENABLE_AT_BOOT=n), because enabling the nRF
 * USBD requests the HFXO whether or not VBUS is present. Here USB is enabled
 * on VBUS_READY and disabled on VBUS_REMOVED, so on battery the USB stack
 * costs nothing (no clock, no wakeups). Log output written while unplugged
 * goes into the CDC ring buffer and is dropped when it fills.
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
#include <zephyr/sys/iterable_sections.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/usb/usbd.h>
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

static void usb_msg_cb(struct usbd_context *const ctx, const struct usbd_msg *msg)
{
	switch (msg->type) {
	case USBD_MSG_VBUS_READY:
		if (usbd_enable(ctx) == 0) {
			LOG_INF("USB cable detected: USB enabled");
		}
		break;
	case USBD_MSG_VBUS_REMOVED:
		(void)usbd_disable(ctx); /* releases the HFXO */
		break;
	case USBD_MSG_CDC_ACM_LINE_CODING: {
		/* 1200-baud touch: host opened the port at 1200 bps. */
		uint32_t baud = 0;

		if (uart_line_ctrl_get(msg->dev, UART_LINE_CTRL_BAUD_RATE, &baud) == 0 &&
		    baud == 1200) {
			reboot_to_bootloader();
		}
		break;
	}
	default:
		break;
	}
}

static int usb_power_init(void)
{
	int n = 0;

	STRUCT_SECTION_FOREACH(usbd_context, ctx) {
		int err = usbd_msg_register_cb(ctx, usb_msg_cb);

		if (err) {
			LOG_ERR("usbd msg cb %d", err);
			continue;
		}
		n++;
		/*
		 * A cable present at boot may have raised VBUS_READY before the
		 * callback existed: check the USB regulator status directly.
		 */
		if (nrf_power_usbregstatus_vbusdet_get(NRF_POWER)) {
			(void)usbd_enable(ctx);
		}
	}
	return n ? 0 : -ENODEV;
}

int devcmd_init(void)
{
	if (!device_is_ready(uart)) {
		return -ENODEV;
	}
	(void)usb_power_init();
	uart_irq_callback_set(uart, uart_cb);
	uart_irq_rx_enable(uart);
	LOG_INF("dev console: poll | reset | boot | help");
	return 0;
}
