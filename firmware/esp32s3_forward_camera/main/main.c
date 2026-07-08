/**
 * Real-time ESP32 camera stream over Wi-Fi (MJPEG).
 */

// =============================== SETUP ======================================

// 1. Board setup:
// Select your camera board in menuconfig:
// Component config > Camera board selection

/**
 * 2. Kconfig setup
 *
 * If you have a Kconfig file, copy the content from
 *  https://github.com/espressif/esp32-camera/blob/master/Kconfig into it.
 * In case you haven't, copy and paste this Kconfig file inside the src directory.
 * This Kconfig file has definitions that allows more control over the camera and
 * how it will be initialized.
 */

/**
 * 3. Enable PSRAM on sdkconfig:
 *
 * CONFIG_ESP32_SPIRAM_SUPPORT=y
 *
 * More info on
 * https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/kconfig.html#config-esp32-spiram-support
 */

// ================================ CODE ======================================

#include "sdkconfig.h"

#include <esp_log.h>
#include <esp_system.h>
#include <esp_wifi.h>
#include <esp_event.h>
#include <esp_netif.h>
#include <esp_psram.h>
#include <nvs_flash.h>
#include <sys/param.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_http_server.h"

// support IDF 5.x
#ifndef portTICK_RATE_MS
#define portTICK_RATE_MS portTICK_PERIOD_MS
#endif

#include "esp_camera.h"

#if defined(CONFIG_CAMERA_AF_SUPPORT) && CONFIG_CAMERA_AF_SUPPORT
#include "esp_camera_af.h"
#endif

#include "camera_pinout.h"

static const char *TAG = "forward:stream";

#if defined(CONFIG_WIFI_ROLE_STA)
#define WIFI_SSID CONFIG_WIFI_SSID
#define WIFI_PASS CONFIG_WIFI_PASSWORD
#endif

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1
#define WIFI_MAX_RETRY     10

static EventGroupHandle_t s_wifi_event_group;
static int s_retry_num = 0;

typedef struct {
    framesize_t frame_size;
    int jpeg_quality;
    int fb_count;
    camera_fb_location_t fb_location;
    camera_grab_mode_t grab_mode;
    const char *label;
} camera_profile_t;

#if ESP_CAMERA_SUPPORTED
static camera_config_t camera_config = {
    .pin_pwdn = CAM_PIN_PWDN,
    .pin_reset = CAM_PIN_RESET,
    .pin_xclk = CAM_PIN_XCLK,
    .pin_sccb_sda = CAM_PIN_SIOD,
    .pin_sccb_scl = CAM_PIN_SIOC,

    .pin_d7 = CAM_PIN_D7,
    .pin_d6 = CAM_PIN_D6,
    .pin_d5 = CAM_PIN_D5,
    .pin_d4 = CAM_PIN_D4,
    .pin_d3 = CAM_PIN_D3,
    .pin_d2 = CAM_PIN_D2,
    .pin_d1 = CAM_PIN_D1,
    .pin_d0 = CAM_PIN_D0,
    .pin_vsync = CAM_PIN_VSYNC,
    .pin_href = CAM_PIN_HREF,
    .pin_pclk = CAM_PIN_PCLK,

    // 10MHz XCLK: more relaxed internal timing on OV2640, often cleaner image.
    // Switch back to 20000000 if FPS is too low.
    .xclk_freq_hz = 10000000,
    .ledc_timer = LEDC_TIMER_0,
    .ledc_channel = LEDC_CHANNEL_0,

    .pixel_format = PIXFORMAT_JPEG,
    .frame_size = FRAMESIZE_QVGA,

    // Lower value is higher quality. Raise this number for higher FPS/lower bandwidth.
    .jpeg_quality = 4,
    // Two frame buffers allow continuous capture and better stream throughput.
    .fb_count = 2,
    .fb_location = CAMERA_FB_IN_PSRAM,
    .grab_mode = CAMERA_GRAB_LATEST,
};

