"""Create a separate working tree without copying completed formal runs."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    destination = args.destination.resolve()
    if destination.exists():
        raise FileExistsError(destination)
    names = ['research/evacuation', 'research/data', 'research/plans', 'research/frozen',
             'research/tests', 'tools', 'audit/implementation_gate.json', 'audit/time_budget_review.md',
             'requirements.txt']
    names += [str(p.relative_to(root)) for p in (root / 'research').glob('*.py')]
    names += ['research/runs/' + n for n in
              ('known_answers_v5', 'equivalence_v3', 'design_validation_v3', 'guardrails_v1')]
    for name in names:
        if not (root / name).exists():
            raise FileNotFoundError(root / name)
    destination.mkdir(parents=True)
    for name in names:
        source, target = root / name, destination / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns('budget_sensitivity_v1.json')
                            if name == 'research/plans' else None)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    print(f'Working copy created at {destination}. No solver was started.')


if __name__ == '__main__':
    main()
