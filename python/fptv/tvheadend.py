from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class Channel:
    name: str
    url: str


def get_channels() -> List[Channel]:
    """
    tvheadend's /playlist/channels returns an m3u file.
    Lines look like:

        #EXTINF:-1 tvg-id="26e30b9fb6fb20429aac61784fb50ed4" tvg-chno="9.1",KQED-HD
        http://localhost:9981/stream/channelid/520872742?profile=pass
    """

    print(f"opening {URL_PLAYLIST}")
    with urllib.request.urlopen(URL_PLAYLIST, timeout=5) as resp:
        text = resp.read().decode("utf-8", errors="replace")
        print(f"resp:\n\n{text}\n\n")
        lines = text.splitlines()

    channels = []
    name = None

    for line in lines:
        print(f"Processing line {line}")

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


URL_TVHEADEND = "http://localhost:9981"
URL_PLAYLIST = f"{URL_TVHEADEND}/playlist/channels"
