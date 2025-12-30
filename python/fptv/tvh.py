#!/usr/bin/env python3

"""
TVHeadend ATSC OTA scanner.

Ownership model:
 - No human users
 - No WebUI edits (except debugging)
 - Appliance owns full lifecycle
 - Destructive cleanup is acceptable

Tuner usage rules:
 - No initial EPG grabs
 - No periodic OTA scans during normal operation
 - Live streaming has absolute priority

Scan Pipeline:
 - Ensure ATSC-T frontends enabled + network-linked
 - Wipe muxes (optional)
 - Create muxes deterministically
 - Force scan
 - Wait until settled
 - Delete orphan channels
 - Map services → channels
 - Delete unnamed channels
 - Deduplicate channels by name
 - Prune invalid services per channel ← final safety net

TODO
 - tightening sleeps vs throughput
 - optional service-grid hygiene
 - maybe locking channel numbers if you want absolute determinism across rescans
 - tighten up callback logic for benefit of UX
 - Add channel numbers to data class
 - Add program in progress to listing? (Requires EPG data, which we're fighting with.)
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, List, Set, Any

import requests
from requests.auth import HTTPDigestAuth

from fptv.log import Logger
from fptv.mpv import MPV_USERAGENT


def json_dumps(obj: object) -> str:
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False)


@dataclass(frozen=True)
class Channel:
    name: str
    url: str


@dataclass
class ScanConfig:
    base_url: str = "http://127.0.0.1:9981"
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
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> ScanConfig:
        """Load configuration from environment variables."""
        return cls(
            base_url=os.getenv("BASE_URL", "http://127.0.0.1:9981"),
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
            dry_run=os.getenv("DRY_RUN", "0") == "1",
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
        return (self.active + self.pending) == 0

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
        """
        Make an authenticated request to TVHeadend API with sane defaults + simple retry.
        """
        url = f"{self.config.base_url}{endpoint}"
        kwargs.setdefault("auth", self.auth)
        kwargs.setdefault("timeout", 10)

        # Light retry for transient 5xx / connection hiccups.
        attempts = 3
        resp = None
        for i in range(attempts):
            try:
                resp = requests.request(method, url, **kwargs)
            except requests.RequestException:
                if i == attempts - 1:
                    raise
                time.sleep(0.2 * (2 ** i) + random.random() * 0.1)
                continue

            if resp.status_code >= 500 and i < attempts - 1:
                time.sleep(0.2 * (2 ** i) + random.random() * 0.1)
                continue

            return resp

        return resp  # pragma: no cover

    def _get(self, endpoint: str, **kwargs) -> requests.Response:
        response = self._request("GET", endpoint, **kwargs)
        response.raise_for_status()
        return response

    def _get_json(self, endpoint: str, **kwargs) -> dict:
        r = self._get(endpoint, **kwargs)
        try:
            return r.json()
        except ValueError:
            self.log.err(f"Non-JSON response for {endpoint}: {r.text[:300]}")
            raise

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

    @staticmethod
    def _channel_stream_id(ch: dict) -> Optional[str]:
        """
        Extract numeric channelid used by /stream/channelid/<id>.
        Tries common field names.
        """
        for k in ("chid", "channelid", "id"):
            v = ch.get(k)
            if isinstance(v, int):
                return str(v)
            if isinstance(v, str) and v.isdigit():
                return v
        return None

    def _enum_key_for_label(self, prop: dict, label_substr: str) -> Optional[object]:
        for e in (prop.get("enum") or []):
            if not isinstance(e, dict):
                continue
            if label_substr.lower() in str(e.get("val", "")).lower():
                return e.get("key")

        self.log.out(f"Did not find enum key for label: {label_substr}")
        return None

    def _find_prop(self, mux_class: dict, *, id_contains=(), caption_contains=()) -> Optional[dict]:
        for p in (mux_class.get("props") or []):
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            cap = p.get("caption") or ""
            if not isinstance(pid, str):
                continue
            low_id = pid.lower()
            low_cap = str(cap).lower()

            if id_contains and not any(s in low_id for s in id_contains):
                continue
            if caption_contains and not any(s in low_cap for s in caption_contains):
                continue
            self.log.out(f"Found prop id: {pid}")
            return p

        self.log.out(f"Did not find prop for mux_class: {mux_class}")
        return None

    def _iter_hw_tree(self, start_uuid: str = "root") -> Iterable[dict]:
        """
        Depth-first walk of /api/hardware/tree starting from start_uuid.
        Yields every node dict returned by the API.
        """
        stack = [start_uuid]
        seen: Set[str] = set()

        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)

            resp = self._get("/api/hardware/tree", params={"uuid": u})
            try:
                nodes = resp.json()
            except ValueError:
                self.log.err(f"hardware/tree returned non-JSON for uuid={u}: {resp.text[:200]}")
                continue

            if not isinstance(nodes, list):
                self.log.err(f"hardware/tree unexpected shape for uuid={u}: {nodes}")
                continue

            for n in nodes:
                if not isinstance(n, dict):
                    continue
                yield n
                # If leaf == 0, it has children; per TVH tree API, use node uuid as next uuid.
                nu = n.get("uuid") or n.get("id")
                if n.get("leaf") == 0 and isinstance(nu, str) and nu:
                    stack.append(nu)

    @staticmethod
    def _is_atsc_t_frontend_node(node: dict) -> bool:
        """
        Heuristic: match LinuxDVB ATSC terrestrial frontends.
        Your nodes look like class 'linuxdvb_frontend_atsc_t' and event 'mpegts_input'.
        """
        cls = node.get("class", "")
        txt = node.get("text", "")
        if not isinstance(cls, str):
            cls = ""
        if not isinstance(txt, str):
            txt = ""

        c = cls.lower()
        t = txt.lower()
        return ("linuxdvb_frontend_atsc_t" in c) or ("atsc-t" in t) or ("atsc t" in t) or ("atsc_t" in t)

    def _maybe_save_idnode_params(self, uuid: str, cls: Optional[str], changes: dict) -> bool:
        if self.config.dry_run:
            self.log.out(f"[dry-run] Would idnode/save uuid={uuid} class={cls} changes={changes}")
            return True
        return self.idnode_save_params(uuid=uuid, cls=cls, changes=changes)

    def _idnode_load_entry(self, uuid: str) -> Optional[dict]:
        loaded = self._get("/api/idnode/load", params={"uuid": uuid}).json()
        ent = (loaded.get("entries") or [None])[0]
        return ent if isinstance(ent, dict) else None

    def _idnode_params_to_map(self, entry: dict) -> dict:
        """
        Convert idnode/load entry params array into {id: value}.
        """
        out = {}
        for p in (entry.get("params") or []):
            if isinstance(p, dict) and isinstance(p.get("id"), str):
                out[p["id"]] = p.get("value")
        return out

    def _service_name_from_idnode(self, service_uuid: str) -> Optional[str]:
        ent = self._idnode_load_entry(service_uuid)
        if not ent:
            return None
        params = self._idnode_params_to_map(ent)
        v = params.get("svcname")
        if isinstance(v, str) and v.strip():
            return v.strip()
        return None

    @staticmethod
    def _mux_is_ok(mux_info: dict) -> bool:
        """
        scan_result is int on your build: 1=OK, 2=FAIL, 0=NONE/unknown.
        Treat enabled + OK as "good".
        """
        if not mux_info.get("enabled", True):
            return False
        r = mux_info.get("scan_result")
        if isinstance(r, int):
            return r == 1
        if isinstance(r, str):
            return r.upper() == "OK"
        return False

    def subscriptions(self) -> dict[str, Any]:
        return self._get_json("status/subscriptions")

    def connections(self) -> dict[str, Any]:
        return self._get_json("status/connections")

    def cancel_connections(self, ids: list[int] | str) -> dict[str, Any]:
        # docs: if id='all' then all connections cancelled :contentReference[oaicite:2]{index=2}
        return self._get_json("connections/cancel", id=ids)

    def debug_channel_service_mux_health(self, net_uuid: str) -> dict:
        mux_index = self.get_mux_index(net_uuid=net_uuid)

        # service_uuid -> mux_uuid (from idnode/load)
        svc_to_mux: dict[str, str] = {}

        def svc_mux_uuid(su: str) -> str | None:
            ent = self._idnode_load_entry(su)
            if not ent:
                return None
            params = self._idnode_params_to_map(ent)
            v = params.get("multiplex_uuid") or params.get("mux_uuid")
            return v if isinstance(v, str) and v else None

        counts = {
            "channels": 0,
            "services": 0,
            "mux_missing": 0,
            "mux_unknown": 0,
            "by_scan_result": {},  # e.g. {1: 51, 2: 0}
            "by_enabled": {},  # e.g. {True: 51, False: 0}
        }

        for ch in self.get_channel_grid():
            counts["channels"] += 1
            services = ch.get("services") or []
            if not isinstance(services, list):
                continue
            for su in services:
                if not isinstance(su, str) or not su:
                    continue
                counts["services"] += 1
                mux_uuid = svc_mux_uuid(su)
                if not mux_uuid:
                    counts["mux_missing"] += 1
                    continue
                svc_to_mux[su] = mux_uuid
                mux = mux_index.get(mux_uuid)
                if not mux:
                    counts["mux_unknown"] += 1
                    continue

                sr = mux.get("scan_result")
                en = bool(mux.get("enabled", True))
                counts["by_scan_result"][sr] = counts["by_scan_result"].get(sr, 0) + 1
                counts["by_enabled"][en] = counts["by_enabled"].get(en, 0) + 1

        self.log.out(f"debug_channel_service_mux_health: {counts}")
        return counts

    def get_mux_health_for_network(self, net_uuid: str) -> dict[str, dict]:
        """
        Robust: accept network_uuid OR (when only network name is present) match by your config.net_name.
        """
        data = self._get_json("/api/mpegts/mux/grid?limit=99999")
        out: dict[str, dict] = {}

        for e in data.get("entries", []):
            if not isinstance(e, dict):
                continue

            nu = e.get("network_uuid") or e.get("network")
            if nu not in (net_uuid, self.config.net_name):
                continue

            mux_uuid = e.get("uuid")
            if not isinstance(mux_uuid, str) or not mux_uuid:
                continue

            out[mux_uuid] = {
                "enabled": bool(e.get("enabled", True)),
                "scan_result": e.get("scan_result"),
                "scan_state": e.get("scan_state"),
            }

        return out

    def get_good_muxes(self, net_uuid: str) -> Set[str]:
        mux_health = self.get_mux_health_for_network(net_uuid)
        return {m for (m, info) in mux_health.items() if self._mux_is_ok(info)}

    def disable_failed_muxes(self, net_uuid: str) -> dict:
        """
        Disable muxes that are enabled but scan_result=FAIL.
        Keeps them around (so you can re-enable later), but stops TVH from retuning to garbage.
        """
        resp = self._get("/api/mpegts/mux/grid?limit=99999")
        data = resp.json()

        disabled = 0
        considered = 0
        errors = 0

        for e in data.get("entries", []):
            # Be robust across builds: sometimes `network_uuid`, sometimes `network` name
            net = e.get("network_uuid") or e.get("network")
            if net != net_uuid and net != self.config.net_name:
                continue

            mux_uuid = e.get("uuid")
            if not isinstance(mux_uuid, str) or not mux_uuid:
                continue

            enabled = bool(e.get("enabled", True))
            scan_result = e.get("scan_result")

            considered += 1

            # Your build: 1=OK, 2=FAIL
            is_fail = (scan_result == 2) or (isinstance(scan_result, str) and scan_result.upper() == "FAIL")
            if enabled and is_fail:
                ent = self._idnode_load_entry(mux_uuid)
                cls = ent.get("class") if ent else None
                ok = self._maybe_save_idnode_params(mux_uuid, cls, {"enabled": False})
                if ok:
                    disabled += 1
                else:
                    errors += 1

        return {"considered": considered, "disabled": disabled, "errors": errors}

    def ensure_atsc_t_frontends_enabled_and_linked(self, net_uuid: str) -> dict:
        """
        Enable all ATSC-T frontends and ensure they're assigned to the given network UUID.

        Returns stats dict.
        """
        found = 0
        updated = 0
        enabled_count = 0
        linked_count = 0
        errors = 0

        for node in self._iter_hw_tree("root"):
            nu = node.get("uuid") or node.get("id")
            if not isinstance(nu, str) or not nu:
                continue

            if not self._is_atsc_t_frontend_node(node):
                continue

            found += 1
            entry = self._idnode_load_entry(nu)
            if not entry:
                self.log.err(f"Could not idnode/load frontend uuid={nu} text={node.get('text')}")
                errors += 1
                continue

            params_map = self._idnode_params_to_map(entry)

            # Determine current state
            cur_enabled = bool(params_map.get("enabled"))
            cur_networks = params_map.get("networks") or []
            if not isinstance(cur_networks, list):
                cur_networks = []

            new_networks = list(cur_networks)
            if net_uuid not in new_networks:
                new_networks.append(net_uuid)

            changes = {}
            if not cur_enabled:
                changes["enabled"] = True
            if new_networks != cur_networks:
                changes["networks"] = new_networks

            if not changes:
                # Already configured
                enabled_count += int(cur_enabled)
                linked_count += int(net_uuid in cur_networks)
                continue

            # Dry-run safe
            ok = self._maybe_save_idnode_params(uuid=nu, cls=entry.get("class"), changes=changes)
            if ok:
                updated += 1
                # Count post-change intended state
                enabled_count += 1
                linked_count += 1
                self.log.out(f"Configured frontend: {node.get('text')} uuid={nu} changes={changes}")
            else:
                errors += 1
                self.log.err(f"Failed to configure frontend uuid={nu} text={node.get('text')} changes={changes}")

        return {
            "frontends_found": found,
            "frontends_updated": updated,
            "frontends_enabled": enabled_count,
            "frontends_linked_to_net": linked_count,
            "errors": errors,
        }

    def get_network_uuid(self) -> Optional[str]:
        """Find network UUID by name."""
        response = self._get("/api/mpegts/network/grid?limit=9999")
        data = response.json()

        for entry in data.get("entries", []):
            name = entry.get("networkname") or entry.get("name")
            if name == self.config.net_name:
                return entry.get("uuid")

        return None

    def set_epg_grabbers_enabled(self, enabled: bool) -> bool:
        """
        On this build, /api/epggrab/config/save expects form field:
          node=<JSON object of config fields>
        Not an idnode params array.

        enabled=False disables startup grabs + clears cron schedules (appliance-safe).
        """
        loaded = self._get_json("/api/epggrab/config/load")
        entry = (loaded.get("entries") or [None])[0]
        if not isinstance(entry, dict):
            self.log.err(f"epggrab/config/load unexpected: {loaded}")
            return False

        # Build node from current values, preserving everything TVH knows about.
        node: dict = {}
        for p in (entry.get("params") or []):
            if isinstance(p, dict) and isinstance(p.get("id"), str):
                node[p["id"]] = p.get("value")

        node["int_initial"] = bool(enabled)
        node["ota_initial"] = bool(enabled)

        if not enabled:
            node["cron"] = ""
            node["ota_cron"] = ""

        if self.config.dry_run:
            self.log.out(f"[dry-run] Would epggrab/config/save node={node}")
            return True

        resp = self._post("/api/epggrab/config/save", data={"node": json_dumps(node)})
        if resp.status_code == 200:
            return True

        self.log.err(f"epggrab/config/save failed: {resp.status_code} {resp.text}")
        return False

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

        else:
            self.log.err(f"No uuid found in node. Cannot save.")

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

    def get_mux_index(self, *, net_uuid: str) -> dict[str, dict]:
        """
        Returns mux_uuid -> info for muxes in this network.
        Matches by either network_uuid==net_uuid OR network name==self.config.net_name
        to handle builds that only return one or the other.
        """
        resp = self._get("/api/mpegts/mux/grid?limit=99999")
        data = resp.json()

        out: dict[str, dict] = {}
        for e in (data.get("entries") or []):
            if not isinstance(e, dict):
                continue

            net = e.get("network_uuid") or e.get("network")
            if net not in (net_uuid, self.config.net_name):
                continue

            mux_uuid = e.get("uuid")
            if not isinstance(mux_uuid, str) or not mux_uuid:
                continue

            out[mux_uuid] = {
                "enabled": bool(e.get("enabled", True)),
                "scan_result": e.get("scan_result"),
                "scan_state": e.get("scan_state"),
                "network_uuid": e.get("network_uuid"),
                "network": e.get("network"),
                "frequency": e.get("frequency"),
            }

        return out

    def get_service_mux_uuid(self, service_uuid: str) -> Optional[str]:
        # First try service/list cached map if you want; but authoritative is idnode/load:
        ent = self._idnode_load_entry(service_uuid)
        if not ent:
            return None
        params = self._idnode_params_to_map(ent)
        v = params.get("multiplex_uuid") or params.get("mux_uuid")
        return v if isinstance(v, str) and v else None

    def service_is_acceptable(self, service_uuid: str, *, net_uuid: str, mux_index: dict[str, dict]) -> tuple[
        bool, str]:
        """
        Returns (ok, reason_key_if_not_ok).
        reason_key must match keys in prune stats["reasons"].
        """

        # Find mux_uuid for this service
        mux_uuid = None

        # Fast path: sometimes list_services has multiplex_uuid
        # (If you already have a map cached, use that; this is direct + safe.)
        ent = self._idnode_load_entry(service_uuid)
        if ent:
            params = self._idnode_params_to_map(ent)
            mux_uuid = params.get("multiplex_uuid") or params.get("mux_uuid")

            # Optional: check service network too (depends on build)
            svc_net = params.get("network_uuid") or params.get("network")
            if svc_net is not None and svc_net not in (net_uuid, self.config.net_name):
                return False, "removed_wrong_network"

        if not isinstance(mux_uuid, str) or not mux_uuid:
            return False, "removed_service_missing_mux"

        mux = mux_index.get(mux_uuid)
        if not mux:
            # This is where your earlier bug would show up as "everything ok"
            # if you were treating unknown as acceptable.
            return False, "removed_unknown_mux"

        if not mux.get("enabled", True):
            return False, "removed_mux_disabled"

        r = mux.get("scan_result")
        # Your build: 1=OK, 2=FAIL, 0=NONE
        if isinstance(r, int):
            if r != 1:
                return False, "removed_mux_bad_scan"
        elif isinstance(r, str):
            if r.upper() != "OK":
                return False, "removed_mux_bad_scan"
        else:
            return False, "removed_mux_bad_scan"

        return True, "ok"

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
        Never set onid, tsid, name, pmt_06_ac3, etc., at creation unless you know you must.
        Otherwise you may bake in bogus values, like 65536.
        """
        try:
            mux_class = self.get_mux_class(net_uuid)

            conf = {
                "enabled": 1,
                "frequency": int(freq_hz),
                "modulation": "VSB/8"
            }

            # modulation (only if this mux class supports it)
            # (some builds use "modulation", some do not)
            # We'll set it if it exists in the class props.
            prop_ids = {p.get("id") for p in (mux_class.get("props") or []) if isinstance(p, dict)}
            if "modulation" in prop_ids:
                conf["modulation"] = self.config.modulation

            # Disable mux EPG scan if supported
            # epg_prop = self._find_prop(mux_class, id_contains=("epg",), caption_contains=("epg", "scan"))
            # if epg_prop:
            #     key = self._enum_key_for_label(epg_prop, "disable")
            #     conf[epg_prop["id"]] = 0 if key is None else key

            # Queue scan if supported
            if "scan_state" in prop_ids:
                conf["scan_state"] = 1

            resp = self._post(
                "/api/mpegts/network/mux_create",
                data={"uuid": net_uuid, "conf": json_dumps(conf)},
            )
            if resp.status_code != 200:
                self.log.err(f"mux_create failed: {resp.status_code} {resp.text} conf={conf}")
                return False
            return True

        except Exception as e:
            self.log.err(f"create_mux_atsc failed: {e}")
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

        return self.idnode_save({"uuid": mux_uuid, "scan_state": "1"})

    def count_mux_states(self, net_uuid: str) -> MuxStates:
        resp = self._get("/api/mpegts/mux/grid?limit=99999")
        data = resp.json()

        states = MuxStates()

        for e in data.get("entries", []):
            network = e.get("network_uuid") or e.get("network")
            if network != net_uuid:
                continue

            states.total += 1

            scan_state = e.get("scan_state")
            scan_result = e.get("scan_result")

            # --- scan_state ---
            # Your build uses ints (we saw 1). Common TVH mapping:
            # 0=IDLE, 1=PEND, 2=ACTIVE (sometimes 3=...) — we treat nonzero as "in progress".
            if isinstance(scan_state, int):
                if scan_state == 0:
                    states.idle += 1
                elif scan_state == 1:
                    states.pending += 1
                else:
                    states.active += 1
            else:
                # string fallback
                if scan_state == "ACTIVE":
                    states.active += 1
                elif scan_state == "PENDING":
                    states.pending += 1
                elif scan_state == "IDLE":
                    states.idle += 1

            # --- scan_result ---
            # Your build also has scan_result int (we saw 0).
            # Treat 0 as NONE/unknown, 1 as OK, 2 as FAIL (common pattern).
            if isinstance(scan_result, int):
                if scan_result == 1:
                    states.ok += 1
                elif scan_result == 2:
                    states.fail += 1
            else:
                # string fallback
                if scan_result == "OK":
                    states.ok += 1
                elif scan_result == "FAIL":
                    states.fail += 1

        return states

    def get_mpegts_service_grid(self, limit: int = 99999) -> List[dict]:
        try:
            data = self._get_json(f"/api/mpegts/service/grid?limit={limit}")
            entries = data.get("entries") or []
            return entries if isinstance(entries, list) else []
        except Exception as e:
            self.log.err(f"get_mpegts_service_grid: {e}")
            return []

    def build_service_index(self) -> dict[str, dict]:
        """
        Returns {service_uuid: service_entry} from mpegts/service/grid.
        """
        out: dict[str, dict] = {}
        for s in self.get_mpegts_service_grid():
            su = s.get("uuid") or s.get("id")
            if isinstance(su, str) and su:
                out[su] = s
        return out

    def get_service_to_mux_map(self) -> dict[str, str]:
        """
        Prefer mpegts/service/grid because it reliably includes multiplex_uuid in your build.
        """
        out: dict[str, str] = {}
        for s in self.get_mpegts_service_grid():
            su = s.get("uuid") or s.get("id")
            if not isinstance(su, str) or not su:
                continue
            mux_uuid = s.get("multiplex_uuid") or s.get("mux_uuid")
            if isinstance(mux_uuid, str) and mux_uuid:
                out[su] = mux_uuid
        return out

    def get_service_best_name(self, service_entry: dict) -> Optional[str]:
        """
        Improve: in your build, mpegts/service/grid has 'svcname' directly.
        Fall back to idnode/load only if needed.
        """
        v = service_entry.get("svcname")
        if isinstance(v, str) and v.strip():
            return v.strip()

        v = service_entry.get("name")
        if isinstance(v, str) and v.strip():
            s = v.strip()
            if "/" in s:
                tail = s.split("/")[-1].strip()
                if tail:
                    return tail
            return s

        su = service_entry.get("uuid") or service_entry.get("id")
        if isinstance(su, str) and su:
            return self._service_name_from_idnode(su)

        return None

    def get_service_name(self, service_entry: dict) -> Optional[str]:
        # First try lightweight list fields
        v = self._service_param(service_entry, "svcname")
        if isinstance(v, str) and v.strip():
            return v.strip()

        # Some builds put something usable in "name"
        v = service_entry.get("name")
        if isinstance(v, str) and v.strip():
            # Often "ATSC OTA/569MHz/KQED-HD" — take the tail
            s = v.strip()
            if "/" in s:
                s2 = s.split("/")[-1].strip()
                if s2:
                    return s2
            return s

        # Fall back to authoritative idnode/load
        su = service_entry.get("uuid") or service_entry.get("id")
        if not isinstance(su, str) or not su:
            return None

        ent = self._idnode_load_entry(su)
        if not ent:
            return None

        params_map = self._idnode_params_to_map(ent)
        v = params_map.get("svcname")
        if isinstance(v, str) and v.strip():
            return v.strip()

        # last resort
        v = params_map.get("name")
        if isinstance(v, str) and v.strip():
            return v.strip()

        return None

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

    def delete_orphan_channels(self) -> int:
        deleted = 0
        for ch in self.get_channel_grid():
            services = ch.get("services") or []
            if not isinstance(services, list):
                services = []
            if len(services) == 0:
                uuid = ch.get("uuid")
                name = ch.get("name") or ""
                if isinstance(uuid, str) and uuid:
                    if self.config.dry_run:
                        self.log.out(f"[dry-run] Would delete orphan channel: {name} uuid={uuid}")
                        deleted += 1
                    else:
                        self.log.out(f"Deleting orphan channel: {name} uuid={uuid}")
                        if self.delete_channel_uuid(uuid):
                            deleted += 1
        return deleted

    def get_playlist_channels(self) -> List[Channel]:
        """
        tvheadend's /playlist/channels returns an m3u file.
        Lines look like:

            #EXTINF:-1 tvg-id="26e30b9fb6fb20429aac61784fb50ed4" tvg-chno="9.1",KQED-HD
            http://localhost:9981/stream/channelid/520872742?profile=pass
        """
        try:
            resp = self._get("/playlist/channels")
        except requests.exceptions.RequestException as e:
            self.log.err(f"get_playlist_channels: {e}")
            return []
        except ValueError as e:
            self.log.err(f"get_playlist_channels: {e}")
            return []

        channels = []
        name = None

        for line in resp.text.splitlines():
            print(f"Processing /playlist/channels line {line}")

            if not line.strip():
                continue

            if line.startswith('#EXTM3U'):
                continue

            elif line.startswith('#EXTINF'):
                name = line.strip().split(',')[-1].strip()

            elif line.startswith('http://'):
                if name is None:
                    raise ValueError(f"No name found before url: {line}")
                channels.append(Channel(name, line))
                name = None

            else:
                raise ValueError(f"Unexpected m3u line: {line}")

        return channels

    def get_channel_grid(self, limit: int = 99999) -> List[dict]:
        """
        List all channels in the admin view.
        """
        try:
            resp = self._get(f"/api/channel/grid?all=1&limit={limit}")
            data = resp.json()
            return data.get("entries", [])
        except requests.exceptions.RequestException as e:
            self.log.err(f"get_channel_grid: {e}")
            return []
        except ValueError as e:
            self.log.err(f"get_channel_grid: {e}")
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

    def ensure_channels_mapped_from_services(self) -> tuple[int, int, int]:
        """
        Create channels for services not yet referenced by any channel.
        Returns (created, skipped_already_mapped, skipped_no_name)
        """
        services = self.list_services()
        channels = self.get_channel_grid()

        mapped_services: set[str] = set()
        for ch in channels:
            for su in (ch.get("services") or []):
                if isinstance(su, str) and su:
                    mapped_services.add(su)

        created = 0
        skipped_mapped = 0
        skipped_no_name = 0

        for svc in services:
            service_uuid = svc.get("uuid") or svc.get("id")
            if not isinstance(service_uuid, str) or not service_uuid:
                continue

            if service_uuid in mapped_services:
                skipped_mapped += 1
                continue

            name = self.get_service_best_name(svc)
            if not name:
                skipped_no_name += 1
                self.log.out(f"Skipping service without name: uuid={service_uuid}")
                continue

            conf = {
                "enabled": True,
                "name": name,
                "autoname": True,
                "epgauto": True,
                "services": [service_uuid],
            }

            if self.config.dry_run:
                self.log.out(f"[dry-run] Would create channel: {name} <- {service_uuid}")
                created += 1
                continue

            resp = self._post("/api/channel/create", data={"conf": json_dumps(conf)})
            if resp.status_code == 200:
                created += 1
            else:
                self.log.err(
                    f"channel/create failed for service {service_uuid} name={name!r}: {resp.status_code} {resp.text}")

        return created, skipped_mapped, skipped_no_name

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
        for ch in self.get_channel_grid():
            name = (ch.get("name") or "")
            if not isinstance(name, str):
                name = ""
            # Delete channels that have placeholder or empty names.
            # Skipping the check for autoname and epgauto because humans can't
            # create channels on this device.
            if name.strip() in unnamed_markers:
                uuid = ch.get("uuid")
                if uuid and isinstance(uuid, str):
                    if self.delete_channel_uuid(uuid):
                        deleted += 1
        return deleted

    def is_channel_streamable(self, ch: dict, *, seconds: float = 1.5) -> bool:
        """
        Quick probe: try GET /stream/channelid/<chid>?profile=pass
        and see if we get HTTP 200 and at least some bytes.
        This will allocate a tuner briefly, so keep it short.
        """
        chid = self._channel_stream_id(ch)
        if not chid:
            return False

        url = f"{self.config.base_url}/stream/channelid/{chid}"
        params = {"profile": "pass"}

        try:
            with requests.get(url, params=params, auth=self.auth, stream=True, timeout=seconds) as r:
                if r.status_code != 200:
                    return False
                # Pull a small chunk; black channel usually still returns TS,
                # but your failing case often never starts (no adapters) => non-200 or no data.
                it = r.iter_content(chunk_size=188 * 10)
                chunk = next(it, b"")
                return bool(chunk)
        except Exception:
            return False

    def deduplicate_channels_by_name(self, net_uuid: str) -> dict:
        """
        Deduplicate channels sharing the same name.
        Canonical selection prefers:
          1) most services on muxes that are enabled+OK in this network
          2) streamable (optional probe) as tie-breaker
          3) enabled
          4) lowest (major, minor)
          5) most total services
        Then merges all services onto canonical and deletes the rest.
        """
        channels = self.get_channel_grid()

        mux_health = self.get_mux_health_for_network(net_uuid)
        good_muxes = {m for (m, info) in mux_health.items() if self._mux_is_ok(info)}
        svc_to_mux = self.get_service_to_mux_map()

        # Group by name
        groups: dict[str, list[dict]] = {}
        for ch in channels:
            name = ch.get("name")
            if not isinstance(name, str):
                continue
            name = name.strip()
            if not name or name == "{name-not-set}":
                continue
            groups.setdefault(name, []).append(ch)

        merged_groups = 0
        updated_channels = 0
        deleted_channels = 0
        stream_probes = 0

        for name, chans in groups.items():
            if len(chans) < 2:
                continue

            def good_service_count(_ch: dict) -> int:
                cnt = 0
                for su in (_ch.get("services") or []):
                    if isinstance(su, str) and su:
                        mux_uuid = svc_to_mux.get(su)
                        if mux_uuid and mux_uuid in good_muxes:
                            cnt += 1
                return cnt

            # Precompute candidate features
            enriched = []
            for ch in chans:
                mm = self._parse_major_minor(ch.get("number")) or (9999, 9999)
                gsc = good_service_count(ch)
                enabled = bool(ch.get("enabled"))
                svc_count = len(ch.get("services") or [])
                enriched.append((ch, gsc, enabled, mm[0], mm[1], svc_count))

            # Sort by viability-first (descending gsc), then enabled, then lowest major/minor, then most services
            enriched.sort(key=lambda t: (-t[1], -int(t[2]), t[3], t[4], -t[5]))

            # If top two are close / tied on viability, probe streamability
            canonical = enriched[0][0]
            if len(enriched) >= 2:
                a = enriched[0]
                b = enriched[1]
                # Only probe when viability doesn't clearly decide it
                if a[1] == b[1]:
                    # Probe just these two (cheap)
                    if not self.config.dry_run:
                        stream_probes += 2
                        a_ok = self.is_channel_streamable(a[0])
                        b_ok = self.is_channel_streamable(b[0])
                        if b_ok and not a_ok:
                            canonical = b[0]

            canon_uuid = canonical.get("uuid")
            if not isinstance(canon_uuid, str) or not canon_uuid:
                continue

            # Merge services (stable order)
            merged_services: list[str] = []
            seen: set[str] = set()
            for ch in [t[0] for t in enriched]:
                for su in (ch.get("services") or []):
                    if isinstance(su, str) and su and su not in seen:
                        seen.add(su)
                        merged_services.append(su)

            # Use class autodetect, not hardcoded "channel"
            canon_ent = self._idnode_load_entry(canon_uuid)
            canon_cls = canon_ent.get("class") if canon_ent else None

            if self.config.dry_run:
                self.log.out(
                    f"[dry-run] Would set canonical services for '{name}' uuid={canon_uuid} services={merged_services}")
                updated_channels += 1
            else:
                ok = self.idnode_save_params(
                    uuid=canon_uuid,
                    cls=canon_cls,
                    changes={"services": merged_services},
                )
                if not ok:
                    self.log.err(f"Failed to merge services onto canonical for '{name}' uuid={canon_uuid}")
                    continue
                updated_channels += 1

            # Delete non-canonical channels
            for ch in [t[0] for t in enriched]:
                u = ch.get("uuid")
                if u == canon_uuid:
                    continue
                if isinstance(u, str) and u:
                    if self.config.dry_run:
                        self.log.out(f"[dry-run] Would delete duplicate channel '{name}' uuid={u}")
                        deleted_channels += 1
                    else:
                        if self.delete_channel_uuid(u):
                            deleted_channels += 1

            merged_groups += 1

        self.log.out(
            f"dedupe: merged_groups={merged_groups}, updated_channels={updated_channels}, "
            f"deleted_channels={deleted_channels}, stream_probes={stream_probes}"
        )
        return {
            "merged_groups": merged_groups,
            "updated_channels": updated_channels,
            "deleted_channels": deleted_channels,
            "stream_probes": stream_probes,
        }

    def prune_invalid_services_per_channel(self, net_uuid: str) -> dict:
        channels = self.get_channel_grid()
        mux_index = self.get_mux_index(net_uuid=net_uuid)

        self.log.out(f"prune: mux_index_size={len(mux_index)}")

        stats = {
            "channels_total": len(channels),
            "channels_updated": 0,
            "channels_deleted": 0,
            "service_links_removed": 0,
            "channels_unchanged": 0,
            "reasons": {
                "no_services": 0,
                "all_services_ok": 0,
                "channel_missing_uuid": 0,

                # new, more specific:
                "removed_unknown_mux": 0,
                "removed_wrong_network": 0,
                "removed_mux_disabled": 0,
                "removed_mux_bad_scan": 0,
                "removed_service_missing_mux": 0,
                "removed_service_uuid_invalid": 0,
            },
            "debug": {
                "mux_index_size": len(mux_index),
                "services_seen": 0,
                "services_kept": 0,
                "services_removed": 0,
            }
        }

        for ch in channels:
            chan_uuid = ch.get("uuid")
            if not isinstance(chan_uuid, str) or not chan_uuid:
                stats["reasons"]["channel_missing_uuid"] += 1
                continue

            services = ch.get("services") or []
            if not isinstance(services, list):
                services = []

            if not services:
                stats["reasons"]["no_services"] += 1
                continue

            kept: list[str] = []
            removed: list[str] = []
            removed_reasons: list[str] = []

            for su in services:
                stats["debug"]["services_seen"] += 1

                if not isinstance(su, str) or not su:
                    removed.append(str(su))
                    removed_reasons.append("removed_service_uuid_invalid")
                    continue

                ok, reason = self.service_is_acceptable(
                    su, net_uuid=net_uuid, mux_index=mux_index
                )
                if ok:
                    kept.append(su)
                    stats["debug"]["services_kept"] += 1
                else:
                    removed.append(su)
                    removed_reasons.append(reason)
                    stats["debug"]["services_removed"] += 1

            if not removed:
                stats["channels_unchanged"] += 1
                stats["reasons"]["all_services_ok"] += 1
                continue

            # Attribute reason counts
            for r in removed_reasons:
                if r in stats["reasons"]:
                    stats["reasons"][r] += 1

            stats["service_links_removed"] += len(removed)

            if self.config.dry_run:
                self.log.out(
                    f"[dry-run] Would prune channel {ch.get('name')} uuid={chan_uuid} "
                    f"removed={removed} kept={kept} reasons={removed_reasons}"
                )
                stats["channels_updated"] += 1
                continue

            ent = self._idnode_load_entry(chan_uuid)
            cls = ent.get("class") if ent else None

            if kept:
                ok = self.idnode_save_params(uuid=chan_uuid, cls=cls, changes={"services": kept})
                if ok:
                    stats["channels_updated"] += 1
            else:
                if self.delete_channel_uuid(chan_uuid):
                    stats["channels_deleted"] += 1

        return stats

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

        self.set_epg_grabbers_enabled(enabled=False)

        # Step 1: Find network
        log(f"Finding network UUID for: {self.config.net_name}")
        net_uuid = self.get_network_uuid()
        if not net_uuid:
            log(f"Network not found: {self.config.net_name}")
            return False
        log(f"Network UUID: {net_uuid}")

        log("Ensuring ATSC-T frontends are enabled and linked to network...")
        stats = self.ensure_atsc_t_frontends_enabled_and_linked(net_uuid)
        log(f"Frontend config: {stats}")

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
        states = self.count_mux_states(net_uuid)
        log(f"Waiting for scan to settle. {states}.")
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
                self.log.out("Scan settled.")
                break

            log(f"Waiting for scan to settle. {elapsed:.1f}s elapsed. {states}.")
            time.sleep(self.config.sleep_secs)

        log("Sleeping to let tvheadend settle...")
        time.sleep(2.0)

        # Step 6: Start service mapping
        # In order:
        # - Delete orphan channels (no services attached)
        # - Map services → channels
        # - Delete unnamed / junk channels
        # - Deduplicate channels
        # - (Optional) renumber / sort

        # After wiping muxes or rescanning, TVH leaves behind channels with: "services": [].
        # These cannot stream or get services again, and they will confuse de-duplication.
        log("Deleting orphan channels...")
        deleted = self.delete_orphan_channels()
        log(f"Deleted {deleted} orphan channels.")

        log("Disabling failed muxes")
        self.disable_failed_muxes(net_uuid)
        log("Sleeping to let service graph settle...")
        time.sleep(0.5)

        # At this point, services are authoritative, channel names come from svcname,
        # and channels have valid services. So we can now map services to channels.
        log("Mapping services -> channels (deterministic)...")
        created, skipped_mapped, skipped_no_name = self.ensure_channels_mapped_from_services()
        log(f"Mapping results: created={created}, already_mapped={skipped_mapped}, no_name={skipped_no_name}")

        # Some channels have names like '{name-not-set}' or ''. They're partially broken objects
        # or maybe garbage from previous scans. They're useless, so delete them now that we've
        # completed the service mapping.
        if self.config.delete_unnamed_channels:
            log("Cleaning up unnamed channels...")
            deleted = self.cleanup_unnamed_channels()
            log(f"Deleted {deleted} unnamed channels.")

        # Deduplicate channels by name, preferring the one with the lowest major number.
        log("Deduplicating channels...")
        dedup_stats = self.deduplicate_channels_by_name(net_uuid)
        log(f"Dedup stats: {dedup_stats}")

        log("Sleeping to let tuners and table decoding settle...")
        time.sleep(1)

        log("Debug: Health check ...")
        self.debug_channel_service_mux_health(net_uuid)

        log("Pruning invalid services per channel (final safety net)...")
        prune_stats = self.prune_invalid_services_per_channel(net_uuid)
        log(f"Prune stats: {prune_stats}")
        log("Sleeping to reduce flakiness due to immediate retuning after write...")
        time.sleep(1)  # Reduce "immediate retune after write" flakiness.

        log("Done with scan.")

        return True

