#!/bin/bash
cd /root
/etc/init.d/miniassistant stop
rm -rf /root/miniassistant
git clone --depth 1 https://github.com/ich777/miniassistant
cd /root/miniassistant
bash install.sh
/etc/init.d/miniassistant start
