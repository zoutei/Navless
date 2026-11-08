#!/usr/bin/env python3
"""Hold the split flight/network lease and run a foreground transaction on one verified link."""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
SSH_BIN = '/usr/bin/ssh'
LOCAL_LOCK = Path('/tmp/drone19-domain219-flight.lock')
REMOTE_LOCK = '/tmp/drone19-domain219-flight-network.lock'
PROFILES = {
    'direct-dhcp': ('10.55.0.2','enP8p1s0','00:e0:4c:60:2f:90'),
    'wifi-only': ('192.168.68.56','wlP1p1s0','f4:d4:88:63:a5:59'),
}
MAC = r'(?:[0-9a-f]{1,2}:){5}[0-9a-f]{1,2}'
REMOTE_FACTS = '''set -e
hostname
printf '%s\n' "$SSH_CONNECTION"
ip -j address show
ip -j -4 route get "${SSH_CONNECTION%% *}"'''


def utc():
    return datetime.now(timezone.utc).isoformat()


def read(*cmd):
    return subprocess.check_output(cmd,text=True,timeout=8)


def mac(raw):
    if not re.fullmatch(MAC,raw.lower()):
        raise ValueError('invalid MAC address')
    return ':'.join(part.zfill(2) for part in raw.lower().split(':'))


def one(pattern,text):
    hits=re.findall(pattern,text,re.M|re.I)
    if len(hits)!=1:
        raise ValueError('missing or ambiguous interface evidence for '+pattern)
    return hits[0]


def validate(profile,client_mac,route,local,arp,facts,dhcp=''):
    ip, remote_iface, _ = PROFILES[profile]
    iface=one(r'^\s*interface:\s*(en\d+)\s*$',route)
    current_mac=mac(one(r'^\s*ether\s+(\S+)',local))
    hostname,connection,devices,routes=facts.splitlines()
    src,port,dest,sshport=connection.split()
    if hostname!='skybeta-19' or (dest,sshport)!=(ip,'22') or not 0<int(port)<65536:
        raise ValueError('SSH master does not terminate on the selected Jetson endpoint')
    if current_mac!=mac(client_mac) or src not in re.findall(r'^\s*inet\s+(\S+)',local,re.M):
        raise ValueError('SSH source does not match selected laptop interface')
    devs=[d for d in json.loads(devices) if d['ifname']==remote_iface and d['operstate']=='UP']
    if len(devs)!=1 or not any(a['local']==ip for a in devs[0]['addr_info']):
        raise ValueError('Jetson interface does not own the SSH server address')
    peer_mac=mac(devs[0]['address'])
    arp_mac=mac(one(r'\bat ('+MAC+r') on '+re.escape(iface)+r'\b',arp))
    if arp_mac!=peer_mac or (profile=='direct-dhcp' and peer_mac!='3c:6d:66:b5:1b:00'):
        raise ValueError('laptop ARP entry and authenticated Jetson interface MAC disagree')
    reverse=json.loads(routes)
    if len(reverse)!=1 or reverse[0].get('dev')!=remote_iface or reverse[0].get('prefsrc')!=ip:
        raise ValueError('Jetson return route does not match SSH interface and source')
    if profile=='direct-dhcp':
        if not int(ipaddress.IPv4Address('10.55.0.11'))<=int(ipaddress.IPv4Address(src))<=int(ipaddress.IPv4Address('10.55.0.254')):
            raise ValueError('SSH client address is outside the reviewed Ethernet DHCP pool')
        if not re.search(r'^\s*server_identifier \(ip\):\s*10\.55\.0\.2\s*$',dhcp,re.M) or not re.search(
                r'^\s*yiaddr\s*=\s*'+re.escape(src)+r'\s*$',dhcp,re.M):
            raise ValueError('DHCP packet does not match client address and server')
    return dict(profile=profile,client_ipv4=src,server_ipv4=ip,client_interface=iface,
                server_interface=remote_iface,client_mac=current_mac,server_mac=peer_mac,
                SSH_CONNECTION=connection,sampled_at_utc=utc())


def sample(profile,client_mac,ssh):
    ip=PROFILES[profile][0]
    route=read('/sbin/route','-n','get',ip)
    iface=one(r'^\s*interface:\s*(en\d+)\s*$',route)
    facts=read(*ssh,REMOTE_FACTS)
    packet=read('/usr/sbin/ipconfig','getpacket',iface) if profile=='direct-dhcp' else ''
    return validate(profile,client_mac,route,read('/sbin/ifconfig',iface),
                    read('/usr/sbin/arp','-n',ip),facts,packet)


