/*
 * HTTP client side of docs/bridge.md: POST /v1/session, POST /v1/result.
 */
#include "hostapi.h"

#include <stdlib.h>
#include <string.h>

#include "esp_http_client.h"
#include "esp_log.h"
#include "sdkconfig.h"

static const char *TAG = "hostapi";

#define REPLY_MAX (9 + WX_MAX_SLOTS * (5 + WX_PAYLOAD_BYTES))

static uint32_t get_le32(const uint8_t *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
	       ((uint32_t)p[3] << 24);
}

static esp_http_client_handle_t client_for(const char *path, const char *ctype)
{
	char url[160];

	snprintf(url, sizeof(url), "%s%s", CONFIG_WXB_HOST_URL, path);
	esp_http_client_config_t cfg = {
		.url = url,
		.method = HTTP_METHOD_POST,
		.timeout_ms = CONFIG_WXB_HTTP_TIMEOUT_MS,
		.disable_auto_redirect = true,
	};
	esp_http_client_handle_t c = esp_http_client_init(&cfg);

	if (c) {
		esp_http_client_set_header(c, "Content-Type", ctype);
	}
	return c;
}

int hostapi_session(const uint8_t *status, size_t status_len, const char *device,
		    struct host_reply *out)
{
	int ret = -1;
	uint8_t *buf = NULL;
	esp_http_client_handle_t c = client_for("/v1/session", "application/octet-stream");

	memset(out, 0, sizeof(*out));
	if (!c) {
		return -1;
	}
	if (device) {
		esp_http_client_set_header(c, "X-Wx-Device", device);
	}

	esp_err_t err = esp_http_client_open(c, (int)status_len);

	if (err != ESP_OK) {
		ESP_LOGW(TAG, "connect %s: %s", CONFIG_WXB_HOST_URL, esp_err_to_name(err));
		goto out;
	}
	if (esp_http_client_write(c, (const char *)status, (int)status_len) != (int)status_len) {
		ESP_LOGW(TAG, "write failed");
		goto out;
	}
	(void)esp_http_client_fetch_headers(c);
	int code = esp_http_client_get_status_code(c);

	if (code != 200) {
		ESP_LOGW(TAG, "/v1/session -> HTTP %d", code);
		goto out;
	}

	buf = malloc(REPLY_MAX);
	if (!buf) {
		ESP_LOGE(TAG, "no memory for reply");
		goto out;
	}
	int len = 0;

	for (;;) {
		int n = esp_http_client_read(c, (char *)buf + len, REPLY_MAX - len);

		if (n < 0) {
			ESP_LOGW(TAG, "read error");
			goto out;
		}
		if (n == 0) {
			break;
		}
		len += n;
		if (len == REPLY_MAX) {
			break;
		}
	}

	/* WXB1 | u32 interval | u8 count | count x {u8 slot, u32 crc, payload} */
	if (len < 9 || memcmp(buf, "WXB1", 4) != 0) {
		ESP_LOGW(TAG, "bad reply (%d B)", len);
		goto out;
	}
	out->interval_s = get_le32(&buf[4]);
	int count = buf[8];
	int off = 9;

	if (count > WX_MAX_SLOTS) {
		ESP_LOGW(TAG, "bad slot count %d", count);
		goto out;
	}
	for (int i = 0; i < count; i++) {
		if (off + 5 + WX_PAYLOAD_BYTES > len) {
			ESP_LOGW(TAG, "truncated reply (%d B)", len);
			goto out;
		}
		out->slots[i].slot = buf[off];
		out->slots[i].crc = get_le32(&buf[off + 1]);
		out->slots[i].data = &buf[off + 5];
		off += 5 + WX_PAYLOAD_BYTES;
	}
	out->count = count;
	out->buf = buf;
	buf = NULL;
	ret = 0;
out:
	free(buf);
	esp_http_client_close(c);
	esp_http_client_cleanup(c);
	return ret;
}

void hostapi_reply_free(struct host_reply *r)
{
	free(r->buf);
	r->buf = NULL;
	r->count = 0;
}

void hostapi_result(const char *json)
{
	esp_http_client_handle_t c = client_for("/v1/result", "application/json");

	if (!c) {
		return;
	}
	esp_http_client_set_post_field(c, json, (int)strlen(json));
	esp_err_t err = esp_http_client_perform(c);

	if (err != ESP_OK) {
		ESP_LOGW(TAG, "/v1/result: %s", esp_err_to_name(err));
	}
	esp_http_client_cleanup(c);
}
