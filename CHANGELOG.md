# Changelog

All notable changes follow Keep a Changelog and Semantic Versioning.

## [1.3.7] - 2026-08-14

### Fixed

- Reload now checks an existing Python environment against the pinned requirements entirely
  offline before contacting a package index, and no longer upgrades pip on every run. Updates
  whose dependencies are already installed therefore cannot fail inside pip merely because the
  Release download used an HTTP or SOCKS proxy.

## [1.3.6] - 2026-08-14

### Fixed

- A module's third logical channel works on a stock host. The virtual smart-card driver
  compiles its slot count in — upstream ships two — while a module needs three readers, one per
  logical channel, so the third had no socket behind it and its bridge thread dialled a port
  pcscd never listened on. The installer now builds the driver with four slots, and the
  orchestrator never requests more than the installed driver reports, so a host that skips or
  fails that build degrades to two working channels instead of one permanently dead one.
- Stopped the `vsmartcard-vpcd` package's own reader definition from taking the port this
  gateway gives a cellular module. Both used vpcd's default, only one could bind it, and
  directory order decided which — so on some hosts every module reader vanished while two
  phantom "Virtual PCD" devices appeared. The packaged definition is parked as a dot file
  (installer and every orchestrator pass, so a package reinstall cannot bring it back) and
  module readers now start well below the ephemeral port range. Saved ports on the old base
  are migrated, and a module whose port moved gets its bridge respawned.
- Kept a module's SIM reachable while VoWiFi is off. The card bridge used to follow the
  VoWiFi switch, but reading the SIM is what lets a line exist in the first place and the
  switch stays disabled until one does — a fresh module could never be provisioned, and
  turning VoWiFi off to run an eSIM operation emptied the reader instead. Bridges now follow
  the hardware: every connected module has one.
- Stopped reporting "this card is not an eUICC" for a reader that simply holds no card, and
  added `install.sh diagnose`: one masked report covering reader definitions, live readers,
  bridges, sockets, orchestrator state and an lpac read per module reader.
- A VPCD slot that pcscd never opens a socket for no longer writes a log line every second for
  as long as the bridge runs. A reader can expose fewer slots than the modem offers, so this is
  a normal steady state rather than an incident, and the unbounded repetition was a continuous
  write stream on hosts whose storage is an SD card. Retries now back off to one minute and only
  a changed reason is reported, so a genuinely broken slot stays visible without the repetition.
- A modem that ModemManager declines to manage no longer costs VoWiFi as well. After three
  minutes without a claim the bridge drives the serial port directly, so SIM access keeps
  working; cellular data and flight mode stay unavailable because both need a ModemManager
  modem. The device now reports that reason and a `direct-serial` VoWiFi backend instead of
  rendering as an indefinite spinner with an empty error. Re-seating the modem retires the
  verdict and lets ModemManager be tried again; the bridge holds the port exclusively, so
  nothing else can hand it back automatically. A container is the common case here — the
  Quectel QMI path needs a net port, and network interfaces belong to the host namespace.

### Added

- Added a route from the console to this project's issue tracker: a sidebar entry, and a prompt
  beside the support-bundle download that asks for the bundle to be attached. Reporting a fault
  previously meant finding the repository first, and the bundle — the one artefact that answers
  most host-side questions on its own — was easy to miss.
- Showed the repository's star count beside the console's Star link, abbreviated the way
  GitHub abbreviates it. The count rides on the existing release check, so it inherits that
  check's cache and proxy setting and the status endpoint every page load hits stays local.
  A count that cannot be read is omitted rather than shown as zero.
- Laid the messages allowance panel out as a scrollable six-column grid, so its fields stay on
  one row instead of wrapping into a column on the message page's narrower card.
