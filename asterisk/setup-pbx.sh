#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
#
# Sets up an Asterisk or FreePBX server for AI on the Line, and checks it.
# Run it on the PBX as root:
#
#   ./setup-pbx.sh --agent-host 192.168.1.20            set up for outgoing calls
#   ./setup-pbx.sh --agent-host 192.168.1.20 --screener --token <API_TOKEN>
#                                                     ...and the call screener
#   ./setup-pbx.sh --check                            only check, change nothing
#   ./setup-pbx.sh --uninstall                        remove what it added
#
# What it changes, all in its own files so nothing of yours is edited:
#   /etc/asterisk/ai-on-the-line-*.conf, each pulled in with one #include line
#   (in the *_custom.conf files on FreePBX, the main .conf files otherwise);
#   an ARI user and an AMI user called ai-on-the-line; the [phone-agent] dialplan
#   context; /usr/local/bin/agent-recording-finish. On FreePBX also the HTTP/ARI
#   settings and a firewall trust entry for the agent's host.
# At the end it prints the lines for the agent's .env and saves them in
# /root/ai-on-the-line.env (readable by root only). Running it again keeps the
# passwords, so the agent's .env stays valid.

set -u
VERSION=1
ETC=/etc/asterisk
NAME=ai-on-the-line
SAVED=/root/ai-on-the-line.env
FINISH=/usr/local/bin/agent-recording-finish
RAW=https://raw.githubusercontent.com/darek-margas/ai-on-the-line/main/asterisk

AGENT_HOST="" AGENT_PORT=8090 SOCKET_PORT=9092 TOKEN="" SCREENER=0 DIAL_CONTEXT=""
FALLBACK="" MODE=install DRY=0 NFS=0

usage() { sed -n '4,22p' "$0" | sed 's/^# \{0,1\}//'; cat <<'EOF'

Options:
  --agent-host HOST     the Docker host running the agent, as the PBX reaches it
  --agent-port N        the agent's web port (default 8090)
  --socket-port N       the agent's AudioSocket port (default 9092)
  --dial-context CTX    the context outgoing calls go through (default from-internal
                        on FreePBX; required on plain Asterisk)
  --screener            also set up incoming calls: test extension 7990 and the
                        [ai-on-the-line-screen] context to use as a destination
  --token TOKEN         the agent's API_TOKEN (needed with --screener)
  --fallback CTX,EXT,PRI  where screened callers go if the agent can't answer,
                        e.g. ext-local,vmu101,1 for 101's voicemail
  --nfs                 share the recordings folder read-only with the agent's host
  --check               only check the setup
  --uninstall           remove everything this script added
  --dry-run             show what would change, change nothing
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --agent-host) AGENT_HOST="$2"; shift ;;
        --agent-port) AGENT_PORT="$2"; shift ;;
        --socket-port) SOCKET_PORT="$2"; shift ;;
        --dial-context) DIAL_CONTEXT="$2"; shift ;;
        --screener) SCREENER=1 ;;
        --token) TOKEN="$2"; shift ;;
        --fallback) FALLBACK="$2"; shift ;;
        --nfs) NFS=1 ;;
        --check) MODE=check ;;
        --uninstall) MODE=uninstall ;;
        --dry-run) DRY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; WARNINGS=$((WARNINGS + 1)); }
