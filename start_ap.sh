#!/bin/bash

# Script to start WiFi access point

# Configure hostapd for AP mode
cat > /tmp/hostapd.conf << EOF
interface=wlan0
#driver=nl80211
ssid=tH-Monitor-Config
channel=1
hw_mode=g
ieee80211n=1
wmm_enabled=1
macaddr_acl=0
ignore_broadcast_ssid=0
EOF

chmod 644 /tmp/hostapd.conf

# Configure dnsmasq for DHCP
cat > /tmp/dnsmasq.conf << EOF
interface=wlan0
dhcp-range=192.168.4.2,192.168.4.20,255.255.255.0,24h
domain=wlan
address=/gw.wlan/192.168.4.1
EOF

chmod 644 /tmp/dnsmasq.conf

# Stop existing network services
nmcli device disconnect wlan0 2>/dev/null || true
systemctl stop dhcpcd 2>/dev/null || true
systemctl stop wpa_supplicant 2>/dev/null || true

# Configure static IP
ifconfig wlan0 192.168.4.1 netmask 255.255.255.0

# Start hostapd and dnsmasq
hostapd /tmp/hostapd.conf -B 2>/dev/null
dnsmasq -C /tmp/dnsmasq.conf -x /var/run/dnsmasq.pid 2>/dev/null

echo "Access point 'tH-Monitor-Config' started on 192.168.4.1"
