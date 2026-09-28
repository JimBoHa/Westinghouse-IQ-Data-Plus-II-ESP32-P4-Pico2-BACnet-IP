# ESP32-P4 / plain Pico 2 validation

This is a development port, not a completed meter installation. The original
Pi deployment evidence in `docs/VALIDATION.md` does not qualify the new P4 path.

## Source and hardware

- Original `main` base: `1add86003bddecb69c1c7ac28c5efd70751febff`.
- Derived repository retains the original Git history. BACnet transport reuse
  and licenses are recorded in `ATTRIBUTION.md`.
- Connected development board: ESP32-P4-WIFI6-POE-ETH design, sold by SMKTelec;
  ESP32-P4 silicon revision 1.3, 40 MHz crystal, 32 MiB GD flash, 32 MiB AP
  PSRAM at 200 MHz, IP101 Ethernet PHY at address 1. The PCB header/revision
  variant was not independently identified.
- Programming interface: CH343 USB-C. Pico connection: USB-A host, directly
  attached plain Raspberry Pi Pico 2. RP2350 ROM enumerates as `2e8a:000f`;
  installed CDC firmware enumerates as `2e8a:0009` and reports `0.4.7 / pico2`.
- Full 32 MiB factory flash and later application/configuration backups were
  saved privately before replacement. Security state was inspected and left
  unchanged; no full-chip erase, eFuse writes or security provisioning occurred.
- The working production gateway was not accessed or changed. This device
  uses a separately commissioned identity; site addresses/tokens/captures are
  excluded from this repository.

## Reproducible builds and native tests

Build commands are in `README.md` and root `AGENTS.md`.

- ESP-IDF 5.5.5, commit `b774170ff46c393eeb5e495ea37936038d3f4f4f`;
  RISC-V GCC 14.2.0 (`esp-14.2.0_20260121`).
- Pico SDK 2.3.1; Arm GNU Toolchain 15.2.Rel1 (GCC 15.2.1),
  `PICO_BOARD=pico2`. Existing 0.4.7 timing/PIO source is unchanged.
- BACnet Stack 1.6.0, cJSON 1.7.19, CDC ACM 2.4.1, MSC 1.1.4 and mDNS 1.9.1
  are pinned by Git submodules/component lock.
- Upstream tests: 86 live/runtime tests, 10 installer tests, five poller tests,
  and analyzer self-test passed.
- Native CTest: five targets passed with AddressSanitizer and UBSan: Pico USB
  backpressure/deadline tests, writer-arm protections, UF2 rejection guard,
  accelerated rolling windows, and eight Python/C parity test suites.
- Parity covers all IMPACC scale bytes and boundary mantissas; all six switch
  banks; standard-buffer bitmaps; partial field validity; flags/trip buffers;
  rolling math; freshness/settings expiration; fragmented/truncated USB
  records; provenance/control-reply/repeat timing; and protected commissioning.
- CSV generation check confirms exactly 106 AI + 92 BI with unchanged metadata.
- Accelerated 90,001-sample run checks 15-minute/hour/day warm-up, interpolation,
  ring wrap, counter decrease, failure continuity and recovery. This simulates
  more than a day; it is not an elapsed-time hardware soak.
- Independent native `bacpypes3` client checked 200 objects, all point metadata,
  indexed object lists, NetworkPort, RP/RPM, Who-Is range filtering, five
  rejected writes, confirmed/unconfirmed COV and expiry. Native fixture-driven
  COV value and quality changes also passed. Fixtures are not linked into the
  production firmware.

## Physical hardware checks

The P4 application boots, preserves commissioned NVS settings across resets,
obtains DHCP, answers mDNS and serves HTTP/BACnet over actual Ethernet.

The independent BACnet client physically verified discovery (unicast and
broadcast), range filtering, 200-object catalog, indexed object list, all 198
point names/descriptions/units/COV increments, missing-meter quality, RP/RPM,
confirmed/unconfirmed COV subscriptions and expiry, NetworkPort properties,
and rejected writes. Healthy numeric meter readings were never fabricated.

ESP32 A/B Ethernet updates were accepted, booted from the alternate slot and
marked valid with the expected ELF hash. An early 0.1.1 update failure required
USB-C repair; the revised sequential-write updater was then exercised
repeatedly. The exact cause of the earlier failure was not captured on UART.

Pico firmware was installed through Ethernet while it remained on the P4's
USB-A port. Both initial BOOTSEL installation and subsequent updates from
running firmware passed; success required the expected CDC firmware identity
to return. A stale USB disk-address race found during repeat testing was fixed.
Both P4 and Pico update paths were repeated with USB-C disconnected and PoE as
the only external power/data connection.

