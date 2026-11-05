/*
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

/* Provided by main.c: end the current sleep and poll the host immediately.
 * Safe from ISR and thread context. A request made while a poll/refresh is
 * running is kept and triggers the next poll right after it.
 */
void wx_wake_now(void);
