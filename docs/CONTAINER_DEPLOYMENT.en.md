# Full container deployment

[中文](CONTAINER_DEPLOYMENT.md)

This guide covers Synology Container Manager and other amd64/arm64 hosts with Docker Compose v2.
The deployment runs three base services plus one Engine for each enabled line, for a steady-state
total of `3 + N` containers:

| Service | Purpose |
| --- | --- |
| Control | Web console, API, persistent data, line configuration and Engine orchestration |
| Hardware | Private D-Bus, ModemManager, NetworkManager, pcscd, USB access and SIM bridges |
| Egress | Country-specific SOCKS5 TCP/UDP exits |
| Engine | Per-line SWu, IMS, calls and messaging; created dynamically by Control |

The Compose file declares only Control, Hardware and Egress. Do not copy an Engine service into
the file for each SIM.

## 1. Release and host requirements

Use `mdd-sim-gateway-compose-vX.Y.Z.yaml` from a GitHub Release. All four GHCR image references in
that asset are pinned to the same version; `latest` is never used. A Release without this asset is
not a supported full-container release.

Before deployment, prepare:

- Docker Engine and Docker Compose v2; use the official Container Manager package on Synology;
- an amd64 or arm64 host;
- at least 6 GiB of free space for the first pull and one rollback image generation;
- a dedicated persistent directory, such as `/volume1/docker/mdd-sim-gateway` on Synology;
- an unused HTTPS host port; the default is `10443`;
- a fixed LAN address or stable LAN DNS name for the host;
- host USB enumeration under `/dev/bus/usb`. Cellular modems also need their serial, QMI/MBIM and
  network-interface device nodes.

Do not install host pcscd, ModemManager or NetworkManager first. The Hardware container owns those
user-space services in full-container mode; a host copy can claim the USB device before it does.
Ubuntu and some other distributions enable ModemManager by default; disable it before deploying:
`sudo systemctl disable --now ModemManager`. The host must run Linux: Docker Desktop (macOS/Windows)
cannot hand USB devices to containers, and rootless Docker is not supported.

## 2. Decide whether the host needs a driver

Plug in the modem or reader before deployment. A modem normally exposes nodes similar to:

```text
/dev/ttyUSB0
/dev/cdc-wdm0
/sys/class/net/wwan0
```

A standard PC/SC reader only needs to appear under `/dev/bus/usb`; pcscd and libccid are supplied by
the Hardware image. If all required nodes exist, the host needs no MDD package at all — just create
the Compose project.

When nodes are missing, the Hardware container stays unhealthy and Control, which depends on it,
never starts. This release does **not** ship an automatic driver preflight status: run the commands
above yourself to establish which nodes are absent, then compare that against the
[NAS compatibility and driver catalogue](../drivers/README.en.md) by hand. A driver must match the
exact NAS vendor, model, CPU platform, architecture, complete OS build and kernel release; never
install a package built for a similar model or OS version, and never load an unknown `.ko` or `.spk`.

**DS1621+ (DSM 7.4.1-90080, kernel 4.4.302+)** has a formal driver pack, published with every Release
as `mdd-driver-synology-ds1621plus-dsm7.4.1-90080-k4.4.302plus-x86_64.tar.gz` and covered by the Release `SHA256SUMS`. CI rebuilds it from Synology's public toolkit and
unmodified Linux v4.4.302 sources, byte for byte identical to the modules validated on hardware.
Installing it takes one SSH session:

```sh
sha256sum -c SHA256SUMS --ignore-missing     # in the directory holding the pack
tar -xzf mdd-driver-synology-ds1621plus-dsm7.4.1-90080-k4.4.302plus-x86_64.tar.gz
cd mdd-driver-synology-ds1621plus-dsm7.4.1-90080-k4.4.302plus-x86_64
sudo sh install.sh
```

The installer checks architecture, kernel, DSM build, platform and module checksums before touching
anything, and the boot hook repeats those checks at every start; after a DSM update that no longer
matches it loads nothing until a pack for the new build exists. Remove it with `sudo sh uninstall.sh`
from the pack. After installing, replug the modem or reboot and confirm the device nodes above appear
before creating the project.

## 3. Create the Synology project in the UI

1. Download `mdd-sim-gateway-compose-vX.Y.Z.yaml` and verify it against the Release
   `SHA256SUMS` file.
2. Open **Container Manager → Project → Create → Create docker-compose.yml**.
3. Enter `mdd-sim-gateway` as the project name and paste the YAML.
4. Replace the example `192.168.1.100` with this NAS's fixed LAN address.
5. If the Docker shared folder is elsewhere, replace `/volume1/docker/mdd-sim-gateway` with its
   absolute path. Do not use a temporary directory.
