#!/usr/bin/env python3
"""Render transport edits into an EMPTY staging folder for review. Never change a live install."""
import argparse
from pathlib import Path
import shutil

HERE=Path(__file__).resolve().parent


def once(text,old,new):
    if text.count(old)!=1:
        raise ValueError('unrecognized launcher around '+old)
    return text.replace(old,new)


def patched(name,s):
    if name=='run_split_course.sh':
        s=once(s,'lock=os.open("/tmp/drone19-domain219-flight.lock",os.O_CREAT|os.O_RDWR,0o600)',
               '''path="/tmp/drone19-domain219-flight.lock"
inherited=os.environ.get("SPLIT_TRANSPORT_LOCK_FD")
lock=int(inherited) if inherited else os.open(path,os.O_CREAT|os.O_RDWR,0o600)
if not os.path.samestat(os.fstat(lock),os.stat(path)): raise SystemExit("incorrect flight lease")''')
        s=once(s,'mkdir "$run/flight"', '''mkdir "$run/flight"
if [[ -n "${SPLIT_VERIFIED_TRANSPORT:-}" ]]; then
  python3 "${SPLIT_TRANSPORT_TOOL:?}" --check-px4
  cp "${SPLIT_TRANSPORT_RECEIPT:?}" "$run/transport.json"
fi''')
        # Both generations have an established SSH master at this point.
        at='container=drone19-split-sim-laptop-1'
        s=once(s,at,'''if [[ -z "${SPLIT_VERIFIED_TRANSPORT:-}" && -z "${SPLIT_REMOTE_LEASE_HELD:-}" ]]; then
  exec python3 "$PWD/with_transport.py" --legacy-flight "$SOCKET" "${TARGET:-dronehacks@skybeta-19.local}" "$0" "$@"
fi
'''+at)
    elif name=='split_network.sh':
        at='  local candidate connection'
        s=once(s,at,at+'''
  if [[ -n "${SPLIT_VERIFIED_TRANSPORT:-}" ]]; then
    python3 "${SPLIT_TRANSPORT_TOOL:?}" --check-active
    return
  fi''')
    elif name=='start.sh':
        s=once(s,'|| echo "Forward $address may already exist; verify the viewer below."', '''|| {
      if [[ -n "${SPLIT_VERIFIED_TRANSPORT:-}" ]]; then
        echo "Private session could not acquire forward $address" >&2; exit 1
      fi
      echo "Forward $address may already exist; verify the viewer below."
    }''')
    elif name=='start_px4_from_mac.sh':
        at="  echo 'PX4 SITL already runs in the laptop simulation container.'"
        s=once(s,at,'''  if [[ -n "${SPLIT_VERIFIED_TRANSPORT:-}" ]]; then
    python3 "${SPLIT_TRANSPORT_TOOL:?}" --check-px4
  fi
'''+at)
    elif name=='prepare_split.sh':
        at='  manifest="$(shasum'
        s=once(s,at,'''  if [[ -s "$run/transport.json" ]]; then
    [[ -n "${SPLIT_TRANSPORT_TOOL:-}" ]] || return 1
    cmp "$run/transport.json" "${SPLIT_TRANSPORT_RECEIPT:?}" || return 1
    python3 "$SPLIT_TRANSPORT_TOOL" --check-active >/dev/null || return 1
    python3 "$SPLIT_TRANSPORT_TOOL" --check-px4 || return 1
  fi
'''+at)
    return s


FILES=('run_split_course.sh','split_network.sh','start.sh','start_px4_from_mac.sh','prepare_split.sh')


def stage(src,dest):
    data={}
    for name in FILES:
        original=(src/name).read_text()
        marker={'run_split_course.sh':'--legacy-flight','split_network.sh':'--check-active',
                'start.sh':'Private session could not acquire forward',
                'start_px4_from_mac.sh':'--check-px4','prepare_split.sh':'--check-active'}[name]
        data[name]=original if marker in original else patched(name,original)
    dest.mkdir(parents=True,exist_ok=False)
    for name,s in data.items():
        (dest/name).write_text(s)
    shutil.copy2(HERE/'with_transport.py',dest/'with_transport.py')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source',type=Path)
    p.add_argument('empty_output',type=Path)
    args=p.parse_args()
    stage(args.source,args.empty_output)