- Added a host-side section to the redacted support bundle. The host orchestrator now publishes
  the state only it can see — detected virtualization, whether the ModemManager unit is reported
  active, the discovered modems and their ttys, VPCD port assignments, live bridge processes and
  its own recent log — and the bundle carries it as `host-diagnostics-redacted.json`. When a tty
  stays unclaimed, the bundle also records the ModemManager objects and their port lines, which
  is exactly what the claim check matches against. A stopped or outdated orchestrator is
  reported as unavailable rather than omitted, so silence is never mistaken for a healthy host.
  Modem and SIM faults were previously diagnosable only by asking the operator to run commands
  by hand.

## [1.3.5] - 2026-08-13

### Changed

- Made the GitHub `main` branch and its Releases the single supported product and update line.
  Safety boundaries now use product-level names, and release updates no longer depend on
  distribution metadata.
- Redesigned the repository homepage around a concise value proposition, interface tour, quick
  install and architecture overview; added matching Chinese and English demo GIFs.
- Added a discreet GitHub Star link beside the existing repository link in the Web console.

## [1.3.4] - 2026-08-13

### Added

- Added a reusable proxy library for subscriptions, individual share-link nodes and SOCKS5
  servers. Country exits now bind to a library entry; subscriptions retain country filtering
  and automatic or pinned node selection, while individual nodes and SOCKS5 are used directly.
- Added VLESS Reality/XHTTP support through a loopback-only, checksummed Xray-core bridge, while
  preserving Reality parameters and common VLESS, Trojan, Hysteria2 and Shadowsocks share links.
- Added standalone SOCKS5 UDP-associate tests for individual nodes and SOCKS5 entries, with
  latency and localized errors. Tests use an isolated temporary runtime, need no country
  assignment and do not change active exits or VoWiFi lines.

### Changed

- Redesigned Network Exits around a top-level country-routing switch, compact horizontal proxy
  rows, an add-proxy dialog, masked sensitive fields with an explicit reveal control, and clearer
  country assignment behavior. Notifications now appear from the top center of the screen.

## [1.3.3] - 2026-08-12

### Fixed

- Release archives now include the CI-built WebUI and an archive checksum. One-click updates
  verify and install that artifact before reload, so a Raspberry Pi no longer needs to pull a
  Node image from Docker Hub to finish an update.
- Added a release-channel guard that prevented an incompatible source distribution from
  replacing an installed tree with the same version number.
- Added a one-release bootstrap manifest that safely recognizes the reviewed WebUI already
  installed by v1.3.2, allowing the first artifact-aware update to complete offline.

## [1.3.2] - 2026-08-12

### Added

- Added a software-update connection setting that remains direct by default and can instead
  use a manual HTTP(S)/SOCKS5 proxy or an existing ready country exit. Release checks, source
  archive downloads and the subsequent reload share the selection; proxy credentials stay out
  of systemd command lines, update status and logs.

## [1.3.1] - 2026-08-12

### Added

- Added ModemManager cellular SMS sending with an explicit Auto, VoWiFi or cellular route;
  Auto prefers a confirmed registered VoWiFi line and otherwise uses its ICCID-matched modem.
- Added experimental outbound cellular calling through ModemManager, including call state and
  hangup controls. This path intentionally provides no audio, DTMF, muting or recording.
- Added cached balance, validity, SMS, data and voice allowances with manual editing, built-in
  SMS queries for Ultra Mobile and CTExcel, and customizable query number and message rules.
- Added an activation date and an enabled-by-default activation reminder category that notifies
  configured channels three, two and one days before the cached expiry date.

### Changed

- Cellular actions are available only when a real modem is bound to the SIM; a disabled 4G
  setting disables cellular calling, and reader-only SIMs no longer show a cellular channel.
- Allowance detection uses SIM-reported carrier identity instead of the editable line name, and
  query responses are timestamped and cached for the overview.

- Completed an AI-assisted review of every open-source component this project uses, comparing the
  source tree against its upstream and auditing the build scripts, container image and dependency
  manifests. The review established that MDD Sim Gateway is a derivative work of
  pagecat/vowifi_gateway (MIT), which contributes the VoWiFi engine and the overall
  control-plane/engine/WebUI architecture, and it identified seven further components that were in
  use but undeclared: sysmocom/pjproject, frankmorgner/vsmartcard (vpcd), pyscard, PyCryptodome,
  panoramisk, jsQR and Tailwind CSS. `NOTICE`, `THIRD_PARTY_LICENSES.md` and both READMEs now
  credit all of them, retain the upstream MIT copyright notice as that license requires, and
  record the GPL source-offer obligations that shipping a built engine image or host install
  carries. No code changed.

