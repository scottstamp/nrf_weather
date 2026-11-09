/*
 * BLE central: scan for the WxPaper display, run one protocol v2 session per
 * appearance, with the images supplied by `wxd serve` (docs/bridge.md,
 * docs/protocol.md).
 *
 * Threading: NimBLE callbacks run in the NimBLE host task and only record
 * results + give a semaphore. The session itself is a plain sequential
 * function in its own task, so the protocol reads top to bottom.
 */
#include "wx_ble.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "hostapi.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "sdkconfig.h"

static const char *TAG = "wx_ble";

/* ---- protocol v2 (keep in sync with docs/protocol.md) ---- */
#define PROTO_VERSION   2
#define STATUS_LEN_V2   32
#define OP_BEGIN        0x01
#define OP_COMMIT       0x02
#define OP_DONE         0x03
#define OP_SET_INTERVAL 0x04
#define FORMAT_RAW      0

/* 7a1e000N-5c3b-4f2a-9e0d-2b6f8c4d1a90, little-endian */
#define WX_UUID(n)                                                                                 \
	BLE_UUID128_INIT(0x90, 0x1a, 0x4d, 0x8c, 0x6f, 0x2b, 0x0d, 0x9e, 0x2a, 0x4f, 0x3b, 0x5c,   \
			 (n), 0x00, 0x1e, 0x7a)
static const ble_uuid128_t svc_uuid = WX_UUID(1);
static const ble_uuid128_t status_uuid = WX_UUID(2);
static const ble_uuid128_t control_uuid = WX_UUID(3);
static const ble_uuid128_t data_uuid = WX_UUID(4);
#define DEVICE_NAME "WxPaper"

#define CONNECT_TIMEOUT_MS 10000
#define OP_TIMEOUT_MS      10000

/* ---- state shared with NimBLE callbacks ---- */
struct found_msg {
	ble_addr_t addr;
	int8_t rssi;
};
static QueueHandle_t found_q;      /* struct found_msg: a matching advertiser */
static SemaphoreHandle_t op_sem;   /* given by every async op callback */
static volatile int op_status;     /* result of the last async op (0 = ok) */
static volatile bool connected;
static volatile uint16_t conn_handle;
static volatile uint16_t mtu = 23;
static volatile bool scanning_wanted;

static uint16_t svc_start, svc_end;
static uint16_t h_status, h_control, h_data;
static uint8_t status_buf[64];
static uint16_t status_len;

static int64_t ignore_until_us;
static ble_addr_t ignore_addr;

static uint8_t own_addr_type;

static int gap_event(struct ble_gap_event *event, void *arg);

/* ---------------------------------------------------------------- helpers */

static void addr_str(const ble_addr_t *a, char *out, size_t n)
{
	snprintf(out, n, "%02X:%02X:%02X:%02X:%02X:%02X", a->val[5], a->val[4], a->val[3],
		 a->val[2], a->val[1], a->val[0]);
}

/* get_le32 / put_le32 come from NimBLE's os/endian.h */

/* Wait for the async op started just before. Returns its status, or
 * BLE_HS_ETIMEOUT / BLE_HS_ENOTCONN. */
static int op_wait(void)
{
	if (xSemaphoreTake(op_sem, pdMS_TO_TICKS(OP_TIMEOUT_MS)) != pdTRUE) {
		return BLE_HS_ETIMEOUT;
	}
	return op_status;
}

static void op_done(int status)
{
	op_status = status;
	xSemaphoreGive(op_sem);
}

static bool adv_matches(const struct ble_gap_disc_desc *d)
{
	if (CONFIG_WXB_DEVICE_ADDR[0]) {
		char a[18];

		addr_str(&d->addr, a, sizeof(a));
		return strcasecmp(a, CONFIG_WXB_DEVICE_ADDR) == 0;
	}

	struct ble_hs_adv_fields f;

	if (ble_hs_adv_parse_fields(&f, d->data, d->length_data) != 0) {
		return false;
	}
	for (int i = 0; i < f.num_uuids128; i++) {
		if (ble_uuid_cmp(&f.uuids128[i].u, &svc_uuid.u) == 0) {
			return true;
		}
	}
	return f.name && f.name_len == strlen(DEVICE_NAME) &&
	       memcmp(f.name, DEVICE_NAME, f.name_len) == 0;
}

