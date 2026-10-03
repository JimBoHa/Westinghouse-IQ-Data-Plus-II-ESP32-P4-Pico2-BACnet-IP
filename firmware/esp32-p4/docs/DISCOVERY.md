# BACnet/IP discovery

The gateway is a normal BACnet/IP application device. It answers a valid,
matching Who-Is with one unicast I-Am to the requesting BACnet address. This
applies to original unicast, original broadcast, global-NPDU, BBMD-forwarded
and BACnet-routed requests. Original sender ports and routed SNET/SADR addresses
are preserved. No server address, platform name or proprietary service is used
to choose a response. Startup and periodic unsolicited announcements remain
broadcasts.

This behavior follows [ASHRAE 135-2008q-1, clause 16.10.4](https://bacnet.org/wp-content/uploads/sites/4/2022/08/Add-135-2008q.pdf),
which permits unicast I-Am responses and requires a response path that reaches
the requester. Before P4 0.4.6, broadcast and global-NPDU requests forced the
reply onto the gateway's broadcast address/port, losing clients reached by a
routed IP path or listening on a different UDP port.

Who-Is requests without limits match every configured instance. Requests with
limits must have exactly one correctly encoded, ordered pair; the configured
instance must be inside the inclusive range. Truncated requests, trailing
fields and reversed ranges produce no I-Am. The 106 AI + 92 BI object map,
read-only properties, meter polling and device identity are unchanged.

## Diagnosing a scanner

`GET /api/status` now includes `ethernet.netmask`, `ethernet.gateway` and
`bacnet.discovery`. The latter reports total Who-Is requests, invalid requests,
range exclusions, I-Am sends and send failures. An eight-entry RAM table records
recent requesting IPs, UDP ports, source networks/addresses, last instance range,
BVLC function, result, uptime timestamps and subsequent ReadProperty / RPM
requests. These records reset on reboot; they contain no meter payloads.

Use the scanner's address to find its peer entry after a discovery scan:

- No new request: check scanner interface, integration network and instance
  range, UDP port, firewall/VLAN path and broadcast delivery.
- `excluded-by-range`: include the gateway's configured Device instance.
- `send-failed`: the UDP stack could not queue the response.
- `sent`: the stack queued an I-Am; this is not an acknowledgement from the
  scanner because I-Am is an unconfirmed service.
- Increasing `read_requests`: follow-up Device/point queries reached the gateway.

Devices on different IP subnets still need directed discovery, an existing
BBMD/foreign-device arrangement or BACnet routing to deliver the initial
Who-Is. Unicast replies do not make IP routers forward broadcast requests.
This gateway does not act as a BBMD or register as a foreign device.

The Device advertises no segmentation. Clients with small APDU limits should
read large arrays such as Object_List by array index or split RPM requests,
rather than requiring an oversized response.

## Qualification

The native `discovery_replies` sanitizer test injects independent wire packets
through the production stack with no LAN I/O. It checks local/global NPDUs,
original unicast/broadcast and forwarded BVLC frames, routed return addresses,
requester ports, inclusive/excluded ranges, malformed requests, send failures
and bounded diagnostic records.

`tools/discovery_probe.py` checks a single gateway after pinned HTTPS identity
verification. It deliberately sends every frame to that device's IP by UDP
unicast, including BVLC broadcast forms; it never broadcasts a scan to other
devices. Run once with dynamically allocated ports and again with
`--local-port 47808`. This proves packet handling and replies on the tested path;
it does not prove Ethernet broadcast delivery from an unrelated scanner.
Actual scanner discovery and object reads remain part of site qualification.