bad()  { printf '  \033[31mfail\033[0m  %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
step() { printf '\n%s\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then echo "  would run: $*"; else "$@"; fi; }
WARNINGS=0 FAILURES=0

[ "$(id -u)" = 0 ] || { echo "Run this as root on the PBX." >&2; exit 1; }
command -v asterisk >/dev/null || { echo "Asterisk isn't installed here." >&2; exit 1; }
ast() { asterisk -rx "$1" 2>/dev/null; }
ast "core show version" | grep -q Asterisk || { echo "Asterisk isn't running (asterisk -rx failed)." >&2; exit 1; }

FREEPBX=0
command -v fwconsole >/dev/null && FREEPBX=1
AST_VERSION=$(ast "core show version" | sed -n 's/^Asterisk \([0-9][0-9.]*\).*/\1/p' | head -1)
AST_MAJOR=${AST_VERSION%%.*}

# Where our #include lines go.
if [ "$FREEPBX" = 1 ]; then
    INC_EXT=$ETC/extensions_custom.conf
    INC_ARI=$ETC/ari_additional_custom.conf
    INC_AMI=$ETC/manager_custom.conf
    [ -n "$DIAL_CONTEXT" ] || DIAL_CONTEXT=from-internal
else
    INC_EXT=$ETC/extensions.conf
    INC_ARI=$ETC/ari.conf
    INC_AMI=$ETC/manager.conf
fi
F_EXT=$ETC/$NAME-extensions.conf
F_ARI=$ETC/$NAME-ari.conf
F_AMI=$ETC/$NAME-manager.conf

# --- helpers -----------------------------------------------------------------

saved() {  # a value from the last run, so passwords survive a re-run
    [ -f "$SAVED" ] && sed -n "s/^$1=//p" "$SAVED" | tail -1
}

newpass() { head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32; }

add_include() {  # add_include <file> <included file>
    local line="#include $2"
    [ -f "$1" ] || { [ "$DRY" = 1 ] && { echo "  would create $1"; return; }; touch "$1"; }
    if grep -qxF "$line" "$1"; then
        ok "$1 already includes $(basename "$2")"
    elif [ "$DRY" = 1 ]; then
        echo "  would add to $1: $line"
    else
        printf '\n; AI on the Line, added by setup-pbx.sh\n%s\n' "$line" >> "$1"
        ok "added #include $(basename "$2") to $1"
    fi
}

remove_include() {
    [ -f "$1" ] || return 0
    if grep -q "$(basename "$2")" "$1"; then
        run sed -i "\|^; AI on the Line, added by setup-pbx.sh\$|d; \|^#include $2\$|d; \|^include => ai-on-the-line-test\$|d" "$1"
        ok "removed the #include from $1"
    fi
}

write_file() {  # write_file <path> <mode>; content on stdin
    local tmp; tmp=$(mktemp)
    cat > "$tmp"
    if [ "$DRY" = 1 ]; then
        echo "  would write $1:"; sed 's/^/      /; s/\(secret\|password\) *=.*/\1 = (hidden)/' "$tmp"; rm -f "$tmp"; return
    fi
    install -m "$2" -o root -g "$(id -gn asterisk 2>/dev/null || echo root)" "$tmp" "$1"
    rm -f "$tmp"
    ok "wrote $1"
}

context_elsewhere() {  # is [ctx] defined in a dialplan file that isn't ours?
    grep -lE "^\[$1\]" $ETC/extensions*.conf 2>/dev/null | grep -v "$F_EXT" | head -1
}

# --- checks (used by every mode) -----------------------------------------------

check() {
    step "Asterisk"
    if [ "${AST_MAJOR:-0}" -ge 18 ] 2>/dev/null; then ok "Asterisk $AST_VERSION"; else bad "Asterisk $AST_VERSION: 18 or newer is needed for AudioSocket"; fi
    [ "$FREEPBX" = 1 ] && ok "FreePBX $(fwconsole -V 2>/dev/null | sed -n 's/.*\([0-9][0-9]*\.[0-9.]*\).*/\1/p' | head -1)"

    for m in res_audiosocket chan_audiosocket; do
        if ast "module show like $m" | grep -q "$m.so"; then ok "$m loaded"
        else bad "$m isn't loaded (asterisk -rx 'module load $m.so'; on FreePBX also check Admin > Asterisk Modules)"; fi
    done
    ast "module show like res_ari.so" | grep -q res_ari.so && ok "res_ari loaded" || bad "res_ari isn't loaded"
    ast "module show like func_curl" | grep -q func_curl && ok "func_curl loaded (screener)" || warn "func_curl isn't loaded: the screener needs it"

    step "HTTP, ARI and AMI"
    local http; http=$(ast "http show status")
    if echo "$http" | grep -q "Server Enabled"; then
        ok "HTTP server: $(echo "$http" | sed -n 's/.*Server Enabled and Bound to \(.*\)/\1/p' | head -1)"
        echo "$http" | grep -q "Bound to 127.0.0.1" && bad "HTTP only listens on 127.0.0.1: the agent's host can't reach ARI"
        echo "$http" | grep -q "/ari/" && ok "ARI is served" || bad "ARI isn't served over HTTP"
        echo "$http" | grep -q "/rawman" && ok "AMI over HTTP (/rawman) is served" || warn "AMI over HTTP isn't served: keypresses fall back to in-band tones (manager.conf: webenabled = yes)"
    else
        bad "Asterisk's HTTP server isn't running"
    fi
    if ast "ari show users" | grep -qw "$NAME"; then ok "ARI user $NAME exists"; else bad "no ARI user $NAME"; fi
    if ast "manager show user $NAME" | grep -q "username: $NAME"; then ok "AMI user $NAME exists"; else warn "no AMI user $NAME"; fi

    step "Dialplan"
    if ast "dialplan show phone-agent" | grep -q "AudioSocket"; then ok "[phone-agent] context loaded"; else bad "[phone-agent] context not loaded"; fi
    if [ -n "$DIAL_CONTEXT" ]; then
        if ast "dialplan show $DIAL_CONTEXT" | grep -q "Context '$DIAL_CONTEXT'"; then ok "outgoing calls go through [$DIAL_CONTEXT]"
        else bad "context [$DIAL_CONTEXT] for outgoing calls doesn't exist"; fi
    fi
    if ast "dialplan show ai-on-the-line-screen" | grep -q "Context"; then ok "screener destination [ai-on-the-line-screen] loaded"; fi
    [ -x "$FINISH" ] && ok "$FINISH installed" || warn "$FINISH missing: recordings won't be levelled or listed in call history"
    command -v sox >/dev/null && ok "sox installed" || warn "sox missing: recordings won't be levelled (apt-get install sox)"

    step "Recordings"
    local mon=/var/spool/asterisk/monitor
    [ -d "$mon/phone-agent" ] && ok "$mon/phone-agent exists ($(ls "$mon/phone-agent" 2>/dev/null | grep -c '\.wav$') files)" || ok "$mon/phone-agent will be created by the first call"
    if command -v exportfs >/dev/null && exportfs -v 2>/dev/null | grep -q "$mon"; then ok "NFS export of $mon: $(exportfs -v | grep "$mon" | awk '{print $2}' | head -1)"
    else ok "no NFS export of $mon (optional: --nfs, or see the guide)"; fi

    if [ -n "$AGENT_HOST" ]; then
        step "The agent at $AGENT_HOST"
        if command -v curl >/dev/null && curl -s -m 4 -o /dev/null "http://$AGENT_HOST:$AGENT_PORT/"; then ok "web port $AGENT_PORT reachable"
        else warn "can't reach http://$AGENT_HOST:$AGENT_PORT/ from here (agent not started yet, or a firewall)"; fi
        if (exec 3<>"/dev/tcp/$AGENT_HOST/$SOCKET_PORT") 2>/dev/null; then ok "AudioSocket port $SOCKET_PORT reachable"
        else warn "can't reach $AGENT_HOST:$SOCKET_PORT from here (agent not started yet, or a firewall)"; fi
    fi
}

# --- uninstall -----------------------------------------------------------------

if [ "$MODE" = uninstall ]; then
    step "Removing AI on the Line from this PBX"
    remove_include "$INC_EXT" "$F_EXT"; remove_include "$INC_ARI" "$F_ARI"; remove_include "$INC_AMI" "$F_AMI"
    for f in "$F_EXT" "$F_ARI" "$F_AMI"; do [ -f "$f" ] && run rm -f "$f" && ok "removed $f"; done
    if [ "$FREEPBX" = 1 ]; then run fwconsole reload >/dev/null; else run asterisk -rx "core reload" >/dev/null; fi
    echo; echo "Left in place: $FINISH, $SAVED, recordings, and any NFS export. The HTTP/ARI settings are unchanged."
    exit 0
fi

if [ "$MODE" = check ]; then
    [ -n "$DIAL_CONTEXT" ] || DIAL_CONTEXT=$(saved DIAL_CONTEXT)
    [ -n "$AGENT_HOST" ] || AGENT_HOST=$(saved AGENT_HOST)
    check
    printf '\n%d failed, %d warnings.\n' "$FAILURES" "$WARNINGS"
    exit $((FAILURES > 0))
fi

# --- install -------------------------------------------------------------------

[ -n "$AGENT_HOST" ] || { echo "--agent-host is required (the Docker host running the agent). See --help." >&2; exit 2; }
[ -n "$DIAL_CONTEXT" ] || { echo "On plain Asterisk, give --dial-context: the context your phones dial out through." >&2; exit 2; }
if [ "$SCREENER" = 1 ] && [ -z "$TOKEN" ]; then TOKEN=$(saved PHONE_AGENT_TOKEN); fi
[ "$SCREENER" = 0 ] || [ -n "$TOKEN" ] || { echo "--screener needs --token (the agent's API_TOKEN)." >&2; exit 2; }

for ctx in phone-agent phone-agent-inbound; do
    other=$(context_elsewhere "$ctx")
    if [ -n "$other" ]; then
        echo "[$ctx] is already defined in $other. Remove it there first (it looks like a manual setup), or keep the manual setup and skip this script." >&2
        exit 1
    fi
done

AGENT_IP=$(getent hosts "$AGENT_HOST" | awk '{print $1}' | head -1)
[ -n "$AGENT_IP" ] || AGENT_IP=$AGENT_HOST
ARI_PASS=$(saved ARI_PASSWORD); [ -n "$ARI_PASS" ] || ARI_PASS=$(newpass)
AMI_PASS=$(saved AMI_PASSWORD); [ -n "$AMI_PASS" ] || AMI_PASS=$(newpass)
PBX_ADDR=$(ip route get "$AGENT_IP" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)
[ -n "$PBX_ADDR" ] || PBX_ADDR=$(hostname -I | awk '{print $1}')

echo "AI on the Line PBX setup v$VERSION: $([ "$FREEPBX" = 1 ] && echo FreePBX || echo plain Asterisk) $AST_VERSION, agent at $AGENT_HOST ($AGENT_IP)$([ "$DRY" = 1 ] && echo ', dry run')"

step "Modules"
for m in res_audiosocket chan_audiosocket; do
    if ast "module show like $m" | grep -q "$m.so"; then ok "$m loaded"
    else run asterisk -rx "module load $m.so" >/dev/null
        ast "module show like $m" | grep -q "$m.so" && ok "loaded $m" || bad "$m isn't available in this Asterisk build"
    fi
done
if [ "$FREEPBX" = 1 ] && [ -f $ETC/modules.conf ] && grep -qE "^noload *=> *(res|chan)_audiosocket" $ETC/modules*.conf 2>/dev/null; then
    warn "AudioSocket is in a noload line in modules.conf; it won't survive a restart until that line goes"
fi

step "HTTP and ARI"
if [ "$FREEPBX" = 1 ]; then
    for kv in HTTPENABLED=1 HTTPBINDADDRESS=0.0.0.0 ENABLE_ARI=1; do
        k=${kv%%=*}; v=${kv#*=}
        cur=$(fwconsole setting "$k" 2>/dev/null | sed -n 's/.*: *//p' | tail -1)
        if [ "$cur" = "$v" ] || { [ "$k" != HTTPBINDADDRESS ] && [ "$cur" = true ]; }; then ok "$k already $cur"
        elif run fwconsole setting "$k" "$v" >/dev/null 2>&1; then ok "set $k to $v (was ${cur:-unset})"
        else warn "couldn't set $k: set it in Settings > Advanced Settings"; fi
    done
else
    ast "http show status" | grep -q "Server Enabled" && ok "HTTP server enabled" \
        || bad "the HTTP server is off: set enabled = yes and bindaddr = 0.0.0.0 in [general] of $ETC/http.conf"
    grep -qE "^\s*enabled\s*=\s*yes" $ETC/ari.conf 2>/dev/null && ok "ARI enabled" \
        || bad "ARI is off: set enabled = yes in [general] of $ETC/ari.conf"
fi
write_file "$F_ARI" 640 <<EOF
; AI on the Line: ARI user the agent places calls with. Written by setup-pbx.sh.
[$NAME]
type = user
read_only = no
password = $ARI_PASS
password_format = plain
EOF
add_include "$INC_ARI" "$F_ARI"

step "AMI user for keypresses"
write_file "$F_AMI" 640 <<EOF
; AI on the Line: AMI user that sends real keypresses (DTMF). Written by setup-pbx.sh.
[$NAME]
secret = $AMI_PASS
deny = 0.0.0.0/0.0.0.0
permit = $AGENT_IP/255.255.255.255
read = call
write = call
webenabled = yes
EOF
add_include "$INC_AMI" "$F_AMI"

step "Dialplan"
{
cat <<'EOF'
; AI on the Line dialplan. Written by setup-pbx.sh; re-run it instead of editing.
;
; [phone-agent]: the agent originates calls over ARI; when the far end answers,
; the call lands here, is recorded (both sides together and each on its own),
; and its audio goes to the agent over AudioSocket at 16 kHz (AUDIO_RATE=16000).
[phone-agent]
exten => s,1,NoOp(AI on the Line call ${AGENT_UUID})
 same => n,Set(CDR(userfield)=phone-agent:${AGENT_UUID})
 same => n,Set(AGENT_DEST=${CUT(CHANNEL,/,2)})
 same => n,Set(AGENT_RECDIR=/var/spool/asterisk/monitor/${STRFTIME(${EPOCH},,%Y/%m/%d)})
 same => n,Set(AGENT_RECNAME=agent-out-${FILTER(0-9A-Za-z,${CUT(AGENT_DEST,@,1)})}-${STRFTIME(${EPOCH},,%Y%m%d-%H%M%S)}-${AGENT_UUID}.wav)
 same => n,Set(CDR(recordingfile)=${AGENT_RECNAME})
 same => n,MixMonitor(phone-agent/${AGENT_UUID}.wav,r(/var/spool/asterisk/monitor/phone-agent/${AGENT_UUID}-rx.wav)t(/var/spool/asterisk/monitor/phone-agent/${AGENT_UUID}-tx.wav),/usr/local/bin/agent-recording-finish ${AGENT_UUID} ${AGENT_RECDIR} ${AGENT_RECNAME})
 same => n,Dial(AudioSocket/${AGENT_SOCKET}/${AGENT_UUID}/c(slin16))
 same => n,Hangup()
EOF
if [ "$SCREENER" = 1 ]; then
cat <<'EOF'

; [phone-agent-inbound]: tell the agent a call is coming (who, forwarded from
; where, which profile), then connect the audio. If the agent is down or busy,
; the caller goes to AGENT_FALLBACK, or hears a short apology. The agent's
; address and token are set here unless globals_custom.conf already has them.
[phone-agent-inbound]
EOF
cat <<EOF
exten => s,1,ExecIf(\$["\${PHONE_AGENT_URL}" = ""]?Set(PHONE_AGENT_URL=http://$AGENT_HOST:$AGENT_PORT))
 same => n,ExecIf(\$["\${PHONE_AGENT_SOCKET}" = ""]?Set(PHONE_AGENT_SOCKET=$AGENT_HOST:$SOCKET_PORT))
 same => n,ExecIf(\$["\${PHONE_AGENT_TOKEN}" = ""]?Set(PHONE_AGENT_TOKEN=$TOKEN))
EOF
cat <<'EOF'
 same => n,Set(AGENT_UUID=${FILTER(0-9a-f-,${SHELL(cat /proc/sys/kernel/random/uuid)})})
 same => n,Set(AGENT_PROFILE=${IF($["${AGENT_PROFILE}" = ""]?screener:${AGENT_PROFILE})})
 same => n,Set(AGENT_DIVERTED=)
 same => n,ExecIf($["${CHANNEL(channeltype)}" = "PJSIP"]?Set(AGENT_DIVERTED=${PJSIP_HEADER(read,Diversion)}))
 same => n,Set(CURLOPT(httpheader)=Authorization: Bearer ${PHONE_AGENT_TOKEN})
 same => n,Set(CURLOPT(conntimeout)=3)
 same => n,Set(CURLOPT(httptimeout)=10)
 same => n,Set(AGENT_REPLY=${CURL(${PHONE_AGENT_URL}/inbound,uuid=${AGENT_UUID}&profile=${AGENT_PROFILE}&caller=${URIENCODE(${CALLERID(num)})}&diverted=${URIENCODE(${AGENT_DIVERTED})})})
 same => n,GotoIf($["${AGENT_REPLY}" != "ok"]?unavailable)
 same => n,Set(CDR(userfield)=phone-agent-in:${AGENT_UUID})
 same => n,Set(AGENT_RECDIR=/var/spool/asterisk/monitor/${STRFTIME(${EPOCH},,%Y/%m/%d)})
 same => n,Set(AGENT_RECNAME=agent-in-${FILTER(0-9A-Za-z,${CALLERID(num)})}-${STRFTIME(${EPOCH},,%Y%m%d-%H%M%S)}-${AGENT_UUID}.wav)
 same => n,Set(CDR(recordingfile)=${AGENT_RECNAME})
 same => n,MixMonitor(phone-agent/${AGENT_UUID}.wav,r(/var/spool/asterisk/monitor/phone-agent/${AGENT_UUID}-rx.wav)t(/var/spool/asterisk/monitor/phone-agent/${AGENT_UUID}-tx.wav),/usr/local/bin/agent-recording-finish ${AGENT_UUID} ${AGENT_RECDIR} ${AGENT_RECNAME})
 same => n,Dial(AudioSocket/${PHONE_AGENT_SOCKET}/${AGENT_UUID}/c(slin16))
 same => n,Hangup()
 same => n(unavailable),NoOp(The agent did not take the call: ${AGENT_REPLY})
 same => n,GotoIf($["${AGENT_FALLBACK}" != ""]?${AGENT_FALLBACK})
 same => n,Answer()
 same => n,Playback(im-sorry&cannot-complete-temp-error)
 same => n,Hangup()
EOF
cat <<EOF

; [ai-on-the-line-screen]: the screener as a destination (inbound route, time
; condition, IVR). On FreePBX add it under Admin > Custom Destinations as
; ai-on-the-line-screen,s,1.
[ai-on-the-line-screen]
exten => s,1,Set(AGENT_FALLBACK=$FALLBACK)
 same => n,Goto(phone-agent-inbound,s,1)

; Test extension: dial 7990 from a handset to talk to the screener.
[ai-on-the-line-test]
exten => 7990,1,Goto(phone-agent-inbound,s,1)
EOF
fi
} | write_file "$F_EXT" 640
add_include "$INC_EXT" "$F_EXT"
if [ "$SCREENER" = 1 ]; then
    if [ "$FREEPBX" = 1 ]; then
        if grep -q "include => ai-on-the-line-test" $ETC/extensions_custom.conf 2>/dev/null; then ok "7990 already reachable from extensions"
        elif [ "$DRY" = 1 ]; then echo "  would add [from-internal-custom] include => ai-on-the-line-test"
        else printf '\n[from-internal-custom]\ninclude => ai-on-the-line-test\n' >> $ETC/extensions_custom.conf; ok "7990 reachable from extensions"; fi
    else
        warn "add 'include => ai-on-the-line-test' to the context your phones use, to reach test extension 7990"
    fi
fi

step "Recording finisher"
SRC="$(dirname "$0")/agent-recording-finish"
if [ -f "$SRC" ]; then run install -m 755 "$SRC" "$FINISH" && ok "installed $FINISH"
elif command -v curl >/dev/null; then run curl -fsSL -o "$FINISH" "$RAW/agent-recording-finish" && run chmod 755 "$FINISH" && ok "downloaded $FINISH"
else warn "couldn't install $FINISH: copy it from the repo's asterisk folder"; fi
command -v sox >/dev/null && ok "sox installed" || warn "sox missing (apt-get install sox): recordings won't be levelled"
run install -d -m 775 -o asterisk -g asterisk /var/spool/asterisk/monitor/phone-agent 2>/dev/null

if [ "$FREEPBX" = 1 ] && fwconsole firewall list trusted >/dev/null 2>&1; then
    step "FreePBX firewall"
    if fwconsole firewall list trusted 2>/dev/null | grep -qw "$AGENT_IP"; then ok "$AGENT_IP is already trusted"
    else run fwconsole firewall trust "$AGENT_IP" >/dev/null && ok "trusted $AGENT_IP (ARI, AMI and NFS from the agent's host)"; fi
fi

if [ "$NFS" = 1 ]; then
    step "Sharing recordings over NFS"
    command -v exportfs >/dev/null || run apt-get install -y nfs-kernel-server >/dev/null
    uid=$(id -u asterisk) gid=$(id -g asterisk)
    line="/var/spool/asterisk/monitor $AGENT_IP(ro,all_squash,anonuid=$uid,anongid=$gid,no_subtree_check,sync)"
    if grep -qF "$line" /etc/exports 2>/dev/null; then ok "already exported to $AGENT_IP"
    elif [ "$DRY" = 1 ]; then echo "  would add to /etc/exports: $line"
    else echo "$line" >> /etc/exports && exportfs -ra && ok "exported read-only to $AGENT_IP"; fi
fi

step "Reloading"
if [ "$FREEPBX" = 1 ]; then run fwconsole reload >/dev/null && ok "fwconsole reload"
else
    for c in "module reload res_ari.so" "manager reload" "dialplan reload"; do run asterisk -rx "$c" >/dev/null; done
    ok "reloaded ARI, AMI and the dialplan"
fi

if [ "$DRY" = 0 ]; then
    umask 077
    cat > "$SAVED" <<EOF
# AI on the Line, written by setup-pbx.sh on $(date '+%Y-%m-%d %H:%M'). Copy the
# lines below into the agent's .env. The rest is for the next run of the script.
ARI_URL=http://$PBX_ADDR:8088/ari
ARI_USER=$NAME
ARI_PASSWORD=$ARI_PASS
AMI_URL=http://$PBX_ADDR:8088/rawman
AMI_USER=$NAME
AMI_PASSWORD=$AMI_PASS
DIAL_ENDPOINT=Local/{number}@$DIAL_CONTEXT/n
AUDIOSOCKET_ADVERTISE=$AGENT_HOST:$SOCKET_PORT
AUDIO_RATE=16000
$([ "$NFS" = 1 ] && echo "RECORDINGS_DIR=/recordings")
# --- used by setup-pbx.sh ---
AGENT_HOST=$AGENT_HOST
DIAL_CONTEXT=$DIAL_CONTEXT
$([ "$SCREENER" = 1 ] && echo "PHONE_AGENT_TOKEN=$TOKEN")
EOF
fi

check
printf '\n%d failed, %d warnings.\n' "$FAILURES" "$WARNINGS"
if [ "$DRY" = 0 ]; then
    echo
    echo "For the agent's .env (also saved in $SAVED):"
    echo
    sed -n '/^ARI_URL/,/^# ---/p' "$SAVED" | grep -v '^# ---' | grep -v '^$'
    if [ "$NFS" = 1 ]; then
        echo
        echo "On the agent's host, mount the recordings and add the volume to docker-compose.yml:"
        echo "  echo '$PBX_ADDR:/var/spool/asterisk/monitor /mnt/freepbx-monitor nfs ro,soft,timeo=50,retrans=2,_netdev,nofail,x-systemd.automount 0 0' >> /etc/fstab"
        echo "  mkdir -p /mnt/freepbx-monitor && systemctl daemon-reload"
        echo "  volumes: - /mnt/freepbx-monitor:/recordings:ro"
    fi
    [ "$SCREENER" = 1 ] && [ "$FREEPBX" = 1 ] && echo && echo "Screener: dial 7990 to try it. To route callers to it, add Admin > Custom Destinations > ai-on-the-line-screen,s,1."
fi
exit $((FAILURES > 0))
