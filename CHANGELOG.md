# Changelog

All notable changes follow Keep a Changelog and Semantic Versioning.

## [Unreleased]

### Fixed

- Missing tunnel evidence and local DNS, SIM, protocol or engine failures no longer count as
  failed exit nodes. Unknown evidence does not trigger node changes or stalled-session cleanup,
  and notifications no longer claim a clean tunnel when that has not been established.
- Direct and non-selectable routes bypass the exit ledger instead of entering an hourly
  candidate-exhaustion retry cycle.
- Persisting a subscription selector's already-active node no longer restarts the shared
  sing-box process. Actual configuration changes and process failures still trigger a restart.
- Notification destinations run independently on a dedicated, eight-worker delivery pool;
  HTTP retries no longer hold the default executor or serialize the configured channels.
- Carrier identification prefers an exact PLMN, including parent-network fallback, before
  attempting compatibility with older zero-padded two-digit MNCs.
- VoWiFi history ignores stale request successes and failures after a newer refresh, line
  change or unmount, and clears the previous line's error when switching lines.

## [1.9.1] - 2026-09-04

### Fixed

- SIMs with no PIN could be reported as PIN-locked on v1.9.0, blocking VoWiFi with a prompt for a
  PIN the card never asked for. v1.9.0 judged each SELECT by the GET RESPONSE that follows it, but
  `61xx` already means the card accepted the command — a card that then declines to hand back the
  response body made `SELECT ADF.USIM` read as a failure, so card reading stopped with every PIN
  field unset. The APDU helper now reports the command's own verdict and returns whatever body it
  could fetch, keeping the v1.9.0 support for APDU-level readers and T=1 cards rather than trading
  one reader class for the other. The `6Cxx` length-correction retry also no longer drops the
  command's data field, which turned a retried case-4 SELECT into a malformed command
  ([#60](https://github.com/MddIdd/mdd-sim-gateway/issues/60)).
- A start refused by the SIM preflight showed only "Capability change failed: Conflict" because the
  409 carried no human-readable message. The refusal now explains itself; when a PIN really is
  required the WebUI prompts for it and retries the start, and when the card could not be read at
  all the error says so instead of asking for a PIN that cannot help
  ([#60](https://github.com/MddIdd/mdd-sim-gateway/issues/60)).
- Switching an eSIM profile while a line started could be reported as a misleading "no card". The
  preflight now compares the live-read ICCID against the line's expected ICCID instead of relying
  only on the sampled card-monitor cache, which lags inside a profile-switch window, and reports a
  card mismatch naming both ICCIDs ([#60](https://github.com/MddIdd/mdd-sim-gateway/issues/60)).

### Added

- Support bundles now record why a line start was refused. A closed-schema `preflight_blocked`
  lifecycle event (reason code plus card-present / ICCID-matches booleans, never an identifier) is
  written for each refusal; previously the refusal happened in the control plane before any engine
  log existed, so a bundle showed no trace of it
  ([#60](https://github.com/MddIdd/mdd-sim-gateway/issues/60)).

## [1.9.0] - 2026-09-03

### Added

- The country-exit picker can now be searched by Chinese or English country name and by two-letter
  country code, with keyboard navigation for selecting a result
  ([#53](https://github.com/MddIdd/mdd-sim-gateway/issues/53)).
- Renaming a running SIM line now updates its display metadata without rebuilding the line's
  IKE and Asterisk engine; edits to operational settings continue to restart the line so they are
  actually applied ([#53](https://github.com/MddIdd/mdd-sim-gateway/issues/53)).
- The manual update selector now offers the five most recent stable releases instead of only the
  latest one, making recent rollback or version switching available from System Settings.

### Fixed

- SIM access now supports readers that expose APDU-level responses and T=1 cards across SELECT and
  READ operations, including direct data responses, `61xx` chaining and `6Cxx` length correction.
  Reader errors are surfaced instead of being mistaken for card data, and verified card bindings
  remain stable across subsequent operations
  ([#51](https://github.com/MddIdd/mdd-sim-gateway/issues/51)).

## [1.8.1] - 2026-09-02

### Fixed

- A bare RFC 3748 EAP-Request/Identity in the first IKE_AUTH reply — how Lebara UK's (PLMN 234-87)
  self-hosted ePDG opens EAP before any EAP-AKA exchange — was not recognised, so the attach
  aborted with a misleading NO EAP PAYLOAD RECEIVED. The engine now answers it with an
  EAP-Response/Identity carrying the IMSI NAI and continues into EAP-AKA. When an EAP payload is
  present but its method is still unsupported, the error now names the received code and type
  instead of claiming no payload arrived
  ([#43](https://github.com/MddIdd/mdd-sim-gateway/issues/43)).

- Number-keeping intervals can now be configured for up to 365 days instead of being capped at
  90 days, covering carriers with 180-day retention policies
  ([#40](https://github.com/MddIdd/mdd-sim-gateway/issues/40)).

### Security

- Updated the WebUI build dependency chain to a patched Browserslist release, resolving
  GHSA-c83g-rgw3-j3cx and GHSA-73wf-gq98-2v4g. Production dependency auditing reports no known
  vulnerabilities ([#42](https://github.com/MddIdd/mdd-sim-gateway/pull/42)).
- Added a pre-push privacy hook that scans every source blob introduced by the commits being
  published, so removing a subscriber identifier in a later commit can no longer hide it from the
  local guard ([#38](https://github.com/MddIdd/mdd-sim-gateway/pull/38)).

## [1.8.0] - 2026-09-01

### Added

- Feishu/Lark notifications can fan out to multiple independently configured custom bots. Each bot
  has its own webhook, signing secret, event switches, templates, test action and optional SIM-line
  filter. Empty filters receive every line and gateway event; filtered bots receive only matching
  line events. Delivery retries and history remain independent per bot, and existing single-bot
  configurations migrate automatically without duplicate sends
  ([PR #36](https://github.com/MddIdd/mdd-sim-gateway/pull/36)).

- DITO Telecommunity (PLMN 515-66) VoWiFi support, contributed in
  [PR #35](https://github.com/MddIdd/mdd-sim-gateway/pull/35). Its ePDG answers the MODP-2048
  proposal set with NO_PROPOSAL_CHOSEN and offers only AES-CBC-128 / HMAC-SHA1 / MODP-1024, so
  that legacy suite is now selected for that PLMN alone — every other carrier keeps the existing
  four proposals in the same order. The RFC 4187 identity requests DITO sends during the first
  IKE_AUTH exchange (AT_PERMANENT_ID_REQ and AT_FULLAUTH_ID_REQ) are answered as well, instead of
  being ignored and reported as NO EAP PAYLOAD RECEIVED.

### Fixed

- An RP-ACK or RP-ERROR — the SMSC reporting on a message the gateway sent — was treated as an
  unknown message type and then handed to the dialplan anyway. Carrying no TPDU, it arrived
  empty, so every submitted segment wrote a bodyless inbound record: one six-segment text left
  six phantom messages behind. Reports are now recognised, answered, and stopped before the
  dialplan, and a refusal is logged with its RP cause.

- A part of a long message that arrived after its group had been flushed started a new group and
  was published as a second, near-duplicate fragment of a message already in the thread. Between
  two carriers the parts of one text arrived ten minutes apart, so this was the normal outcome
  rather than an edge case. A flushed group now stays addressable for an hour and a late part
  fills in the gap it left.

- The WebRTC endpoint advertised opus, which no build of the engine image can encode: codec_opus
  is an external, x86-only binary module, and enabling it in menuselect is silently a no-op on
  arm64. A peer that offered opus alone got a connected call with no audio and no error. Opus is
  no longer offered; calls continue over ulaw/alaw as they already did in practice.

### Changed

- The published control image and the engine image are substantially smaller: 194 MB -> 76 MB and
  272 MB -> 143 MB compressed, 852 MB -> 355 MB and 1.04 GB -> 607 MB on disk. The control image
  no longer ships the toolchain that built it, and the engine ships only the Asterisk modules it
  can load, stripped, which also drops 109 packages that were pulled in by modules the engine
  already refused to load. The documented storage requirements follow: 2 GiB free to install
  instead of 4 GiB, 3 GiB kept free for an upgrade instead of 6 GiB, and an 8 GB rather than
  16 GB system disk. Building the engine from source on the device is unchanged and still needs
  several GiB more.

### Added

- Feishu/Lark notifications can fan out to multiple independently configured custom bots. Each bot
  has its own webhook, signing secret, event switches, templates, test action and optional SIM-line
  filter. Empty filters receive every line and gateway event; filtered bots receive only matching
  line events. Delivery retries and history remain independent per bot, and existing single-bot
  configurations migrate automatically without duplicate sends.

## [1.7.0] - 2026-09-01

### Added

- Feishu/Lark custom bots are now a native notification channel alongside Webhook, Telegram
  and PushPlus. Each event can be enabled separately, use its own title and content template,
  and be tested from the WebUI. Official Feishu and Lark webhook endpoints are accepted, with
  optional HMAC-SHA256 signing; HTTP success is also checked against the platform response so
  rejected messages are retried and reported instead of appearing delivered. Webhook tokens and
  signing secrets are removed from support bundles.

- System Settings can now list published test Releases alongside the latest normal Release,
  install an explicitly selected test version, and switch a test installation back to the
  normal version without using the host command line. Drafts stay hidden, automatic updates
  remain on the stable promotion policy, and the server resolves the selected tag again before
  publishing the verified host update request. The page also retains the last background or
  manual check time instead of returning to “Not checked” when it is opened.

- Telegram delivery and software updates can now explicitly use a proxy-library entry or a
  configured country exit, in addition to direct networking (and automatic fallback for
  updates). Existing Telegram manual-proxy settings remain usable, while legacy updater manual
  proxies migrate into the shared library and legacy country selections stay pinned.

### Fixed

- [Issue #33](https://github.com/MddIdd/mdd-sim-gateway/issues/33): a giffgaff/O2 UK VoWiFi
  line dropped like clockwork every ~2h50m. The carrier silently invalidates its SWu session
  just before a 3-hour lifetime without sending any IKE message, and the engine's proactive
  IKE-SA rekey — the mechanism that resets that carrier clock — defaulted to 600 minutes and
  could not be configured, so it never fired in time. The IKE rekey period is now a real
  setting (System Settings → Calls & VoWiFi, with a per-line `ike_rekey_minutes` override)
  and defaults to 150 minutes, which preempts every carrier clock observed so far (giffgaff
  ~2h50m, EE ~12h). The 30-minute ESP rekey is unchanged and unrelated.

- A `reg_rejected` freeze now records the SIP response code that condemned the line (for
  example 403) in both the lifecycle record and the frozen diagnostics snapshot. The #33
  support bundle reached us after every log line holding that code had rotated away, so the
  bundle could prove the registration was rejected but not why.

- [Issue #30](https://github.com/MddIdd/mdd-sim-gateway/issues/30): a modem bridge identity
  refresh that temporarily failed to read IMEI could replace the already verified hardware
  identity with an empty value. If health recovery later rebuilt the line, the restart was
  blocked by `hardware_imei_required` and VoWiFi stayed off. A bridge now retains its verified
  immutable IMEI across incomplete refreshes. Recovery no longer accepts a line-saved modem
  IMEI when the live bridge cannot verify it, because modems without USB serial numbers reuse
  the same port-derived id after a physical module swap.

- Reopened [Issue #21](https://github.com/MddIdd/mdd-sim-gateway/issues/21): automatic recovery
  decisions now survive a later VoWiFi toggle in a separate bounded lifecycle log. Redacted
  support bundles record structured scheduling, blocking, cancellation, start failure and success
  events without exception text or subscriber/hardware identifiers, so the final reason a rebuild
  did not happen remains diagnosable. An enabled native-reader line also remains eligible for
  recovery when the default for newly detected devices has VoWiFi disabled.

- Support bundles now report per-file coverage and truncation, retain both the beginning and end
  of bounded IKE segments, expose only boolean bridge identity/channel health plus metadata age,
  and enforce a 10 MiB archive ceiling with deterministic low-priority log omission. New lifecycle
  and bridge fields have explicit redaction and archive-content regression coverage. Unbounded
  call history is counted but only its latest 20,000 lines are parsed for safe call evidence.

- Lifecycle writes no longer block the asyncio control loop or contend with multi-megabyte
  diagnostic rewrites. Cancellation is recorded centrally for manual starts/stops, configuration
  restarts, eSIM switches, card removal and disabled lines; repeated identical no-card blocks are
  coalesced so they cannot evict the useful failure history.

- [Issue #27](https://github.com/MddIdd/mdd-sim-gateway/issues/27): a VLESS node using
  Xray 26.7+ VLESS Encryption could never connect. The share link's `encryption` parameter was
  dropped and `none` sent in its place, so the client established a connection the server could
  not read: every request timed out, the server logged nothing, and the node was
  indistinguishable from a dead one. The declared value is carried through to Xray now, such
  nodes are routed to Xray automatically whether or not they use REALITY, and the sing-box
  converter refuses them by name instead of building an outbound that silently never answers.
  Verified against a server configured this way: unreachable before, 79 ms after.

- The UDP validation probe no longer decides an exit's fate from DNS alone. VoWiFi carries IKE
  on UDP 500/4500 and never queries a resolver, while port 53 is among the most intercepted and
  rewritten ports there is. STUN probes now run interleaved with DNS ones on ports nobody
  rewrites, each with its own SOCKS5 association, and any single answer passes the exit. Both
  lists are configurable (`MDD_UDP_PROBE_TARGETS`, `MDD_UDP_STUN_TARGETS`), and a failure names
  every probe tried with what each one did.

- Xray becoming unavailable now fails only the exits it carries. Moving REALITY onto Xray made
  it load-bearing for ordinary exits, where it had mattered only to the rare XHTTP node, and a
  missing or crashed Xray took every country down with it — including exits that never touch
  it. Its absence is reported as what it is, naming REALITY and how to install it.

- VLESS nodes on the Xray path request XUDP packet encoding, which the XHTTP path already did
  while the raw/ws path sent nothing — UDP is what these exits exist for.

- A failed node test shows why on screen instead of behind a hover, where a vanishing toast was
  all an operator could screenshot. The parsed summary keeps the SNI behind the
  sensitive-information switch, since that line names the operator's own server and is the part
  people screenshot into public issues.

- Notification event switches now collapse like the message-template editor, and channel test
  buttons show a disabled testing state while their request is running. Update-network guidance
  is shown beneath the network selection, while release-range guidance is shown beneath the
  update method and version range instead of the two descriptions appearing swapped.

- Telegram and software-update proxy pickers no longer offer subscription profiles as if they
  were a single route. Subscriptions remain available through explicit country exits, automatic
  update fallback skips them, and a previously selected subscription migrates to its first
  enabled assigned country exit so an upgrade keeps the route it used before.

- A release candidate now recognizes the final Release with the same numeric version as newer
  (for example, `1.6.1-rc2` → `1.6.1`), so promotion-gated automatic updates can move test
  installations back onto the normal release line.

## [1.6.0] - 2026-08-29

### Added

- Webhook, Telegram and PushPlus notifications can now override title and content per event,
  with a shared field-only `{{variable}}` syntax, an in-page preview, per-event test delivery
  and one-click restore. Empty templates preserve the existing wording. Standard webhooks now
  also include rendered `title` and `content` fields, while custom webhook payloads can keep
  using those fields inside their JSON/form/raw templates. Unknown events, properties and
  variables are rejected when settings are saved; templates cannot evaluate expressions or run
  code. PushPlus's existing HTML/text/Markdown/JSON selector is now labelled “content format”
  so it is not confused with the new message templates.

- Incoming VoWiFi calls now remain available for the configured answer window while the browser
  is closed. After a call notification, signing in opens a global Answer/Decline overlay from any
  WebUI page and automatically registers every enabled line; voicemail still begins at the same
  configured deadline when nobody answers.

- Testing an individual node now returns a redacted summary of how the gateway parsed the
  link — protocol, transport, TLS/Reality, SNI, ALPN, obfuscation, UDP capability and which
  engine carries it, with no address or secret — so a node that works in another client can
  be compared field by field.

### Changed

- VLESS REALITY nodes now run on the bundled Xray-core instead of sing-box, over the same
  loopback bridge XHTTP already used. REALITY's wire details move with Xray, so a server on a
  newer Xray build could answer Xray clients while sing-box failed the handshake with
  "reality verification failed" — a version skew the gateway no longer sits in the middle of.
  Nodes sing-box handles correctly are untouched. The pinned Xray version stays on the newest
  release upstream marks stable; `MDD_XRAY_VERSION` may now be overridden together with
  `MDD_XRAY_SHA256_AMD64`/`_ARM64` for an operator who must match a prerelease server.

### Fixed

- Opening the Calls or Devices page during its first refresh no longer briefly claims that a
  line is unregistered or that a known device has no SIM. The softphone now keeps its initial
  connection state until registration produces real evidence, and device/SIM cards stay on the
  discovery placeholder until the control plane finishes its first hardware scan.

- Asynchronous WebUI data now consistently distinguishes loading, confirmed empty state and
  request failure. First sign-in, line/device switches, call history, logs, allowance, keeping,
  eSIM capability, proxy status, notification delivery and system settings no longer flash a
  false “none”, “off”, “not connected” or default configuration while their APIs are pending.

- [Issue #22](https://github.com/MddIdd/mdd-sim-gateway/issues/22): switching to another
  device while a VoWiFi, cellular-data or flight-mode request was still pending could carry
  the first device's temporary “starting/stopping” display into the second device. Capability
  operation state is now keyed by both device and capability; the request still completes on
  its original device, while every other device continues to show its own live state.

- [Issue #26](https://github.com/MddIdd/mdd-sim-gateway/issues/26): a physical-eSIM profile
  switch could report failure — and leave the page and device state on the previous SIM —
  even though the eUICC had already switched. The modem bridge published the baseband's
  cached ICCID, so the post-switch rebuild verification timed out; it now reads EF_ICCID
  from the card itself over AT+CSIM and only falls back to the cache. When the switch
  succeeds but line recovery still fails, the API now reports the switch with the recovery
  error instead of a plain failure, and the UI shows the new profile as active with a hint
  to check its line. Native card readers now retry the post-switch identity probe through
  the eUICC REFRESH window instead of keeping the old ICCID after a single failed read,
  and they disable the old profile's line during the switch (restored on failure) so the
  old and new SIM can no longer both show as enabled.

- [Issue #27](https://github.com/MddIdd/mdd-sim-gateway/issues/27): pasted Hysteria2 and VLESS
  nodes that other clients connect to could fail here with a generic "no healthy UDP-capable
  node is ready". Share links now keep the parameters that were silently dropped — Hysteria2
  obfuscation (`obfs`/`obfs-password`, without which the server discards every packet), an auth
  string containing a colon, and `alpn` for protocols other than VLESS — and a link naming a
  transport this gateway cannot render (grpc, httpupgrade, h2, quic) is refused by name instead
  of being downgraded to a plain TCP outbound that never completes a handshake.

- Country exit failures now say what actually went wrong: a disabled exit, a country-routing
  master switch left off, and a host orchestrator that is not publishing status are reported
  as themselves rather than as an unhealthy node pool. An exit whose sing-box refused to start
  is no longer published as ready, and the node test surfaces what sing-box/Xray-core reported
  instead of discarding it.

## [1.5.4] - 2026-08-28

### Fixed

- Failed Issue analyses now leave a separate bounded notice even when the failed model Job cannot
  expose its step outputs, while preserving the last successful analysis comment.

- Fixed update-scope selection so “all versions” follows the approved latest Release while
  “main versions only” can still install its independently configured stable Release by tag after
  newer patches are published. The legacy promotion field remains synchronized so gateways older
  than v1.5.4 can receive the approved patch.

- [Issue #21](https://github.com/MddIdd/mdd-sim-gateway/issues/21): a VoWiFi health rebuild
  could remove its failed container, sample the reader while its card identity was briefly
  unavailable, and then erase the only automatic retry timer. A transient card-cache miss now
  keeps a bounded eligibility retry, while an actual removal event or a disabled VoWiFi switch
  still cancels recovery.

- Redacted support bundles could retain operator-controlled proxy profile labels and node values,
  the active exit-node label, and host interface addresses because those generic field names were
  not sensitive outside their document context. Redaction now follows each field's path, hides
  profile identifiers and references, and keeps the health evidence needed to diagnose a rebuild.

## [1.5.3] - 2026-08-27

### Changed

- Official releases now provide native ARM64 and amd64 Engine and Control image assets. Fresh
  installs from the official source archive and one-click upgrades download only the host's
  architecture, verify its checksum and image identity, and avoid compiling Asterisk or Docker
  control images on the gateway. Development checkouts retain an explicit source-build path.
  Continuous integration now builds and validates the Engine natively on both architectures so
  an architecture-specific packaging difference is caught before a release tag is created.

- After moving an official installation to verified prebuilt images, the installer removes
  dangling legacy Docker build cache left by earlier on-device builds. It uses the conservative
  builder prune mode without `--all` and does not remove images, containers, or volumes.

- Host diagnostics now show the filesystem's total, used and available space together with
  separate MDD file, image, container-layer and shared Docker build-cache figures. System
  maintenance can explicitly remove dangling builder records and reports the bytes reclaimed,
  without deleting images, containers, volumes or reusable cache.

- Administrators can explicitly remove unused old and rollback MDD images when disk space matters
  more than one-click rollback. Current images, the trusted Engine base and every image referenced
  by a container remain protected. The product overview now states the minimum free space,
  recommended system-disk capacity and the need to expand a VM's root partition after its disk.

### Fixed

- The update dialog no longer truncates bilingual Release notes at 4,000 characters. It retains
  a bounded 16,000-character response so important notices, patch changes and the current feature
  release summary remain visible together.

- Host storage diagnostics no longer present the sum of repeated Docker virtual image sizes as
  physical MDD disk usage. They now show Docker's real layer-store total and independently report
  the image and build-cache bytes Docker considers reclaimable. The maintenance button displays
  its conservative build-cache estimate before confirmation. Build-cache cleanup no longer sends
  Docker's unsupported `dangling` build/prune filter, and a daemon failure is returned as an
  actionable error instead of a generic Internal Error.

- Existing amd64 Docker-control installations on v1.4.x can now complete a direct v1.5.3
  upgrade after the documented one-time mode-marker bootstrap. The target installer detects the
  live Docker control plane, imports both verified amd64 Engine and Control assets through the
  old updater's selected route, and restores the persisted Docker mode only after reload succeeds.
  The bootstrap remains necessary because the immutable v1.4.x updater otherwise requests its
  hard-coded ARM64 Control asset before any v1.5.3 code can run.

- The navigation sidebar now follows Safari's dynamic viewport and provides native momentum
  touch scrolling with bottom safe-area padding, so iPad users can reach version, repository and
  sign-out controls instead of having the lower sidebar clipped behind browser chrome.

- [Issue #17](https://github.com/MddIdd/mdd-sim-gateway/issues/17): cellular SMS sending
  relied on an `mmcli` text-file option that is absent from ModemManager 1.20 on Ubuntu 22.04.
  SMS objects are now created through ModemManager's stable D-Bus interface, preserving message
  punctuation and Unicode without requiring a newer command-line client.

- [Issue #18](https://github.com/MddIdd/mdd-sim-gateway/issues/18): ModemManager can report the
  hexadecimal portion of an ICCID in uppercase while the saved PC/SC identity is lowercase.
  Cellular SMS modem lookup and receive mapping now compare canonical case-insensitive ICCIDs.

- [Issue #19](https://github.com/MddIdd/mdd-sim-gateway/issues/19): inserting a new SIM while
  VoWiFi was disabled left its automatically discovered line permanently in draft state and
  disabled the UI switch needed to recover it. Complete drafts are now promoted to usable line
  records before the VoWiFi start policy is evaluated; the engine remains stopped until enabled.

- [Issue #15](https://github.com/MddIdd/mdd-sim-gateway/issues/15): the v1.5.2 image cleanup
  ran in the updater process copied from the installed version, so the first upgrade from an
  older release could not execute the newly added cleanup. Cleanup now runs from the target
  release's installer after a successful reload, making it effective on that first upgrade.

- Superseded version tags in the MDD Control and Engine release repositories kept old images
  from becoming dangling, so `docker image prune` alone could not enforce one-generation
  rollback retention. Reload now removes only old MDD release-version tags before pruning,
  while preserving the current version, `:previous`, the trusted Engine base, unrelated images,
  and every image still referenced by a container.

## [1.5.2] - 2026-08-26

### Fixed

- The sign-in screen laid out the “keep me signed in” checkbox above its label because the
  generic form-label grid rule overrode the row layout. The checkbox and text now stay aligned
  on one line.

- [Issue #14](https://github.com/MddIdd/mdd-sim-gateway/issues/14): the 30-second status refresh
  replaced a number-keeping form even while it contained unsaved edits. Polling now refreshes an
  untouched form but leaves an active draft alone until it is saved or the row is closed.

- [Issue #15](https://github.com/MddIdd/mdd-sim-gateway/issues/15): successful self-updates left
  superseded untagged Docker images and obsolete build stages on the gateway. The updater now
  prunes dangling images after services reload successfully, while retaining live images and the
  explicitly tagged rollback image.

## [1.5.1] - 2026-08-26

### Fixed

- The balance and allowance page told users with an unknown carrier query method to configure
  it on the Messages page, even though that editor had moved away with the old Messages-page
  integration. Query settings are now available directly on the balance and allowance card,
  and attempting a query without a rule expands the editor in place.

- [Issue #13](https://github.com/MddIdd/mdd-sim-gateway/issues/13): an amd64 host upgrading
  from v1.4.1 downloaded the ARM64 Engine Release asset, then failed only after the source had
  already been replaced because the imported image could not pass the host-architecture check.
  Release image assets now remain ARM64-only by design, while amd64 upgrades skip them and refresh
  or build native Engine and Docker-control images locally. The v1.4.1 handoff also overrides its
  old `--no-engines` request on amd64 so the previous Engine cannot be left behind.

- A locally preserved virtual environment whose `pip` launcher had lost its executable bit made
  reload fail even though the Python interpreter and installed dependencies were healthy. Reload
  now invokes pip through the virtual environment's interpreter, avoiding that unnecessary
  dependency on the wrapper script's mode.

## [1.5.0] - 2026-08-26

### Added

- Balance and number keeping, on one page. A carrier reclaims a number that never bills, and
  nothing here tracked that: balance was buried on two other screens and activity was not
  shown at all. The page answers whether each number is still on a network, still funded, and
  whether anything is keeping it used — and can now keep it used, by producing one real
  chargeable event on a schedule you set. A prepaid SIM sends a billed SMS; a plan SIM renews
  itself and instead has its balance watched against the next cycle's fee. A free balance
  lookup is not usage with most carriers and cannot stand in for either.

  Lines whose SIM is not currently in the gateway are listed separately: they cannot be kept
  alive, but they are the ones sitting unused, so their expiry is the most useful thing the
  page can show. They can also be deleted there — ported-away numbers and old test entries
  accumulate, and a line whose reader is absent could not be reached from the device page.

- Voicemail. An incoming call nobody answers — which, for a SIM kept at home, usually means
  no browser was open — now records the caller's message instead of ringing out into nothing.
  Recordings play from the call log beside the call they belong to, and stay on the gateway:
  they are never attached to a notification and never collected into a support bundle. Off by
  default, with a per-line override, because recording a caller is the operator's decision
  rather than something the product should assume. A call declined on the softphone is never
  recorded.

- Missed calls are announced. Until now the only call notification fired while the phone was
  still ringing, which is the moment least useful to someone who is not at the browser. A
  message left after the call replaces that notification rather than adding to it, so one
  unanswered call cannot buzz a phone twice.

- The control plane now checks releases in the background even when nobody is signed in and
  can send a deduplicated new-version notice through the configured Webhook, Telegram and
  PushPlus channels. Administrators can announce every release or only major/minor feature
  updates, ignoring a patch-only change to the final version component.

- Automatic updates are opt-in and use a separate promotion gate. Publishing a GitHub Release
  does not authorize unattended installation: the exact latest version and its earliest rollout
  time must also be approved in `update-policy.json`, allowing a release to soak before rollout.

### Changed

- Push notifications lead with the product name. A notification arrives out of context — on a
  lock screen, or in a Telegram list beside a dozen other bots — where "未接来电" alone does
  not say which machine is talking.

- The Engine image now builds Asterisk, pjproject and pcsc-lite in a disposable build stage and
  copies only their runtime closure into the image sent to the gateway. The ARM64 image is about
  1.04 GB unpacked instead of 3.22 GB, while retaining the same 334 Asterisk module files; this
  materially reduces release downloads and gateway system-disk use without narrowing the
  supported codec or module surface.

- Releases now build the Engine natively on an ARM64 GitHub runner from reviewed mirrors of the
  pinned sysmocom commits and publish the same versioned image through GHCR and as a checksummed
  Release asset. One-click update downloads that asset through the same direct-to-proxy fallback
  as the rest of an update only when Engine inputs changed, verifies its architecture, version
  and source fingerprints, preserves the previous image for rollback, then recreates only
  affected lines.

### Fixed

- [Issue #10](https://github.com/MddIdd/mdd-sim-gateway/issues/10): two identical USB readers
  without serial numbers could assign different live SIMs to the same Instance after reader names
  changed across a reboot. Live card identity now decides attribution before the saved reader index
  is refreshed, preserving the one-to-one mapping between SIMs and lines.

- [Issue #12](https://github.com/MddIdd/mdd-sim-gateway/issues/12): recreating the Docker control
  container during a self-update dropped the sing-box, xray and host-tool mounts used by network
  exit tests. The installer now restores those mounts and their environment variables every time
  it recreates the container.

- Signing in again several times a day. Sessions were held in memory only, which reads as a
  deliberate choice for an appliance until you count the restarts: replacing an Engine image,
  reloading and every self-update restart the control plane, and each one logged every browser
  out. Sessions now survive a restart, and the sign-in screen offers to keep the browser signed
  in for 30 days instead of the 12-hour default. Only a hash of each token is stored, so the
  file cannot be replayed as a cookie by anyone who can read it, and changing the password
  still revokes every session everywhere.

- Reloading an updated Engine image no longer leaves every replaced WiFi Calling line stopped.
  After removing containers that still use the previous image, the installer now restarts only
  the control plane so its initial reader scan brings each present SIM line back automatically;
  Engine containers that were not replaced keep running.

- Engine release builds no longer depend on `wget` completing Asterisk's sound downloads without
  a deadline. Those small upstream assets now use bounded retries, low-speed detection and resume,
  preventing a transient slow connection from leaving the ARM64 release job hung indefinitely.

- A forced Engine source build had no way to override the reviewed GitHub source mirrors, so an
  installation network that could reach the pinned upstream sysmocom repositories but not GitHub
  failed before compilation began. The installer now passes explicitly configured pjproject and
  Asterisk repository overrides into the Docker build while retaining the reviewed mirrors as the
  safe default.

- Four backend status messages appeared in English on a Chinese interface, including the one
  shown when a reader holds another line's SIM — the sentence a user sees precisely when they
  need to understand a binding mistake. The connectivity timeline could not label that state
  at all and drew the raw code. Both tables are now checked against the backend by a test,
  because a missing translation is invisible until someone reaches that exact state.

### Removed

- The 3/2/1-day activation reminder. Number keeping covers what it was for: a plan SIM now
  reports a balance too low to renew, and every line's expiry is on the page with the same
  countdown. It only ever fired for lines whose activation date had been filled in by hand.

## [1.4.1] - 2026-08-22

### Fixed

- Dialling a carrier service code announced the wrong outcome while its answer was still on
  its way. A code's verdict and its reply text reach the browser separately, and the screen
  drew a conclusion from whichever arrived first — reporting that no reply was coming, or
  that the code returns no text, a second before the text appeared. It now says it is waiting
  until there is something to report, and distinguishes a code the carrier accepted (a reply
  may follow) from one it refused (nothing more is coming).

- The same screen could flip back and forth between outcomes. Every event refreshed the call
  list, and those concurrent requests could return out of order, letting a stale response
  overwrite a newer one — the result alternated between "waiting" and the answer. Refreshes
  are now ordered so only the newest may take effect, and a verdict, once shown, is no longer
  withdrawn.

## [1.4.0] - 2026-08-22

### Added

- The dialler now accepts carrier service codes such as `*21*<number>#` (divert), `*#21#`
  (check divert) and `#225#` (balance). Three separate layers rejected them before, each
  looking like the last: the browser validated the input as either a short numeric code or an
  E.164 number, JsSIP refused to build a request URI because `#` is not a legal user character
  in a SIP URI, and the dialplan's outgoing extension pattern matched only `+` or a digit in
  first position. All three now pass the code through untouched, escaping `#` for transport and
  restoring it on the way out, so what reaches the network is what was dialled. What a code
  then means is the carrier's IMS to decide, not this gateway's: supplementary-service codes
  are the ones a TAS normally answers, whereas USSD codes need a USSI gateway the carrier may
  no longer operate, and a carrier that has moved self-service into an app may simply decline.
  Codes a handset answers by itself, currently `*#06#`, are answered from the line's own
  provisioning instead of being dialled, since no network ever replies to those. Service codes
  are refused on the cellular-modem transport rather than dialled as a voice call, which is all
  that path can do; reaching them through a modem would need `AT+CUSD`, which is not built.

- A dialled service code now reports whether the carrier served it. Because the code travels
  as a call, the outcome used to arrive on a call's vocabulary — "declined", "no answer", a
  running duration timer — none of which describe a request that is answered and torn down in
  the same second, and all of which hide the one thing worth knowing: whether this carrier
  supports the code at all. The Q.850 cause already reaching the control plane distinguishes
  the cases, so codes are now scored on their own scale. An unknown code (404 Not Found),
  a malformed one (484) and an unimplemented service (501/488) read as not supported; a 403
  or 603 reads as refused, which is a different problem with a different fix — the request
  reached the carrier and was declined by account policy rather than missing from the network.
  Silence stays "no response" instead of being reported as unsupported, since nothing came
  back to justify that claim.

- A service code now shows what the carrier actually replied. The answer to `#225#` or `*#21#`
  is not audio: the carrier puts it in the body of a SIP request inside the established dialog
  — T-Mobile US uses the BYE — which is why such a "call" is silent and over in under a second.
  That body was discarded, so an accepted code could confirm only that the carrier acted, never
  what it said. The engine now copies the `application/vnd.3gpp.ussd+xml` payload onto the
  channel and the control plane parses it (3GPP TS 24.390), storing the text with the call so
  the balance or divert status appears where the code was dialled. The payload is bounded on
  both sides — the engine refuses a body larger than 4 KB before copying it onto the stack, and
  the parser caps the text it will store — and a body that is not decodable text is logged and
  ignored rather than stored. Carriers that namespace the XML differently are handled; one that
  returns no payload at all still reports the outcome as before.

### Fixed

- A line could retransmit forever against an exit connection that had already died. When the
  exit is blamed but cannot be moved — strikes still short, pool exhausted, or the node pinned
  — holding is the right call for node selection, but it used to leave one recoverable failure
  unattended. sing-box keys a UDP session on its 5-tuple and retires it on an idle timer, and a
  line rebuilding its tunnel refreshes that timer with every IKE retransmit: a session whose
  outbound is dead is held open by the very retries meant to recover it, every later packet
  goes to the same dead connection, no new session is ever created, and nothing is logged.
  Rebuilding the container does not help, because it produces the same 5-tuple and lands on the
  same dead session. Seen after a self-update restarted the orchestrator: two lines sharing the
  only GB exit dialled before the route was up, got "no route to host", and stayed stuck for
  twenty minutes while their tunnels reported CONNECTING and the exit itself tested fine. The
  control plane now names the country when it blames an exit and declines to move it, and the
  orchestrator closes that country's connections so the next packet has to dial afresh. This is
  deliberately the weaker sibling of switching nodes: it changes no node, respects a pin, and
  is gated on the same check, so an exit carrying a registered sibling line is never touched.

- A service code beginning with `#` produced no call-log entry at all. The dialplan reports a
  call through a shell, where an unquoted `#` at the start of a word opens a comment: dialling
  `#225#` therefore discarded the number and every argument after it, and the record was never
  created — not stuck in a wrong state, simply absent. `*#21#` was unaffected, its `#` falling
  mid-word, which is why the failure looked arbitrary. The argument is now quoted.

- A very short call could stay on "dialing" in the call log forever. The dialplan reports a
  call's start and its outcome from separate backgrounded processes, so nothing orders the two:
  when a call ends in under a second, the outcome can reach the manager before the record it is
  meant to close, and it was then dropped silently — the call never left "dialing" even though
  it had completed. A dialled service code answered on the BYE does exactly that, which is how
  this surfaced, but the race was never specific to service codes and had been latent for any
  short call. The outcome now waits briefly for the record it belongs to instead of being
  discarded.

- A SIM in an ordinary USB smart-card reader could report NO_CARD with the tunnel already up
  (issue #8). A modem bridge presents one SIM on three logical slots so PIN keeping, tunnel
  authentication and IMS-AKA can work independently; an ordinary reader has a single slot and
  does all three through it. The engine contract nevertheless filled the unset slot numbers with
  the modem layout, sending IMS-AKA to slot 2 — which on a one-reader gateway does not exist, so
  the SIM read as absent while the same card answered the tunnel perfectly. Each role now
  follows the reader the line is actually bound to, and the engine falls back to the only reader
  present rather than refusing a slot number that names nothing. A modem line keeps its
  dedicated channels. Gateways with two or more readers were unaffected: the stable USB-port
  binding already resolved the right one there.

- Learning a line's phone number no longer risks the connection it just made (issue #8). WiFi
  Calling came up, held for a few seconds and was then torn down and re-established, with the
  carrier answering "503 Service Unavailable" — the number was learned by enabling SIP tracing
  and sending an extra REGISTER purely to produce a response that could be read, and some
  carrier IMS cores decline an unsolicited re-registration seconds after accepting one. Asterisk
  reports that as a rejected registration, and the health policy acts on rejections. The carrier
  already announces the number in the registration the line makes anyway, so the engine now
  records it from that response and the control plane reads it from the log: nothing extra is
  sent, and SIP tracing — which also writes authentication headers into the container log — is
  no longer switched on to ask a line for its own number. The same applies to the six-hourly
  ported-number check. Every public identity the carrier lists is recorded, so a network that
  puts an IMSI-derived identity ahead of the dialable number is read correctly. Requires a
  rebuilt engine image; an older engine keeps a number already learned but cannot learn a new
  one, which a manually entered number covers.

- Binary SMS no longer appear in your conversations as walls of mojibake. Not every message is
  meant for a person: carriers and services also send machine payloads — SIM data-download,
  silent app pushes — whose content is arbitrary bytes rather than characters. The PDU says so
  in its header, but Asterisk unpacks 8-bit data one byte per character and hands back a
  string, so these landed in the message list looking like a text with a broken encoding, and
  raised a notification each time. The engine now reports the message's TP-PID, TP-DCS, user
  data header and raw PDU, and the control plane files anything that is 8-bit, addressed to the
  SIM (message class 2) or marked as SIM data-download into a separate store instead of showing
  it. Payloads already in the database are moved there on startup — nothing is discarded, and
  the bytes are kept verbatim, since identifying an encrypted payload needs the PDU as it
  arrived rather than a decode of it. Messages gains a collapsed "Non-text payloads" section
  listing what was filed, with the reason it was filed and the raw bytes: the classification
  reads the PDU header, so a carrier that mislabels a real text's data-coding scheme would
  otherwise hide it for good with no way to notice. A rebuilt engine image is what supplies the
  header fields; until then the control plane falls back to recognising a payload by its
  content, which catches most but not all of them.

## [1.3.15] - 2026-08-20

### Fixed

- A line whose SIM is reached through a modem bridge could stop registering after the engine
  was rebuilt. The card-binding check introduced in 1.3.13 reads EF.ICCID through a call that
  has no timeout; on a bridge channel that read does not fail but hangs, and every caller's
  "a card that will not answer is a fault, not proof of a swap" rule only applies once the
  read comes back. The line then rebuilt every couple of minutes with its tunnel established
  each time, so nothing pointed at the real cause. Card reads are now bounded, and the binding
  question is settled once when the SIM bridge starts rather than on every authentication —
  the carrier allows three seconds for that exchange, and re-checking a settled question
  inside it was enough to lose the registration on its own. The check keeps its full force on
  every reader: two look-alike modems swapping USB ports is exactly what it exists to catch.
  Only installations that rebuilt their engine image were affected; a self-update preserves
  the existing image and could not reach this.

## [1.3.14] - 2026-08-20

### Added

- A text longer than one SMS now arrives as one message instead of several. The SMSC splits
  such a text into separate SMS-DELIVER PDUs, each carrying a header that says which part it is;
  Asterisk unpacked that header but discarded it, so every part surfaced as its own message —
  out of order, since the parts are not delivered in sequence, and each one raising its own
  notification. The engine now exposes the part's reference/total/sequence and the control plane
  buffers the parts until the whole text can be assembled, then stores and notifies once. Parts
  the carrier re-pushes when an acknowledgement is missed are absorbed rather than duplicated.
  If a part never arrives, the rest is still shown after three minutes with the gap marked, so
  nothing is held back indefinitely. Requires a rebuilt engine image; an older engine keeps the
  previous per-part behaviour.

- Added a read-only USB passthrough diagnostic for gateways running inside a Proxmox VM. The
  support bundle answers the card-path questions from inside the gateway, but a VM cannot see
  the layer above it: when passthrough breaks the guest only observes that the modem is gone,
  while the reason lives in the host's USB and QEMU state. The script runs on either side —
  host mode queries `qm status`, the passthrough-relevant config lines, host USB topology and a
  scripted read-only `info usbhost` / `info usb` monitor snapshot; guest mode covers device
  nodes, bound drivers, service state, kernel events and the live bridge and VPCD listeners —
  so the two reports can be compared. It refuses host mode without an explicit VM id, because a
  report about the wrong VM is worse than no report, and masks IMSI/ICCID/IMEI-shaped digit runs
  by default since the report is meant to be shared.

## [1.3.13] - 2026-08-17

### Added

- Maintenance can restart the gateway, in three scopes ordered by what they interrupt: the
  control plane alone (the page drops for a few seconds; SIM bridges, engine containers and
  calls in progress are untouched), all gateway services (the control plane and the
  orchestrator together, which rebuilds every SIM bridge and re-registers the lines), and the
  host itself. Each states what it will cost before it runs. The control plane can carry out
  none of them — it is unprivileged and is itself restarted in every scope — so it publishes
  the request and the root orchestrator performs it, detaching into a transient systemd unit
  whatever would otherwise kill the process running it. A request nothing picks up within a
  minute is reported as such instead of leaving the page waiting for a restart that will never
  come, and the two scopes that take the orchestrator with them — which therefore cannot report
  their own completion — are closed out by the orchestrator when it comes back, so no restart
  leaves a document stuck on "running" either.

### Fixed

- A line no longer authenticates against another line's SIM. A reader binding names a slot —
  by PC/SC name, USB port or index — and says nothing about which SIM sits in it; when a line
  opened its sibling's card the only symptom was `SW=9862` from the carrier's AKA challenge,
  byte for byte what an ePDG returns when it genuinely rejects a subscriber. The freeze was
  therefore charged to the exit node and the line rebuilt every few minutes while the actual
  fault went unmentioned. EF.ICCID needs no PIN, so the card identifies itself before anything
  else touches it: the PIN keeper, the AMI USIM worker and the SWu/IKE worker each refuse a
  card that is provably not the line's and name both ICCIDs. Only an ICCID actually read
  convicts a reader — an unreadable EF.ICCID is a transient card fault, not evidence of a swap.
  The control plane classifies this as a local card fault before the exit policy sees it, so a
  binding mix-up can no longer cost a healthy exit node its place.
- A drifted PC/SC reader name is no longer treated as a fault by itself. The USB-port binding
  exists precisely so a line keeps opening the reader that physically holds its SIM after
  pcscd renames or re-enumerates it, so "opened name != stored name" is a normal state — and
  one the ICCID check already settles. Reporting it held the line forever and silently
  disabled exit failover for it, so a line whose real problem was its exit could never move
  off a bad node while the UI blamed the binding. The name is now consulted only when the card
  will not identify itself, which is the case it was added for.
- `SW=9862` is described by what the host can actually see. A mix-up is physically impossible
  with a single SIM present, where 9862 is the carrier rejecting that SIM's key material —
  provisioning or subscription, not hardware. Holding is still correct either way, and an
  unreadable card cache falls back to the cautious plural reading instead of asserting a
  single-SIM host that may not be one.
- An interrupted update no longer leaves the update dialog spinning forever. An updater killed
  mid-flight — the host rebooted or lost power, its transient unit was stopped, the process was
  OOM-killed — cannot record its own death, so the progress document it was publishing to
  stayed "running" and the dialog resumed into that dead progress view on every visit, counting
  up for days, with no way out but deleting the file over SSH. The orchestrator now retires a
  run whose updater unit no longer exists, keeping the stage and asset it died on; the control
  plane stops treating a document nothing has refreshed as proof of a live update, so the
  dialog offers the update again instead of resuming it; and a run that goes quiet while the
  dialog is open can be dismissed from the dialog itself.
- A download in progress now says how far along it is. The progress bar was only drawn once a
  byte had arrived and the updater published no byte counts until its first heartbeat, so a
  transfer that was stuck — curl working through its connect retries — presented as a file
  name and a climbing clock, indistinguishable from one running normally. The bar is drawn
  from the first poll, at zero bytes included, alongside the transferred and total size, the
  rate and an estimate of the time left. The rate is measured over the recent window instead
  of averaged since the start, so a slow beginning no longer depresses the estimate for the
  rest of the transfer, and a Release whose size the update check never returned gets an
  indeterminate bar rather than a countdown that would be a guess.

## [1.3.12] - 2026-08-17

### Added

- Added a guarded PVE helper that rebinds exactly two `2c7c:0125` modems by stable physical
  USB topology after a port change. It refuses ambiguous hardware, unrelated target-VM
  settings, and devices configured in or still held by another VM before changing anything.

### Fixed

- eSIM operations now work on a cellular module's own SIM. Each modem VPCD slot emulates the
  LPA's exact `MANAGE CHANNEL` OPEN/CLOSE handshake over its preallocated physical channel:
  OPEN returns the channel the slot already owns and CLOSE succeeds without releasing it,
  preventing duplicate allocation or closure of the bridge-owned channel. When the LPA closes
  that channel the bridge restores the plain USIM view, because the slot is shared with the PIN
  keeper and the engine, which select ADF.USIM on the same real UICC channel.
- A repeated MANAGE CHANNEL OPEN answer no longer takes the SIM down. It was treated as proof
  that the UICC had no channels left, so the bridge exited within seconds of every start and
  stopped both lines; a late AT reply read as the answer to the next command produces the same
  symptom with a healthy SIM. The port is settled and the channel requested again, and only a
  duplicate that survives the retries is reported as an allocation failure.
- A disabled line no longer serves the last observation taken while it ran, which left a device
  reading "no SIM card" after VoWiFi was switched off until the next poll overwrote it.
- Flight-mode-only VoWiFi can settle on a direct-serial SIM bridge without keeping
  ModemManager active. Three consecutive ModemManager `PhoneFailure` logical-channel
  allocation failures also trigger that fail-closed fallback while cellular data stays off.
- VoWiFi-only serial mode no longer probes or controls modem radio ports after it has claimed
  the SIM AT port, preventing ModemManager-style contention and empty `ATE0` replies on
  virtualized USB passthrough.
- Saved lines now follow their SIM by ICCID/IMEI when a modem is replugged onto a different USB
  path. Provisioning can recover from persisted modem metadata when live APDU access is not yet
  available, while preserving each line's PIN, SWu and AMI virtual-reader slots. Every manual
  start and health-policy rebuild revalidates the complete virtual-reader group against the
  bridge's current ICCID metadata before creating the engine, so a stale live reader name cannot
  send one carrier's IMS-AKA challenge to the other modem's SIM (`SW=9862`).
- Engine PIN and IMS authentication workers now honor the exact per-modem virtual PC/SC reader
  names supplied by the control plane instead of falling back to a global reader index.
- Each modem now loads an isolated VPCD driver copy. This prevents the driver's process-global
  slot table from making two identical modems overwrite one another, which previously left both
  lines reporting `NO_CARD` even though all bridge sockets were connected.
- The sidebar Star count is no longer erased by the one-minute system-status refresh. Its
  GitHub metadata lookup now has an independent cached retry path instead of waiting up to
  six hours for the next release check after a transient network failure.
- Switching a saved eSIM profile in a cellular modem now stops that modem's old lines,
  rebuilds only its VPCD bridge, waits for a new ready logical-channel generation, verifies
  every exposed virtual reader against the target profile and starts only the matching line.
  Failed LPA operations restore the exact previous running snapshot; post-switch recovery
  failures stay stopped instead of authenticating an old line against the new card identity.
- Proxy node names render in the UI font instead of the emoji-flag font, and the call log's
  remaining English strings are translated.
- Support bundles now carry the per-line diagnostic records instead of blanking them. A
  record embeds its tunnel log tail, and one routine engine message inside that tail matched
  the log redactor's key-material rules — which blanked the whole record and the two records
  after it, emptying the one file written to survive a rebuild loop. Records are now redacted
  as structure, so only the offending log lines go and the registration, SIP and host evidence
  beside them survives.
- Support bundles now state each line's status, classified reason and retry-budget position.
  Tunnel logs that all end at `CONNECTED` cannot explain why a line kept being rebuilt; the
  reason code and how long the line has been failing can.
- Release cross-builds compile the architecture-independent WebUI on the runner's native
  platform, avoiding an indefinitely slow `npm ci` under ARM64 QEMU on GitHub-hosted runners.
- The update dialog now exposes every host-side stage, install mode, selected download route,
  current Release asset, byte progress, speed, elapsed time, live reload activity and the exact
  failed stage instead of presenting a generic spinner. The detached updater publishes a
  heartbeat throughout downloads and service reloads.
- Auto update networking now falls through its remaining proxy-library routes when an asset
  download fails or the current route remains too slow, then reuses the route that succeeded
  for the checksum and Docker control-image assets.

## [1.3.11] - 2026-08-16

### Changed

- Software updates now default to direct-first Auto networking and fall back through the
  shared proxy library, reusing the successful check route for downloads. Docker-mode updates
  import a checksummed ARM64 control-image Release asset through that route instead of asking
  the Docker daemon to pull it.

### Fixed

- Proxy node country flags now use the bundled Twemoji Mozilla font across overview, detail,
  status and selection surfaces, so regional-indicator sequences remain flags on platforms
  that otherwise render them as country-code letters. Proxy source types also use recognizable
  Emoji icons.
- The sidebar Star count keeps a stable slot to the right of its icon and retains the last
  successful value when a later GitHub metadata request fails.

## [1.3.10] - 2026-08-16

### Added

- Added a VoWiFi-only mode switch to System settings (default off). Enabled, ModemManager
  never runs and every SIM bridge drives its modem's serial port directly: VoWiFi keeps
  working, while cellular data, flight mode and cellular SMS/calls are presented as
  unsupported rather than forever starting. This is for hosts — virtual machines, containers —
  where ModemManager's modem objects are unstable: on such a host its periodic loss of a
  modem object severed SIM access mid-tunnel even though the serial port never went away.
  Flipping the switch restarts the card path once (about thirty seconds) and is confirmed
  before it applies; the ModemManager unit is disabled while the mode is on so a reboot does
  not start, stop and reset the modems on every boot.
- The redacted support bundle now answers the card-path questions that previously cost a
  support round trip each: the exact command every SIM bridge runs and its recent output
  (bridge output now lands in per-modem files that survive journal rotation), whether pcscd
  is actually listening on each assigned virtual-reader port (read from /proc/net/tcp — a
  probe connection could hijack a reader slot, a file cannot), the reader-definition
  directory listing, the configured modem backend, and the live reader list as pcscd exposes
  it. `install.sh diagnose` keeps its role for active probing (per-reader lpac reads) and
  now includes the bridge log files as well.

## [1.3.9] - 2026-08-15

### Fixed

- ModemManager is stood down once it has refused every present modem and no device asks for
  cellular. Without a modem object it provides nothing — data, flight mode and cellular SMS all
  need one — but its periodic probes still opened the same AT ports the direct bridges hold,
  and the interleaved traffic corrupted SIM channel allocation: a bridge would read the reply
  to ModemManager's own probe where its +CSIM answer should have been, and only allocate
  channels in the gap between probes. Standing it down skips the modem reset (it never owned
  the modems) so the refusal verdicts survive. Enabling cellular on any device brings it back,
  refusals notwithstanding: that request must fail visibly, not be silently pre-empted.
  ModemManager-managed deployments are unaffected — the stand-down requires a recorded refusal
  for every present modem.

## [1.3.8] - 2026-08-15

### Fixed

- The serial fallback acts on ModemManager's own refusal instead of waiting it out. When
  ModemManager has logged that it cannot create a modem for this hardware, the bridge takes the
  serial port at once; the three-minute grace period remains only for hosts whose journal says
  nothing. An affected host previously paid the full wait on every boot.
- Opening the modem's AT port tolerates absent modem-control lines. pyserial raises DTR and RTS
  as part of open with no way to opt out, and on virtualised USB passthrough that control
  transfer can fail — which killed the bridge for two lines an AT channel never uses. Unrelated
  errors still fail loudly.
- A bridge that keeps dying is now visible and paced. Its exits are recorded with the exception
  it wrote on the way down, respawns back off exponentially to ten minutes, and the device error
  names the count and reason. Status no longer reports a just-respawned process as a running
  bridge while it crash-loops, and a bridge that runs stably lives its failure history down.
- The pcsc-lite source build works on a fresh Debian 13 host: meson resolves its systemd
  dependency through systemd.pc, which trixie moved into the new systemd-dev package.
- `install.sh diagnose` no longer filters the one line that names a crashed bridge's exception
  out of its own report; traceback context is kept.
- The packaged "Virtual PCD" reader definition can no longer reappear as phantom devices even
  if a package reinstall restores the file the installer disables: the device list drops that
  endpoint on its own.

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
- Answered MANAGE CHANNEL inside the module SIM bridge instead of refusing it. An LPA opens a
  logical channel before it can select the ISD-R, and lpac reports that refusal as a bare
  `euicc_init`, so eSIM management over a cellular module could never start. The slot already
  owns a UICC channel, so OPEN now reports it and CLOSE is acknowledged without releasing it.
  Closing that channel also restores the USIM file system, because the slot is shared with
  PIN keeping and the engine and an eUICC application left selected there reads as no card.
- Stopped a device from reporting the last problem of a line that was just switched off.
  The status cache kept serving that observation until the next poll, which is how a module
  could read "no SIM card" with its SIM in the reader. A disabled line now reports stopped
  immediately, and switching VoWiFi off records the stop the way an explicit stop does.
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
