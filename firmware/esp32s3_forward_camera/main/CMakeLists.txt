idf_component_register(
    SRCS "main.c"
    PRIV_INCLUDE_DIRS "."
    PRIV_REQUIRES nvs_flash esp_psram esp32-camera esp_wifi esp_event esp_netif esp_http_server mdns
)
