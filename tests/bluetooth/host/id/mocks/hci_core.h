/*
 * Copyright (c) 2022 Nordic Semiconductor ASA
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/kernel.h>
#include <zephyr/fff.h>
#include <zephyr/bluetooth/addr.h>

/* List of fakes used by this unit tester */
#define HCI_CORE_FFF_FAKES_LIST(FAKE)       \
		FAKE(bt_unpair)                     \
		FAKE(bt_hci_cmd_create)             \
		FAKE(bt_hci_cmd_send_sync)

DECLARE_FAKE_VALUE_FUNC(int, bt_unpair, uint8_t, const bt_addr_le_t *);
DECLARE_FAKE_VALUE_FUNC(struct net_buf *, bt_hci_cmd_create, uint16_t, uint8_t);
DECLARE_FAKE_VALUE_FUNC(int, bt_hci_cmd_send_sync, uint16_t, struct net_buf *, struct net_buf **);
