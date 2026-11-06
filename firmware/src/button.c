/*
 * Push buttons (all optional, from devicetree):
 *   poll_button    - poll the host immediately
 *   weather_button - show the weather view
 *   tide_button    - show the tide view
 * All work in release (battery) builds. Edge detection uses GPIO SENSE (see
 * `sense-edge-mask` in the board overlay), so waiting for a press costs
 * nothing in System ON idle. The user LED blinks briefly as acknowledgement,
 * since a poll that finds no new data (or a press of the view already shown)
 * changes nothing on the panel.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "button.h"

#include <errno.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>

#include "wake.h"

LOG_MODULE_REGISTER(button, LOG_LEVEL_INF);

#define DEBOUNCE_MS 30
#define BLINK_MS    50

struct button {
	struct gpio_dt_spec spec;
	const char *name;
	void (*action)(void);
	struct gpio_callback cb;
	struct k_work_delayable debounce;
};

static void act_poll(void)
{
	wx_wake_now();
}

static void act_weather(void)
{
	wx_request_view(WX_VIEW_WEATHER);
}

static void act_tide(void)
{
	wx_request_view(WX_VIEW_TIDE);
}

#define BUTTON_ENTRY(label, fn)                                                                    \
	IF_ENABLED(DT_NODE_EXISTS(DT_NODELABEL(label)),                                            \
		   ({.spec = GPIO_DT_SPEC_GET(DT_NODELABEL(label), gpios),                         \
		     .name = #label,                                                               \
		     .action = fn},))

static struct button buttons[] = {
	BUTTON_ENTRY(poll_button, act_poll)
	BUTTON_ENTRY(weather_button, act_weather)
	BUTTON_ENTRY(tide_button, act_tide)
};

#if DT_NODE_EXISTS(DT_ALIAS(led0))
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
#define HAVE_LED 1
#else
#define HAVE_LED 0
#endif

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
	struct button *b =
		CONTAINER_OF(k_work_delayable_from_work(work), struct button, debounce);

	if (gpio_pin_get_dt(&b->spec) <= 0) {
		return; /* bounce / glitch */
	}
	LOG_INF("%s pressed", b->name);
#if HAVE_LED
	gpio_pin_configure_dt(&led, GPIO_OUTPUT_ACTIVE);
	k_work_reschedule(&led_off_work, K_MSEC(BLINK_MS));
#endif
	b->action();
}

static void btn_isr(const struct device *dev, struct gpio_callback *cb, uint32_t pins)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(pins);
	struct button *b = CONTAINER_OF(cb, struct button, cb);

	k_work_reschedule(&b->debounce, K_MSEC(DEBOUNCE_MS));
}

int button_init(void)
{
	int found = 0;

	for (size_t i = 0; i < ARRAY_SIZE(buttons); i++) {
		struct button *b = &buttons[i];
		int err;

		if (!gpio_is_ready_dt(&b->spec)) {
			LOG_ERR("%s: gpio not ready", b->name);
			continue;
		}
		k_work_init_delayable(&b->debounce, debounce_fn);
		err = gpio_pin_configure_dt(&b->spec, GPIO_INPUT);
		if (!err) {
			gpio_init_callback(&b->cb, btn_isr, BIT(b->spec.pin));
			err = gpio_add_callback_dt(&b->spec, &b->cb);
		}
		if (!err) {
			err = gpio_pin_interrupt_configure_dt(&b->spec, GPIO_INT_EDGE_TO_ACTIVE);
		}
		if (err) {
			LOG_ERR("%s: init %d", b->name, err);
			return err;
		}
		found++;
	}
	return found ? 0 : -ENODEV;
}
