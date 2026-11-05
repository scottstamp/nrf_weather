/*
 * BLE peripheral + GATT service implementing docs/protocol.md (v1).
 *
 * Threading: GATT callbacks run in the BT RX thread; the main thread blocks
 * in wx_poll() on a semaphore that is given when the link goes down. The
 * session is strictly sequential (one connection, one host), so plain
 * variables are sufficient.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "wx_service.h"

#include <app_version.h>
#include <errno.h>
#include <string.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/crc.h>

#include "framebuf.h"

LOG_MODULE_REGISTER(wx_svc, LOG_LEVEL_INF);

#define OP_BEGIN        0x01
#define OP_COMMIT       0x02
#define OP_NO_CHANGE    0x03
#define OP_SET_INTERVAL 0x04

#define ERR_BAD_LENGTH 0x80
#define ERR_CRC        0x81
#define ERR_NO_BEGIN   0x82
#define ERR_BAD_VALUE  0x83
#define ERR_SEQUENCE   0x84

#define FORMAT_RAW 0

#define WX_UUID(n) BT_UUID_128_ENCODE(0x7a1e0000 + (n), 0x5c3b, 0x4f2a, 0x9e0d, 0x2b6f8c4d1a90)

static const struct bt_uuid_128 svc_uuid = BT_UUID_INIT_128(WX_UUID(1));
static const struct bt_uuid_128 status_uuid = BT_UUID_INIT_128(WX_UUID(2));
static const struct bt_uuid_128 control_uuid = BT_UUID_INIT_128(WX_UUID(3));
static const struct bt_uuid_128 data_uuid = BT_UUID_INIT_128(WX_UUID(4));

struct status_pdu {
	uint8_t proto_version;
	uint8_t fw_major;
	uint8_t fw_minor;
	uint8_t fw_patch;
	uint16_t battery_mv;
	uint8_t battery_pct;
	uint8_t flags;
	uint32_t displayed_crc;
	uint32_t uptime_s;
	uint32_t poll_interval_s;
	uint32_t refresh_count;
} __packed;
BUILD_ASSERT(sizeof(struct status_pdu) == 24, "Status PDU must be 24 bytes");

/* Receive state (one transfer at a time) */
static uint8_t rx_buf[FB_BYTES];
static size_t rx_len;
static uint32_t rx_expect_crc;
static bool rx_begun;
static bool rx_seq_err;

/* Committed image */
static uint32_t committed_crc;

static struct wx_status status;
static uint32_t poll_interval_s = CONFIG_WX_POLL_INTERVAL_S;

static struct bt_conn *cur_conn;
static enum wx_poll_result session_result;
static K_SEM_DEFINE(link_down, 0, 1);

static void disconnect_work_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(disconnect_work, disconnect_work_fn);
static K_WORK_DELAYABLE_DEFINE(inactivity_work, disconnect_work_fn);

