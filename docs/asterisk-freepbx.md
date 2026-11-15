# Asterisk and FreePBX setup

This is how I connected the agent to FreePBX 17 (Asterisk 22) with an Internode SIP trunk. Plain Asterisk works the same way; only the menus differ. Numbers and addresses below are placeholders: `pbx.lan` is the PBX, `agent.lan` is the Docker host running the container.

The outbound part (the agent calling businesses) needs sections 1 to 5. Sections 6 onwards are for answering calls.

## 1. Trunk and codecs

Any SIP trunk that can make normal outbound calls will do. Put alaw (or ulaw) first in the trunk's codec list, and G.722 first on your own extensions if your phones support it.

The agent talks to Asterisk at 16 kHz (signed linear, `slin16`). Asterisk converts to whatever the trunk uses, so the far end hears normal phone audio and your own G.722 handsets get wideband speech when you test against them.

On Internode I got an instant `403 Forbidden` on every outbound call until the trunk had `contact_user`, `from_user` and `from_domain` set to my number and Internode's domain. If outbound calls from your own handsets work, you've already solved this.

## 2. ARI user

The agent places calls through the Asterisk REST Interface.

In FreePBX: Settings → Advanced Settings, enable the mini-HTTP server and ARI, and make it listen on an address the Docker host can reach (`HTTPBINDADDRESS`, port 8088). Then Settings → Asterisk REST Interface Users → add a user, for example `phoneagent`, with a long password.

Check from the Docker host:

```bash
curl -u phoneagent:<password> http://pbx.lan:8088/ari/asterisk/info
```

You should get JSON with the Asterisk version. In `.env`:

```
ARI_URL=http://pbx.lan:8088/ari
ARI_USER=phoneagent
ARI_PASSWORD=<password>
DIAL_ENDPOINT=Local/{number}@from-internal/n
```

`DIAL_ENDPOINT` sends each call through `from-internal`, so it uses your normal outbound routes, caller ID and trunk, as if someone dialled from an extension.

## 3. AMI user for keypad presses

Phone menus need keypad presses. The agent can play DTMF tones into the audio, but the first retailer menu I tried (Harvey Norman) ignored them completely. Real DTMF, sent by Asterisk as RFC 2833 events, worked everywhere. That needs a manager (AMI) user that the agent reaches over HTTP.

Add to `/etc/asterisk/manager_custom.conf`:

```
[phoneagent]
secret = <another long password>
deny = 0.0.0.0/0.0.0.0
permit = <agent.lan IP>/255.255.255.255
read = call
write = call
webenabled = yes
```

Then `asterisk -rx "manager reload"`. In `.env`:

```
AMI_URL=http://pbx.lan:8088/rawman
AMI_USER=phoneagent
AMI_PASSWORD=<password>
```

Without AMI the agent falls back to in-band tones.

## 4. The outbound dialplan

When the far end answers, the call lands in the `[phone-agent]` context. It records the call and hands the audio to the container over AudioSocket. Add this to `/etc/asterisk/extensions_custom.conf` (in FreePBX also reachable as Admin → Config Edit) and run `fwconsole reload`:

```
[phone-agent]
exten => s,1,NoOp(Phone agent call ${AGENT_UUID})
 same => n,Set(CDR(userfield)=phone-agent:${AGENT_UUID})
 same => n,MixMonitor(phone-agent/${AGENT_UUID}.wav)
 same => n,Dial(AudioSocket/${AGENT_SOCKET}/${AGENT_UUID}/c(slin16))
 same => n,Hangup()
```

The agent sets `AGENT_UUID` and `AGENT_SOCKET` when it originates the call. `AGENT_SOCKET` comes from `AUDIOSOCKET_ADVERTISE` in `.env`, which must be the Docker host's address as Asterisk sees it, port 9092.

`c(slin16)` and `AUDIO_RATE=16000` go together. The `AudioSocket()` dialplan application also works, but only at 8 kHz; use `AUDIO_RATE=8000` with it.

Check that the module is there:

```bash
asterisk -rx "module show like audiosocket"
```

Recordings end up in `/var/spool/asterisk/monitor/phone-agent/`, named after the call ID. To play them on the call page, download them with the credit, and export [test samples](test-samples.md) with each side's audio, the container needs read access to `/var/spool/asterisk/monitor`; [section 12](#12-recordings-in-the-container) shows how I share it over NFS.

FreePBX's call history (Reports → CDR) won't show them as they are: it only plays files in `monitor/YYYY/MM/DD/` whose name has the date as its fourth dash-separated part, the way its own recordings are named. The version of this context in [`asterisk/extensions_custom.conf`](../asterisk/extensions_custom.conf) handles that: it sets `CDR(recordingfile)` to a name like `agent-out-0491570156-20261115-120313-<call id>.wav`, and when the recording finishes it symlinks the file into that day's folder. The incoming-call context below does the same with `agent-in-`. I noticed this only after recording a set of test calls I wanted to keep and finding no play buttons for them.