static void apply_sensor_tuning(void)
{
    sensor_t *s = esp_camera_sensor_get();
    if (!s) {
        ESP_LOGW(TAG, "Sensor handle not available for tuning");
        return;
    }

    /* OV2640 tuning based on Espressif's own camera example defaults.
     * Key points:
     *  - AEC2 off: designed for OV3660/OV5640, interferes with OV2640's AE loop
     *  - GAINCEILING_2X: very low gain keeps noise floor down; AEC uses exposure time instead
     *  - raw_gma + lenc: gamma correction and lens shading correction, biggest visual improvement
     *  - BPC off / WPC+DCW on: matches Espressif's validated defaults */
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_wb_mode(s, 0);
    s->set_exposure_ctrl(s, 1);
    s->set_gain_ctrl(s, 1);
    s->set_aec2(s, 0);
    s->set_gainceiling(s, GAINCEILING_16X);
    s->set_brightness(s, 0);
    s->set_ae_level(s, 0);
    s->set_contrast(s, 0);
    s->set_saturation(s, 0);
    s->set_bpc(s, 0);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);
    s->set_lenc(s, 1);
    s->set_dcw(s, 1);

    ESP_LOGI(TAG, "Applied forward-camera sensor tuning (OV2640)");
}

static esp_err_t init_camera(void)
{
    bool has_psram = esp_psram_is_initialized();
    camera_profile_t primary;
    camera_profile_t fallback;

    if (has_psram) {
        primary = (camera_profile_t){
            .frame_size = FRAMESIZE_VGA,
            .jpeg_quality = 8,
            .fb_count = 2,
            .fb_location = CAMERA_FB_IN_PSRAM,
            .grab_mode = CAMERA_GRAB_LATEST,
            .label = "PSRAM profile (VGA)",
        };
        fallback = (camera_profile_t){
            .frame_size = FRAMESIZE_QVGA,
            .jpeg_quality = 10,
            .fb_count = 2,
            .fb_location = CAMERA_FB_IN_PSRAM,
            .grab_mode = CAMERA_GRAB_LATEST,
            .label = "PSRAM fallback (QVGA)",
        };
    } else {
        primary = (camera_profile_t){
            .frame_size = FRAMESIZE_QVGA,
            .jpeg_quality = 12,
            .fb_count = 1,
            .fb_location = CAMERA_FB_IN_DRAM,
            .grab_mode = CAMERA_GRAB_WHEN_EMPTY,
            .label = "DRAM profile (QVGA)",
        };
        fallback = (camera_profile_t){
            .frame_size = FRAMESIZE_QQVGA,
            .jpeg_quality = 14,
            .fb_count = 1,
            .fb_location = CAMERA_FB_IN_DRAM,
            .grab_mode = CAMERA_GRAB_WHEN_EMPTY,
            .label = "DRAM fallback (QQVGA)",
        };
    }

    camera_config.frame_size = primary.frame_size;
    camera_config.jpeg_quality = primary.jpeg_quality;
    camera_config.fb_count = primary.fb_count;
    camera_config.fb_location = primary.fb_location;
    camera_config.grab_mode = primary.grab_mode;

    ESP_LOGI(TAG, "Camera init with %s", primary.label);
    esp_err_t err = esp_camera_init(&camera_config);
    if (err == ESP_OK) {
        apply_sensor_tuning();
        return ESP_OK;
    }

    ESP_LOGW(TAG, "Primary profile failed: %s. Retrying with fallback profile.", esp_err_to_name(err));

    camera_config.frame_size = fallback.frame_size;
    camera_config.jpeg_quality = fallback.jpeg_quality;
    camera_config.fb_count = fallback.fb_count;
    camera_config.fb_location = fallback.fb_location;
    camera_config.grab_mode = fallback.grab_mode;

    ESP_LOGI(TAG, "Camera init with %s", fallback.label);
    err = esp_camera_init(&camera_config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Camera Init Failed: %s", esp_err_to_name(err));
        return err;
    }

    apply_sensor_tuning();

    return ESP_OK;
}

