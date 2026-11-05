# SPDX-License-Identifier: Apache-2.0
cmake_minimum_required(VERSION 3.20.0)

list(APPEND DTS_ROOT ${CMAKE_CURRENT_SOURCE_DIR})

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(wxpaper)

target_sources(app PRIVATE
	src/main.c
	src/epd_ssd1680.c
	src/framebuf.c
	src/battery.c
	src/wx_service.c
)
target_sources_ifdef(CONFIG_WX_DEVCMD app PRIVATE src/devcmd.c)
