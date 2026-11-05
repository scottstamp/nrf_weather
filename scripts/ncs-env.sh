#!/usr/bin/env bash
# Source this file to get west + the Zephyr SDK toolchain from the nRF Connect SDK
# toolchain bundle without needing nrfutil-toolchain-manager.
#   source scripts/ncs-env.sh
NCS_ROOT="${NCS_ROOT:-/home/scott/ncs}"
NCS_VERSION="${NCS_VERSION:-v3.4.2}"
TC="$(ls -d "$NCS_ROOT"/toolchains/*/ | head -n1)"
TC="${TC%/}"
export PATH="$TC/bin:$TC/usr/bin:$TC/usr/local/bin:$TC/opt/bin:$TC/nrfutil/bin:$TC/opt/zephyr-sdk/gnu/arm-zephyr-eabi/bin:$PATH"
export LD_LIBRARY_PATH="$TC/lib:$TC/lib/x86_64-linux-gnu:$TC/usr/local/lib:${LD_LIBRARY_PATH:-}"
export GIT_EXEC_PATH="$TC/usr/local/libexec/git-core"
export GIT_TEMPLATE_DIR="$TC/usr/local/share/git-core/templates"
export PYTHONHOME="$TC/usr/local"
export PYTHONPATH="$TC/usr/local/lib/python3.12:$TC/usr/local/lib/python3.12/site-packages"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export ZEPHYR_SDK_INSTALL_DIR="$TC/opt/zephyr-sdk"
export ZEPHYR_BASE="$NCS_ROOT/$NCS_VERSION/zephyr"
