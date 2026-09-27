"""Verify released files and the recorded experiment identities without solving."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    manifest = read(ROOT / 'release_manifest.json')
    for item in manifest['files']:
        path = (ROOT / item['path']).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ValueError(f'Missing or invalid path: {item["path"]}')
        if path.stat().st_size != item['bytes'] or sha(path) != item['sha256']:
            raise ValueError(f'Integrity mismatch: {item["path"]}')
    freeze = read(ROOT / 'research/frozen/formal_v1/manifest.json')
    for name, value in freeze['source_hashes'].items():
        if sha(ROOT / name) != value:
            raise ValueError(f'Frozen source mismatch: {name}')
    runs = 0
    for plan_path in sorted((ROOT / 'research/plans').glob('*.json')):
        plan = read(plan_path)
        for entry in plan['runs']:
            directory = ROOT / 'research/runs' / plan_path.stem / entry['id']
            provenance = read(directory / 'provenance.json')
            if provenance['plan_sha256'] != sha(plan_path) or provenance['entry'] != entry:
                raise ValueError(f'Plan mismatch: {entry["id"]}')
            for name, value in provenance['source_hashes'].items():
                if sha(ROOT / 'research' / name.replace('\\', '/')) != value:
                    raise ValueError(f'Source mismatch: {entry["id"]}')
            instance = read(directory / 'instance.json')
            instance_hash = hashlib.sha256(json.dumps(instance, sort_keys=True).encode()).hexdigest()
            if read(directory / 'result.json')['instance_sha256'] != instance_hash:
                raise ValueError(f'Instance mismatch: {entry["id"]}')
            runs += 1
    if runs != manifest['formal_runs']:
        raise ValueError('Formal run count differs')
    print(f'Verified {len(manifest["files"])} files and {runs} formal run identities. No solver was started.')


if __name__ == '__main__':
    main()
