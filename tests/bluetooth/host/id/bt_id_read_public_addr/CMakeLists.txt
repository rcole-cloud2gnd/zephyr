# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.20.0)

FILE(GLOB SOURCES src/*.c)

project(bt_id_read_public_addr)
add_compile_definitions(test_unit_name="${PROJECT_NAME}")

include_directories(BEFORE
    $ENV{ZEPHYR_BASE}/zephyr/tests/bluetooth/host/id/force_mocks
)

find_package(Zephyr COMPONENTS unittest HINTS $ENV{ZEPHYR_BASE})

add_subdirectory(${ZEPHYR_BASE}/tests/bluetooth/host host_mocks)
add_subdirectory(${ZEPHYR_BASE}/tests/bluetooth/host/id mocks)

target_link_libraries(testbinary PRIVATE mocks host_mocks)