The same context also records each side of the call on its own (`<id>-rx.wav`, `<id>-tx.wav`). On a real line one side is often much quieter: on a test call Piper came out about 6 dB louder than the receptionist. When the call ends, [`asterisk/agent-recording-finish`](../asterisk/agent-recording-finish) compresses and normalises each side with sox, mixes them back into `<id>.wav` (the untouched mix stays as `<id>-raw.wav`), and creates the call-history link. Copy it to `/usr/local/bin/` and make it executable; it needs `sox`, which FreePBX already has.

## 5. A test receptionist

Before calling anyone real, call something that can't be annoyed. `asterisk/test-receptionist.conf` is a scripted hotel receptionist on extension 7999. It greets, says "let me just check that for you, one moment", goes quiet for a while, then answers questions about a family room and offers to book it. It's crude, but it exercises the whole path: originate, AudioSocket, speech in both directions, a pause the agent must sit through, an offer it must decline, and hang-up.

It plays six short recordings, `custom/pa-test-1` to `pa-test-6`, listed at the top of the file. I generated them with the same Piper voice. Append the file to `extensions_custom.conf`, reload, and ask Claude to call 7999.

After that, call your own mobile and play a difficult receptionist: interrupt it, go quiet, say "press 1 for reservations", ask whether it's a robot.

## 6. Answering calls

For incoming calls Asterisk has to tell the agent a call is coming before it connects the audio. The agent needs to know who's calling, whether the call was forwarded and which profile to use, and it can refuse (for example when it's already on a call).

Put the agent's address and token in `/etc/asterisk/globals_custom.conf`. Keep this file readable only by the asterisk user, since it holds the token:

```
PHONE_AGENT_URL=http://agent.lan:8090
PHONE_AGENT_SOCKET=agent.lan:9092
PHONE_AGENT_TOKEN=<API_TOKEN from the agent's .env>
```

Then the context that hands a call over:

```
[phone-agent-inbound]
exten => s,1,Set(AGENT_UUID=${FILTER(0-9a-f-,${SHELL(cat /proc/sys/kernel/random/uuid)})})
 same => n,Set(AGENT_PROFILE=${IF($["${AGENT_PROFILE}" = ""]?screener:${AGENT_PROFILE})})
 same => n,Set(AGENT_DIVERTED=)
 same => n,ExecIf($["${CHANNEL(channeltype)}" = "PJSIP"]?Set(AGENT_DIVERTED=${PJSIP_HEADER(read,Diversion)}))
 same => n,Set(CURLOPT(httpheader)=Authorization: Bearer ${PHONE_AGENT_TOKEN})
 same => n,Set(CURLOPT(conntimeout)=3)
 same => n,Set(CURLOPT(httptimeout)=10)
 same => n,Set(AGENT_REPLY=${CURL(${PHONE_AGENT_URL}/inbound,uuid=${AGENT_UUID}&profile=${AGENT_PROFILE}&caller=${URIENCODE(${CALLERID(num)})}&diverted=${URIENCODE(${AGENT_DIVERTED})})})
 same => n,GotoIf($["${AGENT_REPLY}" != "ok"]?unavailable)
 same => n,Set(CDR(userfield)=phone-agent-in:${AGENT_UUID})
 same => n,MixMonitor(phone-agent/${AGENT_UUID}.wav)
 same => n,Dial(AudioSocket/${PHONE_AGENT_SOCKET}/${AGENT_UUID}/c(slin16))
 same => n,Hangup()
 same => n(unavailable),NoOp(Phone agent did not take the call: ${AGENT_REPLY})
 same => n,GotoIf($["${AGENT_FALLBACK}" != ""]?${AGENT_FALLBACK})
 same => n,Answer()
 same => n,Playback(im-sorry&cannot-complete-temp-error)
 same => n,Hangup()
```

Registration takes about a second. The agent uses that time to warm up the model and speech recognition, and the greeting is pre-rendered, so the caller hears "Hi, you've reached Darek's phone, I'm his assistant" half a second after the call connects.

To try it, add a test extension under `[from-internal-custom]` and ring it from a handset:

```
exten => 7990,1,Goto(phone-agent-inbound,s,1)
```

In FreePBX you can also register it as a Custom Extension so it shows up with a name. Mine is called Purgatory.

## 7. Purgatory with a voicemail fallback

To use the screener as a destination (from an inbound route, a time condition, an IVR), wrap it in a context that sets a fallback, so callers reach voicemail if the agent is down or busy:

