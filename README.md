## Installing tvheadend



### Install Prerequisites

$ sudo apt update
$ sudo apt install -y ca-certificates gnupg

### Install `tvheadend` Signing Key

$ curl -fsSL https://dl.cloudsmith.io/public/tvheadend/tvheadend/gpg.C6CC06BD69B430C6.key | sudo gpg --dearmor -o /usr/share/keyrings/tvheadend-tvheadend-archive-keyring.gpg
$ sudo chmod 644 /usr/share/keyrings/tvheadend-tvheadend-archive-keyring.gpg

Confirm the key looks good

$ gpg --show-keys /usr/share/keyrings/tvheadend-tvheadend-archive-keyring.gpg

```
pub   rsa3072 2021-04-15 [SCEA]
      70E910E6924F822992891E6EC6CC06BD69B430C6
uid                      Cloudsmith Package (tvheadend/tvheadend) <support@cloudsmith.io>
```

### Add the `tvheadend` Repository

$ sudo vim /etc/apt/sources.list.d/tvheadend-tvheadend.list

Add this single line

```
deb [signed-by=/usr/share/keyrings/tvheadend-tvheadend-archive-keyring.gpg] https://dl.cloudsmith.io/public/tvheadend/tvheadend/deb/debian trixie main
```

Save and quit.

### Update and Install `tvheadend`

$ sudo apt install tvheadend

## Configuration

Administrator
Password

    After installation Tvheadend can be accessed via HTTP on port 9981. From this
    machine you can point your web-browser to http://localhost:9981/. 

    If you want to completely remove configuration, use your package managers
    --purge option, e.g, apt-get remove --purge tvheadend*


## Verification

When it's done installing dependencies, you can check that everything went ok:

$ apt policy tvheadend

For me, this responded:

```
tvheadend:
  Installed: 4.3-2471~g5fd594910~trixie
  Candidate: 4.3-2471~g5fd594910~trixie
  Version table:
 *** 4.3-2471~g5fd594910~trixie 500
        500 http://archive.raspberrypi.com/debian trixie/main arm64 Packages
        100 /var/lib/dpkg/status
```

Confirm the daemon is running

```
● tvheadend.service - Tvheadend - a TV streaming server and DVR
     Loaded: loaded (/usr/lib/systemd/system/tvheadend.service; enabled; preset: enabled)
     Active: active (running) since Thu 2025-12-18 23:18:17 PST; 2min 10s ago
 Invocation: fefd832643cf4f7fbb183f6bdc5d3ecd
   Main PID: 3988 (tvheadend)
      Tasks: 36 (limit: 1572)
        CPU: 2.734s
     CGroup: /system.slice/tvheadend.service
             └─3988 /usr/bin/tvheadend -f -p /run/tvheadend.pid -u hts -g video

Dec 18 23:18:19 FPTV tvheadend[3988]: epggrab: module /usr/bin/tv_grab_fi created
Dec 18 23:18:19 FPTV tvheadend[3988]: epggrab: module /usr/bin/tv_grab_ch_search created
Dec 18 23:18:19 FPTV tvheadend[3988]: epggrab: module /usr/bin/tv_grab_zz_sdjson_sqlite created
Dec 18 23:18:19 FPTV tvheadend[3988]: epggrab: module /usr/bin/tv_grab_pt_vodafone created
Dec 18 23:18:19 FPTV tvheadend[3988]: epggrab: module /usr/bin/tv_grab_is created
Dec 18 23:18:19 FPTV tvheadend[3988]: tbl-eit: module eit - scraper disabled by config
Dec 18 23:18:19 FPTV tvheadend[3988]: dvr: Purging obsolete autorec entries for current schedule
Dec 18 23:18:19 FPTV tvheadend[3988]: START: HTS Tvheadend version 4.3-2471~g5fd594910 started, running as PID:3988 UID:109 GID:44, CWD:/ CNF:/var/lib/tvheade>
Dec 18 23:18:19 FPTV tvheadend[3988]: bouquet: new bouquet 'Tvheadend Network'
Dec 18 23:18:20 FPTV tvheadend[3988]: avahi: Service 'Tvheadend' successfully established.
```

Confirm your tuner devices are visible

$ ls -R /dev/dvb

```
/dev/dvb:
adapter0  adapter1

/dev/dvb/adapter0:
demux0  dvr0  frontend0  net0

/dev/dvb/adapter1:
demux0  dvr0  frontend0  net0
```

You should see `/dev/dvb`, `/dev/dev/adapter0`, and `/dev/dvb/adapter1`.

## Test Channel Scanning and Playback

Terminology:

- A Network describes the kind of signal that exists
- A Mux describes a specific RF frequency (a particular "channel")

Note that tvheadend does not ship with a US ATSC frequency table. We will
deal with this below.

### Check Tuner is Visible

$ dmesg | grep -i dvb

You should see that the firmware was loaded and frontend(s) registered.

