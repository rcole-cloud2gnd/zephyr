/*
 * Copyright (c) 2022 Nordic Semiconductor ASA
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/kernel.h>
#include <zephyr/bluetooth/hci.h>
#include <host/hci_core.h>
#include "mocks/hci_core.h"

struct bt_dev bt_dev = {
	.manufacturer = 0x1234,
};

DEFINE_FFF_GLOBALS;

DEFINE_FAKE_VALUE_FUNC(int, bt_unpair, uint8_t, const bt_addr_le_t *);
DEFINE_FAKE_VALUE_FUNC(struct net_buf *, bt_hci_cmd_create, uint16_t, uint8_t);
DEFINE_FAKE_VALUE_FUNC(int, bt_hci_cmd_send_sync, uint16_t, struct net_buf *, struct net_buf **);
