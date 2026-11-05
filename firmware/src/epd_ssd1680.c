/*
 * Minimal SSD1680 black/white/red e-paper driver (128x296).
 *
 * Init/refresh sequence follows the SSD1680 datasheet and the widely used
 * 2.9" B/W/R panel sequence (e.g. GxEPD2 GxEPD2_290_C90c). See docs/display.md.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "epd_ssd1680.h"

#include <errno.h>
#include <string.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/spi.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(epd, LOG_LEVEL_INF);

#define EPD_NODE DT_NODELABEL(epd)

/* SSD1680 commands */
#define CMD_DRIVER_OUTPUT     0x01
#define CMD_DEEP_SLEEP        0x10
#define CMD_DATA_ENTRY        0x11
#define CMD_SW_RESET          0x12
#define CMD_TEMP_SENSOR       0x18
#define CMD_MASTER_ACTIVATE   0x20
#define CMD_UPDATE_CTRL1      0x21
#define CMD_UPDATE_CTRL2      0x22
#define CMD_WRITE_RAM_BW      0x24
#define CMD_WRITE_RAM_RED     0x26
#define CMD_BORDER_WAVEFORM   0x3C
#define CMD_RAM_X_RANGE       0x44
#define CMD_RAM_Y_RANGE       0x45
#define CMD_RAM_X_COUNTER     0x4E
#define CMD_RAM_Y_COUNTER     0x4F

static const struct device *const spi_dev = DEVICE_DT_GET(DT_PHANDLE(EPD_NODE, spi_bus));
static const struct gpio_dt_spec cs = GPIO_DT_SPEC_GET(EPD_NODE, cs_gpios);
static const struct gpio_dt_spec dc = GPIO_DT_SPEC_GET(EPD_NODE, dc_gpios);
static const struct gpio_dt_spec rst = GPIO_DT_SPEC_GET(EPD_NODE, reset_gpios);
static const struct gpio_dt_spec busy = GPIO_DT_SPEC_GET(EPD_NODE, busy_gpios);
#if DT_NODE_HAS_PROP(EPD_NODE, power_gpios)
#define HAVE_POWER_SWITCH 1
static const struct gpio_dt_spec pwr = GPIO_DT_SPEC_GET(EPD_NODE, power_gpios);
#else
#define HAVE_POWER_SWITCH 0
#endif

static const struct spi_config spi_cfg = {
	.frequency = DT_PROP(EPD_NODE, spi_max_frequency),
	.operation = SPI_WORD_SET(8) | SPI_TRANSFER_MSB | SPI_OP_MODE_MASTER,
	/* .cs left zeroed: chip select is driven manually (see binding). */
};

static int spi_tx(const uint8_t *buf, size_t len)
{
	const struct spi_buf b = {.buf = (void *)buf, .len = len};
	const struct spi_buf_set set = {.buffers = &b, .count = 1};

	return spi_write(spi_dev, &spi_cfg, &set);
}

/*
 * Send a command followed by optional data. Data is streamed through a small
 * stack buffer so it is always in RAM (EasyDMA) and can be inverted on the fly.
 */
static int epd_cmd_stream(uint8_t cmd, const uint8_t *data, size_t len, bool invert)
{
	uint8_t chunk[64];
	int err;

	gpio_pin_set_dt(&cs, 1);
	gpio_pin_set_dt(&dc, 0);
	chunk[0] = cmd;
	err = spi_tx(chunk, 1);
	gpio_pin_set_dt(&dc, 1);

	while (!err && len) {
		size_t n = MIN(len, sizeof(chunk));

		for (size_t i = 0; i < n; i++) {
			chunk[i] = invert ? (uint8_t)~data[i] : data[i];
		}
		err = spi_tx(chunk, n);
		data += n;
		len -= n;
	}

	gpio_pin_set_dt(&cs, 0);
	return err;
}

static int epd_cmd(uint8_t cmd, const uint8_t *data, size_t len)
{
	return epd_cmd_stream(cmd, data, len, false);
}

static int epd_cmd1(uint8_t cmd, uint8_t val)
{
	return epd_cmd(cmd, &val, 1);
}

static int epd_wait_busy(int32_t timeout_ms)
{
	int64_t deadline = k_uptime_get() + timeout_ms;

	/* Sleep between polls: the CPU idles (System ON) while the panel works. */
	while (gpio_pin_get_dt(&busy) > 0) {
		if (k_uptime_get() > deadline) {
			LOG_ERR("BUSY timeout after %d ms", timeout_ms);
			return -ETIMEDOUT;
		}
		k_msleep(20);
	}
	return 0;
}

/* Every line to the panel floats: no back-powering through ESD diodes. */
static void epd_pins_release(void)
{
	gpio_pin_configure_dt(&busy, GPIO_DISCONNECTED);
	gpio_pin_configure_dt(&dc, GPIO_DISCONNECTED);
#if HAVE_POWER_SWITCH
	gpio_pin_configure_dt(&cs, GPIO_DISCONNECTED);
	gpio_pin_configure_dt(&rst, GPIO_DISCONNECTED);
	gpio_pin_configure_dt(&pwr, GPIO_OUTPUT_INACTIVE);
#else
	/* Panel stays powered (in deep sleep): keep CS/RST deasserted, not floating. */
	gpio_pin_configure_dt(&cs, GPIO_OUTPUT_INACTIVE);
	gpio_pin_configure_dt(&rst, GPIO_OUTPUT_INACTIVE);
#endif
}