/* ---------------------------------------------------------------- scanning */

static void scan_start(void)
{
	scanning_wanted = true;
	if (ble_gap_disc_active()) {
		return;
	}
	/*
	 * Passive, ~60 % duty: the display advertises every 100 ms for 30 s, so
	 * it is caught within a few hundred ms; the gaps leave airtime for Wi-Fi
	 * (shared radio).
	 */
	struct ble_gap_disc_params p = {
		.itvl = 80,   /* 50 ms */
		.window = 48, /* 30 ms */
		.passive = 1,
		.filter_duplicates = 0,
	};
	int rc = ble_gap_disc(own_addr_type, BLE_HS_FOREVER, &p, gap_event, NULL);

	if (rc != 0 && rc != BLE_HS_EALREADY) {
		ESP_LOGE(TAG, "scan start rc=%d", rc);
	}
}

static void scan_stop(void)
{
	scanning_wanted = false;
	if (ble_gap_disc_active()) {
		(void)ble_gap_disc_cancel();
	}
}

/* ---------------------------------------------------------------- GAP */

static int gap_event(struct ble_gap_event *event, void *arg)
{
	switch (event->type) {
	case BLE_GAP_EVENT_DISC: {
		const struct ble_gap_disc_desc *d = &event->disc;

		if (!adv_matches(d)) {
			return 0;
		}
		if (esp_timer_get_time() < ignore_until_us &&
		    ble_addr_cmp(&d->addr, &ignore_addr) == 0) {
			return 0; /* BLE stacks keep reporting the last advert for a moment */
		}
		struct found_msg msg = {d->addr, d->rssi};

		(void)xQueueSend(found_q, &msg, 0);
		return 0;
	}
	case BLE_GAP_EVENT_DISC_COMPLETE:
		if (scanning_wanted) {
			scan_start();
		}
		return 0;
	case BLE_GAP_EVENT_CONNECT:
		if (event->connect.status == 0) {
			conn_handle = event->connect.conn_handle;
			connected = true;
		}
		op_done(event->connect.status);
		return 0;
	case BLE_GAP_EVENT_DISCONNECT:
		ESP_LOGI(TAG, "disconnected, reason 0x%x", event->disconnect.reason);
		connected = false;
		op_done(BLE_HS_ENOTCONN); /* wakes any waiter */
		return 0;
	case BLE_GAP_EVENT_MTU:
		mtu = event->mtu.value;
		return 0;
	case BLE_GAP_EVENT_CONN_UPDATE:
		if (event->conn_update.status == 0) {
			struct ble_gap_conn_desc desc;

			if (ble_gap_conn_find(event->conn_update.conn_handle, &desc) == 0) {
				ESP_LOGI(TAG, "conn interval %u.%02u ms",
					 desc.conn_itvl * 125 / 100, (desc.conn_itvl * 125) % 100);
			}
		}
		return 0;
	default:
		return 0;
	}
}

/* ---------------------------------------------------------------- GATT callbacks */

static int mtu_cb(uint16_t ch, const struct ble_gatt_error *err, uint16_t m, void *arg)
{
	if (err->status == 0) {
		mtu = m;
	}
	op_done(err->status);
	return 0;
}

static int svc_cb(uint16_t ch, const struct ble_gatt_error *err, const struct ble_gatt_svc *svc,
		  void *arg)
{
	if (err->status == 0) {
		svc_start = svc->start_handle;
		svc_end = svc->end_handle;
		return 0;
	}
	op_done(err->status == BLE_HS_EDONE ? 0 : err->status);
	return 0;
}

