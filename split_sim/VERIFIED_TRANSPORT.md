# Select one transport for a split simulation

`with_transport.py` runs one foreground transaction using either `direct-dhcp` or `wifi-only`. The former expects the Jetson Ethernet address 10.55.0.2 and a client lease from its DHCP server. The latter expects 192.168.68.56 on the Jetson Wi-Fi interface. The reviewed Mac adapter MACs are defaults for the team Mac. Another laptop supplies its actual adapter MAC with `--client-mac`. Host-key verification uses the existing `skybeta-19.local` known_hosts identity. The existing split authentication setup applies.

The archived Jetson NetworkManager profile establishes 10.55.0.2/24 on enP8p1s0. The previous 169.254.55.2 link-local address was removed. The simulator passes the selected authenticated address to PX4 as its XRCE UDP agent on port 8889. The recorded DHCP ACK alone establishes neither end-to-end SSH nor successful PX4 delivery.

From a quiet simulator with the old tunnel released, use the command below to run a **read-only transport probe**. Choose a new evidence directory on each invocation. It labels the transaction and writes its immutable ID, baseline and observed participants in `transport.json` and `run-comparison.json`. The shared lease covers only the duration of the command.

```bash
python3 split_sim/with_transport.py --profile direct-dhcp --purpose flight \
  --name 'Direct Ethernet DHCP preflight' --baseline previous-run-id --before 'prior reported link' \
  --evidence "./evidence/$(date -u +%Y%m%dT%H%M%SZ)-direct-probe" -- /usr/bin/true
```

For a separately approved flight, substitute the single existing **foreground** reset → prepare → approval → monitor → archive transaction for `/usr/bin/true`. A detached launcher releases its parent’s ownership prematurely. Pass `--split /path/to/installed/jetson_sim` for the installed lab checkout after reviewing its staged changes. The accepted receipt is copied into the flight archive as `transport.json`. The caller can record `lease-end.json` or `lease-lost.json` alongside the completed grade. For Wi-Fi, select `--profile wifi-only` before launching. Each selection starts a new SSH master bound to the reviewed Mac interface. Release any previous owner of the camera/IMU forwards before starting the replacement; a port collision stops preparation.

Verification uses the authenticated master’s actual SSH_CONNECTION, the Mac route and adapter MAC, the Jetson interface/IP/MAC, the Mac ARP neighbor, and the Jetson return route. Direct selection additionally checks the Mac's DHCP packet and lease range. The same master carries camera/IMU forwards, supervision, and archive transfers. The launcher checks the existing PX4 process’s signed IPv4 agent parameter, UDP port and domain. The supervisor resamples transport every ten seconds. If verification fails, it leaves the reviewed onboard authority/failsafe behavior in place, records the loss and rejects further SSH/SCP operations. Live flight qualification still uses the existing sensor, authority, timing and grading checks.

## Shared flight and maintenance lease

The Mac flock is `/tmp/drone19-domain219-flight.lock`, shared with historical launchers on that Mac. Every updated launcher also holds `/tmp/drone19-domain219-flight-network.lock` on the actual Jetson for its entire flight transaction. The second host lock excludes cooperating jobs from different Macs. For a **separately authorized Jetson network maintenance** window, use the same wrapper with `--purpose network -- /absolute/path/to/reviewed-remote-program [args...]`. In this mode the argv runs **on the Jetson** under that same flock. The guard emits the receipt before authorizing execution. Run maintenance that affects one interface through the other qualified interface. Only foreground maintenance commands preserve lifecycle ownership. A network-purpose probe can use remote `/usr/bin/true`. Administrators and external NetworkManager/GUI actions must respect the lease; advisory locks cannot prohibit unrelated privileged changes or already-running jobs launched by unpatched clients.

Unprofiled course runs keep their existing network choice and participate in the Jetson lease on updated launchers. Prepare/approve invocations belonging to one verified transaction must inherit the same wrapper environment. Never use a new independent wrapper to approve a live run.

## Stage the installed variant

The installed runner includes extra legs, private runtime snapshots and supervision paths that differ from the public package. From this checkout run:

```bash
python3 split_sim/stage_transport.py /path/to/installed/jetson_sim /new/empty/staging-directory
```

The tool writes five reviewed shell scripts and the wrapper to a new directory. Review each against the installed original and deploy only between completed runs. Keep the installed renderer, mission and snapshots intact. The adapter rejects unknown source anchors. Source and fixtures are the scope of this change. A new end-to-end run on the physical DHCP link still needs measured evidence.

For local verification:

```bash
(cd split_sim && python3 -m unittest -v test_verified_transport test_prepare_split)
```
