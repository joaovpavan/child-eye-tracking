// Board selection is defined via Kconfig (menuconfig):
// Component config > Camera board selection

// If no board is selected yet, pick a sensible default for each target.
#if !defined(CONFIG_CAMERA_BOARD_WROVER_KIT) && \
	!defined(CONFIG_CAMERA_BOARD_ESP32CAM_AITHINKER) && \
	!defined(CONFIG_CAMERA_BOARD_ESP32S3_WROOM) && \
	!defined(CONFIG_CAMERA_BOARD_ESP32S3_GOOUUU) && \
	!defined(CONFIG_CAMERA_BOARD_ESP32S3_XIAO)
#if defined(CONFIG_IDF_TARGET_ESP32)
#define CONFIG_CAMERA_BOARD_ESP32CAM_AITHINKER 1
#elif defined(CONFIG_IDF_TARGET_ESP32S3)
#define CONFIG_CAMERA_BOARD_ESP32S3_WROOM 1
#endif
#endif

#if defined(CONFIG_IDF_TARGET_ESP32) && \
	(defined(CONFIG_CAMERA_BOARD_ESP32S3_WROOM) || \
	 defined(CONFIG_CAMERA_BOARD_ESP32S3_GOOUUU) || \
	 defined(CONFIG_CAMERA_BOARD_ESP32S3_XIAO))
#error "Selected ESP32-S3 board while IDF target is ESP32. Change target or board selection."
#endif

#if defined(CONFIG_IDF_TARGET_ESP32S3) && \
	(defined(CONFIG_CAMERA_BOARD_WROVER_KIT) || \
	 defined(CONFIG_CAMERA_BOARD_ESP32CAM_AITHINKER))
#error "Selected ESP32 board while IDF target is ESP32-S3. Change target or board selection."
#endif

#if !defined(CONFIG_CAMERA_BOARD_WROVER_KIT) && \
	!defined(CONFIG_CAMERA_BOARD_ESP32CAM_AITHINKER) && \
	!defined(CONFIG_CAMERA_BOARD_ESP32S3_WROOM) && \
	!defined(CONFIG_CAMERA_BOARD_ESP32S3_GOOUUU) && \
	!defined(CONFIG_CAMERA_BOARD_ESP32S3_XIAO)
#error "No camera board selected. Use menuconfig: Component config > Camera board selection."
#endif

// WROVER-KIT PIN Map
#if defined(CONFIG_CAMERA_BOARD_WROVER_KIT)

#define CAM_PIN_PWDN -1  // power down is not used
#define CAM_PIN_RESET -1 // software reset will be performed
#define CAM_PIN_XCLK 21
#define CAM_PIN_SIOD 26
#define CAM_PIN_SIOC 27

#define CAM_PIN_D7 35
#define CAM_PIN_D6 34
#define CAM_PIN_D5 39
#define CAM_PIN_D4 36
#define CAM_PIN_D3 19
#define CAM_PIN_D2 18
#define CAM_PIN_D1 5
#define CAM_PIN_D0 4
#define CAM_PIN_VSYNC 25
#define CAM_PIN_HREF 23
#define CAM_PIN_PCLK 22

#endif

// ESP32-CAM (AI Thinker) PIN Map
#if defined(CONFIG_CAMERA_BOARD_ESP32CAM_AITHINKER)

#define CAM_PIN_PWDN 32
#define CAM_PIN_RESET -1 // software reset will be performed
#define CAM_PIN_XCLK 0
#define CAM_PIN_SIOD 26
#define CAM_PIN_SIOC 27

#define CAM_PIN_D7 35
#define CAM_PIN_D6 34
#define CAM_PIN_D5 39
#define CAM_PIN_D4 36
#define CAM_PIN_D3 21
#define CAM_PIN_D2 19
#define CAM_PIN_D1 18
#define CAM_PIN_D0 5
#define CAM_PIN_VSYNC 25
#define CAM_PIN_HREF 23
#define CAM_PIN_PCLK 22

#endif

// ESP32-S3 (WROOM) PIN Map
#if defined(CONFIG_CAMERA_BOARD_ESP32S3_WROOM)
#define CAM_PIN_PWDN 38
#define CAM_PIN_RESET -1 // software reset will be performed
#define CAM_PIN_VSYNC 6
#define CAM_PIN_HREF 7
#define CAM_PIN_PCLK 13
#define CAM_PIN_XCLK 15
#define CAM_PIN_SIOD 4
#define CAM_PIN_SIOC 5
#define CAM_PIN_D0 11
#define CAM_PIN_D1 9
#define CAM_PIN_D2 8
#define CAM_PIN_D3 10
#define CAM_PIN_D4 12
#define CAM_PIN_D5 18
#define CAM_PIN_D6 17
#define CAM_PIN_D7 16
#endif

// ESP32-S3 (GOOUUU TECH)
#if defined(CONFIG_CAMERA_BOARD_ESP32S3_GOOUUU)
#define CAM_PIN_PWDN -1
#define CAM_PIN_RESET -1 // software reset will be performed
#define CAM_PIN_VSYNC 6
#define CAM_PIN_HREF 7
#define CAM_PIN_PCLK 13
#define CAM_PIN_XCLK 15
#define CAM_PIN_SIOD 4
#define CAM_PIN_SIOC 5
#define CAM_PIN_D0 11
#define CAM_PIN_D1 9
#define CAM_PIN_D2 8
#define CAM_PIN_D3 10
#define CAM_PIN_D4 12
#define CAM_PIN_D5 18
#define CAM_PIN_D6 17
#define CAM_PIN_D7 16
#endif

// ESP32-S3 (XIAO)
#if defined(CONFIG_CAMERA_BOARD_ESP32S3_XIAO)
#define CAM_PIN_PWDN -1
#define CAM_PIN_RESET -1 // software reset will be performed
#define CAM_PIN_VSYNC 38
#define CAM_PIN_HREF 47
#define CAM_PIN_PCLK 13
#define CAM_PIN_XCLK 10
#define CAM_PIN_SIOD 40
#define CAM_PIN_SIOC 39
#define CAM_PIN_D0 15
#define CAM_PIN_D1 17
#define CAM_PIN_D2 18
#define CAM_PIN_D3 16
#define CAM_PIN_D4 14
#define CAM_PIN_D5 12
#define CAM_PIN_D6 11
#define CAM_PIN_D7 48
#endif