@contextlib.contextmanager
def local_lease(path):
    fd=os.open(path,os.O_NOFOLLOW|os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def remote_lease(ssh,nonce,maintenance=None):
    # The same authenticated server arbitrates all cooperating Macs. EOF releases flock.
    action='printf "%s\\n" '+nonce+'; cat >/dev/null'
    if maintenance is not None:
        # The maintenance program itself inherits the server flock. A dropped SSH
        # connection cannot release it while the foreground program still runs.
        action='printf "%s\\n" '+nonce+'; read -r token && test "$token" = '+nonce+' && exec '+shlex.join(maintenance)
    cmd=f'umask 077; exec flock -n {REMOTE_LOCK} sh -c '+shlex.quote(action)
    with subprocess.Popen([*ssh,cmd],stdin=subprocess.PIPE,stdout=subprocess.PIPE) as worker:
        try:
            if not select.select([worker.stdout],[],[],8)[0] or worker.stdout.readline().decode().strip()!=nonce:
                raise RuntimeError('Jetson flight/network lease unavailable')
            yield worker
        finally:
            if not worker.stdin.closed:
                worker.stdin.close()
            try:
                worker.wait(timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()


def write_receipt(path,data):
    with path.open('x') as out:
        json.dump(data,out,indent=2)
        out.write('\n')


def authorize_current():
    env=os.environ
    receipt=json.loads(Path(env['SPLIT_TRANSPORT_RECEIPT']).read_text())
    if receipt['lease_id']!=env['SPLIT_TRANSPORT_LEASE_ID'] or not Path(env['SPLIT_TRANSPORT_ACTIVE']).is_file():
        raise RuntimeError('transaction no longer owns its flight/network lease')
    target=env['JETSON_SSH_TARGET']
    ssh=[SSH_BIN,'-S',env['JETSON_SSH_SOCKET'],'-o','ProxyCommand=/usr/bin/false','-o','BatchMode=yes',target]
    current=sample(receipt['profile'],receipt['client_mac'],ssh)
    for field in ('SSH_CONNECTION','server_ipv4','client_ipv4','server_mac','client_mac','server_interface','client_interface'):
        if current[field]!=receipt[field]:
            raise RuntimeError('verified transport changed: '+field)
    if env['JETSON_IP']!=current['server_ipv4']:
        raise ValueError('XRCE override disagrees with authenticated server')
    return current['server_ipv4']


def check_px4():
    ip=authorize_current()
    command="""from pathlib import Path
import sys
matches = [p for p in Path('/proc').glob('[0-9]*') if (p/'comm').is_file() and (p/'comm').read_text().strip()=='px4']
if len(matches)!=1: raise SystemExit('expected one PX4 SITL process')
raw=(matches[0]/'environ').read_bytes().split(b'\\0')
env=dict(v.decode().split('=',1) for v in raw if b'=' in v)
if any(env.get(k)!=v for k,v in zip(('JETSON_IP','PX4_PARAM_UXRCE_DDS_AG_IP','PX4_UXRCE_DDS_PORT','PX4_PARAM_UXRCE_DDS_DOM_ID'),sys.argv[1:])): raise SystemExit('running PX4 XRCE destination differs from leased SSH transport')
"""
    packed=int(ipaddress.IPv4Address(ip))
    if packed>=2**31:
        packed-=2**32
    subprocess.run(['docker','exec','drone19-split-sim-laptop-1','python3','-c',command,ip,str(packed),'8889','219'],check=True)


def legacy(sock,target,script,*args):
    ssh=[SSH_BIN,'-S',sock,'-o','BatchMode=yes','-o','ProxyCommand=/usr/bin/false',target]
    with remote_lease(ssh,uuid.uuid4().hex):
        return subprocess.call(['bash',script,*args],env=dict(os.environ,SPLIT_REMOTE_LEASE_HELD='1'))


def main(args):
    if not args.command or args.command[0]!='--' or len(args.command)<2:
        raise ValueError('supply -- followed by one foreground transaction')
    ip,_,default_mac=PROFILES[args.profile]
    expected_mac=args.client_mac or default_mac
    target='dronehacks@'+ip
    env=dict(os.environ)
    if env.get('JETSON_IP',ip)!=ip or env.get('JETSON_SSH_TARGET',target)!=target:
        raise ValueError('existing SSH/XRCE target conflicts with selected profile')
    args.evidence.mkdir(parents=True,exist_ok=False)
    evidence=args.evidence.resolve()
    with local_lease(LOCAL_LOCK) as fd, tempfile.TemporaryDirectory(prefix='split-link-',dir='/tmp') as raw:
        private=Path(raw)
        sock=str(private/'master')
        env.pop('SPLIT_FLIGHT_LOCK_HELD',None)
        env.update(SPLIT_TRANSPORT_TOOL=str(Path(__file__).resolve()),JETSON_SSH_TARGET=target,JETSON_SSH_SOCKET=sock,SPLIT_SSH_SOCKET=sock,JETSON_IP=ip,
                   JETSON_HOST_KEY_ALIAS='skybeta-19.local',JETSON_SSH_FALLBACK='',
                   SPLIT_VERIFIED_TRANSPORT=args.profile,SPLIT_TRANSPORT_RECEIPT=str(evidence/'transport.json'),
                   SPLIT_TRANSPORT_LOCK_FD=str(fd))
        # Make a new strict master using the same authentication setup as the split launcher.
        route=read('/sbin/route','-n','get',ip)
        iface=one(r'^\s*interface:\s*(en\d+)\s*$',route)
        local=read('/sbin/ifconfig',iface)
        if mac(one(r'^\s*ether\s+(\S+)',local))!=mac(expected_mac):
            raise ValueError('route is not using the reviewed client adapter')
        mux=[SSH_BIN,'-S',sock,'-o','ProxyCommand=/usr/bin/false','-o','BatchMode=yes']
        attach=[*mux,target]
        init='source "$1"; SSH+=(-o "ProxyCommand=/usr/bin/nc -b $2 %h %p"); split_ensure_ssh'
        try:
            subprocess.run(['bash','-ec',init,'init',str(args.split/'split_network.sh'),iface],env=env,check=True)
            nonce=uuid.uuid4().hex
            maintenance=args.command[1:] if args.purpose=='network' else None
            with remote_lease(attach,nonce,maintenance) as lease:
                active=private/'acquired'
                active.touch(mode=0o600)
                env.update(SPLIT_TRANSPORT_ACTIVE=str(active),SPLIT_TRANSPORT_LEASE_ID=nonce)
                record=sample(args.profile,expected_mac,attach)
                record.update(schema=1,lease_id=nonce,acquired_utc=utc(),purpose=args.purpose,
                              baseline=args.baseline,run_id=evidence.name,name=args.name)
                write_receipt(evidence/'transport.json',record)
                write_receipt(evidence/'run-comparison.json',dict(schema=1,run_id=evidence.name,name=args.name,
                    kind='bench',baseline_id=args.baseline,changes=[dict(field='Mac–Jetson transport',
                    before=args.before,after=record['profile'],evidence=['transport.json'])],
                    notes=['Participants: Mac split runner and authenticated skybeta-19. Verification covers the SSH tuple, both routes and adapter identities.'],
                    confounds=['This receipt does not measure ROS delivery or establish a flight outcome.'],evidence=['transport.json']))
                if maintenance is not None:
                    lease.stdin.write((nonce+'\n').encode())
                    lease.stdin.flush()
                    lease.stdin.close()
                    shutil.copyfileobj(lease.stdout,sys.stdout.buffer)
                    rc=lease.wait()
                    write_receipt(evidence/'lease-end.json',dict(lease_id=nonce,ended_utc=utc(),exit_code=rc))
                    return rc
                # Legacy helpers include ssh/scp commands using the historical hostname.
                # OpenSSH must never reconnect to it after the verified master exits.
                for tool in ('ssh','scp'):
                    shim=private/tool
                    shim.write_text('#!/bin/sh\ntest -f '+str(active)+' || exit 125\nexec /usr/bin/'+tool+' -o ProxyCommand=/usr/bin/false'
                                    ' -o BatchMode=yes -o StrictHostKeyChecking=yes -o HostKeyAlias=skybeta-19.local'
                                    ' -o ControlPath='+sock+' "$@"\n')
                    shim.chmod(0o700)
                env['PATH']=str(private)+os.pathsep+env['PATH']
                with subprocess.Popen(args.command[1:],env=env,pass_fds=(fd,)) as work:
                    next_sample=time.monotonic()+10
                    while work.poll() is None:
                        reason=None
                        if active.exists() and lease.poll() is not None:
                            reason='Jetson lease channel closed'
                        elif active.exists() and time.monotonic()>=next_sample:
                            try:
                                now=sample(args.profile,expected_mac,attach)
                                for field in ('SSH_CONNECTION','client_interface','server_interface','client_mac','server_mac'):
                                    if now[field]!=record[field]:
                                        raise ValueError('changed '+field)
                            except (OSError,ValueError,subprocess.SubprocessError) as exc:
                                reason=str(exc)
                            next_sample=time.monotonic()+10
                        if reason:
                            active.unlink()
                            write_receipt(evidence/'lease-lost.json',dict(lease_id=nonce,observed_utc=utc(),reason=reason))
                            print('Transport verification lost. Further SSH/SCP calls are disabled.',file=sys.stderr)
                        time.sleep(.25)
                    rc=work.returncode if active.exists() and lease.poll() is None else 125
                write_receipt(evidence/'lease-end.json',dict(lease_id=nonce,ended_utc=utc(),exit_code=rc))
                return rc
        finally:
            subprocess.run([*mux,'-O','exit',target],capture_output=True,timeout=5)


if __name__=='__main__':
    if sys.argv[1:2]==['--legacy-flight']:
        sys.exit(legacy(*sys.argv[2:]))
    if sys.argv[1:]==['--check-active']:
        print(authorize_current())
        sys.exit(0)
    if sys.argv[1:]==['--check-px4']:
        check_px4()
        sys.exit(0)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True,choices=PROFILES)
    parser.add_argument('--purpose',required=True,choices=('flight','network'))
    parser.add_argument('--evidence',required=True,type=Path)
    parser.add_argument('--name',required=True)
    parser.add_argument('--baseline',required=True)
    parser.add_argument('--before',required=True,help='observed baseline transport, or unknown')
    parser.add_argument('--client-mac')
    parser.add_argument('--split',type=Path,default=HERE)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    try:
        sys.exit(main(parser.parse_args()))
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        sys.exit(str(error))