6. The default port mapping is `10443:8443`. If host port `10443` is occupied, change only the
   left side; keep the container port at `8443`.
7. If a proxy subscription is served by this NAS under its public DNS name, set
   `MDD_NAS_HOSTNAME` to that name. This changes resolution only inside Egress and does not alter
   NAS DNS or its default route.
8. Save and build the project. Container Manager pulls Control, Hardware and Egress. Control uses
   the same pinned version of the Engine image when the first line starts.

If GHCR is unavailable, import all four architecture-specific offline image archives from the same
Release after verifying `SHA256SUMS`. Never combine components from different versions.

## 4. Register a project created through SSH

Running `docker compose up -d` over SSH adds the containers to the **Container** page, but Synology
does not automatically add the stack to its **Project** registry. After starting it, create the
three Container Manager registration files once:

```sh
PROJ=mdd-sim-gateway
UUID=$(cat /proc/sys/kernel/random/uuid)
NOW=$(date -u +"%Y-%m-%dT%H:%M:%S.%6NZ")
CMDIR=/volume1/@appconf/ContainerManager/projects
sudo tee "$CMDIR/${UUID}.config.json" >/dev/null <<EOF
{"created_at":"${NOW}","enable_service_portal":false,"id":"${UUID}","is_package":false,"name":"${PROJ}","service_portal_name":"","service_portal_port":0,"service_portal_protocol":"","services":null,"share_path":"/docker/${PROJ}","state":"","updated_at":"${NOW}","version":2}
EOF
sudo touch "$CMDIR/${UUID}.action.log" "$CMDIR/${UUID}.lock"
sudo chmod 600 "$CMDIR/${UUID}.config.json"
sudo chmod 660 "$CMDIR/${UUID}.action.log"
sudo chmod 444 "$CMDIR/${UUID}.lock"
```

The Compose top-level `name`, the registered `name`, and `share_path` must describe the same
project. Refresh DSM after registration. If that DSM build still caches the old list, restart the
Container Manager package only during a maintenance window. **DSM 7.4 has been observed to stop and
then restart every container project during this package restart**; it is not a zero-downtime UI
refresh. Projects created in the DSM UI are registered automatically and do not need this procedure.

Create the data directory in File Station as usual; its owner and mode need no changes. Every file
inside is created by the containers as root with mode `0600`/`0700`, so messages, certificates, SIM
configuration and notification credentials are not readable by DSM accounts. Do not copy the common
`chown -R user:users` step used by ordinary Compose projects: it would hand those files to that
account and expose them through File Station or SMB. Edit the YAML through Container Manager.

## 5. Security boundaries in the Compose file

You may change the persistent path, NAS address, host management port and log sizes. Keep these
boundaries:

- fixed Control, Hardware and Egress names and ownership labels;
- Control's Docker socket mount, used only for ownership-checked MDD Engines and service restarts;
- Hardware's `/dev`, `/sys/devices`, private D-Bus and PC/SC volumes;
- Hardware host networking and its bounded capabilities; do not use `privileged: true`;
- the internal Engine network;
- no host-published Egress SOCKS port;
- `restart: unless-stopped` for recovery after a NAS reboot.

Hardware NetworkManager is restricted to `wwan*` and `cdc-wdm*`, and cellular connections are
created with `never-default`. Hardware stops cellular setup if it sees a NAS physical NIC, Open
vSwitch, VLAN, Docker bridge or loopback under its control. Country exits stay inside container
networks and do not install carrier routes into the NAS main routing table.

## 6. First-start acceptance checks

Wait for the three base containers to become running/healthy, then open:

```text
https://NAS_LAN_IP:10443/
```

Accept the initial self-signed certificate warning on a trusted LAN or VPN and create the
administrator account immediately. Then verify:

1. Control, Hardware, Egress and every Engine report the same version;
2. Hardware is healthy, no host pcscd competes for the readers, and NetworkManager has
   claimed no non-cellular interface;
3. hot-plugged modems and readers appear without rebuilding the project;
4. PC/SC readers expose VoWiFi/eSIM capabilities without cellular controls;
5. enabling modem 4G leaves the NAS default route unchanged;
6. the chosen country exit passes UDP checks and shows the selected node;
7. each enabled line reaches SWu connected, USIM authentication and IMS registered;
8. the Calls page opens and any reverse proxy forwards WebSocket upgrade headers;
9. a test SMS and call complete before production use.

The browser phone shares the WebUI origin and needs no separate WSS host port. RTP ranges begin at
UDP 30000 by default; allow the assigned range between clients and the NAS when crossing VLANs or
firewalls.

## 7. Persistent data, backup and certificates

The configured data directory contains the SQLite database, messages, call records, settings,
line state, certificates, eSIM cache, notification credentials and update state. All three base
services mount the same directory; none of this data should remain only in a container layer.