```
[phone-agent-screen]
exten => s,1,Set(AGENT_FALLBACK=ext-local,vmu101,1)
 same => n,Goto(phone-agent-inbound,s,1)
```

Then Admin → Custom Destinations → add `phone-agent-screen,s,1`. It can now be picked anywhere FreePBX asks for a destination.

## 8. Calls forwarded from a mobile

This is the part I actually use every day. My mobile forwards calls to the home number when I'm busy, decline, don't answer or have blocked the caller, and the home number decides what to do with them.

On Telstra the conditional forwards are set with codes from the phone dialler:

```
**67*+61XXXXXXXXX#        busy, declined, and calls iOS silences or blocks
**61*+61XXXXXXXXX*11*20#  no answer after 20 seconds
**62*+61XXXXXXXXX#        unreachable
*#67#  check            ##67#  remove
```

iOS "Silence Unknown Callers" and blocked numbers both use the busy forward, so `**67` alone already catches most spam. Call Waiting on the iPhone keeps genuinely busy calls from forwarding.

A forwarded call arrives with a Diversion header naming the mobile, for example:

```
Diversion: <sip:4XXXXXXXX@203.2.134.1;user=phone>;reason=unknown;counter=1
```

Telstra always sends `reason=unknown`, so you can't tell "declined" from "no answer". The caller's own number is in the From header as usual.

Point the inbound route for the home number at a custom destination `phone-agent-route,s,1`:

```
[phone-agent-route]
exten => s,1,Set(AGENT_DIVERTED_BY=${PJSIP_HEADER(read,Diversion)})
 same => n,Set(CMCID=)
 same => n,ExecIf($["${PHONE_AGENT_CONTACT_GROUPS}" != ""]?AGI(contactmanager.agi,${CALLERID(num)},${PHONE_AGENT_CONTACT_GROUPS},1))
 same => n,Set(AGENT_CONTACT=${IF($[${LEN(${CMCID})} > 0 & ${REGEX("^Unknown$" ${CMCID})} = 0]?1:0)})
 same => n,ExecIf(${AGENT_CONTACT}?Set(CALLERID(name)=${CMCID}))
 same => n,GotoIf($["${PHONE_AGENT_MOBILE}" != "" & ${REGEX("${PHONE_AGENT_MOBILE}" ${AGENT_DIVERTED_BY})}]?forwarded)
 same => n,Goto(timeconditions,1,1)
 same => n(forwarded),GotoIf(${AGENT_CONTACT}?contact)
 same => n,Goto(phone-agent-screen,s,1)
 same => n(contact),GotoIf($["${PHONE_AGENT_CONTACTS_TOGGLE}" != ""]?app-daynight,${PHONE_AGENT_CONTACTS_TOGGLE},1)
 same => n,Goto(timeconditions,1,1)
```

with, in `globals_custom.conf`:

```
PHONE_AGENT_MOBILE=4XXXXXXXX
PHONE_AGENT_CONTACT_GROUPS=3_2
PHONE_AGENT_CONTACTS_TOGGLE=1
```

`timeconditions,1,1` stands for whatever the route did before; change it to your own destination. Calls that weren't forwarded from the mobile go there untouched.

## 9. Contacts

The lookup uses FreePBX's Contact Manager through its own `contactmanager.agi` script. It runs on every call, so adding or deleting a contact in the GUI takes effect immediately. A match also sets the caller name your handsets show.

`PHONE_AGENT_CONTACT_GROUPS` lists the Contact Manager group ids to search, joined with `_`. Numbers must be stored the way the trunk presents caller ID. Internode sends national format (`04XXXXXXXX`, `02XXXXXXXX`), so that's how I store them.

I imported my iPhone contacts with `tools/contacts_to_freepbx.py` (vCard to national-format numbers, no faxes, optional list of blocked contacts to skip) and `asterisk/load_contacts.php`, which loads the result into a named group. Export from the iPhone through iCloud: select all contacts, then Export vCard.

## 10. A switch for contacts

`PHONE_AGENT_CONTACTS_TOGGLE` points at a Call Flow Control (day/night) entry. Create one under Applications → Call Flow Control, with normal mode going to your usual destination and override mode going to `phone-agent-screen,s,1`. Its feature code (for example `*281`) flips it from any handset. In override mode, forwarded calls from contacts go to Purgatory as well.

## 11. Voicemail

The fallback goes to voicemail 101. I replaced its greeting with one recorded by the same Piper voice, so it doesn't sound like a different system, and turned off the standard "after the tone" instructions (`VM_OPTS=s` in voicemail settings), because the custom greeting already says it.

## 12. Recordings in the container

The call page can play each call, offer it as a download with the credit, and put each side's audio into a [test sample](test-samples.md). For that the container needs to read FreePBX's recordings. FreePBX has no module that shares a folder (Filestore only sends backups out), so I run a plain NFS server next to it, read-only, open to the Docker host only.

