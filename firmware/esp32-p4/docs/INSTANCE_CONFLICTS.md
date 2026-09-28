# BACnet identity conflict warnings

The commissioned Device instance remains fixed. At startup and every minute,
the gateway sends a local-subnet Who-Is restricted to exactly that instance.
It also checks unsolicited I-Am messages. A well-formed reply claiming the
same instance from another BACnet address creates a diagnostic event and a
sticky warning for this boot. The gateway never renumbers, reboots, disables
polling, or changes its BACnet point map in response.

Status includes `bacnet.instance_status` (`checking`, `checked`, or `conflict`),
check attempts, conflict observations, and the last claimant's IP/UDP port,
source network, and observation uptime. Routed observations identify the
network and reachable IP next hop, which need not be the remote device itself.
Own reflected announcements are ignored; malformed or truncated I-Am packets
cannot create a warning. `checked` records a completed initial observation
window, not a guarantee that every site device was reachable.

Warnings remain after the competing device disappears and reset on gateway
reboot. Resolve the conflicting device's commissioning and verify the site's
instance allocation. Offline devices, blocked broadcasts, and other subnets
cannot be ruled out by this check. BACnet/IP is unauthenticated, so a claim is
evidence to investigate, not a trusted reason to change the gateway's identity.
