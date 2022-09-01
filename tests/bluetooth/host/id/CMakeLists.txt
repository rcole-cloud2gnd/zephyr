#
# CMakeLists.txt file for creating of mocks library.
#

add_library(mocks STATIC
            mocks/scan.c
            mocks/addr.c
            mocks/crypto.c
            mocks/net_buf.c
            mocks/net_buf_expects.c
            mocks/hci_core.c
            mocks/hci_core_expects.c

            ${ZEPHYR_BASE}/subsys/bluetooth/host/id.c
            ${ZEPHYR_BASE}/subsys/bluetooth/common/addr.c
)

target_include_directories(mocks PUBLIC
  .
  ${ZEPHYR_BASE}/subsys/bluetooth
  ${ZEPHYR_BASE}/subsys/bluetooth/host
  ${ZEPHYR_BASE}/tests/bluetooth/host
  ${ZEPHYR_BASE}/tests/bluetooth/host/id/mocks
)

target_link_libraries(mocks PRIVATE test_interface)
