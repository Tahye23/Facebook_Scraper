#!/usr/bin/env bash
# Configure le serveur (Ubuntu) pour autoriser le bind sortant sur n'importe
# quelle adresse du bloc IPv6 /64 route vers cette machine (technique AnyIP).
#
# Usage:
#   sudo ./tools/setup_ipv6_anyip.sh 2001:41d0:2:9434::/64
#
# Idempotent: peut etre relance sans effet de bord.

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Ce script doit etre execute en root (sudo)." >&2
    exit 1
fi

PREFIX="${1:-}"
if [[ -z "${PREFIX}" ]]; then
    echo "Usage: sudo $0 <PREFIX_IPV6>/64" >&2
    echo "Exemple: sudo $0 2001:41d0:2:9434::/64" >&2
    exit 1
fi

echo "==> Verification du bloc IPv6 delegue (via 'ip -6 route')"
ip -6 route show | grep -F "${PREFIX%%/*}" || echo "    (info: le prefixe n'apparait pas explicitement, c'est normal s'il est route via la passerelle)"

echo "==> Activation du bind non-local IPv6 (net.ipv6.ip_nonlocal_bind=1)"
sysctl -w net.ipv6.ip_nonlocal_bind=1
cat > /etc/sysctl.d/60-ipv6-nonlocal-bind.conf <<EOF
# Autorise le bind sortant sur des adresses IPv6 non explicitement
# attachees a une interface (necessaire pour la rotation d'IPv6 du
# scraper). Voir tools/ipv6_rotating_proxy.py.
net.ipv6.ip_nonlocal_bind = 1
EOF

echo "==> Ajout de la route locale AnyIP pour ${PREFIX}"
ip -6 route replace local "${PREFIX}" dev lo

echo "==> Persistance de la route AnyIP au redemarrage (networkd-dispatcher)"
mkdir -p /usr/lib/networkd-dispatcher/routable.d
DISPATCH_SCRIPT="/usr/lib/networkd-dispatcher/routable.d/50-ipv6-anyip"
cat > "${DISPATCH_SCRIPT}" <<EOF
#!/bin/sh
ip -6 route replace local ${PREFIX} dev lo
exit 0
EOF
chmod 755 "${DISPATCH_SCRIPT}"

echo "==> Verification"
ip -6 route show table local | grep -F "${PREFIX}" && echo "OK: route locale AnyIP active."
sysctl net.ipv6.ip_nonlocal_bind

echo ""
echo "Test rapide (doit reussir): choisir une adresse au hasard dans le bloc et pinger un service externe."
echo "  python3 -c \"import ipaddress,random; n=ipaddress.IPv6Network('${PREFIX}'); print(n.network_address + random.getrandbits(n.max_prefixlen - n.prefixlen))\""
echo "  curl -6 --interface <adresse_generee_ci-dessus> https://api64.ipify.org"