#if defined(CONFIG_WIFI_ROLE_STA)

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry_num < WIFI_MAX_RETRY) {
            esp_wifi_connect();
            s_retry_num++;
            ESP_LOGW(TAG, "Retrying Wi-Fi connection (%d/%d)", s_retry_num, WIFI_MAX_RETRY);
        } else {
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Connected. Open http://" IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static esp_err_t init_wifi_sta(void)
{
    s_wifi_event_group = xEventGroupCreate();
    if (s_wifi_event_group == NULL) {
        ESP_LOGE(TAG, "Failed to create Wi-Fi event group");
        return ESP_FAIL;
    }

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT,
                                                        ESP_EVENT_ANY_ID,
                                                        &wifi_event_handler,
                                                        NULL,
                                                        &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT,
                                                        IP_EVENT_STA_GOT_IP,
                                                        &wifi_event_handler,
                                                        NULL,
                                                        &instance_got_ip));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASS,
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
                                           WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                                           pdFALSE,
                                           pdFALSE,
                                           pdMS_TO_TICKS(20000));

    if (bits & WIFI_CONNECTED_BIT) {
        return ESP_OK;
    }

    if (bits & WIFI_FAIL_BIT) {
        ESP_LOGE(TAG, "Wi-Fi authentication failed for SSID: %s", WIFI_SSID);
    } else {
        ESP_LOGE(TAG, "Wi-Fi connection timed out for SSID: %s", WIFI_SSID);
    }
    vEventGroupDelete(s_wifi_event_group);
    return ESP_FAIL;
}

#endif /* CONFIG_WIFI_ROLE_STA */

#if defined(CONFIG_WIFI_ROLE_AP)

static esp_err_t init_wifi_ap(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    const char *ssid = CONFIG_AP_SSID;
    const char *pass = CONFIG_AP_PASSWORD;

    wifi_config_t ap_config = {};
    strncpy((char *)ap_config.ap.ssid, ssid, sizeof(ap_config.ap.ssid) - 1);
    ap_config.ap.ssid_len     = (uint8_t)strlen(ssid);
    strncpy((char *)ap_config.ap.password, pass, sizeof(ap_config.ap.password) - 1);
    ap_config.ap.max_connection = CONFIG_AP_MAX_CONN;
    ap_config.ap.authmode       = (strlen(pass) >= 8) ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_config));
    /* Disable power-save so streaming clients see consistent throughput. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "AP started — SSID: \"%s\" | IP: 192.168.4.1", ssid);
    ESP_LOGI(TAG, "Connect PC to \"%s\" then open http://192.168.4.1", ssid);
    return ESP_OK;
}

#endif /* CONFIG_WIFI_ROLE_AP */

/* Start mDNS so clients can reach this device by hostname instead of IP.
 * AP unit:  forward-cam.local
 * STA unit: eye-cam.local
 * The mdns component must be listed in idf_component.yml or CMakeLists REQUIRES. */
#include "mdns.h"
static void start_mdns(void)
{
#if defined(CONFIG_WIFI_ROLE_AP)
    const char *hostname = "forward-cam";
#else
    const char *hostname = "eye-cam";
#endif
    esp_err_t err = mdns_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "mDNS init failed: %s", esp_err_to_name(err));
        return;
    }
    mdns_hostname_set(hostname);
    mdns_instance_name_set("ESP32-S3 camera stream");
    mdns_service_add(NULL, "_http", "_tcp", 80, NULL, 0);
    ESP_LOGI(TAG, "mDNS ready — http://%s.local/stream", hostname);
}

