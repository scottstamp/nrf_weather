/*
 * WxPaper Wi-Fi <-> BLE bridge for ESP32-C3 / C6 (docs/bridge.md).
 * Wi-Fi station with build-time credentials; BLE central in wx_ble.c.
 */
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "sdkconfig.h"
#include "wx_ble.h"

static const char *TAG = "wxbridge";

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
	if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
		esp_wifi_connect();
	} else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
		const wifi_event_sta_disconnected_t *d = data;

		ESP_LOGW(TAG, "Wi-Fi disconnected (reason %d), reconnecting", d->reason);
		vTaskDelay(pdMS_TO_TICKS(2000));
		esp_wifi_connect();
	} else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
		const ip_event_got_ip_t *e = data;

		ESP_LOGI(TAG, "Wi-Fi up, IP " IPSTR ", host %s", IP2STR(&e->ip_info.ip),
			 CONFIG_WXB_HOST_URL);
	}
}

static void wifi_start(void)
{
	ESP_ERROR_CHECK(esp_netif_init());
	ESP_ERROR_CHECK(esp_event_loop_create_default());
	esp_netif_t *netif = esp_netif_create_default_wifi_sta();

	esp_netif_set_hostname(netif, "wxbridge");

	wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();

	ESP_ERROR_CHECK(esp_wifi_init(&init));
	ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
	ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));

	wifi_config_t wc = {0};

	strlcpy((char *)wc.sta.ssid, CONFIG_WXB_WIFI_SSID, sizeof(wc.sta.ssid));
	strlcpy((char *)wc.sta.password, CONFIG_WXB_WIFI_PASSWORD, sizeof(wc.sta.password));
	ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
	ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
	/* Modem sleep (the default) is required while BLE shares the radio. */
	ESP_ERROR_CHECK(esp_wifi_start());
}

void app_main(void)
{
	esp_err_t err = nvs_flash_init();

	if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
		ESP_ERROR_CHECK(nvs_flash_erase());
		err = nvs_flash_init();
	}
	ESP_ERROR_CHECK(err);

	if (!CONFIG_WXB_WIFI_SSID[0]) {
		ESP_LOGE(TAG, "no Wi-Fi SSID configured: see sdkconfig.defaults.local.example");
	}
	wifi_start();
	if (wx_ble_start() != 0) {
		ESP_LOGE(TAG, "BLE start failed, rebooting in 10 s");
		vTaskDelay(pdMS_TO_TICKS(10000));
		esp_restart();
	}
}
