/* BLE central side of the bridge (docs/bridge.md). */
#pragma once

/* Init NimBLE and start scanning; sessions run in their own task. */
int wx_ble_start(void);