Before an update or migration, create a WebUI backup, stop configuration changes, copy the entire
data directory, and record the current Compose asset and image digests. Named volumes hold sockets
or rebuildable runtime state and are not a replacement for this backup. To restore on another NAS,
restore the data directory first and start the matching Compose version.

## 8. Update, rollback and removal

The one-click action in System Settings launches a temporary update helper in full-container mode;
it does not add another resident service. The helper:

1. downloads the native Control, Hardware, Egress and Engine Release assets and verifies their
   `SHA256SUMS`, architecture, component, ownership and version identities;
2. creates a consistent messages/MMS/configuration backup and retains the previous Compose file;
3. preserves the administrator's port, data path, NAS address and hostname mapping while replacing
   only the four image references;
4. recreates Hardware, Egress and Control, passes their health gates, then rolls the Engines;
5. records the installed image IDs and archive SHA-256 values in `update/installed-images.json`;
6. restores the previous Compose file, base containers and Engine images if a switch or health
   gate fails.

The helper starts from the old Control image and survives replacement of Control itself. It mounts
only the project data directory and Docker socket; it receives no host PID/network namespace or
privileged mode. Keep the NAS powered on during the update and sign in again when the UI returns.

The update is performed by the helper of the **currently running** release, so fixes to the helper
itself take effect from the next update.

A successful report means the base containers are healthy and the Engines were recreated on the new
image. If the health policy had stopped a line during the update, Control recovers it on the new image
shortly afterwards, so that line may re-register a minute or two later.

**Rollback drill** (for release validation): run `sudo touch <data-dir>/update/fail-after-switch`, then
start an update. The helper fails deliberately after every container runs the new release and the
Engines have been recreated, and restores the whole stack to the previous release. The marker is
removed when it fires, so the next update is a normal one. The drill interrupts the lines twice; do not
run it during business hours.

If the WebUI is unavailable, use the same boundaries for a manual recovery:

1. download and verify the target Release Compose asset and images;
2. back up data and retain the current Compose file and images;
3. rebuild the Container Manager project with the new YAML;
4. verify Hardware, Egress and Control, then wait for Engines;
5. recheck the NAS default route, devices, country exits, SWu and IMS;
6. restore the previous YAML, images and pre-update data backup if a base service fails.

Do not let Watchtower update one component independently. Stopping or deleting the project does
not delete its bind-mounted data unless the administrator removes that directory. Manually
installed kernel drivers are independent of Compose and must be removed separately.

Engine containers are created per line by Control and are not part of the Compose project, so deleting
the project leaves them behind, still attached to `mdd-sim-gateway-engine`; removing that network then
fails with "Resource is still in use". When uninstalling over SSH, remove the Engines first:

```sh
sudo docker ps -aq --filter "label=io.mdd-sim-gateway.component=engine" | xargs -r sudo docker rm -f
```

## 9. Common problems

**Hardware is unhealthy:** confirm the host enumerated the USB device, then use the section 2
commands to check whether the required device nodes exist — missing nodes mean the host lacks the
kernel driver. `docker logs mdd-sim-gateway-hardware` reports why the supervisor failed.
Re-evaluate drivers against the complete build and kernel after every DSM update.

**Rebuilding the project reports "dependency failed to start: container mdd-sim-gateway-hardware is
unhealthy":** a new Hardware container inherits the modem's stale QMI session and must reset the modem
and wait for it to re-enumerate, which takes a minute or two; if that outlasts the health-check grace
period, Control stays in `Created`. Wait for Hardware to
become healthy, then click Start on the project — not Build, which recreates Hardware again.

**Startup logs say "PIDs limit discarded":** the DSM 7.4 kernel (4.4) has no pids cgroup, so Docker
ignores `pids_limit` from the Compose file and warns. It is harmless; memory limits still apply.

**A reader is missing:** stop any host pcscd or other container that has claimed it. The Hardware
image already contains pcsc-lite, libccid and the project's verified reader patches.

**A NAS-hosted subscription works in a browser but not in Egress:** configure
`MDD_NAS_HOSTNAME` with the original public DNS name. Do not replace the subscription URL with a
container IP or alter global NAS DNS.

**Concern that modem 4G changes NAS networking:** verify the original LAN default route remains in
place. Cellular profiles use `never-default`; diagnostics stop the cellular configuration if
NetworkManager claims a non-cellular interface.

**The page opens but VoWiFi does not register:** inspect the reader/SIM path, country-exit UDP
result, ePDG resolution, SWu and IMS in order. An implemented software path cannot override a
carrier restriction on the SIM, plan, region or device identity.

Submit new compatibility results through the repository's **NAS hardware compatibility** issue
template after reading the [catalogue privacy and driver admission rules](../drivers/README.md).
