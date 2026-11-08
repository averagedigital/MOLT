import json
import pytest
from nsc.core import Awg31, objective, summarize, client_config


def test_awg_rejects_incompatible_headers_and_padding():
    with pytest.raises(ValueError):
        Awg31(headers=((1, 5), (5, 6), (7, 8), (9, 10))).validate()
    with pytest.raises(ValueError):
        Awg31(paddings=(0, 12, 12, 12), header_protection=True).validate()
    with pytest.raises(ValueError):
        Awg31(jmin=50, jmax=40).validate()
    with pytest.raises(ValueError):
        Awg31(mtu=1500).validate()


def test_awg_valid_and_export_has_no_secrets():
    a = Awg31()
    a.validate()
    assert 'HeaderProtectionKey' not in a.export()
    assert 'Jc = 4' in a.export()


def test_objective_excludes_failures_and_uses_bottleneck():
    assert objective({'ok': False}) == 0
    assert objective({'ok': True, 'download_mbps': 20, 'upload_mbps': 5}) == 5
    assert objective({'ok': True, 'download_mbps': 20, 'upload_mbps': 5, 'overhead': .3}, .2) == 0


def test_stability_requires_all_repeats():
    assert not summarize([{'ok': True}, {'ok': False}])['ok']


def test_client_has_no_direct_fallback():
    c = client_config({'server': 'example.invalid', 'server_port': 443,
                       'password': 'test', 'tls': {'enabled': True, 'server_name': 'example.invalid'}},
                      {'up_mbps': 10, 'down_mbps': 20}, 18080)
    assert c['route']['final'] == 'experiment'
    assert len(c['outbounds']) == 1
    assert c['inbounds'][0]['listen'] == '127.0.0.1'
    with pytest.raises(ValueError):
        client_config({'tls': {'enabled': True, 'insecure': True}}, {}, 18080)


def test_controller_balances_budgets_and_persists_failures(tmp_path, monkeypatch):
    import io
    import sqlite3
    from nsc.experiment import run
    db = tmp_path / 'results.sqlite'
    # 1 baseline + 2*(2 candidates + control) + 3*(2 finalists + control).
    count = 16
    monkeypatch.setattr('sys.stdin', io.StringIO('\n'.join([json.dumps({'ok': False, 'reason': 'unreachable'})] * count) + '\n'))
    run(str(db), 'opaque-network', 42, 2, 3)
    con = sqlite3.connect(db)
    assert con.execute('SELECT count(*) FROM measurements').fetchone()[0] == count
    assert con.execute("SELECT strategy,count(*) FROM measurements WHERE phase='search' GROUP BY strategy").fetchall() == [('random', 2), ('tpe', 2)]
    from nsc.cli import compare
    report = compare(db)
    assert report['strategies']['tpe']['successes'] == 0
    assert report['strategies']['tpe']['median_bottleneck_mbps'] is None


@pytest.mark.parametrize('kwargs', [{'jc': 1.5}, {'jc': True}, {'paddings': (12.0, 12, 12, 12)}, {'headers': ((True, 1), (2, 2), (3, 3), (4, 4))}])
def test_awg_rejects_non_integer_wire_values(kwargs):
    with pytest.raises(ValueError):
        Awg31(**kwargs).validate()


def test_report_excludes_objective_rejected_results(tmp_path):
    import sqlite3
    from nsc.cli import compare
    db = tmp_path / 'metrics.sqlite'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE measurements(run, strategy, phase, metrics, timestamp)')
    metrics = {'ok': True, 'download_mbps': 10, 'upload_mbps': 10, 'overhead': .5}
    con.execute('INSERT INTO measurements VALUES (?,?,?,?,?)', ('x','tpe','confirmation',json.dumps(metrics),1))
    con.commit()
    assert compare(db)['strategies']['tpe']['successes'] == 0


