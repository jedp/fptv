#!/usr/bin/env python3

"""
TVHeadend ATSC OTA scanner.

Goals:
 1) Find ATSC OTA network UUID by name
 2) (Optional) delete existing muxes for that network
 3) Create muxes for ATSC RF channels (2–36, or 14–36)
 4) Force scan all muxes
 5) Poll until scanning settles
 6) (Optional) trigger service mapping
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional, List, Callable

import requests
from requests.auth import HTTPDigestAuth

from log import Logger


def json_dumps(obj: object) -> str:
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False)


@dataclass
class ScanConfig:
    base_url: str = "http://localhost:9981"
    net_name: str = "ATSC OTA"
    user: str = ""
    password: str = ""
    # VHF = 2 .. 13; UHF = 14 .. 36
    rf_start: int = 2
    rf_end: int = 36
    wipe_existing_muxes: bool = True
    map_services_to_channels: bool = True
    delete_unnamed_channels: bool = True
    unnamed_channel_names: str = "{name-not-set}"  # Comma-separated to allow future variants
    modulation: str = "VSB/8"
    sleep_secs: float = 2.0
    timeout_secs: int = 600  # 10 minutes

    @classmethod
    def from_env(cls) -> ScanConfig:
        """Load configuration from environment variables."""
        return cls(
            base_url=os.getenv("BASE_URL", "http://localhost:9981"),
            net_name=os.getenv("NET_NAME", "ATSC OTA"),
            user=os.getenv("TVH_USER", ""),
            password=os.getenv("TVH_PASS", ""),
            rf_start=int(os.getenv("RF_START", "14")),
            rf_end=int(os.getenv("RF_END", "36")),
            wipe_existing_muxes=os.getenv("WIPE_EXISTING_MUXES", "0") == "1",
            map_services_to_channels=os.getenv("MAP_SERVICES_TO_CHANNELS", "1") == "1",
            delete_unnamed_channels=os.getenv("DELETE_UNNAMED_CHANNELS", "1") == "1",
            unnamed_channel_names=os.getenv("UNNAMED_CHANNEL_NAMES", "{name-not-set}"),
            modulation=os.getenv("MODULATION", "VSB/8"),
            sleep_secs=float(os.getenv("SLEEP_SECS", "2")),
            timeout_secs=int(os.getenv("TIMEOUT_SECS", "600")),
        )


@dataclass
class MuxStates:
    active: int = 0
    pending: int = 0
    ok: int = 0
    fail: int = 0
    idle: int = 0
    total: int = 0

    def is_settled(self) -> bool:
        """Returns True if no muxes are actively scanning."""
        return self.active + self.pending == 0

    def __str__(self) -> str:
        return f"ACTIVE: {self.active}, PENDING: {self.pending}, OK: {self.ok}, FAIL: {self.fail}, IDLE: {self.idle}, TOTAL: {self.total}"


class TVHeadendScanner:
    """Scanner for TVHeadend ATSC OTA channels."""

    def __init__(self, config: ScanConfig):
        self.log = Logger("tvh")
        self.config = config
        self.auth = None
        if config.user and config.password:
            self.auth = HTTPDigestAuth(config.user, config.password)

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Make an authenticated request to TVHeadend API."""
        url = f"{self.config.base_url}{endpoint}"
        kwargs.setdefault("auth", self.auth)
        response = requests.request(method, url, **kwargs)
        # Don't raise_for_status here - let callers handle errors
        return response

    def _get(self, endpoint: str, **kwargs) -> requests.Response:
        response = self._request("GET", endpoint, **kwargs)
        response.raise_for_status()
        return response

    def _post(self, endpoint: str, **kwargs) -> requests.Response:
        # POST requests often return non-200 codes that we handle gracefully
        return self._request("POST", endpoint, **kwargs)

    @staticmethod
    def rf_to_freq_hz(rf: int) -> int:
        """Convert ATSC RF channel number to frequency in Hz."""
        if rf >= 14:
            return 473000000 + (rf - 14) * 6000000
        elif rf >= 7:
            return 177000000 + (rf - 7) * 6000000
        else:
            freq_map = {
                2: 57000000,
                3: 63000000,
                4: 69000000,
                5: 79000000,
                6: 85000000,
            }
            if rf not in freq_map:
                raise ValueError(f"Invalid RF channel: {rf}")
            return freq_map[rf]

    @staticmethod
    def _service_param(service: dict, param_id: str) -> object | None:
        """
        Extract a value from service['params'] by id.
        """
        for p in service.get("params", []):
            if p.get("id") == param_id:
                return p.get("value")
        return None

    def _parse_major_minor(self, num: object) -> Optional[tuple[int, int]]:
        """
        Parse TVH channel 'number' which (in this build) is a string like '9.4'.
        Returns (major, minor) or None.
        """
        if num is None:
            return None
        if isinstance(num, (int, float)):
            # Be conservative; floats are risky. Convert to str.
            num = str(num)
        if not isinstance(num, str):
            self.log.err(f"Failed to stringify channel number: {num}")
            return None
        s = num.strip()
        if not s:
            return None
        if "." in s:
            a, b = s.split(".", 1)
            try:
                return int(a), int(b)
            except ValueError as e:
                self.log.err(f"Can't parse channel number from '{s}': {e}")
                return None
        # Sometimes it's just "9"
        try:
            return int(s), 0
        except ValueError as e:
            self.log.err(f"Invalid channel number: {num}: {e}")
            return None

    def _channel_score(self, ch: dict) -> tuple:
        """
        Higher is better. We return a tuple for sorting.
        Heuristic:
          - enabled channels first
          - has a parseable major/minor first
          - lower major preferred (we'll invert sign so it sorts higher)
          - more services preferred
        """
        enabled = bool(ch.get("enabled"))
        mm = self._parse_major_minor(ch.get("number"))
        has_mm = mm is not None
        major = mm[0] if mm else 9999
        services = ch.get("services") or []
        svc_count = len(services) if isinstance(services, list) else 0

        # Sort descending, so:
        # enabled True > False; has_mm True > False; lower major is better => use negative major
        return enabled, has_mm, -major, svc_count

    def get_network_uuid(self) -> Optional[str]:
        """Find network UUID by name."""
        response = self._get("/api/mpegts/network/grid?limit=9999")
        data = response.json()

        for entry in data.get("entries", []):
            name = entry.get("networkname") or entry.get("name")
            if name == self.config.net_name:
                return entry.get("uuid")

        return None

    def list_muxes_for_network(self, net_uuid: str) -> List[str]:
        """List all mux UUIDs for a given network."""
        response = self._get("/api/mpegts/mux/grid?limit=99999")
        data = response.json()

        muxes = []
        for entry in data.get("entries", []):
            network = entry.get("network_uuid") or entry.get("network")
            if network == net_uuid:
                muxes.append(entry.get("uuid"))

        return muxes

    def idnode_load(self, uuid: str) -> dict:
        """
        Utility for seeing how this build of tvheadend structures this data.
        """
        return self._get("/api/idnode/load", params={"uuid": uuid}).json()

    def idnode_save(self, node: dict) -> bool:
        # Adapt to different tvheadend builds.
        # Load the idnode and see what it expects for fields.
        if "class" not in node and "uuid" in node:
            try:
                loaded = self.idnode_load(str(node["uuid"]))
                ent = (loaded.get("entries") or [None])[0]
                if isinstance(ent, dict) and isinstance(ent.get("class"), str):
                    node["class"] = ent["class"]
            except Exception:
                pass

        payload = json_dumps(node)

        # Try common encodings across tvheadend builds.
        attempts = [
            {"node": payload},
            {"node[]": payload},
        ]

        for data in attempts:
            resp = self._post("/api/idnode/save", data=data)
            if resp.status_code == 200:
                self.log.out(f"idnode_save successfully saved data: {data}")
                return True

        # Last resort: legacy style (uuid + fields)
        self.log.out("idnode_save falling back to legacy style. Cross your fingers.")
        if "uuid" in node:
            legacy = {"uuid": str(node["uuid"])}
            for k, v in node.items():
                if k in ("uuid", "class"):
                    continue
                # lists should be JSON
                if isinstance(v, (list, dict)):
                    legacy[k] = json_dumps(v)
                else:
                    legacy[k] = str(v)
            resp = self._post("/api/idnode/save", data=legacy)
            if resp.status_code == 200:
                return True

        self.log.err(f"idnode_save failed: {resp.status_code} {resp.text} node={node}")
        return False

    def idnode_save_params(self, uuid: str, cls: Optional[str], changes: dict) -> bool:
        """
        Save using TVH's idnode 'params' format (matches idnode/load structure).
        `changes` is {param_id: value}.
        If cls is None, we auto-detect it via idnode/load.
        """
        uuid = str(uuid)

        # Auto-detect class if needed
        if not cls:
            try:
                loaded = self.idnode_load(uuid)
                ent = (loaded.get("entries") or [None])[0]
                if not isinstance(ent, dict) or not isinstance(ent.get("class"), str):
                    self.log.err(f"idnode_save_params: couldn't detect class for uuid={uuid}, loaded={loaded}")
                    return False
                cls = ent["class"]
            except Exception as e:
                self.log.err(f"idnode_save_params: idnode_load failed for uuid={uuid}: {e}")
                return False

        # Filter out None values (optional but avoids some TVH 400s)
        filtered = {k: v for k, v in changes.items() if v is not None}

        node = {
            "uuid": uuid,
            "class": cls,
            # Sort to keep a stable order.
            "params": [{"id": k, "value": filtered[k]} for k in sorted(filtered.keys())],
        }
        payload = json_dumps(node)

        last_resp = None
        for data in ({"node": payload}, {"node[]": payload}):
            try:
                resp = self._post("/api/idnode/save", data=data)
                last_resp = resp
                if resp.status_code == 200:
                    return True
            except requests.exceptions.RequestException as e:
                self.log.err(f"idnode_save_params request error: {e} node={node}")
                return False

        if last_resp is None:
            self.log.err(f"idnode_save_params failed: no response node={node}")
            return False

        self.log.err(
            f"idnode_save_params failed: {last_resp.status_code} {last_resp.text} "
            f"uuid={uuid} class={cls} changes={filtered}"
        )
        return False

    def delete_mux_uuid(self, mux_uuid: str) -> bool:
        """Delete a mux by UUID."""
        # Try idnode/delete first
        try:
            response = self._post("/api/idnode/delete", data={"uuid": mux_uuid})
            if response.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass

        # Fallback to mpegts/mux/delete
        try:
            self._post("/api/mpegts/mux/delete", data={"uuid": mux_uuid})
            return True
        except requests.exceptions.RequestException:
            return False

    def get_mux_class(self, net_uuid: str) -> dict:
        """
        Return the "props" list with ids + defaults for the mux type on that network.
        """
        return self._get("/api/mpegts/network/mux_class", params={"uuid": net_uuid}).json()

    def build_mux_conf_from_defaults(self, mux_class: dict) -> dict:
        """
        Build a conf dict containing defaults for fields TVH expects on mux_create.
        We only include fields that are savable and have defaults.
        """
        conf: dict = {}
        for p in mux_class.get("props", []) or []:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            if not isinstance(pid, str) or not pid:
                continue

            # Skip read-only / not-saveable fields
            if p.get("rdonly") or p.get("nosave"):
                continue

            if "default" in p:
                conf[pid] = p["default"]

        return conf

    def create_mux_atsc(self, net_uuid: str, freq_hz: int) -> bool:
        """
        Create a mux on an existing network using the correct API:
          POST /api/mpegts/network/mux_create
            - uuid=<network_uuid>
            - conf=<json>
        """
        try:
            mux_class = self.get_mux_class(net_uuid)
            conf = self.build_mux_conf_from_defaults(mux_class)

            # Set the frequency (this field should exist for terrestrial/atsc)
            conf["frequency"] = int(freq_hz)

            # If these fields exist for your mux type, set them.
            # They’re present on many builds, but not all.
            if "modulation" in conf:
                conf["modulation"] = self.config.modulation

            # Queue a scan if scan_state exists (1 == PEND in many builds)
            # Your mux_class output showed scan_state enum including PEND.
            if "scan_state" in conf:
                conf["scan_state"] = 1

            resp = self._post(
                "/api/mpegts/network/mux_create",
                data={"uuid": net_uuid, "conf": json_dumps(conf)},
            )

            if resp.status_code != 200:
                self.log.err(f"mux_create failed: {resp.status_code} {resp.text} conf={conf}")
                return False

            return True

        except requests.exceptions.RequestException as e:
            self.log.err(f"create_mux_atsc request failed: {e}")
            return False
        except ValueError as e:
            self.log.err(f"create_mux_atsc parse failed: {e}")
            return False

    def force_scan_mux(self, mux_uuid: str) -> bool:
        """
        Force scan a mux by UUID.
        """
        # Try mpegts/mux/scan first
        try:
            response = self._post("/api/mpegts/mux/scan", data={"uuid": mux_uuid})
            if response.status_code == 200:
                return True
        except requests.exceptions.RequestException as e:
            self.log.err(f"force_scan_mux failed for mux_uuid {mux_uuid}: {e}")
            pass

        # Fallback: set scan_state via idnode/save (node= JSON)
        if self.idnode_save({"uuid": mux_uuid, "scan_state": "PENDING"}):
            return True

        # Last fallback: numeric scan_state
        return self.idnode_save({"uuid": mux_uuid, "scan_state": "1"})

    def count_mux_states(self, net_uuid: str) -> MuxStates:
        """Count mux states for the network."""
        response = self._get("/api/mpegts/mux/grid?limit=99999")
        data = response.json()

        states = MuxStates()
        for entry in data.get("entries", []):
            network = entry.get("network_uuid") or entry.get("network")
            if network != net_uuid:
                continue

            states.total += 1
            scan_state = entry.get("scan_state") or entry.get("scan_status", "UNKNOWN")

            if scan_state == "ACTIVE":
                states.active += 1
            elif scan_state == "PENDING":
                states.pending += 1
            elif scan_state == "OK":
                states.ok += 1
            elif scan_state == "FAIL":
                states.fail += 1
            elif scan_state == "IDLE":
                states.idle += 1

        return states

    def list_services(self, limit: int = 99999) -> List[dict]:
        """
        List all services.

        Note that /api/service/list returns entries containing UUIDs and a name-like
        field, depending on the build. We use a few fallbacks.
        """
        try:
            resp = self._get(f"/api/service/list?limit={limit}")
            data = resp.json()
            return data.get("entries", [])
        except requests.exceptions.RequestException as e:
            self.log.err(f"list_services: {e}")
            return []
        except ValueError as e:
            self.log.err(f"list_services: {e}")
            return []

    def list_channels(self, limit: int = 99999) -> List[dict]:
        """
        List all channels in the admin view.
        """
        try:
            resp = self._get(f"/api/channel/grid?all=1&limit={limit}")
            data = resp.json()
            return data.get("entries", [])
        except requests.exceptions.RequestException as e:
            self.log.err(f"list_channels: {e}")
            return []
        except ValueError as e:
            self.log.err(f"list_channels: {e}")
            return []

    def create_channel(self, name: str, service_uuid: str) -> bool:
        """
        Create a channel and attach a service.

        Uses /api/channel/create which expects 'conf' JSON.
        """
        return self.create_channel_with_service(name, service_uuid) is not None

    def create_channel_with_service(self, name: str, service_uuid: str) -> Optional[str]:
        """
        Create a channel and attach a service.
        Returns created channel UUID if TVH returns it, else None.
        """
        conf = {
            "enabled": True,
            "name": name,
            # these two make TVH keep trying to use broadcast/EPG names
            "autoname": True,
            "epgauto": True,
            "services": [service_uuid],
        }
        resp = self._post("/api/channel/create", data={"conf": json_dumps(conf)})
        if resp.status_code != 200:
            self.log.err(f"create_channel_with_service response: {resp.status_code} {resp.text}")
            return None

        # Some builds return {"uuid": "..."}; others just {"success": 1} etc.
        try:
            payload = resp.json()
            if isinstance(payload, dict) and isinstance(payload.get("uuid"), str):
                self.log.out(f"create_channel_with_service: created channel '{name}' -> {payload['uuid']}")
                return payload["uuid"]
        except ValueError as e:
            self.log.err(f"create_channel_with_service: {e}")
            pass

        self.log.out("create_channel_with_service: no UUID returned.")
        return None

    def save_channel_fields(self, chan_uuid: str, *, name: Optional[str] = None, number: Optional[int] = None) -> bool:
        """
        Update channel fields via idnode/save.
        """
        node = {"uuid": chan_uuid}
        if name is not None:
            node["name"] = name
        if number is not None:
            node["number"] = str(number)

        return self.idnode_save(node)

    def delete_channel_uuid(self, chan_uuid: str) -> bool:
        """
        Delete a channel by UUID.
        """
        self.log.out(f"Deleting channel {chan_uuid}...")
        try:
            resp = self._post("/api/idnode/delete", data={"uuid": chan_uuid})
            return resp.status_code == 200
        except requests.exceptions.RequestException as e:
            self.log.err(f"delete_channel_uuid: {e}")
            return False

    def ensure_channels_mapped_from_services(self) -> tuple[int, int]:
        """
        Create channels for services that are not attached to any channel.

        Returns:
            (created_count, already_mapped_count)
        """
        services = self.list_services()
        channels = self.list_channels()

        # Collect a set of service UUIDs already referenced by channels.
        mapped_services: set[str] = set()
        for ch in channels:
            for service in (ch.get("services") or []):
                if isinstance(service, str):
                    mapped_services.add(service)

        created = 0
        skipped = 0

        for service in services:
            service_uuid = service.get("uuid") or service.get("id")
            if not service_uuid or not isinstance(service_uuid, str):
                continue

            if service_uuid in mapped_services:
                skipped += 1
                continue

            # Try common name fields across tvheadend builds.
            name = self._service_param(service, "svcname") or service.get("name") or service.get("text")

            if not name or not isinstance(name, str):
                # If TVH didn't provide a name yet, don't create a junk channel;
                # it can appear later after tuning/PSIP.
                self.log.out(f"Skipping service without name: {service}")
                continue

            name = name.strip()

            if self.create_channel(name=name, service_uuid=service_uuid):
                created += 1

        return created, skipped

    def cleanup_unnamed_channels(self) -> int:
        """
        Delete channels whose name is blank or matches configured unnamed marker(s).

        Returns number deleted.
        """
        self.log.out("Cleaning up unnamed channels...")
        unnamed_markers = {
            s.strip() for s in (self.config.unnamed_channel_names or "").split(",") if s.strip()
        }
        unnamed_markers.add("")  # always treat blank as unnamed

        deleted = 0
        for ch in self.list_channels():
            name = (ch.get("name") or "")
            if not isinstance(name, str):
                name = ""
            if name.strip() in unnamed_markers:
                uuid = ch.get("uuid")
                if uuid and isinstance(uuid, str):
                    if self.delete_channel_uuid(uuid):
                        deleted += 1
        return deleted

    def deduplicate_channels_by_name_prefer_low_major(self) -> dict:
        """
        Deduplicate channels that share the same name by keeping the lowest major number (e.g. 9.x)
        and merging services onto it.

        Safe for appliance-managed channels: you can optionally restrict to autoname+epgauto.
        """
        channels = self.list_channels()

        # Group channels by name (ignore blanks and name-not-set)
        groups: dict[str, list[dict]] = {}
        for ch in channels:
            name = ch.get("name")
            if not isinstance(name, str):
                self.log.out(f"Skipping channel {ch['uuid']}. Name is not a string.")
                continue
            name = name.strip()
            if not name or name == "{name-not-set}":
                self.log.out(f"Skipping channel {ch['uuid']}. Name is '{name}'.")
                continue

            # Safety check.
            if ch.get("autoname") is not True or ch.get("epgauto") is not True:
                continue

            groups.setdefault(name, []).append(ch)

        merged_groups = 0
        updated_channels = 0
        deleted_channels = 0

        for name, chans in groups.items():
            if len(chans) < 2:
                self.log.out(f"Only one channel in group '{name}'. Nothing to de-duplicate.")
                continue

            # Choose canonical: lowest major, then lowest minor, then keep enabled, then most services
            def score(_ch: dict):
                mm = self._parse_major_minor(_ch.get("number"))
                major, minor = mm if mm else (9999, 9999)
                enabled = bool(_ch.get("enabled"))
                svc_count = len(_ch.get("services") or [])
                # lower major/minor preferred => sort ascending for those, so use them directly
                # enabled preferred => sort descending, so negate enabled
                return major, minor, -int(enabled), -svc_count

            chans_sorted = sorted(chans, key=score)
            canonical = chans_sorted[0]
            canon_uuid = canonical.get("uuid")
            if not isinstance(canon_uuid, str) or not canon_uuid:
                continue

            # Merge all services into canonical
            merged_services: list[str] = []
            seen: set[str] = set()

            for ch in chans_sorted:
                for su in (ch.get("services") or []):
                    if isinstance(su, str) and su and su not in seen:
                        seen.add(su)
                        merged_services.append(su)

            # Save canonical with merged services list
            ok = self.idnode_save_params(
                uuid=canon_uuid,
                cls="channel",  # We know this is correct for this function.
                changes={"services": merged_services}
            )
            if not ok:
                continue

            updated_channels += 1

            # Delete all non-canonical channels
            for ch in chans_sorted[1:]:
                uuid = ch.get("uuid")
                if isinstance(uuid, str) and uuid:
                    if self.delete_channel_uuid(uuid):
                        deleted_channels += 1

            merged_groups += 1

        self.log.out(f"dedupe: merged {merged_groups} groups, " +
                     f"updated {updated_channels} canonical channels, " +
                     f"deleted {deleted_channels} non-canonical channels.")

        return {
            "merged_groups": merged_groups,
            "updated_channels": updated_channels,
            "deleted_channels": deleted_channels,
        }

    def scan(self, progress_callback: Optional[Callable[[str, MuxStates], None]] = None) -> bool:
        """
        Perform a full ATSC OTA scan.

        Args:
            progress_callback: Optional callback function(message: str, states: MuxStates)
                              called periodically with progress updates.

        Returns:
            True if scan completed successfully, False otherwise.
        """

        # Log helper to log progress as well.
        def log(msg: str, states: Optional[MuxStates] = None):
            if progress_callback:
                progress_callback(msg, states or MuxStates())
            else:
                self.log.out(msg)

        # Step 1: Find network
        log(f"Finding network UUID for: {self.config.net_name}")
        net_uuid = self.get_network_uuid()
        if not net_uuid:
            log(f"Network not found: {self.config.net_name}")
            return False
        log(f"Network UUID: {net_uuid}")

        # Step 2: Optionally wipe existing muxes
        if self.config.wipe_existing_muxes:
            log("Wiping existing muxes for network...")
            muxes = self.list_muxes_for_network(net_uuid)
            for mux_uuid in muxes:
                self.delete_mux_uuid(mux_uuid)
            log(f"Deleted {len(muxes)} muxes.")

        # Step 3: Create muxes
        log(f"Creating ATSC muxes RF {self.config.rf_start}..{self.config.rf_end} (modulation={self.config.modulation})...")
        created_count = 0
        for rf in range(self.config.rf_start, self.config.rf_end + 1):
            try:
                freq = self.rf_to_freq_hz(rf)
                log(f"  RF {rf} -> {freq} Hz")
                if self.create_mux_atsc(net_uuid, freq):
                    created_count += 1
            except ValueError:
                continue
        log(f"Should have created {created_count} muxes.")
        response = self._get("/api/mpegts/mux/grid?limit=99999")
        if response.status_code != 200:
            self.log.err(f"Failed to fetch mux grid: {response.status_code}: {response.text}")
        else:
            muxes = response.json().get('entries', [])
            self.log.out(f"There are now {len(muxes)} muxes in the network: {muxes}")

        # Step 4: Force scan all muxes
        log("Forcing scan on all muxes in network...")
        muxes = self.list_muxes_for_network(net_uuid)
        for mux_uuid in muxes:
            self.force_scan_mux(mux_uuid)
        log(f"Requested scan for {len(muxes)} muxes.")

        # Step 5: Poll until settled
        log(f"Polling scan progress (timeout {self.config.timeout_secs}s)...")
        start_time = time.time()
        while True:
            elapsed = time.time() - start_time
            if elapsed > self.config.timeout_secs:
                log("Timed out waiting for scan to settle.")
                states = self.count_mux_states(net_uuid)
                log(str(states), states)
                return False

            states = self.count_mux_states(net_uuid)
            log(str(states), states)

            if states.is_settled():
                break

            time.sleep(self.config.sleep_secs)

        # Step 6: Start service mapping
        log("Scan settled.")
        if self.config.map_services_to_channels:
            log("Mapping services to channels (deterministic)...")
            created, skipped = self.ensure_channels_mapped_from_services()
            log(f"Service → channel reconciliation: created={created}, already_mapped={skipped}")

        if self.config.delete_unnamed_channels:
            log("Cleaning up unnamed channels...")
            deleted = self.cleanup_unnamed_channels()
            log(f"Deleted {deleted} unnamed channels.")

        log("Deduplicating channels (prefer low major, merge services, delete dup channels)...")
        dedup_stats = self.deduplicate_channels_by_name_prefer_low_major()
        log(f"Dedup stats: {dedup_stats}")

        log("Done with scan.")

        return True


def main():
    """Command-line interface for the scanner."""
    import sys

    config = ScanConfig.from_env()
    scanner = TVHeadendScanner(config)

    try:
        success = scanner.scan()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\nScan interrupted by user.")
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
