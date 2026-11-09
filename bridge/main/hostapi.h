/* HTTP client for `wxd serve` (docs/bridge.md). */
#pragma once

#include <stddef.h>
#include <stdint.h>

#define WX_PAYLOAD_BYTES 9472
#define WX_MAX_SLOTS     2

struct host_slot {
	uint8_t slot;
	uint32_t crc;
	const uint8_t *data; /* WX_PAYLOAD_BYTES, points into host_reply.buf */
};

struct host_reply {
	uint32_t interval_s; /* 0 = leave as is */
	int count;
	struct host_slot slots[WX_MAX_SLOTS];
	uint8_t *buf; /* owned; free with hostapi_reply_free() */
};

/* POST /v1/session with the display's raw Status. 0 on success. */
int hostapi_session(const uint8_t *status, size_t status_len, const char *device,
		    struct host_reply *out);
void hostapi_reply_free(struct host_reply *r);

/* POST /v1/result (best effort, errors only logged). */
void hostapi_result(const char *json);
