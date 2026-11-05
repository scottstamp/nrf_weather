/*
 * Poll button: a press wakes the main loop to poll the host immediately.
 * Works in release (battery) builds. Edge detection uses GPIO SENSE (see
 * `sense-edge-mask` in the board overlay), so waiting for a press costs
 * nothing in System ON idle. The user LED blinks briefly as acknowledgement,
 * since a poll that finds no new data changes nothing on the panel.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "button.h"

#include <errno.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include "wake.h"

LOG_MODULE_REGISTER(button, LOG_LEVEL_INF);

#define BTN_NODE DT_NODELABEL(poll_button)

#if DT_NODE_EXISTS(BTN_NODE)

#define DEBOUNCE_MS 30
#define BLINK_MS    50

static const struct gpio_dt_spec btn = GPIO_DT_SPEC_GET(BTN_NODE, gpios);
#if DT_NODE_EXISTS(DT_ALIAS(led0))
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
#define HAVE_LED 1
#else
#define HAVE_LED 0
#endif

static struct gpio_callback btn_cb;

static void led_off_fn(struct k_work *work)
{
	ARG_UNUSED(work);
#if HAVE_LED
	gpio_pin_set_dt(&led, 0);
	/* Back to disconnected: no pin left driven in sleep. */
	gpio_pin_configure_dt(&led, GPIO_DISCONNECTED);
#endif
}
static K_WORK_DELAYABLE_DEFINE(led_off_work, led_off_fn);

static void debounce_fn(struct k_work *work)
{
	ARG_UNUSED(work);
	if (gpio_pin_get_dt(&btn) <= 0) {
		return; /* bounce / glitch */
	}
	LOG_INF("button: poll requested");
#if HAVE_LED
	gpio_pin_configure_dt(&led, GPIO_OUTPUT_ACTIVE);
	k_work_reschedule(&led_off_work, K_MSEC(BLINK_MS));
#endif
	wx_wake_now();
}
static K_WORK_DELAYABLE_DEFINE(debounce_work, debounce_fn);

static void btn_isr(const struct device *dev, struct gpio_callback *cb, uint32_t pins)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(cb);
	ARG_UNUSED(pins);
	k_work_reschedule(&debounce_work, K_MSEC(DEBOUNCE_MS));
}

int button_init(void)
{
	int err;

	if (!gpio_is_ready_dt(&btn)) {
		return -ENODEV;
	}
	err = gpio_pin_configure_dt(&btn, GPIO_INPUT);
	if (err) {
		return err;
	}
	gpio_init_callback(&btn_cb, btn_isr, BIT(btn.pin));
	err = gpio_add_callback_dt(&btn, &btn_cb);
	if (err) {
		return err;
	}
	return gpio_pin_interrupt_configure_dt(&btn, GPIO_INT_EDGE_TO_ACTIVE);
}

#else /* no poll_button in devicetree */

int button_init(void)
{
	return -ENODEV;
}

#endif