static int chr_cb(uint16_t ch, const struct ble_gatt_error *err, const struct ble_gatt_chr *chr,
		  void *arg)
{
	if (err->status == 0) {
		if (ble_uuid_cmp(&chr->uuid.u, &status_uuid.u) == 0) {
			h_status = chr->val_handle;
		} else if (ble_uuid_cmp(&chr->uuid.u, &control_uuid.u) == 0) {
			h_control = chr->val_handle;
		} else if (ble_uuid_cmp(&chr->uuid.u, &data_uuid.u) == 0) {
			h_data = chr->val_handle;
		}
		return 0;
	}
	op_done(err->status == BLE_HS_EDONE ? 0 : err->status);
	return 0;
}

static int read_cb(uint16_t ch, const struct ble_gatt_error *err, struct ble_gatt_attr *attr,
		   void *arg)
{
	if (err->status == 0 && attr) {
		uint16_t n = OS_MBUF_PKTLEN(attr->om);

		if (n > sizeof(status_buf)) {
			n = sizeof(status_buf);
		}
		(void)os_mbuf_copydata(attr->om, 0, n, status_buf);
		status_len = n;
	}
	op_done(err->status);
	return 0;
}

static int write_cb(uint16_t ch, const struct ble_gatt_error *err, struct ble_gatt_attr *attr,
		    void *arg)
{
	op_done(err->status);
	return 0;
}

/* ---------------------------------------------------------------- session ops */

static int control_write(const uint8_t *buf, uint16_t len)
{
	xSemaphoreTake(op_sem, 0);
	int rc = ble_gattc_write_flat(conn_handle, h_control, buf, len, write_cb, NULL);

	return rc ? rc : op_wait();
}

static int data_write(const uint8_t *buf, uint16_t len, bool response)
{
	if (response) {
		xSemaphoreTake(op_sem, 0);
		int rc = ble_gattc_write_flat(conn_handle, h_data, buf, len, write_cb, NULL);

		return rc ? rc : op_wait();
	}
	/* Write Without Response: back off while the host is out of buffers. */
	for (int tries = 0; tries < 500; tries++) {
		int rc = ble_gattc_write_no_rsp_flat(conn_handle, h_data, buf, len);

		if (rc != BLE_HS_ENOMEM && rc != BLE_HS_EBUSY) {
			return rc;
		}
		if (!connected) {
			return BLE_HS_ENOTCONN;
		}
		vTaskDelay(pdMS_TO_TICKS(5));
	}
	return BLE_HS_ETIMEOUT;
}

static int upload_slot(const struct host_slot *s, bool response)
{
	uint8_t begin[15];
	uint8_t chunk[256];
	uint16_t step = (mtu > 247 ? 247 : mtu) - 3 - 2; /* ATT header, u16 offset */
	int64_t t0 = esp_timer_get_time();
	int rc;

	begin[0] = OP_BEGIN;
	put_le32(&begin[1], WX_PAYLOAD_BYTES);
	put_le32(&begin[5], s->crc);
	begin[9] = FORMAT_RAW;
	put_le32(&begin[10], (uint32_t)(esp_timer_get_time() / 1000000)); /* only "contact" */
	begin[14] = s->slot;
	rc = control_write(begin, sizeof(begin));
	if (rc) {
		return rc;
	}
	int writes = 0;

	for (uint32_t off = 0; off < WX_PAYLOAD_BYTES; off += step) {
		uint16_t n = WX_PAYLOAD_BYTES - off < step ? WX_PAYLOAD_BYTES - off : step;

		chunk[0] = off & 0xff;
		chunk[1] = off >> 8;
		memcpy(&chunk[2], s->data + off, n);
		rc = data_write(chunk, n + 2, response);
		if (rc) {
			return rc;
		}
		writes++;
	}
	uint8_t commit = OP_COMMIT;

	rc = control_write(&commit, 1);
	ESP_LOGI(TAG, "slot %u: %d writes of %u B, rsp=%d, %lld ms, commit rc=%d", s->slot, writes,
		 step + 2, response, (esp_timer_get_time() - t0) / 1000, rc);
	return rc;
}

static void terminate(void)
{
	if (connected) {
		xSemaphoreTake(op_sem, 0);
		if (ble_gap_terminate(conn_handle, BLE_ERR_REM_USER_CONN_TERM) == 0) {
			(void)op_wait(); /* until DISCONNECT */
		}
	}
}

