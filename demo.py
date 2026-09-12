"""Run a synthetic batch, idempotent replay, and warehouse report."""
import json
from pathlib import Path
import tempfile
from pipeline import ingest, report


def main():
    root = Path(__file__).parent
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / 'demo.db'
        args = (root / 'examples/orders.csv', root / 'examples/orders.contract.json', db)
        first = ingest(*args)
        second = ingest(*args)
        assert (first['accepted'], first['rejected']) == (3, 3)
        assert second['skipped']
        summary = report(db)
        assert summary['record_count'] == 3
        print(json.dumps({'accepted': first['accepted'], 'rejected': first['rejected'],
                          'replay_skipped': second['skipped'], 'records': summary['record_count']}, indent=2))


if __name__ == '__main__':
    main()