### Security

- ModemManager SMS and call operations require exact ICCID matching and do not silently change
  radio state or retry over a different transport after an explicit route fails.

## [1.2.2] - 2026-08-10

### Fixed

- Hardened CHILD_SA and IKE_SA rekey handling against retransmits, delayed responses and worker
  shutdown races, and restored IMS reauthentication when a carrier refreshes registration
  security state.
- Recovered stale IMS registrations faster when no call is active, while preserving live calls
  and recording clearer outage reasons and recovery transitions in connection history.
- Added missing Asterisk runtime configuration and documentation safeguards so engine startup
  remains deterministic and avoids misleading module warnings on the supported patched build.

## [1.2.1] - 2026-08-08

### Security

- Updated the transitive WebUI build dependency `nanoid` to 3.3.18, resolving the high-severity
  zero-size custom-generator denial-of-service advisory reported by `npm audit`.

### Fixed

- An exit reselect request is evidence of a line failure that is happening now, so it expires
  after ten minutes and the watermark of served requests is persisted. Restarting the
  orchestrator no longer replays a days-old request and moves a healthy live tunnel onto
  whichever node measures fastest today. A request is also consumed only once a selector change
  actually lands: a ranking that measures nothing usable is retried on a slow cadence and
  abandoned after three attempts instead of silently counting as served. Both paths into ranking
  are rate limited — measuring an unreachable pool is synchronous and would otherwise re-probe
  every reconcile cycle, starving the modem and SIM work that shares that loop.
- A pinned exit that has already been given up on stays stopped when a manual retry fails again.
  The stop was previously only applied on the transition, so restarting such a line put it into
  a rebuild loop every few minutes that no longer announced itself.
- Diagnostics capture is asynchronous and can outlive the cooldown before an automatic rebuild,
  so it now removes the container it snapshotted rather than whatever container carries that
  name when it finishes. A slow capture could otherwise delete the replacement the recovery had
  just started and leave the line stopped until someone intervened.
- IMS number verification enables PJSIP packet logging and refreshes the registration for the
  one exchange it reads, instead of tailing a log that no longer contains the public identity
  once a container rebuild has reset that runtime flag. Because this now perturbs a working
  registration, it runs every six hours rather than every ten minutes, retries ten minutes after
  a failure, and commits the new number only once the rebuild that applies it has succeeded.
- Telegram command failures are logged by exception class. The `requests` exceptions raised on
  that path carry the API URL, and therefore the bot token, in their representation.
- A retransmitted CHILD_SA rekey is answered once. The peer retransmits its response when it
  sees a retransmitted request, and applying that response a second time deleted the SA that
  the first one had just installed and left the message id window out of step.
- The forked ESP workers release the log pipe, restore default signal handling and terminate
  with their parent. A hard kill of the tunnel process previously left them holding the pipe
  open, so the supervisor waited on an EOF that never arrived and never restarted the line.
  Their diagnostics go to a bounded per-role file instead of the shared pipe.

## [1.1.0] - 2026-08-08

### Added

- Telegram chat commands: the notification bot becomes two-way, so a line can be operated
  from a phone without opening the WebUI — `/sms` sends a message, `/call` rings the
  softphone and dials out, `/hangup`, `/status`, `/lines`, `/messages` and `/calls` read
  state back, and replying to an incoming-SMS notification answers that sender on that line.
  It shares the existing bot token and proxy mode (direct / manual / country exit), runs every
  action through the same control-plane functions the WebUI calls, and records each one in the
  administrative audit log. Because chat bypasses the web login, commands run only for the
  numeric chat/user IDs listed in Settings → Notifications; a queued command older than three
  minutes is dropped rather than executed late, and the update offset is checkpointed before
  execution so a restart cannot resend an SMS or replace a call. A line can be named by id,
  name or own number, but lines are auto-named `MCC-MNC` and two SIMs on one carrier therefore
  share a name until renamed — an ambiguous name is refused with the matching ids instead of
  silently texting or dialling from the wrong SIM.