class TVHWatchdog:
    """
    “Soft” recovery:
      - We only act on subscriptions that look like *ours* (by client User-Agent substring).
      - If the subscription is stuck Bad / 0 in/out for too long, we restart the mpv load.
      - Optionally, if TVH reports a matching connection id, we cancel just that connection.
    """
    def __init__(self, tvh: TVHeadendScanner, *, ua_tag: str = MPV_USERAGENT):
        self.tvh = tvh
        self.ua_tag = ua_tag
        self.log = Logger("watchdog")
        self._bad_since: Optional[float] = None
        self._last_fix: float = 0.0

        self.log.out(f"Watchdog initialized with UA tag {self.ua_tag}")

    def find_our_subscription(self, subs: dict[str, Any]) -> Optional[dict[str, Any]]:
        for e in subs.get("entries", []):
            useragent = (e.get("useragent") or "")
            client = (e.get("client") or "")
            title = (e.get("title") or "")
            # Depending on TVH version/config, your tag may appear in client; title often just "HTTP".
            if self.ua_tag in useragent or self.ua_tag in client or self.ua_tag in title:
                return e
        return None

    def check_and_fix(
            self,
            *,
            now: float,
            mpv,
            current_url: str,
            expecting: bool = True,
            missing_grace_s: float = 3.0,
    ) -> bool:
        """
        Returns True if we took a corrective action.

        This has two modes:
          1) If a matching subscription exists and looks stuck (Bad / errors / 0 rate), we reload.
          2) If we *expect* to be streaming but TVH shows **no matching subscription** for too long,
             we also reload. (This is the case when TVH drops the subscription with
             "No input detected".)
        """
        # Don’t spam TVH if you call this at 60fps.
        if now - self._last_fix < 1.0:
            return False

        try:
            subs = self.tvh.subscriptions()
        except Exception:
            return False

        ours = self.find_our_subscription(subs)

        # --- Case A: expected stream but no subscription at all ---
        if not ours:
            if not expecting:
                self._bad_since = None
                return False

            if self._bad_since is None:
                self._bad_since = now
                return False

            if now - self._bad_since < missing_grace_s:
                return False

            self.log.out(f"No matching subscription for {now - self._bad_since:.1f}s; reloading {current_url}")
            self._last_fix = now
            self._bad_since = now  # reset window so we don't thrash

            try:
                mpv.stop()
            except Exception as e:
                self.log.err(f"Failed to stop mpv: {e}")

            try:
                # Prefer immediate reload if available; fall back to loadfile.
                if hasattr(mpv, "loadfile_now"):
                    mpv.loadfile_now(current_url)
                else:
                    mpv.loadfile(current_url)
            except Exception as e:
                self.log.err(f"Failed to reload url {current_url}: {e}")

            return True

        # --- Case B: we have a subscription, but it looks stuck ---
        state = (ours.get("state") or "")
        errs = int(ours.get("errors") or 0)
        rate_in = int(ours.get("in") or 0)
        rate_out = int(ours.get("out") or 0)
        started = int(ours.get("start") or 0)
        age = now - started if started else 0.0

        looks_stuck = (
                state.lower() == "bad"
                or errs > 0
                or (age > 3.0 and rate_in == 0 and rate_out == 0)
        )

        if not looks_stuck:
            self._bad_since = None
            return False

        if self._bad_since is None:
            self._bad_since = now
            return False

        if now - self._bad_since < 2.0:
            return False  # give it a moment before acting

        self.log.out(f"Looks stuck: state={state}, errors={errs}, in={rate_in}, out={rate_out}, age={age:.1f}s")
        # --- corrective action ---
        self._last_fix = now
        self._bad_since = now  # reset window so we don't thrash

        try:
            mpv.stop()
        except Exception as e:
            self.log.err(f"Failed to stop mpv: {e}")

        try:
            if hasattr(mpv, "loadfile_now"):
                mpv.loadfile_now(current_url)
            else:
                mpv.loadfile(current_url)
        except Exception as e:
            self.log.err(f"Failed to load current url {current_url}: {e}")

        # Optional: cancel a local connection if TVH reports one.
        try:
            conns = self.tvh.connections()
            for c in conns.get("entries", []):
                if c.get("peer") in ("127.0.0.1", "::1"):
                    cid = c.get("id")
                    if isinstance(cid, int):
                        self.tvh.cancel_connections([cid])
                        break
        except Exception as e:
            self.log.err(f"Failed to cancel connections: {e}")

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
