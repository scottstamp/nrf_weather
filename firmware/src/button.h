/*
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

/* Poll button (DT node `poll_button`). Returns -ENODEV if the board has none. */
int button_init(void);