- Connection history per VoWiFi line: the device VoWiFi tab shows an up/down timeline with
  availability, outage count and an outage table, and every overview card with VoWiFi enabled
  carries a compact version of it. The control plane records line state as merged segments
  (`line_states`), keeps two days, and reports periods when it was not running as “not
  recorded” instead of guessing what happened during them.

### Changed

- Cellular SMS polling keeps the five-second new-message detection interval but caches stable
  ModemManager modem/SIM identity and previously read SMS objects for one minute, avoiding
  repeated subprocess and D-Bus reads on every idle poll while still periodically validating
  object paths after modem restarts.
- Steady-state line sampling reuses one Docker connection and one container inspection per
  line, and reads IMS registration through the persistent AMI connection before falling back
  to a bounded Docker exec. An event-backed runtime registry now wakes status sampling
  immediately on container lifecycle changes, validates its cache periodically, and lets
  healthy lines back off from four-second to fifteen-second sampling without delaying container
  failure detection. New lines publish a 12-port RTP pool instead of 60 ports and do
  not publish the host AMI debugging port unless explicitly enabled, substantially reducing
  per-line `docker-proxy` processes. Existing saved lines retain their 60-port pool until they
  are deliberately re-provisioned, so an upgrade cannot silently reduce SIP call capacity.
- New lines are named `MCC-MNC-<last four ICCID digits>` (for example `234-10-4409`) instead
  of `MCC-MNC`, which repeated for every SIM of one carrier. The ICCID is always available
  when a line is created — MCC/MNC is not, and previously produced `New SIM` — so a SIM read
  before its carrier is now named `SIM-4409` rather than being indistinguishable. Four digits
  are not unique on their own, so a generated name that still collides gains a ` (2)` suffix,
  and renaming a line onto another line's name is refused (case-insensitively, matching how
  the Telegram bot resolves names). Existing lines keep their current names.

### Fixed

- Expired in-memory Web sessions now return the browser to sign-in and stop its API and
  WebSocket retry loops instead of producing a permanent stream of 401/403 requests after a
  control-plane restart. The Messages page shows its initial conversation/message reads as
  loading rather than briefly claiming the inbox is empty, and stale reads can no longer cross
  between SIM lines or conversations when the selection changes.
- Line creation no longer races itself: `upsert_instance` holds the config lock across its
  whole read-modify-write, so two SIMs appearing at once can no longer read a config that
  lacks the other and then claim the same name or port index.
- Signing in no longer reports “0 devices”. Sessions are memory-only, so a sign-in usually
  follows a control-plane restart — while the first card scan is still running. `/api/devices`
  now reports that discovery is in progress, the UI shows it instead of an empty result, and
  a completed scan refreshes the device list immediately rather than on the next poll.

## [1.0.2] - 2026-08-04

### Added

- One-click update from the WebUI: the version badge opens a confirmation dialog with the
  release notes; on confirmation the host orchestrator runs a detached updater
  (`host/mdd_update.py`) that downloads the tagged release, backs up the current checkout,
  overlays the new files and runs `install.sh reload`, with live progress in the dialog.
- QR-code input for eSIM downloads: the download dialog accepts an uploaded, pasted or
  dropped QR image and decodes the LPA activation code locally in the browser (jsQR); the
  image never leaves the page.
- One-click eSIM profile switching: the last successful chip read is persisted on the
  gateway (`esim-chip-cache.json`, matched to the inserted card by profile ICCID), so any
  browser shows the profile list without an exclusive read, and Enable now stops a running
  line automatically — the line for the newly enabled profile restarts via auto-provisioning.