def test_measurement_command_stops_if_controller_dies():
    import sys
    from nsc.measure import execute
    with pytest.raises(RuntimeError, match='controller_disconnected'):
        execute([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=10, alive=lambda: False)


def test_endpoint_lifetime_bounds_a_partial_request(tmp_path):
    import socket
    import subprocess
    import sys
    import time
    proof = tmp_path / 'proof'
    proof.write_text('test-proof')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    proc = subprocess.Popen([sys.executable, '-m', 'nsc.cli', 'endpoint', '--port', str(port),
                             '--proof-file', str(proof), '--lifetime', '1'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    connection = None
    try:
        for _ in range(50):
            try:
                connection = socket.create_connection(('127.0.0.1', port), timeout=.1)
                break
            except OSError:
                time.sleep(.02)
        assert connection is not None
        connection.sendall(b'POST /upload HTTP/1.0\r\nContent-Length: 1048576\r\n\r\nx')
        proc.wait(timeout=3)
    finally:
        if connection:
            connection.close()
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)


def test_measurement_reuses_port_after_previous_engine_exit(monkeypatch):
    import socket
    from nsc.measure import measure
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    listener.listen()
    client = socket.create_connection(('127.0.0.1', port))
    accepted, _ = listener.accept()
    accepted.close()
    client.recv(1)
    client.close()
    listener.close()
    monkeypatch.setattr('nsc.measure.subprocess.run', lambda *a, **kw: None)
    def launch(*a, **kw):
        raise RuntimeError('engine_launch_reached')
    monkeypatch.setattr('nsc.measure.subprocess.Popen', launch)
    settings = {'socks_port':port, 'endpoint':'http://127.0.0.1:18081',
                'outbound': {'tls': {'enabled':True}}}
    with pytest.raises(RuntimeError, match='engine_launch_reached'):
        measure(settings, {})


def test_direct_control_is_explicit_and_has_its_own_records(tmp_path, monkeypatch):
    import io
    import sqlite3
    from nsc.experiment import run
    monkeypatch.setattr('sys.stdin', io.StringIO((json.dumps({'ok':False})+'\n')*22))
    db=tmp_path/'direct.sqlite'
    run(str(db),'opaque',42,2,3,direct_controls=True)
    con=sqlite3.connect(db)
    rows=con.execute("SELECT params FROM measurements WHERE strategy='direct'").fetchall()
    assert len(rows)==6
    assert all(json.loads(p) is None for (p,) in rows)


def test_export_keeps_default_if_search_does_not_improve_it(tmp_path):
    import sqlite3
    import subprocess
    import sys
    db=tmp_path/'export.sqlite'
    con=sqlite3.connect(db)
    con.execute('CREATE TABLE measurements(run,strategy,phase,metrics,timestamp,params)')
    con.execute('CREATE TABLE runs(run,completed)')
    con.execute('INSERT INTO runs VALUES (?,?)', ('r',1))
    for strategy,speed,params in [('default',20,{}),('tpe',10,{'up_mbps':10})]:
        for i in range(3):
            con.execute('INSERT INTO measurements VALUES (?,?,?,?,?,?)',
                        ('r',strategy,'confirmation',json.dumps({'ok':True,'download_mbps':speed,'upload_mbps':speed}),i,json.dumps(params)))
    con.commit()
    result=subprocess.run([sys.executable,'-m','nsc.cli','export','--db',str(db)],capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)['params']=={}


def test_export_rejects_interrupted_confirmation_round(tmp_path):
    import sqlite3
    import subprocess
    import sys
    db=tmp_path/'incomplete.sqlite'
    con=sqlite3.connect(db)
    con.execute('CREATE TABLE measurements(run,strategy,phase,metrics,timestamp,params)')
    con.execute('CREATE TABLE runs(run,completed)')
    con.execute('INSERT INTO runs VALUES (?,?)',('r',0))
    for strategy,count in [('random',3),('tpe',2),('default',2)]:
        for i in range(count):
            con.execute('INSERT INTO measurements VALUES (?,?,?,?,?,?)',
                        ('r',strategy,'confirmation',json.dumps({'ok':True,'download_mbps':10,'upload_mbps':10}),i,'{}'))
    con.commit()
    result=subprocess.run([sys.executable,'-m','nsc.cli','export','--db',str(db)],capture_output=True,text=True)
    assert result.returncode != 0
    assert 'incomplete' in result.stderr.lower()
