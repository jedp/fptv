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

import os
import time
from dataclasses import dataclass
from typing import Optional, List, Callable

import requests
from requests.auth import HTTPDigestAuth


@dataclass
class ScanConfig:
    base_url: str = "http://localhost:9981"
    net_name: str = "ATSC OTA"
    user: str = ""
    password: str = ""
    rf_start: int = 14
    rf_end: int = 36
    wipe_existing_muxes: bool = False
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

    def create_mux_atsc(self, net_uuid: str, freq_hz: int) -> bool:
        """Create an ATSC mux for the given frequency."""
        # Try without explicit class first
        try:
            response = self._post("/api/mpegts/mux/create", data={
                "network_uuid": net_uuid,
                "frequency": str(freq_hz),
                "modulation": self.config.modulation,
            })
            if response.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass

        # Fallback: discover ATSC-T mux class
        try:
            response = self._get("/api/mpegts/mux/class")
            if response.status_code != 200:
                return False
            data = response.json()

            # Look for ATSC-T class
            mux_class = None
            if isinstance(data, dict):
                entries = data.get("entries", [])
            elif isinstance(data, list):
                entries = data
            else:
                return False

            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                class_name = entry.get("class") or entry.get("id", "")
                if "atsc" in class_name.lower() and "t" in class_name.lower():
                    mux_class = class_name
                    break

            if not mux_class:
                return False

            response = self._post("/api/mpegts/mux/create", data={
                "class": mux_class,
                "network_uuid": net_uuid,
                "frequency": str(freq_hz),
                "modulation": self.config.modulation,
            })
            if response.status_code == 200:
                return True
            else:
                return False

        except requests.exceptions.RequestException:
            return False

    def force_scan_mux(self, mux_uuid: str) -> bool:
        """Force scan a mux by UUID."""
        # Try mpegts/mux/scan first
        try:
            response = self._post("/api/mpegts/mux/scan", data={"uuid": mux_uuid})
            if response.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass

        # Fallback: set scan_state to PENDING via idnode/save
        try:
            response = self._post("/api/idnode/save", data={
                "uuid": mux_uuid,
                "scan_state": "PENDING",
            })
            if response.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass

        # Last fallback: numeric scan_state=1
        try:
            self._post("/api/idnode/save", data={
                "uuid": mux_uuid,
                "scan_state": "1",
            })
            return True
        except requests.exceptions.RequestException:
            return False

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

    def start_service_mapping(self) -> bool:
        """Start service mapping."""
        # Try mapper/start first
        try:
            response = self._post("/api/service/mapper/start")
            if response.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass

        # Fallback to mapper/map
        try:
            self._post("/api/service/mapper/map")
            return True
        except requests.exceptions.RequestException:
            return False

    def scan(self, progress_callback: Optional[Callable[[str, MuxStates], None]] = None) -> bool:
        """
        Perform a full ATSC OTA scan.

        Args:
            progress_callback: Optional callback function(message: str, states: MuxStates)
                              called periodically with progress updates.

        Returns:
            True if scan completed successfully, False otherwise.
        """

        def log(msg: str, states: Optional[MuxStates] = None):
            if progress_callback:
                progress_callback(msg, states or MuxStates())
            else:
                print(msg)

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
        log(f"Created {created_count} muxes.")

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
        log("Starting service mapping...")
        self.start_service_mapping()
        log("Done.")

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