static int epd_power_up(void)
{
#if HAVE_POWER_SWITCH
	gpio_pin_configure_dt(&pwr, GPIO_OUTPUT_ACTIVE);
	k_msleep(DT_PROP(EPD_NODE, power_on_delay_ms));
#endif
	gpio_pin_configure_dt(&cs, GPIO_OUTPUT_INACTIVE);
	gpio_pin_configure_dt(&dc, GPIO_OUTPUT_INACTIVE);
	gpio_pin_configure_dt(&rst, GPIO_OUTPUT_INACTIVE);
	gpio_pin_configure_dt(&busy, GPIO_INPUT);

	/* Hardware reset (also required to leave deep sleep). */
	gpio_pin_set_dt(&rst, 1);
	k_msleep(10);
	gpio_pin_set_dt(&rst, 0);
	k_msleep(10);
	return epd_wait_busy(1000);
}

static int epd_controller_init(void)
{
	int err;

	err = epd_cmd(CMD_SW_RESET, NULL, 0);
	if (err) {
		return err;
	}
	k_msleep(10);
	err = epd_wait_busy(1000);
	if (err) {
		return err;
	}

	/* MUX = 296 gates: (EPD_HEIGHT - 1) = 0x127, scan G0 -> G295. */
	const uint8_t drv[] = {(EPD_HEIGHT - 1) & 0xff, (EPD_HEIGHT - 1) >> 8, 0x00};
	/* Data entry: X increment, Y increment, address counter updated in X. */
	const uint8_t xr[] = {0x00, EPD_ROW_BYTES - 1};
	const uint8_t yr[] = {0x00, 0x00, (EPD_HEIGHT - 1) & 0xff, (EPD_HEIGHT - 1) >> 8};
	/* Ctrl1: normal BW + normal RED RAM; 0x80 = source output S8..S167 (128 px). */
	const uint8_t upd1[] = {0x00, 0x80};

	err = epd_cmd(CMD_DRIVER_OUTPUT, drv, sizeof(drv));
	err = err ?: epd_cmd1(CMD_DATA_ENTRY, 0x03);
	err = err ?: epd_cmd(CMD_RAM_X_RANGE, xr, sizeof(xr));
	err = err ?: epd_cmd(CMD_RAM_Y_RANGE, yr, sizeof(yr));
	err = err ?: epd_cmd1(CMD_BORDER_WAVEFORM, 0x05);
	err = err ?: epd_cmd(CMD_UPDATE_CTRL1, upd1, sizeof(upd1));
	err = err ?: epd_cmd1(CMD_TEMP_SENSOR, 0x80); /* internal temperature sensor */
	return err;
}

static int epd_set_counter(void)
{
	const uint8_t y0[] = {0x00, 0x00};
	int err = epd_cmd1(CMD_RAM_X_COUNTER, 0x00);

	return err ?: epd_cmd(CMD_RAM_Y_COUNTER, y0, sizeof(y0));
}

int epd_init(void)
{
	if (!device_is_ready(spi_dev) || !gpio_is_ready_dt(&cs) || !gpio_is_ready_dt(&dc) ||
	    !gpio_is_ready_dt(&rst) || !gpio_is_ready_dt(&busy)) {
		return -ENODEV;
	}
#if HAVE_POWER_SWITCH
	if (!gpio_is_ready_dt(&pwr)) {
		return -ENODEV;
	}
#endif
	epd_pins_release();
	return 0;
}

int epd_show(const uint8_t *bw, const uint8_t *red)
{
	int64_t t0 = k_uptime_get();
	int err;

	err = epd_power_up();
	err = err ?: epd_controller_init();
	err = err ?: epd_set_counter();
	err = err ?: epd_cmd_stream(CMD_WRITE_RAM_BW, bw, EPD_PLANE_BYTES, false);
	err = err ?: epd_set_counter();
	err = err ?: epd_cmd_stream(CMD_WRITE_RAM_RED, red, EPD_PLANE_BYTES,
				    IS_ENABLED(CONFIG_WX_EPD_RED_INVERT));
	/* Ctrl2 0xF7: clock+analog on, load temp, load LUT (mode 1), display, power down. */
	err = err ?: epd_cmd1(CMD_UPDATE_CTRL2, 0xF7);
	err = err ?: epd_cmd(CMD_MASTER_ACTIVATE, NULL, 0);
	if (!err) {
		k_msleep(1);
		err = epd_wait_busy(CONFIG_WX_EPD_BUSY_TIMEOUT_MS);
	}

	/* Deep sleep mode 1 (RAM retained) even after an error, then power off. */
	(void)epd_cmd1(CMD_DEEP_SLEEP, 0x01);
	k_msleep(2);
	epd_pins_release();

	LOG_INF("refresh %s in %lld ms", err ? "FAILED" : "done", k_uptime_get() - t0);
	return err;
}
