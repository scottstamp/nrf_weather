/*
 * WxPaper: battery-powered BLE e-paper weather display.
 * Main state machine: poll host -> (maybe) refresh panel -> sleep.
 * See docs/firmware.md.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include <zephyr/device.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/reboot.h>

#include "battery.h"
#include "devcmd.h"
#include "epd_ssd1680.h"
#include "framebuf.h"
#include "wx_service.h"

LOG_MODULE_REGISTER(main, LOG_LEVEL_INF);

static uint8_t fb[FB_BYTES];

static const struct device *const wdt = DEVICE_DT_GET_OR_NULL(DT_NODELABEL(wdt0));
static int wdt_channel = -1;

static void wdt_start(void)
{
	if (!wdt || !device_is_ready(wdt)) {
		return;
	}
	const struct wdt_timeout_cfg cfg = {
		.window = {.min = 0, .max = (uint32_t)CONFIG_WX_WDT_TIMEOUT_S * 1000U},
		.flags = WDT_FLAG_RESET_SOC,
	};

	wdt_channel = wdt_install_timeout(wdt, &cfg);
	if (wdt_channel < 0) {
		LOG_ERR("wdt install %d", wdt_channel);
		return;
	}
	/* Keep counting while the CPU sleeps: catches a hung main loop. */
	int err = wdt_setup(wdt, WDT_OPT_PAUSE_HALTED_BY_DBG);

	if (err) {
		LOG_ERR("wdt setup %d", err);
		wdt_channel = -1;
	}
}

static void wdt_kick(void)
{
	if (wdt_channel >= 0) {
		(void)wdt_feed(wdt, wdt_channel);
	}
}

static K_SEM_DEFINE(wake_sem, 0, 1);

void wx_wake_now(void)
{
	k_sem_give(&wake_sem);
}

/*
 * Sleep in chunks short enough to keep feeding the watchdog. Ends early if
 * wx_wake_now() is called (debug console "poll"). The CPU idles in System ON
 * either way: a semaphore wait with timeout costs the same as k_sleep().
 */
static void sleep_s(uint32_t secs)
{
	const uint32_t chunk = MAX(CONFIG_WX_WDT_TIMEOUT_S / 3, 60);

	/* A wake request made while polling/refreshing is kept: it ends this sleep at once. */
	while (secs) {
		uint32_t s = MIN(secs, chunk);
		bool woken = k_sem_take(&wake_sem, K_SECONDS(s)) == 0;

		wdt_kick();
		if (woken) {
			return;
		}
		secs -= s;
	}
}

static int refresh(struct wx_status *st)
{
	int err = epd_show(fb, fb + EPD_PLANE_BYTES);

	st->refresh_count++;
	if (err) {
		/* Panel content unknown: make the host resend on the next poll. */
		st->flags |= WX_FLAG_REFRESH_FAILED;
		st->displayed_crc = 0;
	} else {
		st->flags &= ~WX_FLAG_REFRESH_FAILED;
	}
	return err;
}

/*
 * The splash costs a full refresh; only draw it when a person caused the
 * reset (power-on, reset button), not after a watchdog/fault/software reset,
 * so a crash loop doesn't keep flashing the panel and draining the cell.
 */
static bool want_splash(void)
{
	uint32_t cause = 0;

	if (hwinfo_get_reset_cause(&cause) != 0) {
		return true;
	}
	(void)hwinfo_clear_reset_cause();
	/* nRF reports a plain power-on as cause 0; RESETREAS bits accumulate until cleared. */
	return (cause & RESET_PIN) ||
	       !(cause & (RESET_WATCHDOG | RESET_SOFTWARE | RESET_CPU_LOCKUP));
}

