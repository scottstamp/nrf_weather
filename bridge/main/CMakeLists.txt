idf_component_register(
    SRCS "main.c" "wx_ble.c" "hostapi.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES bt nvs_flash esp_wifi esp_netif esp_event esp_http_client
)
