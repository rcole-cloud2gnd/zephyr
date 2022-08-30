/*
 * Copyright (c) 2022 Nordic Semiconductor ASA
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/kernel.h>
#include <zephyr/fff.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <host/adv.h>

/* List of fakes used by this unit tester */
#define ADV_FFF_FAKES_LIST(FAKE)         \
		FAKE(bt_le_adv_set_enable)       \

DECLARE_FAKE_VALUE_FUNC(int, bt_le_adv_set_enable, struct bt_le_ext_adv *, bool);
