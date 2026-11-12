# WxPaper Wi-Fi <-> BLE bridge (ESP32-C3, also builds for C6). See docs/bridge.md.
cmake_minimum_required(VERSION 3.22)

# Wi-Fi credentials / host URL live in a git-ignored local defaults file.
set(SDKCONFIG_DEFAULTS "sdkconfig.defaults")
if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.local")
    list(APPEND SDKCONFIG_DEFAULTS "sdkconfig.defaults.local")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(wxbridge)