/* One complete session; the outcome is logged and reported to the host. */
static void run_session(const ble_addr_t *addr, int8_t rssi)
{
	char dev[18], err[64] = "";
	char slots_json[16] = "[]";
	const char *outcome = "failed";
	struct host_reply reply = {0};
	uint32_t batt_mv = 0;
	int64_t t0 = esp_timer_get_time();
	int rc;

	addr_str(addr, dev, sizeof(dev));
	ESP_LOGI(TAG, "session %s rssi %d", dev, rssi);

	/* connect */
	const struct ble_gap_conn_params cp = {
		.scan_itvl = 16,
		.scan_window = 16,
		.itvl_min = 12, /* 15 ms (the display asks for 7.5-15 ms itself) */
		.itvl_max = 24, /* 30 ms */
		.latency = 0,
		.supervision_timeout = 400, /* 4 s */
		.min_ce_len = 0,
		.max_ce_len = 0,
	};
	mtu = 23;
	h_status = h_control = h_data = 0;
	svc_start = svc_end = 0;
	xSemaphoreTake(op_sem, 0);
	rc = ble_gap_connect(own_addr_type, addr, CONNECT_TIMEOUT_MS, &cp, gap_event, NULL);
	if (rc == 0) {
		rc = op_wait();
	}
	if (rc || !connected) {
		snprintf(err, sizeof(err), "connect rc=%d", rc);
		goto done;
	}

	/* link setup: bigger MTU, then let the controller pick DLE/2M if both support it */
	xSemaphoreTake(op_sem, 0);
	rc = ble_gattc_exchange_mtu(conn_handle, mtu_cb, NULL);
	rc = rc ? rc : op_wait();
	if (rc) {
		snprintf(err, sizeof(err), "mtu rc=%d", rc);
		goto done;
	}

	/* discovery */
	xSemaphoreTake(op_sem, 0);
	rc = ble_gattc_disc_svc_by_uuid(conn_handle, &svc_uuid.u, svc_cb, NULL);
	rc = rc ? rc : op_wait();
	if (rc || !svc_start) {
		snprintf(err, sizeof(err), "service discovery rc=%d", rc);
		goto done;
	}
	xSemaphoreTake(op_sem, 0);
	rc = ble_gattc_disc_all_chrs(conn_handle, svc_start, svc_end, chr_cb, NULL);
	rc = rc ? rc : op_wait();
	if (rc || !h_status || !h_control || !h_data) {
		snprintf(err, sizeof(err), "chr discovery rc=%d", rc);
		goto done;
	}

	/* status */
	status_len = 0;
	xSemaphoreTake(op_sem, 0);
	rc = ble_gattc_read(conn_handle, h_status, read_cb, NULL);
	rc = rc ? rc : op_wait();
	if (rc || status_len < 24) {
		snprintf(err, sizeof(err), "status read rc=%d len=%u", rc, status_len);
		goto done;
	}
	if (status_buf[0] != PROTO_VERSION) {
		snprintf(err, sizeof(err), "display protocol v%u", status_buf[0]);
		goto done;
	}
	batt_mv = status_buf[4] | (status_buf[5] << 8);
	uint32_t dev_interval = get_le32(&status_buf[16]);

	/* ask the host what to send (the display waits; its inactivity timeout is 20 s) */
	if (hostapi_session(status_buf, status_len, dev, &reply) != 0) {
		snprintf(err, sizeof(err), "host unavailable");
		goto done; /* no DONE: the display counts this as no contact */
	}

	if (reply.interval_s && reply.interval_s != dev_interval) {
		uint8_t si[5] = {OP_SET_INTERVAL};

		put_le32(&si[1], reply.interval_s);
		rc = control_write(si, sizeof(si));
		ESP_LOGI(TAG, "SET_INTERVAL %lu rc=%d", (unsigned long)reply.interval_s, rc);
		if (rc == BLE_HS_ENOTCONN || rc == BLE_HS_ETIMEOUT) {
			snprintf(err, sizeof(err), "set interval rc=%d", rc);
			goto done;
		}
	}

	int n = 0;

	for (int i = 0; i < reply.count; i++) {
		rc = upload_slot(&reply.slots[i], false);
		if (rc && connected) {
			ESP_LOGW(TAG, "slot %u attempt 1 rc=%d, retrying with responses",
				 reply.slots[i].slot, rc);
			rc = upload_slot(&reply.slots[i], true);
		}
		if (rc) {
			snprintf(err, sizeof(err), "slot %u upload rc=%d", reply.slots[i].slot, rc);
			goto done;
		}
		n += snprintf(slots_json + n, sizeof(slots_json) - n, "%s%u", n ? "," : "[",
			      reply.slots[i].slot);
	}
	if (n) {
		snprintf(slots_json + n, sizeof(slots_json) - n, "]");
	}

	/* DONE: the display drops the link right after; a lost ack is still success */
	uint8_t done[5] = {OP_DONE};

	put_le32(&done[1], (uint32_t)(esp_timer_get_time() / 1000000));
	rc = control_write(done, sizeof(done));
	if (rc && rc != BLE_HS_ENOTCONN) {
		snprintf(err, sizeof(err), "done rc=%d", rc);
		goto done;
	}
	outcome = reply.count ? "uploaded" : "no_change";
	ignore_addr = *addr;
	ignore_until_us = esp_timer_get_time() + (int64_t)CONFIG_WXB_DEBOUNCE_S * 1000000;

done:
	terminate();
	hostapi_reply_free(&reply);

	int ms = (int)((esp_timer_get_time() - t0) / 1000);

	if (err[0]) {
		ESP_LOGW(TAG, "session %s failed after %d ms: %s", dev, ms, err);
		/* short pause so a broken session doesn't hammer the display */
		ignore_addr = *addr;
		ignore_until_us = esp_timer_get_time() + 2000000;
	} else {
		ESP_LOGI(TAG, "session %s: %s slots=%s %d ms", dev, outcome, slots_json, ms);
	}

	char json[256];

	snprintf(json, sizeof(json),
		 "{\"device\":\"%s\",\"outcome\":\"%s\",\"slots\":%s,\"rssi\":%d,\"ms\":%d,"
		 "\"batt_mv\":%lu,\"error\":\"%s\"}",
		 dev, outcome, slots_json, rssi, ms, (unsigned long)batt_mv, err);
	hostapi_result(json);
}