int main(void)
{
	struct wx_status st = {
		.battery_pct = 255,
		.flags = WX_FLAG_NO_IMAGE,
	};
	int64_t last_contact_ms = k_uptime_get();
	bool retry_pending = false;
	bool crit_shown = false;
	int err;

	wdt_start();
#if defined(CONFIG_WX_DEVCMD)
	(void)devcmd_init();
#endif

	err = epd_init();
	if (err) {
		LOG_ERR("epd init %d", err);
	}
	err = battery_init();
	if (err) {
		LOG_ERR("battery init %d", err);
	}
	err = wx_service_init();
	if (err) {
		LOG_ERR("bt init %d", err);
		/* Without BLE the device is useless: back off, then reboot and retry. */
		k_sleep(K_SECONDS(60));
		sys_reboot(SYS_REBOOT_COLD);
	}

	int mv = battery_read_mv();

	if (want_splash() && (mv <= 0 || mv >= CONFIG_WX_CRIT_BATT_MV)) {
		fb_draw_splash(fb, "WXPAPER", "WAITING FOR HOST");
		(void)refresh(&st);
	}

	for (;;) {
		bool need_refresh = false;

		wdt_kick();

		mv = battery_read_mv();
		bool mv_valid = mv > 0;
		bool low = mv_valid && mv < CONFIG_WX_LOW_BATT_MV;
		bool crit = mv_valid && mv < CONFIG_WX_CRIT_BATT_MV;

		st.battery_mv = mv_valid ? (uint16_t)mv : 0;
		st.battery_pct = mv_valid ? battery_pct(mv) : 255;
		WRITE_BIT(st.flags, 1, low); /* WX_FLAG_LOW_BATT */
		if (!crit) {
			crit_shown = false;
		}

		wx_service_set_status(&st);
		enum wx_poll_result res = wx_poll();
		int64_t now = k_uptime_get();

		LOG_INF("poll result %d, batt %d mV", res, mv);

		switch (res) {
		case WX_POLL_NEW_IMAGE:
			st.displayed_crc = wx_take_image(fb);
			st.flags &= ~(WX_FLAG_NO_IMAGE | WX_FLAG_STALE_SHOWN);
			need_refresh = true;
			__fallthrough;
		case WX_POLL_NO_CHANGE:
			last_contact_ms = now;
			retry_pending = false;
			break;
		case WX_POLL_NO_CONTACT: {
			/* Never call it stale before at least two polls were missed. */
			int64_t stale_s = MAX((int64_t)CONFIG_WX_STALE_AFTER_S,
					      2 * (int64_t)wx_poll_interval_s() + 300);

			if ((now - last_contact_ms) / 1000 >= stale_s &&
			    !(st.flags & (WX_FLAG_STALE_SHOWN | WX_FLAG_NO_IMAGE))) {
				fb_draw_offline(fb);
				st.displayed_crc = 0; /* panel no longer matches any host image */
				st.flags |= WX_FLAG_STALE_SHOWN;
				need_refresh = true;
			}
			break;
		}
		}

		if (crit) {
			/*
			 * A full refresh draws several mA for ~20 s and can brown out a
			 * nearly empty cell. Show LOW BATTERY once, then stop refreshing.
			 */
			if (!crit_shown) {
				fb_draw_low_batt(fb);
				st.displayed_crc = 0;
				need_refresh = true;
				crit_shown = true;
			} else if (need_refresh) {
				/* New image accepted but not drawn: don't claim it's shown. */
				need_refresh = false;
				st.displayed_crc = 0;
			}
		}

		if (need_refresh) {
			(void)refresh(&st);
		}

		uint32_t interval = crit ? CONFIG_WX_CRIT_POLL_INTERVAL_S : wx_poll_interval_s();

		if (res == WX_POLL_NO_CONTACT && !retry_pending) {
			retry_pending = true;
			interval = MIN(interval, CONFIG_WX_RETRY_INTERVAL_S);
		}
		/* retry_pending is cleared on the next contact: one early retry per outage. */

		sleep_s(interval);
	}

	return 0;
}
