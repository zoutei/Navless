import argparse
import json
import os
import sys
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import with_transport as link


def fixture(profile):
    ip,iface,localmac=link.PROFILES[profile]
    src='10.55.0.168' if profile=='direct-dhcp' else '192.168.68.23'
    peer='3c:6d:66:b5:1b:00' if profile=='direct-dhcp' else '02:00:00:00:00:ab'
    device=[dict(ifname=iface,operstate='UP',address=peer,addr_info=[dict(local=ip)])]
    reverse=[dict(dev=iface,prefsrc=ip)]
    facts='\n'.join(['skybeta-19',f'{src} 52411 {ip} 22',json.dumps(device),json.dumps(reverse)])
    return dict(profile=profile,client_mac=localmac,route=' interface: en7\n',
                local=f' ether {localmac}\n inet {src} netmask 0xffffff00\n',
                arp=f'? ({ip}) at {peer} on en7 ifscope [ethernet]',facts=facts,
                dhcp=f'yiaddr = {src}\nserver_identifier (ip): {ip}\n')


class TransportTests(unittest.TestCase):
    def test_both_profiles_and_retired_address(self):
        for profile in link.PROFILES:
            self.assertEqual(link.validate(**fixture(profile))['server_ipv4'],link.PROFILES[profile][0])
        f=fixture('direct-dhcp')
        f['facts']=f['facts'].replace('10.55.0.2','169.254.55.2')
        with self.assertRaises(ValueError): link.validate(**f)

    def test_misdirected_master_adapter_arp_reverse_and_dhcp(self):
        for field,old,new in [('facts','10.55.0.168','10.55.0.167'),('facts','enP8p1s0','wlan0'),
                              ('facts','"prefsrc": "10.55.0.2"','"prefsrc": "10.55.0.3"'),
                              ('client_mac','2f:90','2f:91'),('arp','3c:6d','4c:6d'),
                              ('route','en7','en9'),('dhcp','10.55.0.2','10.55.0.3')]:
            with self.subTest(field=field):
                f=fixture('direct-dhcp')
                f[field]=f[field].replace(old,new)
                with self.assertRaises(ValueError): link.validate(**f)

    @unittest.skipUnless(shutil.which('flock'),'requires Linux/BSD flock executable')
    def test_separate_client_shared_lease_and_maintenance_payload(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            with mock.patch.object(link,'REMOTE_LOCK',str(root/'remote.lock')):
                with link.remote_lease(['sh','-c'],'first'):
                    with self.assertRaises(RuntimeError):
                        with link.remote_lease(['sh','-c'],'second',['touch',str(root/'unexpected')]): pass
                    self.assertFalse((root/'unexpected').exists())
                output=root/'maintained'
                with link.remote_lease(['sh','-c'],'third',['touch',str(output)]) as worker:
                    self.assertFalse(output.exists())
                    worker.stdin.write(b'third\n'); worker.stdin.flush()
                    self.assertEqual(worker.wait(timeout=4),0)
                    self.assertTrue(output.exists())

    def test_local_lock_conflicts_with_unmodified_legacy_flight(self):
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/'legacy.lock'
            with link.local_lease(path):
                with self.assertRaises(BlockingIOError):
                    with link.local_lease(path): pass
            with link.local_lease(path): pass

    @unittest.skipUnless(shutil.which('flock'),'requires Linux/BSD flock executable')
    def test_foreground_work_receipt_and_lease_lifetime(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as raw:
            root=Path(raw)
            fake=root/'ssh-fixture'
            fake.write_text('#!/bin/bash\n[[ "$*" == *"-O exit"* ]] && exit 0\n/bin/sh -c "${!#}"\n')
            fake.chmod(0o700)
            (root/'split_network.sh').write_text('SSH=(); split_ensure_ssh() { :; }')
            args=argparse.Namespace(profile='wifi-only',client_mac=None,purpose='flight',evidence=root/'run-id',
                 command=['--',sys.executable,'-c',
                 "import os,sys; from pathlib import Path; "
                 "assert os.environ['JETSON_IP']=='192.168.68.56'; "
                 "assert os.environ['SPLIT_SSH_SOCKET']==os.environ['JETSON_SSH_SOCKET']; "
                 "assert Path(os.environ['SPLIT_TRANSPORT_ACTIVE']).is_file(); "
                 "assert os.fstat(int(os.environ['SPLIT_TRANSPORT_LOCK_FD'])); sys.exit(13)"],
                 name='Two-host fixture',baseline='before',before='old path',split=root)
            facts=fixture('wifi-only')
            def local_read(cmd,*_):
                return facts['route'] if cmd.endswith('route') else facts['local']
            with mock.patch.object(link,'SSH_BIN',str(fake)),mock.patch.object(link,'LOCAL_LOCK',root/'a'), \
                 mock.patch.object(link,'REMOTE_LOCK',str(root/'b')),mock.patch.object(link,'read',local_read), \
                 mock.patch.object(link,'sample',return_value=link.validate(**facts)),mock.patch.dict(os.environ,{},clear=False):
                for key in ('JETSON_IP','JETSON_SSH_TARGET'):
                    os.environ.pop(key,None)
                self.assertEqual(link.main(args),13)
            receipt=json.loads((args.evidence/'transport.json').read_text())
            end=json.loads((args.evidence/'lease-end.json').read_text())
            self.assertEqual((receipt['lease_id'],13),(end['lease_id'],end['exit_code']))
            self.assertEqual(json.loads((args.evidence/'run-comparison.json').read_text())['baseline_id'],'before')

    def test_no_target_contact_from_source_or_offline_preview(self):
        result=subprocess.run(['bash','-c','source ./split_network.sh'],cwd=link.HERE,capture_output=True)
        self.assertEqual((result.returncode,result.stdout,result.stderr),(0,b'',b''))


if __name__=='__main__':
    unittest.main()