On the PBX:

```bash
apt-get install -y nfs-kernel-server
id asterisk     # note the uid and gid, 999 and 996 on mine
```

Add to `/etc/exports`, with the Docker host's address and the asterisk uid and gid:

```
/var/spool/asterisk/monitor 192.0.2.20(ro,all_squash,anonuid=999,anongid=996,no_subtree_check,sync)
```

then `exportfs -ra`. `ro` and `all_squash` mean that whatever user reads from the Docker host, the PBX treats it as asterisk and only lets it read. Export the whole `monitor` folder, not `monitor/phone-agent`: the agent finds recordings by a path that starts with `phone-agent/`, and the sample tool can then also read FreePBX's own recordings.

The FreePBX firewall has to let the Docker host reach port 2049. Mine has the whole LAN in the Trusted zone, so nothing was needed. If yours doesn't, add the Docker host under Connectivity → Firewall → Networks as Trusted.

On the Docker host (Ubuntu here):

```bash
apt-get install -y nfs-common
mkdir -p /mnt/freepbx-monitor
echo "pbx.lan:/var/spool/asterisk/monitor /mnt/freepbx-monitor nfs ro,soft,timeo=50,retrans=2,_netdev,nofail,x-systemd.automount 0 0" >> /etc/fstab
systemctl daemon-reload && ls /mnt/freepbx-monitor
```

`nofail` and `x-systemd.automount` matter when both machines come back after a power cut: the Docker host boots and the agent starts even if the PBX isn't up yet, and the folder mounts the first time it's read.

Then in `docker-compose.yml` uncomment

```yaml
      - /mnt/freepbx-monitor:/recordings:ro
```

set `RECORDINGS_DIR=/recordings` in `.env`, and `docker compose up -d`. A call page should now have a player and a "Download with credit" link.

Docker can also mount NFS itself, which keeps everything in the compose file and needs nothing in `/etc/fstab`:

```yaml
services:
  phone-agent:
    volumes:
      - pbx-monitor:/recordings:ro
volumes:
  pbx-monitor:
    driver: local
    driver_opts:
      type: nfs
      o: addr=192.0.2.10,ro,soft,nfsvers=4
      device: ":/var/spool/asterisk/monitor"
```

I don't use it. If the PBX is down when the container starts, Docker can't create the volume and the agent doesn't start at all. The `fstab` way the agent starts anyway and the recordings appear once the PBX is back.

## 13. After reinstalling FreePBX

Updating modules (`fwconsole ma upgradeall`) or Debian packages leaves all of this alone. A major upgrade is usually a fresh install plus a FreePBX backup restore, and the backup brings back the dialplan in `extensions_custom.conf` and `globals_custom.conf`, the ARI and AMI users and the inbound routes. It doesn't bring back anything outside FreePBX itself. After a reinstall, redo:

- `/usr/local/bin/agent-recording-finish` (copy it from `asterisk/` and make it executable). Without it calls are still recorded, but not levelled and not listed in call history.
- The AudioSocket module, if the new Asterisk build doesn't load it.
- The NFS server and the line in `/etc/exports` (section 12).
- The test receptionist's six sound files (`custom/pa-test-1` to `pa-test-6`). Its dialplan is in `extensions_custom.conf` and comes back, but sound files copied in by hand aren't part of the backup unless you added them through System Recordings.
- The firewall: check the Docker host is still trusted, for ports 8088 (ARI), 9092 (AudioSocket, the other way) and 2049 (NFS).

Then run `check_setup` and a call to 7999. A missing piece shows up as a missing player on the call page, a call with no keypresses, or calls that never connect.

## Troubleshooting

`check_setup` tells you which piece is broken: Piper, Whisper, ARI or the model.

If calls originate but the far end hears nothing, `AUDIOSOCKET_ADVERTISE` is usually wrong, or the Docker host's firewall blocks port 9092 from the PBX.

If call pages have no player, check `ls /mnt/freepbx-monitor` on the Docker host. An empty folder usually means the PBX firewall blocks port 2049 or the export lists a different address (`exportfs -v` on the PBX).

If keypresses don't work, check the AMI user's `permit` line and that `webenabled = yes`. `asterisk -rx "manager show users"` should list it.

Calling your own trunk number from the PBX returns `486 Busy` on Internode rather than ringing back in. Test inbound from a mobile.

To see SIP headers such as Diversion, `asterisk -rx "pjsip set logger on"`, make a call, read `/var/log/asterisk/full`, and turn it off again: it logs every caller's number.

---

Part of [AI on the Line](https://github.com/darek-margas/ai-on-the-line) by Darek Margas. This page is licensed under [CC BY-SA 4.0](LICENSE.md).