static esp_err_t index_handler(httpd_req_t *req)
{
    static const char html[] =
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Forward Camera (ESP32-S3)</title></head><body style=\"margin:0;background:#111\">"
        "<img src=\"/stream\" style=\"width:100%;max-width:960px;display:block;margin:auto\">"
        "</body></html>";

    httpd_resp_set_type(req, "text/html");
    return httpd_resp_send(req, html, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t stream_handler(httpd_req_t *req)
{
    camera_fb_t *fb = NULL;
    esp_err_t res = ESP_OK;
    size_t jpg_len = 0;
    uint8_t *jpg_buf = NULL;
    char part_buf[64];

    httpd_resp_set_hdr(req, "Cache-Control", "no-store, no-cache, must-revalidate, private");
    httpd_resp_set_hdr(req, "Pragma", "no-cache");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    res = httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=frame");
    if (res != ESP_OK) {
        return res;
    }

    while (true) {
        fb = esp_camera_fb_get();
        if (!fb) {
            ESP_LOGW(TAG, "Frame capture failed");
            continue;
        }

        if (fb->format == PIXFORMAT_JPEG) {
            jpg_buf = fb->buf;
            jpg_len = fb->len;
        } else {
            bool converted = frame2jpg(fb, 80, &jpg_buf, &jpg_len);
            if (!converted) {
                ESP_LOGW(TAG, "JPEG conversion failed");
                esp_camera_fb_return(fb);
                continue;
            }
        }

        int hlen = snprintf(part_buf, sizeof(part_buf),
                            "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                            (unsigned int)jpg_len);

        res = httpd_resp_send_chunk(req, part_buf, hlen);
        if (res == ESP_OK) {
            res = httpd_resp_send_chunk(req, (const char *)jpg_buf, jpg_len);
        }
        if (res == ESP_OK) {
            res = httpd_resp_send_chunk(req, "\r\n", 2);
        }

        if (fb->format != PIXFORMAT_JPEG && jpg_buf) {
            free(jpg_buf);
            jpg_buf = NULL;
        }
        esp_camera_fb_return(fb);
        fb = NULL;

        if (res != ESP_OK) {
            break;
        }
    }

    ESP_LOGI(TAG, "Stream client disconnected");
    return res;
}

static void start_webserver(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 80;

    httpd_handle_t server = NULL;
    if (httpd_start(&server, &config) == ESP_OK) {
        httpd_uri_t index_uri = {
            .uri = "/",
            .method = HTTP_GET,
            .handler = index_handler,
            .user_ctx = NULL,
        };

        httpd_uri_t stream_uri = {
            .uri = "/stream",
            .method = HTTP_GET,
            .handler = stream_handler,
            .user_ctx = NULL,
        };

        httpd_register_uri_handler(server, &index_uri);
        httpd_register_uri_handler(server, &stream_uri);
        ESP_LOGI(TAG, "Web server started");
    } else {
        ESP_LOGE(TAG, "Failed to start web server");
    }
}

#if defined(CONFIG_CAMERA_AF_SUPPORT) && CONFIG_CAMERA_AF_SUPPORT
static void maybe_init_autofocus(void)
{
    sensor_t *s = esp_camera_sensor_get();
    if (!s) {
        ESP_LOGW(TAG, "AF: no sensor handle");
        return;
    }

    if (!esp_camera_af_is_supported(s)) {
        ESP_LOGI(TAG, "AF: not supported by this sensor");
        return;
    }

    esp_camera_af_config_t af_cfg = {
        .mode = ESP_CAMERA_AF_MODE_AUTO,
        .timeout_ms = CONFIG_CAMERA_AF_DEFAULT_TIMEOUT_MS,
    };

    esp_err_t ret = esp_camera_af_init(s, &af_cfg);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "AF init failed: %s", esp_err_to_name(ret));
        return;
    }

    ESP_LOGI(TAG, "AF initialized (AUTO mode)");
}
#endif
#endif

void app_main(void)
{
#if ESP_CAMERA_SUPPORTED
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    if(ESP_OK != init_camera()) {
        return;
    }

#if defined(CONFIG_CAMERA_AF_SUPPORT) && CONFIG_CAMERA_AF_SUPPORT
    // Initialize autofocus if configured and supported by the sensor.
    // In menuconfig: Component config > Camera configuration > Enable autofocus support
    maybe_init_autofocus();
#endif

#if defined(CONFIG_WIFI_ROLE_AP)
    if (init_wifi_ap() != ESP_OK) {
        return;
    }
#else
    if (init_wifi_sta() != ESP_OK) {
        return;
    }
#endif
    start_mdns();
    start_webserver();

    while (1) {
        vTaskDelay(1000 / portTICK_RATE_MS);
    }
#else
    ESP_LOGE(TAG, "Camera support is not available for this chip");
    return;
#endif
}