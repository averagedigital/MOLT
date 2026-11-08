"""Conservative search spaces and predeclared scoring rules."""
from dataclasses import dataclass
import math
import statistics


@dataclass(frozen=True)
class Awg31:
    version: str = '3.1'
    jc: int = 4
    jmin: int = 40
    jmax: int = 100
    mtu: int = 1280
    paddings: tuple = (12, 12, 12, 12)
    headers: tuple = ((1, 1), (2, 2), (3, 3), (4, 4))
    header_protection: bool = True

    def validate(self):
        values = [self.jc, self.jmin, self.jmax, self.mtu, *self.paddings,
                  *(v for pair in self.headers for v in pair)]
        if any(type(v) is not int for v in values):
            raise ValueError('AWG wire values must be integers')
        if self.version != '3.1':
            raise ValueError('Only the reviewed AWG 3.1 subset is supported')
        # Conservative research bounds, not the complete wire-format limits.
        if not (0 <= self.jc <= 12 and 0 <= self.jmin <= self.jmax <= 1200):
            raise ValueError('Invalid junk packet bounds')
        if not 1280 <= self.mtu <= 1420:
            raise ValueError('MTU outside conservative research bounds')
        if len(self.paddings) != 4 or any(not 0 <= s <= 64 for s in self.paddings):
            raise ValueError('Invalid S1-S4')
        if self.header_protection and min(self.paddings) < 12:
            raise ValueError('Header protection needs S1-S4 >= 12')
        if len(self.headers) != 4:
            raise ValueError('Four header ranges required')
        for i, (lo, hi) in enumerate(self.headers):
            if not 0 <= lo <= hi <= 2**32 - 1:
                raise ValueError('Invalid uint32 header range')
            for other_lo, other_hi in self.headers[:i]:
                if max(lo, other_lo) <= min(hi, other_hi):
                    raise ValueError('Header ranges overlap')

    def export(self):
        self.validate()
        lines = [f'Jc = {self.jc}', f'Jmin = {self.jmin}', f'Jmax = {self.jmax}']
        lines += [f'S{i} = {s}' for i, s in enumerate(self.paddings, 1)]
        lines += [f'H{i} = {lo}-{hi}' for i, (lo, hi) in enumerate(self.headers, 1)]
        return '# Parameter fragment; merge matching S/H into both peers.\n' + '\n'.join(lines) + '\n'


def client_config(outbound, params, port):
    tls = outbound.get('tls', {})
    if not tls.get('enabled') or tls.get('insecure'):
        raise ValueError('Verified TLS is mandatory')
    if outbound.get('type', 'hysteria2') != 'hysteria2':
        raise ValueError('Only hysteria2 runtime implemented')
    out = dict(outbound, type='hysteria2', tag='experiment', **params)
    return {'log': {'disabled': True},
            'inbounds': [{'type': 'socks', 'listen': '127.0.0.1', 'listen_port': port}],
            'outbounds': [out], 'route': {'final': 'experiment'}}


def objective(m, overhead_limit=.2):
    if not m.get('ok') or (m.get('overhead') is not None and m['overhead'] > overhead_limit):
        return 0.0
    values = [m['download_mbps'], m['upload_mbps']]
    if any(not math.isfinite(v) or v <= 0 for v in values):
        return 0.0
    return min(values)


def summarize(rows):
    if not rows or not all(r.get('ok') for r in rows):
        return {'ok': False, 'reason': 'one_or_more_repeats_failed', 'samples': rows}
    result = {'ok': True, 'samples': rows}
    for key in ('download_mbps', 'upload_mbps', 'ttfb_ms', 'request_ms'):
        values = [r[key] for r in rows]
        result[key] = statistics.median(values)
        result[key + '_range'] = [min(values), max(values)]
    return result
