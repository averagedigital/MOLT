import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import statistics
from .core import Awg31, objective


def compare(db, run_id=None):
    con = sqlite3.connect(db)
    if run_id is None:
        row = con.execute('SELECT run FROM measurements ORDER BY timestamp DESC LIMIT 1').fetchone()
        if not row:
            raise ValueError('No measurements')
        run_id = row[0]
    rows = con.execute('SELECT strategy, phase, metrics FROM measurements WHERE run=?', (run_id,)).fetchall()
    report = {'run': run_id, 'warning': 'Small sample; no inference about DPI or algorithm superiority', 'strategies': {}}
    for name in ('default', 'direct', 'random', 'tpe'):
        samples = [json.loads(m) for s, p, m in rows if s == name and p == 'confirmation']
        valid = [objective(m) for m in samples if objective(m) > 0]
        report['strategies'][name] = {'attempts': len(samples), 'successes': len(valid),
            'median_bottleneck_mbps': statistics.median(valid) if valid else None,
            'range_mbps': [min(valid), max(valid)] if valid else None}
    con.close()
    return report


def main():
    os.umask(0o077)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda number, frame: sys.exit(128 + number))
    p = argparse.ArgumentParser(description='Network Strategy Compiler research prototype')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    sub.add_parser('inspect')
    sub.add_parser('awg-export')
    opt = sub.add_parser('optimize', help='VPS JSON-lines controller; use agent on Mac')
    opt.add_argument('--db', required=True)
    opt.add_argument('--profile', required=True, help='Opaque network label; no SSID/IP')
    opt.add_argument('--seed', type=int, default=42)
    opt.add_argument('--trials', type=int, default=12, help='Per algorithm')
    opt.add_argument('--confirmations', type=int, default=3)
    opt.add_argument('--direct-controls', action='store_true')
    agent = sub.add_parser('agent')
    agent.add_argument('--settings', required=True)
    agent.add_argument('--ssh', required=True, help='Existing SSH alias')
    agent.add_argument('--ssh-config')
    agent.add_argument('--remote-dir', required=True)
    agent.add_argument('--profile', required=True)
    agent.add_argument('--trials', type=int, default=12)
    agent.add_argument('--seed', type=int, default=42)
    agent.add_argument('--executor-ssh')
    agent.add_argument('--executor-ssh-config')
    agent.add_argument('--executor-dir')
    sub.add_parser('worker', help='Single measurement over JSON stdin/stdout')
    for name in ('compare', 'export'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--db', required=True)
        cmd.add_argument('--run')
    endpoint = sub.add_parser('endpoint')
    endpoint.add_argument('--port', type=int, default=18081)
    endpoint.add_argument('--proof-file', required=True)
    endpoint.add_argument('--bytes', type=int, default=1048576)
    endpoint.add_argument('--lifetime', type=int, default=3600)
    endpoint.add_argument('--listen', default='127.0.0.1')
    endpoint.add_argument('--allow-client')
    args = p.parse_args()
    if args.command == 'doctor':
        print(json.dumps({name: bool(shutil.which(name)) for name in ('sing-box', 'curl', 'ssh', 'awg', 'amneziawg-go')}))
        if shutil.which('sing-box'):
            subprocess.run(['sing-box', 'version'], check=True)
    elif args.command == 'inspect':
        print('Hysteria2: up_mbps/down_mbps 5..100 step 5; TLS required. AWG: fragment validation only, no tunnel automation.')
    elif args.command == 'awg-export':
        print(Awg31().export())
    elif args.command == 'optimize':
        from .experiment import run
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', args.profile):
            p.error('Use an opaque alphanumeric profile label')
        run(args.db, args.profile, args.seed, args.trials, args.confirmations, args.direct_controls)
    elif args.command == 'agent':
        from .measure import measure, execute
        settings = json.loads(Path(args.settings).read_text())
        if not 2 <= settings['repeats'] <= 10 or not 65536 <= settings['bytes'] <= 8 * 1048576:
            p.error('repeats 2..10; bytes 64 KiB..8 MiB')
        settings['engine_version'] = subprocess.check_output(['sing-box', 'version'], text=True).splitlines()[0]
        command = 'cd ' + shlex.quote(args.remote_dir) + ' && .venv/bin/nsc optimize --db results.sqlite --profile ' + shlex.quote(args.profile)
        command += f' --seed {args.seed} --trials {args.trials}'
        if settings.get('direct_endpoint'):
            command += ' --direct-controls'
        ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2']
        if args.ssh_config:
            ssh += ['-F', args.ssh_config]
        proc = subprocess.Popen(ssh + [args.ssh, command], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            for line in proc.stdout:
                request = json.loads(line)
                if request.get('done'):
                    print(json.dumps(request))
                    break
                try:
                    if args.executor_ssh:
                        if not args.executor_dir:
                            p.error('--executor-dir required')
                        worker = ['ssh', '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2']
                        if args.executor_ssh_config:
                            worker += ['-F', args.executor_ssh_config]
                        directory = shlex.quote(args.executor_dir)
                        remote = f'cd {directory} && PATH="$PWD:$PATH" PYTHONPATH=src timeout 300 python3 -m nsc.cli worker'
                        reply = execute(worker + [args.executor_ssh, remote],
                            input=json.dumps({'settings': settings, 'params': request['params']}).encode(),
                            timeout=310, alive=lambda: proc.poll() is None)
                        if reply.returncode:
                            raise RuntimeError('executor_failed')
                        result = json.loads(reply.stdout)
                    else:
                        result = measure(settings, request['params'], alive=lambda: proc.poll() is None)
                except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
                    result = {'ok': False, 'reason': str(error) if isinstance(error, RuntimeError) else type(error).__name__}
                result.setdefault('engine_version', settings['engine_version'])
                print(json.dumps({'phase': request['phase'], 'ok': result['ok']}), file=sys.stderr)
                proc.stdin.write(json.dumps(result) + '\n')
                proc.stdin.flush()
            proc.stdin.close()
            if proc.wait(timeout=15):
                raise RuntimeError('Remote optimizer failed')
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=15)
    elif args.command == 'worker':
        from .measure import measure
        payload = json.load(sys.stdin)
        try:
            result = measure(payload['settings'], payload['params'])
        except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
            result = {'ok': False, 'reason': str(error) if isinstance(error, RuntimeError) else type(error).__name__}
        result['engine_version'] = subprocess.check_output(['sing-box', 'version'], text=True).splitlines()[0]
        print(json.dumps(result))
    elif args.command == 'compare':
        print(json.dumps(compare(args.db, args.run), indent=2))
    elif args.command == 'export':
        report = compare(args.db, args.run)
        con = sqlite3.connect(args.db)
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
            p.error('Experiment incomplete: missing completion record')
        completed = con.execute('SELECT completed FROM runs WHERE run=?', (report['run'],)).fetchone()
        if not completed or completed[0] != 1:
            p.error('Experiment incomplete; export refused')
        eligible = {k: v for k, v in report['strategies'].items() if k in ('default', 'random', 'tpe') and v['attempts'] >= 3 and v['successes'] == v['attempts']}
        if not eligible:
            p.error('No independently confirmed candidate')
        winner = max(eligible, key=lambda k: eligible[k]['median_bottleneck_mbps'])
        params = con.execute("SELECT params FROM measurements WHERE run=? AND strategy=? AND phase='confirmation' LIMIT 1", (report['run'], winner)).fetchone()[0]
        print(json.dumps({'engine': 'hysteria2', 'params': json.loads(params), 'run': report['run']}))
    elif args.command == 'endpoint':
        from .endpoint import serve
        if not 65536 <= args.bytes <= 8 * 1048576 or not 1 <= args.lifetime <= 14400:
            p.error('Endpoint size/lifetime outside bounds')
        signal.signal(signal.SIGALRM, lambda number, frame: sys.exit(0))
        signal.alarm(args.lifetime)
        if args.listen != '127.0.0.1' and not args.allow_client:
            p.error('Non-loopback endpoint requires --allow-client')
        serve(args.port, Path(args.proof_file).read_text().strip(), args.bytes, args.lifetime, args.listen, args.allow_client)


if __name__ == '__main__':
    main()