```
[    6.149704] em28xx 1-1.2:1.0: DVB interface 0 found: bulk
[    7.471472] tveeprom: TV standards PAL(B/G) NTSC(M) PAL(I) SECAM(L/L') PAL(D/D1/K) ATSC/DVB Digital (eeprom 0xfc)
[    7.471490] em28xx 1-1.2:1.0: dvb set to bulk mode.
[    8.752006] tveeprom: TV standards PAL(B/G) NTSC(M) PAL(I) SECAM(L/L') PAL(D/D1/K) ATSC/DVB Digital (eeprom 0xfc)
[    8.752024] em28xx 1-1.2:1.0: dvb ts2 set to bulk mode.
[    9.022015] em28xx 1-1.2:1.0: Binding DVB extension
[    9.082029] dvbdev: DVB: registering new adapter (1-1.2:1.0)
[    9.082038] em28xx 1-1.2:1.0: DVB: registering adapter 0 frontend 0 (LG Electronics LGDT3306A VSB/QAM Frontend)...
[    9.082052] dvbdev: dvb_create_media_entity: media entity 'LG Electronics LGDT3306A VSB/QAM Frontend' registered.
[    9.082892] dvbdev: dvb_create_media_entity: media entity 'dvb-demux' registered.
[    9.090926] em28xx 1-1.2:1.0: DVB extension successfully initialized
[    9.090947] em28xx 1-1.2:1.0: Binding DVB extension
[    9.114790] dvbdev: DVB: registering new adapter (1-1.2:1.0)
[    9.114802] em28xx 1-1.2:1.0: DVB: registering adapter 1 frontend 0 (LG Electronics LGDT3306A VSB/QAM Frontend)...
[    9.114815] dvbdev: dvb_create_media_entity: media entity 'LG Electronics LGDT3306A VSB/QAM Frontend' registered.
[    9.115854] dvbdev: dvb_create_media_entity: media entity 'dvb-demux' registered.
[    9.121133] em28xx 1-1.2:1.0: DVB extension successfully initialized
[    9.121154] em28xx: Registered (Em28xx dvb Extension) extension
```

### Open the Web UI

You can do this on the pi, or from another machine. (My Pi has no keyboard; I'm
ssh'd in from another machine, so I can open http://<pi-ip>:9981/)

When prompted, configure Language preferences.

Navigate to: Configuration -> DVB Inputs -> Networks:

1. Click Add
2. Choose ATSC-T (Terrestrial ATSC; Non-cable TV service in the US)
3. Give it a name (ATSC OTA)
4. Leave everything else alone
5. Click Create to save changes as a new entry

You should see a new row with your network details.

1. Click the Enable box on the new row.
2. Click the Save button.

### Assign the Tuner to the ATSC Network

Navigate to: Configuration -> DVB Inputs -> TV Adapters

You should see the adapter0 and adapter1 from above.

For each adapter:

1. Click the adapter's name (Be sure it's the one containing ATSC-T)
2. In the Parameters window,
   - Check Enabled
   - In Networks, choose your network's name from above


### Start the Scan

Navigate to: Configuration -> DVB Inputs -> Muxes

Click Add



Pick a known, strong local RF channel

Go to rabbitears.info and find something close to your address

Near me are: 9‑1 (30)
54‑1 (30)	KQED
KQEH	PBS
PBS 	SAN FRANCISCO
SAN JOSE 	CA
CA 		12.7 	230.3° 	217.4° 	110.46  Good  	70.14


Convert RF channel to frequency

What “9-1 (30) KQED” means

9-1 → virtual channel (what viewers see)

(30) → RF (physical) channel

KQED → station callsign

For tuning, tvheadend only cares about the RF channel, not 9-1.

So you want RF channel 30.

RF channel 30 → frequency

US ATSC UHF channels are spaced every 6 MHz, starting at channel 14 = 473 MHz.

Channel 30 is:

Frequency = 569,000,000 Hz


Go to:

Configuration → DVB Inputs → Muxes → Add

Fill in:

Enabled: ✔

Delivery system: ATSC-T

Frequency (Hz): 569000000

Modulation: 8VSB

Leave everything else unchanged.

Click Save.



Initially it's may be empty. 

Go to: Configuration -> DVB Inputs -> Networks

1. Click your network
2. Click the Force Scan button in the button bar

Then return to Muxes




$ curl -s --digest -u 'jed:i like pie' 'http://localhost:9981/api/mpegts/network/grid' | jq '.entries[0].networkname'
"ATSC OTA"

journald.conf

SystemMaxUse=200M
SystemKeepFree=500M
MaxRetentionSec=1month


Credentials

sudo install -m 600 -o root -g root /dev/null /etc/fptv-tvheadend-api.env

edit /etc/fptv-tvheadend-api.env:

```
TVH_USER=<username>
TVH_PASS=<password>
```

Create and edit `/etc/systemd/system/fptv.service`:

```
[Unit]
Description=FPTV ATSC control / rescan service
After=network.target tvheadend.service
Requires=tvheadend.service

[Service]
Type=oneshot
User=jed
Group=jed

# Optional: credentials live here
EnvironmentFile=/etc/fptv-tvheadend-api.env

# Your startup script (rescan, maintenance, etc.)
ExecStart=/usr/local/bin/fptv-begin.sh

# Hardening (recommended)
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes

[Install]
WantedBy=multi-user.target
```

Run at boot:

`sudo systemctl enable fptv.service`

Now you can manually `sudo systemctl start fptv.service`