Management rejection tests passed: missing authentication, wrong P4 project,
wrong Pico family, bad UF2 digest, truncated UF2 and protected Device instance.
These requests left configuration/boot selection and the Pico connection
unchanged. Private per-device tokens are required for writes.

A no-meter fault run on P4 0.1.4 exercised four bounded Pico transactions
(Pico `STOP_AMBIGUOUS`, code 8, with the meter absent), abort/drain,
re-identification and retry backoff.
The gateway served 5,205 independent BACnet reads during a 60-second load phase
without reboot. Across 37 status samples, internal free heap was
259,411–268,879 bytes; the final sample was 268,755 bytes. Pico stayed qualified,
missing measurements remained faulted, and commissioning was restored with
polling disabled. This short test does not establish long-term leak freedom.

A second fault/load run also successfully uploaded and reboot-verified Pico
firmware through Ethernet during retry backoff. BACnet remained available and
the test restored polling to disabled afterward. Both runs used PoE only.

## Ethernet feature rollout (September 27, 2026)

The separate feature PRs contain per-change validation. Native coverage now
comprises 13 CTest targets, including startup acceptance, authenticated client,
diagnostic ring, duplicate identity, restart codec/integration, soak review,
and release integrity tests. All pass locally; the Linux CI native job also
passes. Full/recovery ESP-IDF and pinned plain-Pico-2 builds pass locally.

On the PoE-only development gateway with meter signals disconnected:

- A deliberately unhealthy pending P4 image hit the startup deadline and
  rolled back to the exact prior ELF/slot; positive sustained-health acceptance
  also passed. This was a software-reset test, not a physical power cut.
- Per-device HTTPS identity, authenticated certificate pinning, signed P4/Pico
  updates, nonce replay/expiry/path/body binding, wrong keys, unsigned or
  wrong-target images, wrong P4 project/Pico family, and protected configuration
  rejection passed. Certificate/configuration persisted across updates.
- Real NTP synchronization and bounded diagnostic-ring overwrite passed.
  An independently encoded unicast duplicate Device claim produced the
  expected warning without renumbering, reconfiguration, or reboot.
- An independent BACnet client received one restart notification and matched
  its three properties to Device reads. Default local-broadcast recipients
  remained read-only. The observed boot timestamp came from actual NTP.
- Chrome rendered desktop/mobile dashboards and tested all 198 point rows,
  invalid-value hiding, key rejection/no persistent storage, reviewed settings
  save/reboot, diagnostics download, both signed firmware update paths, safe
  reboot, and key removal on lock/reload. Exact P4 ELF/slot/health and Pico
  runtime return were verified. The test trusted only the pin-verified TLS key.
- A 60-second real health monitor run completed 13 samples with a minimum
  219,527 bytes of internal free heap and no failures. A wrong-port run retained
  HTTPS evidence while independently failing BACnet. Offline review correctly
  rejected the short successful run as 24-hour evidence.

Polling was restored/left disabled. Site identities, captures, tokens, signed
backups, and detailed reports remain private. Final CI artifact deployment and
regression outcomes are recorded in PR #8 and the private hardware handoff.

## Remaining qualification

- **Meter absent:** no physical electrical/logic-analyzer qualification, real
  meter response/word/display comparison, CT/PT/site-address verification or
  complete Pico-to-meter-to-BACnet path validation was possible.
- The four-gateway commissioning and meter connection checklist is in the
  build guide. Polling is left disabled until the meter address and wiring are
  verified. GP0/GP1 are CLK/RW inputs; GP2/GP3 are DATA/INT LOW-or-released.
  Keep Pico powered whenever meter signals are connected.
- A continuous physical 15-minute meter run, physical hour/day windows, long
  soak, network reconnect, power-interruption rollback and multi-client COV
  timeout/retry load qualification remain pending unless separately recorded.
- Software rollback is enabled. Deliberate power-cut rollback and the optional
  recovery application have not been physically qualified.
- Pico has no A/B image rollback. An application unable to expose its USB reset
  interface may require physical BOOTSEL recovery.
- Health counters/rolling histories reset with the P4 session; COV leases are
  RAM-only and clients must resubscribe. BI2 remains inactive. HTTPS management
  uses a pinned device certificate, one-use HMAC requests and signed firmware. No BBMD/foreign-device
  service or database/history download is provided.

Local build reports, captures, firmware artifacts and their checksums are kept
outside Git or under the ignored `build/` directory. This file reports observed
tests; it is not BACnet certification.
