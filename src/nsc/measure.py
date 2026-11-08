"""Measurement executor. Direct controls are explicit; VPN requests never fall back."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from .core import client_config, summarize


def execute(args, timeout, alive=lambda: True, input=None):
    proc = subprocess.Popen(args, stdin=subprocess.PIPE if input is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if not alive():
                raise RuntimeError('controller_disconnected')
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(args[0], timeout)
            try:
                stdout, stderr = proc.communicate(input=input, timeout=.2)
                return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                input = None
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def curl(url, proxy, upload=None, alive=lambda: True):
    args = ['curl', '--silent', '--fail', '--noproxy', '', '--connect-timeout', '5',
            '--max-time', '20', '--proxy', proxy, '--output', os.devnull,
            '--write-out', '%{json}', url]
    if upload:
        args += ['--data-binary', '@' + upload]
    result = execute(args, timeout=25, alive=alive)
    if result.returncode:
        raise RuntimeError('curl_exit_' + str(result.returncode))
    return json.loads(result.stdout)


def measure(settings, params, alive=lambda: True):
    if params is None:
        with tempfile.TemporaryDirectory(prefix='nsc-direct-') as directory:
            return transfer(settings, settings['direct_endpoint'].rstrip('/'), '', directory, time.monotonic(), alive)
    port = settings['socks_port']
    base = settings['endpoint'].rstrip('/')
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='nsc-') as directory:
        config = Path(directory) / 'client.json'
        config.write_text(json.dumps(client_config(settings['outbound'], params, port)))
        config.chmod(0o600)
        subprocess.run(['sing-box', 'check', '-c', str(config)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        # Refuse a pre-existing listener; never accidentally measure another proxy.
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', port))
        proc = subprocess.Popen(['sing-box', 'run', '-c', str(config)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError('engine_exited')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
            else:
                raise RuntimeError('proxy_start_timeout')
            proxy = f'socks5h://127.0.0.1:{port}'
            return transfer(settings, base, proxy, directory, started, alive)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def transfer(settings, base, proxy, directory, started, alive):
    proof = execute(['curl', '--silent', '--fail', '--noproxy', '', '--proxy', proxy,
                     '--max-time', '10', base + '/proof'], timeout=12, alive=alive)
    if proof.returncode or proof.stdout.decode() != settings['proof']:
        raise RuntimeError('route_proof_failed')
    setup_ms = (time.monotonic() - started) * 1000
    upload = Path(directory) / 'upload'
    upload.write_bytes(os.urandom(settings['bytes']))
    rows = []
    for _ in range(settings['repeats']):
        try:
            down = curl(base + '/download', proxy, alive=alive)
            up = curl(base + '/upload', proxy, str(upload), alive=alive)
            if down['size_download'] != settings['bytes'] or up['size_upload'] != settings['bytes']:
                raise RuntimeError('byte_count_mismatch')
            rows.append({'ok': True, 'download_mbps': down['speed_download'] * 8 / 1e6,
                         'upload_mbps': up['speed_upload'] * 8 / 1e6,
                         'ttfb_ms': down['time_starttransfer'] * 1000,
                         'request_ms': down['time_total'] * 1000})
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            rows.append({'ok': False, 'reason': str(error) if isinstance(error, RuntimeError) else 'transfer_timeout'})
    return dict(summarize(rows), setup_ms=setup_ms, overhead=None, payload_bytes=settings['bytes'], repeats=settings['repeats'],
                overhead_constraint='unmeasured')