### Fixed

- Serial-less modem replug migration now requires both the USB model and the published
  15-digit hardware IMEI to match, preventing a different same-model modem from inheriting
  the old device configuration.
- Switching an eSIM profile now creates or matches the newly active ICCID after the LPA
  refresh and schedules its VoWiFi line to start. Cached eSIM views can open the download
  dialog, and the action is labelled “Download eSIM” instead of “Download profile”.
- Replugging a modem that exposes no USB serial (identity falls back to the USB path) no
  longer leaves a permanently-absent ghost device: the orchestrator folds the stale device
  id into its re-enumerated successor, preserving desired capabilities and the VPCD port
  assignment. Only unambiguous same-model devices with the same published IMEI migrate.
- eSIM operations now reach the reader they were asked for. Upstream lpac 2.3.0 ignores
  `LPAC_APDU_PCSC_DRV_NAME` and always connects to the first PC/SC reader (and segfaults on a
  non-zero `LPAC_APDU_PCSC_DRV_IFID`), so on hosts where a modem's virtual slots enumerate
  first, every chip read failed with `euicc_init`. `install.sh build-lpac` now applies
  `patches/lpac/01_pcsc_reader_selection.patch`. Existing installations must rebuild once with
  `sudo ./install.sh build-lpac`.

## [1.0.1] - 2026-08-03

### Added

- Automatic end-to-end provisioning for newly inserted SIMs, including hot-plug device
  discovery, hardware IMEI inheritance, country-exit selection and visible backend activity.
- Cellular SMS import through ModemManager so messages remain available while a SIM uses 4G
  or its VoWiFi engine is stopped.
- Device and SIM-line lifecycle controls with scoped deletion, optional history retention and
  safe suppression of immediate line recreation while a deleted SIM remains inserted.
- Carrier SIP identity profiles and an advanced IMS identity editor; O2 UK/giffgaff lines now
  receive a compliant PANI, access type and telephone-URI behavior automatically.

### Fixed

- Prevented transient IMS `Rejected` states from permanently freezing a line; bounded retries,
  cooldown rebuilding and manual stop now have consistent recovery semantics.
- Bounded stale `OK` status reuse, removed blocking Docker work from HTTP paths and fixed the
  reader enable race that could stop a newly started line.
- Preserved stable SIM-to-device matching across reader re-enumeration, modem swaps and missing
  virtual-reader snapshots; 4G-only lines remain selectable for calls and messages.
- Applied IMS-learned phone numbers to running engines, accepted carrier service short codes and
  made call/message selectors identify the physical device and SIM clearly.
- Restored legacy call and SMS history into recreated numeric lines with idempotent migration.
- Quoted generated engine environment values safely and disabled persistent SIP debug logging by
  default so reader names with spaces work without exposing IMS signaling.
- Routed Telegram country-exit notifications through remote-DNS SOCKS instead of host DNS.
- Treated blank advanced IMS fields as a request to restore carrier defaults rather than an empty
  override that can make registration fail.

## [1.0.0] - 2026-08-02

Initial release.

### Added

- Unified physical-device UI for independent 4G data, flight-mode RF and VoWiFi controls.
- Automatic modem/reader discovery, multi-modem ModemManager backend and PC/SC reader mode.
- SWu Wi‑Fi Calling, Asterisk voice/SMS, browser softphone and per-country UDP-verified exits.
- eSIM profile management, Webhook/Telegram/PushPlus notifications, bilingual UI and diagnostics.
- First-run administrator setup, authenticated sessions, CSRF protection and engine callback tokens.
- Pinned dependency installation and Web release checking.
- Native per-device ModemManager/NetworkManager cellular control without an external compatibility service.
- Public TLS certificate reuse for the browser softphone WSS endpoint, iOS-style settings switches, and sidebar project metadata.
- Safe reuse of an existing system Docker daemon with ownership, privilege and port preflight checks.
- Automatic public release checks with a lower-left update marker, plus standard button/Enter login form submission with duplicate-request guards.
- Eight-combination tests for independent flight-mode, 4G-data and VoWiFi intent, including effective state isolation across multiple modems.
- Per-UICC logical-channel capacity, allocation, role and error reporting in bridge metadata and the hardware UI.
- An in-product carrier/firmware availability notice beside device and VoWiFi controls.
- Automatic line drafts, SIM-country exit selection and hardware IMEI inheritance when a new SIM or reader is detected.
- Persistent physical-device records with hot-plug rediscovery, explicit offline state and safe removal after disconnection.
- An opt-in, pinned libccid patch for the verified Santi Electronics SCR Prime (`04d9:c001`) reader.
- A device-focused hardware view with consistent device cards and responsive IMEI/removal actions.

