"""VPS-only Optuna controller; JSON lines carry metrics, never credentials."""
import json
import sqlite3
import sys
import time
import optuna
from .core import objective


def run(db, profile, seed, trials, repeats, direct_controls=False):
    if not 1 <= trials <= 100 or not 2 <= repeats <= 10:
        raise ValueError('trials 1..100; finalist repeats 2..10')
    optuna.logging.set_verbosity(optuna.logging.ERROR)
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE IF NOT EXISTS measurements (run TEXT, profile TEXT, strategy TEXT, '
                'seed INTEGER, phase TEXT, timestamp REAL, params TEXT, metrics TEXT)')
    run_id = str(time.time_ns())
    con.execute('CREATE TABLE IF NOT EXISTS runs (run TEXT PRIMARY KEY, completed INTEGER, config TEXT)')
    con.execute('INSERT INTO runs VALUES (?,?,?)', (run_id, 0, json.dumps({
        'seed': seed, 'trials_per_strategy': trials, 'confirmations': repeats,
        'direct_controls': direct_controls, 'optuna_version': optuna.__version__,
        'objective': 'min(median_download_mbps, median_upload_mbps)',
        'overhead_constraint': 'unmeasured'})))
    con.commit()
    def evaluate(strategy, phase, params):
        print(json.dumps({'params': params, 'phase': phase}), flush=True)
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError('Mac disconnected; experiment stopped')
        metrics = json.loads(line)
        con.execute('INSERT INTO measurements VALUES (?,?,?,?,?,?,?,?)',
                    (run_id, profile, strategy, seed, phase, time.time(), json.dumps(params), json.dumps(metrics)))
        con.commit()
        return objective(metrics)
    studies = {}
    for name, sampler in [('random', optuna.samplers.RandomSampler(seed=seed)),
                          ('tpe', optuna.samplers.TPESampler(seed=seed, n_startup_trials=min(4, max(1, trials // 3))))]:
        studies[name] = optuna.create_study(sampler=sampler, direction='maximize')
    evaluate('default', 'baseline', {})
    if direct_controls:
        evaluate('direct', 'baseline', None)
    for i in range(trials):
        # Alternating first algorithm reduces systematic ordering bias.
        for name in (['random', 'tpe'] if i % 2 == 0 else ['tpe', 'random']):
            study = studies[name]
            trial = study.ask()
            params = {'up_mbps': trial.suggest_int('up_mbps', 5, 100, step=5),
                      'down_mbps': trial.suggest_int('down_mbps', 5, 100, step=5)}
            study.tell(trial, evaluate(name, 'search', params))
        evaluate('default', 'control', {})
        if direct_controls:
            evaluate('direct', 'control', None)
    for _ in range(repeats):
        for name, study in studies.items():
            evaluate(name, 'confirmation', study.best_params)
        evaluate('default', 'confirmation', {})
        if direct_controls:
            evaluate('direct', 'confirmation', None)
    con.execute('UPDATE runs SET completed=1 WHERE run=?', (run_id,))
    con.commit()
    print(json.dumps({'done': True, 'run': run_id}), flush=True)
    con.close()