static void disconnect_work_fn(struct k_work *work)
{
	ARG_UNUSED(work);
	if (cur_conn) {
		(void)bt_conn_disconnect(cur_conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
	}
}

static void touch(void)
{
	k_work_reschedule(&inactivity_work, K_SECONDS(CONFIG_WX_SESSION_TIMEOUT_S));
}

/* Finish the session: let the ATT response go out, then drop the link. */
static void finish(enum wx_poll_result res)
{
	session_result = res;
	k_work_reschedule(&disconnect_work, K_MSEC(150));
}

/* ---- GATT ---- */

static ssize_t status_read(struct bt_conn *conn, const struct bt_gatt_attr *attr, void *buf,
			   uint16_t len, uint16_t offset)
{
	struct status_pdu pdu = {
		.proto_version = WX_PROTO_VERSION,
		.fw_major = APP_VERSION_MAJOR,
		.fw_minor = APP_VERSION_MINOR,
		.fw_patch = APP_PATCHLEVEL,
		.battery_mv = sys_cpu_to_le16(status.battery_mv),
		.battery_pct = status.battery_pct,
		.flags = status.flags,
		.displayed_crc = sys_cpu_to_le32(status.displayed_crc),
		.uptime_s = sys_cpu_to_le32((uint32_t)(k_uptime_get() / 1000)),
		.poll_interval_s = sys_cpu_to_le32(poll_interval_s),
		.refresh_count = sys_cpu_to_le32(status.refresh_count),
	};

	touch();
	return bt_gatt_attr_read(conn, attr, buf, len, offset, &pdu, sizeof(pdu));
}

static ssize_t control_write(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			     const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
	const uint8_t *p = buf;

	touch();
	if (offset != 0 || len < 1) {
		return BT_GATT_ERR(ERR_BAD_LENGTH);
	}

	switch (p[0]) {
	case OP_BEGIN: {
		/* u32 total_len, u32 crc32, u8 format, u32 unix_time */
		if (len != 14) {
			return BT_GATT_ERR(ERR_BAD_LENGTH);
		}
		uint32_t total = sys_get_le32(&p[1]);

		if (total != FB_BYTES) {
			return BT_GATT_ERR(ERR_BAD_LENGTH);
		}
		if (p[9] != FORMAT_RAW) {
			return BT_GATT_ERR(ERR_BAD_VALUE);
		}
		rx_expect_crc = sys_get_le32(&p[5]);
		rx_len = 0;
		rx_seq_err = false;
		rx_begun = true;
		LOG_INF("BEGIN crc=%08x", rx_expect_crc);
		return len;
	}
	case OP_COMMIT: {
		if (len != 1) {
			return BT_GATT_ERR(ERR_BAD_LENGTH);
		}
		if (!rx_begun) {
			return BT_GATT_ERR(ERR_NO_BEGIN);
		}
		if (rx_seq_err) {
			rx_begun = false;
			return BT_GATT_ERR(ERR_SEQUENCE);
		}
		if (rx_len != FB_BYTES) {
			rx_begun = false;
			return BT_GATT_ERR(ERR_BAD_LENGTH);
		}
		uint32_t crc = crc32_ieee(rx_buf, rx_len);

		rx_begun = false;
		if (crc != rx_expect_crc) {
			LOG_WRN("CRC mismatch %08x != %08x", crc, rx_expect_crc);
			return BT_GATT_ERR(ERR_CRC);
		}
		committed_crc = crc;
		LOG_INF("COMMIT ok");
		finish(WX_POLL_NEW_IMAGE);
		return len;
	}
	case OP_NO_CHANGE:
		if (len != 5) {
			return BT_GATT_ERR(ERR_BAD_LENGTH);
		}
		LOG_INF("NO_CHANGE");
		finish(WX_POLL_NO_CHANGE);
		return len;
	case OP_SET_INTERVAL: {
		if (len != 5) {
			return BT_GATT_ERR(ERR_BAD_LENGTH);
		}
		uint32_t s = sys_get_le32(&p[1]);

		if (s < 300 || s > 86400) {
			return BT_GATT_ERR(ERR_BAD_VALUE);
		}
		poll_interval_s = s;
		LOG_INF("interval=%u s", s);
		return len;
	}
	default:
		return BT_GATT_ERR(ERR_BAD_VALUE);
	}
}

static ssize_t data_write(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *buf,
			  uint16_t len, uint16_t offset, uint8_t flags)
{
	const uint8_t *p = buf;
	bool with_rsp = !(flags & BT_GATT_WRITE_FLAG_CMD);

	touch();
	if (offset != 0 || len < 2) {
		return with_rsp ? BT_GATT_ERR(ERR_BAD_LENGTH) : len;
	}
	if (!rx_begun) {
		return with_rsp ? BT_GATT_ERR(ERR_NO_BEGIN) : len;
	}

	uint16_t off = sys_get_le16(p);
	size_t n = len - 2;

	if (rx_seq_err || off != rx_len || rx_len + n > sizeof(rx_buf)) {
		rx_seq_err = true;
		return with_rsp ? BT_GATT_ERR(ERR_SEQUENCE) : len;
	}
	memcpy(&rx_buf[rx_len], &p[2], n);
	rx_len += n;
	return len;
}

BT_GATT_SERVICE_DEFINE(wx_svc,
	BT_GATT_PRIMARY_SERVICE(&svc_uuid),
	BT_GATT_CHARACTERISTIC(&status_uuid.uuid, BT_GATT_CHRC_READ, BT_GATT_PERM_READ,
			       status_read, NULL, NULL),
	BT_GATT_CHARACTERISTIC(&control_uuid.uuid, BT_GATT_CHRC_WRITE, BT_GATT_PERM_WRITE,
			       NULL, control_write, NULL),
	BT_GATT_CHARACTERISTIC(&data_uuid.uuid,
			       BT_GATT_CHRC_WRITE_WITHOUT_RESP | BT_GATT_CHRC_WRITE,
			       BT_GATT_PERM_WRITE, NULL, data_write, NULL),
);

/* ---- Connection handling ---- */

static void connected(struct bt_conn *conn, uint8_t err)
{
	if (err) {
		LOG_WRN("connect failed 0x%02x", err);
		return;
	}
	cur_conn = bt_conn_ref(conn);
	rx_begun = false;
	touch();
	LOG_INF("connected");

	/* Fast interval for the short transfer; the link lives < 2 s. */
	const struct bt_le_conn_param param = BT_LE_CONN_PARAM_INIT(6, 12, 0, 400);

	(void)bt_conn_le_param_update(conn, &param);
	(void)bt_conn_le_phy_update(conn, BT_CONN_LE_PHY_PARAM_2M);
	(void)bt_conn_le_data_len_update(conn, BT_LE_DATA_LEN_PARAM_MAX);
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	LOG_INF("disconnected 0x%02x", reason);
	k_work_cancel_delayable(&inactivity_work);
	k_work_cancel_delayable(&disconnect_work);
	if (cur_conn) {
		bt_conn_unref(cur_conn);
		cur_conn = NULL;
	}
	rx_begun = false;
	k_sem_give(&link_down);
}

BT_CONN_CB_DEFINE(conn_cb) = {
	.connected = connected,
	.disconnected = disconnected,
};

/* ---- Advertising ---- */

static const struct bt_data ad[] = {
	BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
	BT_DATA_BYTES(BT_DATA_UUID128_ALL, WX_UUID(1)),
	BT_DATA(BT_DATA_NAME_COMPLETE, CONFIG_BT_DEVICE_NAME, sizeof(CONFIG_BT_DEVICE_NAME) - 1),
};

static const struct bt_le_adv_param adv_param = BT_LE_ADV_PARAM_INIT(
	BT_LE_ADV_OPT_CONN, BT_GAP_ADV_FAST_INT_MIN_2, BT_GAP_ADV_FAST_INT_MAX_2, NULL);

int wx_service_init(void)
{
	return bt_enable(NULL);
}

void wx_service_set_status(const struct wx_status *st)
{
	status = *st;
}

uint32_t wx_poll_interval_s(void)
{
	return poll_interval_s;
}

uint32_t wx_take_image(uint8_t *dst)
{
	memcpy(dst, rx_buf, FB_BYTES);
	return committed_crc;
}

enum wx_poll_result wx_poll(void)
{
	int64_t deadline = k_uptime_get() + (int64_t)CONFIG_WX_ADV_WINDOW_S * 1000;

	session_result = WX_POLL_NO_CONTACT;
	k_sem_reset(&link_down);

	/*
	 * Advertise until a session succeeds or the window closes. A failed
	 * session (host dropped mid-transfer) re-arms advertising so the host's
	 * retry can still land within the same window.
	 */
	while (session_result == WX_POLL_NO_CONTACT) {
		int64_t remaining = deadline - k_uptime_get();

		if (remaining <= 0) {
			break;
		}

		int err = bt_le_adv_start(&adv_param, ad, ARRAY_SIZE(ad), NULL, 0);

		if (err) {
			LOG_ERR("adv start %d", err);
			break;
		}

		if (k_sem_take(&link_down, K_MSEC(remaining)) == 0) {
			continue; /* a connection came and went */
		}

		/* Window over. Connectable adv stops by itself on connect. */
		(void)bt_le_adv_stop();
		if (cur_conn) {
			/* Session in progress: the inactivity timer bounds this. */
			(void)k_sem_take(&link_down,
					 K_SECONDS(CONFIG_WX_SESSION_TIMEOUT_S + 5));
		}
		break;
	}

	(void)bt_le_adv_stop();
	if (cur_conn) {
		(void)bt_conn_disconnect(cur_conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
		(void)k_sem_take(&link_down, K_SECONDS(5));
	}
	return session_result;
}
