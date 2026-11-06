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
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/reboot.h>

#include <string.h>

#include "battery.h"
#include "button.h"
#include "devcmd.h"
#include "epd_ssd1680.h"
#include "framebuf.h"
#include "wake.h"
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

#define EV_POLL BIT(0)
#define EV_VIEW BIT(1)
static atomic_t events;
static atomic_t req_view = ATOMIC_INIT(WX_VIEW_WEATHER);

void wx_wake_now(void)
{
	atomic_or(&events, EV_POLL);
	k_sem_give(&wake_sem);
}

void wx_request_view(int view)
{
	atomic_set(&req_view, view);
	atomic_or(&events, EV_VIEW);
	k_sem_give(&wake_sem);
}

/*
 * Sleep until the uptime deadline, in chunks short enough to keep feeding
 * the watchdog. Ends early on a wake request (poll or view button, debug
 * console). The CPU idles in System ON either way: a semaphore wait with
 * timeout costs the same as k_sleep(). A request made while polling or
 * refreshing is kept: it ends this sleep at once.
 */
static void sleep_until(int64_t deadline_ms)
{
	const int64_t chunk_ms = (int64_t)MAX(CONFIG_WX_WDT_TIMEOUT_S / 3, 60) * 1000;

	for (;;) {
		int64_t remaining = deadline_ms - k_uptime_get();

		if (remaining <= 0) {
			return;
		}
		bool woken = k_sem_take(&wake_sem, K_MSEC(MIN(remaining, chunk_ms))) == 0;

		wdt_kick();
		if (woken) {
			return;
		}
	}
}

/*
 * Build what the panel should show: the selected view's stored image (or a
 * placeholder if the host never sent one), plus the OFFLINE badge if stale.
 * Slot images stay untouched, so overlays come and go without the host
 * having to resend anything.
 */
static void compose(int view, bool stale)
{
	uint32_t crc;
	const uint8_t *img = wx_slot(view, &crc);

	if (img) {
		memcpy(fb, img, FB_BYTES);
		if (stale) {
			fb_draw_offline(fb);
		}
	} else if (view == WX_VIEW_TIDE) {
		fb_draw_splash(fb, "TIDES", "NO DATA YET");
	} else {
		fb_draw_splash(fb, "WXPAPER", "WAITING FOR HOST");
	}
}

/* Full refresh; returns the CRC now on the panel (0 = unknown, retry next time). */
static uint32_t refresh(struct wx_status *st)
{
	int err = epd_show(fb, fb + EPD_PLANE_BYTES);

	st->refresh_count++;
	if (err) {
		st->flags |= WX_FLAG_REFRESH_FAILED;
		return 0;
	}
	st->flags &= ~WX_FLAG_REFRESH_FAILED;
	return crc32_ieee(fb, FB_BYTES);
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
		.view = WX_VIEW_WEATHER,
	};
	int64_t last_contact_ms = k_uptime_get();
	int64_t next_poll_ms = 0; /* poll right after boot */
	uint32_t shown_crc;       /* CRC of the composed image on the panel */
	bool retry_pending = false;
	bool stale = false;
	bool crit = false;
	bool crit_shown = false;
	int err;

	wdt_start();
#if defined(CONFIG_WX_DEVCMD)
	(void)devcmd_init();
#endif
	err = button_init();
	if (err && err != -ENODEV) {
		LOG_ERR("button init %d", err);
	}

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

	compose(st.view, false);
	if (want_splash() && (mv <= 0 || mv >= CONFIG_WX_CRIT_BATT_MV)) {
		shown_crc = refresh(&st);
	} else {
		/* Leave the panel alone; the first host image will differ and refresh it. */
		shown_crc = crc32_ieee(fb, FB_BYTES);
	}

	for (;;) {
		sleep_until(next_poll_ms);
		wdt_kick();

		uint32_t ev = (uint32_t)atomic_clear(&events);
		bool do_poll = (ev & EV_POLL) || k_uptime_get() >= next_poll_ms;

		if (ev & EV_VIEW) {
			int v = (int)atomic_get(&req_view);

			if (v != st.view) {
				LOG_INF("view %d -> %d", st.view, v);
				st.view = (uint8_t)v;
			}
		}

		if (do_poll) {
			mv = battery_read_mv();
			bool mv_valid = mv > 0;
			bool low = mv_valid && mv < CONFIG_WX_LOW_BATT_MV;

			crit = mv_valid && mv < CONFIG_WX_CRIT_BATT_MV;
			st.battery_mv = mv_valid ? (uint16_t)mv : 0;
			st.battery_pct = mv_valid ? battery_pct(mv) : 255;
			WRITE_BIT(st.flags, 1, low); /* WX_FLAG_LOW_BATT */

			wx_service_set_status(&st);
			enum wx_poll_result res = wx_poll();
			int64_t now = k_uptime_get();
			uint32_t crc0;

			LOG_INF("poll result %d, batt %d mV", res, mv);
			WRITE_BIT(st.flags, 2, wx_slot(WX_SLOT_WEATHER, &crc0) == NULL);

			if (res != WX_POLL_NO_CONTACT) {
				last_contact_ms = now;
				retry_pending = false;
				stale = false;
			} else {
				/* Never call it stale before at least two polls were missed. */
				int64_t stale_s = MAX((int64_t)CONFIG_WX_STALE_AFTER_S,
						      2 * (int64_t)wx_poll_interval_s() + 300);

				stale = (now - last_contact_ms) / 1000 >= stale_s;
			}
			WRITE_BIT(st.flags, 0, stale); /* WX_FLAG_STALE_SHOWN */

			uint32_t interval =
				crit ? CONFIG_WX_CRIT_POLL_INTERVAL_S : wx_poll_interval_s();

			if (res == WX_POLL_NO_CONTACT && !retry_pending) {
				/* One early retry per outage; cleared on the next contact. */
				retry_pending = true;
				interval = MIN(interval, CONFIG_WX_RETRY_INTERVAL_S);
			}
			next_poll_ms = now + (int64_t)interval * 1000;
		}

		if (crit) {
			/*
			 * A full refresh draws several mA for ~20 s and can brown out a
			 * nearly empty cell. Show LOW BATTERY once, then stop refreshing
			 * (view buttons included) until the cell recovers.
			 */
			if (!crit_shown) {
				compose(st.view, stale);
				fb_draw_low_batt(fb);
				(void)refresh(&st);
				shown_crc = 0; /* redraw without the badge once charged */
				crit_shown = true;
			}
			continue;
		}
		crit_shown = false;

		/* Refresh only if the composed image differs from what is on the panel. */
		compose(st.view, stale);
		if (crc32_ieee(fb, FB_BYTES) != shown_crc) {
			shown_crc = refresh(&st);
		}
	}

	return 0;
}
