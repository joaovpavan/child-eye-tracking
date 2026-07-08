# The following lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.5)

# Local, gitignored Wi-Fi credentials (copy sdkconfig.local.example -> sdkconfig.local).
if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.local")
    set(SDKCONFIG_DEFAULTS "sdkconfig.defaults;sdkconfig.local")
endif()

set(COMPONENTS main)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(eye_camera_stream)