/* ---------------------------------------------------------------- tasks */

static void session_task(void *arg)
{
	struct found_msg msg;

	for (;;) {
		if (xQueueReceive(found_q, &msg, portMAX_DELAY) != pdTRUE) {
			continue;
		}
		scan_stop();
		run_session(&msg.addr, msg.rssi);
		xQueueReset(found_q); /* drop adverts queued during the session */
		scan_start();
	}
}

static void on_sync(void)
{
	int rc = ble_hs_util_ensure_addr(0);

	if (rc == 0) {
		rc = ble_hs_id_infer_auto(0, &own_addr_type);
	}
	if (rc) {
		ESP_LOGE(TAG, "address setup rc=%d", rc);
		return;
	}
	ESP_LOGI(TAG, "BLE ready, scanning for %s",
		 CONFIG_WXB_DEVICE_ADDR[0] ? CONFIG_WXB_DEVICE_ADDR : DEVICE_NAME);
	scan_start();
}

static void on_reset(int reason)
{
	ESP_LOGW(TAG, "BLE host reset, reason %d", reason);
}

static void host_task(void *param)
{
	nimble_port_run();
	nimble_port_freertos_deinit();
}

int wx_ble_start(void)
{
	found_q = xQueueCreate(4, sizeof(struct found_msg));
	op_sem = xSemaphoreCreateBinary();
	if (!found_q || !op_sem) {
		return -1;
	}
	int rc = nimble_port_init();

	if (rc != ESP_OK) {
		ESP_LOGE(TAG, "nimble init %d", rc);
		return rc;
	}
	ble_hs_cfg.sync_cb = on_sync;
	ble_hs_cfg.reset_cb = on_reset;

	xTaskCreate(session_task, "wx_session", 6144, NULL, 5, NULL);
	nimble_port_freertos_init(host_task);
	return 0;
}