### Fixed

- Turning 4G off now disconnects only the mobile-data bearer instead of implicitly entering flight mode, and transitional badges preserve the requested direction while device state refreshes.
- Partial or duplicate UICC logical-channel allocations are released immediately and reported with an explicit allocation count.
- Planned orchestrator restarts publish PC/SC maintenance before virtual readers are torn down, with a 45-second rebuild window, so a healthy VoWiFi engine is no longer deleted as if its reader were physically unplugged.
- Engine recreation clears persisted runtime observations before launch, preventing a stale SWu `CONNECTED` marker from appearing as the new engine's live state.
- Product naming is fixed to MDD Sim Gateway; the legacy system-name setting and duplicate sidebar language picker were removed, and sign-out now has a dedicated sidebar position.
- Release discovery is now an unauthenticated read-only GET against GitHub's public API and never sends a GitHub token. Private/unreleased repositories report “no public release” instead of requesting authentication.
- Released every temporary PC/SC context after card operations so repeated hot-plug and VoWiFi activity cannot exhaust pcscd contexts.
- Kept SIM identity and line configuration attached to the card rather than stale physical-device state when cards are moved between readers or modems.
- Announced planned PC/SC maintenance before applying the SCR Prime driver patch so healthy VoWiFi lines are not stopped as if their readers were unplugged.

### Security

- Removed AKA, IKE and ESP traffic-decryption material from persistent engine logs, including
  CK/IK/MSK/EMSK, derived keys, decoded payloads and Wireshark decryption tables.
- Expanded support-bundle redaction to cover multi-line key tables, URLs, custom authentication
  headers, proxy credentials and eSIM activation data, with regression tests.
- Enforced owner-only runtime directories and mode 0600 for configuration, line credentials and
  modem/orchestrator identity state; weak AMI/WebRTC fallback passwords now fail closed.
- Replaced the EOL Fedora 40 engine base with a digest-pinned Fedora 44 image, pinned engine Python
  packages and action revisions. CI/Release build the control image and statically validate the
  engine Dockerfile; a clean target-ARM64 engine build remains a mandatory manual release gate.
- Excluded runtime data, credentials, repository metadata and local build artifacts from Docker
  build contexts, and updated the affected PostCSS build dependency after a high-severity advisory.
- Kept EAP-AKA rejection diagnostics visible after redaction and made APDU tracing tolerate unusual
  response values without logging their bodies or changing card behavior.
- Removed software Ki/OP/OPc and demonstration-vector fallback paths. AKA now fails closed in the physical SIM/eSIM.
- Engine AMI (5038) is published on `127.0.0.1` instead of every host interface. AMI grants `system`/`command`/`originate`, so LAN reachability was equivalent to remote command execution in the engine container. The manager is unaffected — it dials the container's bridge address directly. On-host tooling must now connect via loopback.
- The release-check endpoint requires an administrator session and no longer forces a cache bypass on every call. Previously any unauthenticated client that could reach the management port could trigger unlimited outbound GitHub API requests and exhaust the unauthenticated rate limit. Only an explicit "Check for updates" click bypasses the cache.
- Build-time patches derived from Asterisk (GPL-2.0-only) and CCID (LGPL-2.1-or-later) now carry their upstream licenses explicitly instead of falling under the repository's GPL-3.0-only default.
