import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pipeline import ingest, load_contract, report, validate


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'warehouse.db'
        self.contract = Path('examples/orders.contract.json')
        self.csv = Path('examples/orders.csv')

    def write(self, rows):
        file = self.root / 'input.csv'
        file.write_text('order_id,date,region,quantity,unit_price\n' + rows, encoding='utf-8')
        return file

    def test_valid_rows_and_quarantine(self):
        result = ingest(self.csv, self.contract, self.db)
        self.assertEqual((result['accepted'], result['rejected']), (3, 3))
        self.assertEqual(report(self.db)['record_count'], 3)
        self.assertEqual(report(self.db)['quarantined_count'], 3)

    def test_same_file_is_idempotent(self):
        first = ingest(self.csv, self.contract, self.db)
        second = ingest(self.csv, self.contract, self.db)
        self.assertTrue(second['skipped'])
        self.assertEqual(first['batch_id'], second['batch_id'])
        self.assertEqual(len(report(self.db)['batches']), 1)

    def test_update_by_key_and_exact_decimal(self):
        ingest(self.csv, self.contract, self.db)
        ingest(self.write('DEMO-001,2026-09-01,north,5,0.10\n'), self.contract, self.db)
        with sqlite3.connect(self.db) as db:
            record = json.loads(db.execute("SELECT body FROM records WHERE record_key='DEMO-001'").fetchone()[0])
        self.assertEqual(record['quantity'], 5)
        self.assertEqual(record['unit_price'], '0.10')
        self.assertEqual(report(self.db)['record_count'], 3)

    def test_invalid_update_preserves_good_record(self):
        ingest(self.csv, self.contract, self.db)
        ingest(self.write('DEMO-001,2026-09-01,north,-1,0.10\n'), self.contract, self.db)
        with sqlite3.connect(self.db) as db:
            record = json.loads(db.execute("SELECT body FROM records WHERE record_key='DEMO-001'").fetchone()[0])
        self.assertEqual(record['quantity'], 2)

    def test_dry_run_and_strict_do_not_create_database(self):
        self.assertEqual(ingest(self.csv, self.contract, self.db, dry_run=True)['accepted'], 3)
        self.assertFalse(self.db.exists())
        with self.assertRaises(ValueError):
            ingest(self.csv, self.contract, self.db, strict=True)
        self.assertFalse(self.db.exists())

    def test_contract_change_is_rejected_without_new_batch(self):
        ingest(self.csv, self.contract, self.db)
        changed = json.loads(self.contract.read_text())
        changed['fields'][-1]['min'] = 1
        file = self.root / 'changed.json'
        file.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            ingest(self.csv, file, self.db)
        self.assertEqual(len(report(self.db)['batches']), 1)

    def test_malformed_csv_makes_no_partial_write(self):
        import csv
        file = self.write('OK,2026-09-01,north,1,1.00\n"unterminated')
        with self.assertRaises(csv.Error):
            ingest(file, self.contract, self.db)
        self.assertFalse(self.db.exists())

    def test_schema_and_nonfinite_decimal_errors(self):
        contract = load_contract(self.contract)
        with self.assertRaises(ValueError):
            validate(b'bad,header\n1,2\n', contract)
        result = ingest(self.write('BAD,2026-09-01,north,1,NaN\n'), self.contract, self.db)
        self.assertEqual(result['rejected'], 1)

    def test_duplicate_first_invalid_row_reserves_key(self):
        file = self.write('A,2026-09-01,north,0,1.00\nA,2026-09-01,north,1,1.00\n')
        result = ingest(file, self.contract, self.db)
        self.assertEqual((result['accepted'], result['rejected']), (0, 2))

    def test_warehouse_failure_rolls_back_batch_and_rows(self):
        ingest(self.csv, self.contract, self.db)
        with sqlite3.connect(self.db) as db:
            db.execute("""CREATE TRIGGER deny_insert BEFORE INSERT ON records
                          BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            ingest(self.write('NEW,2026-09-01,north,1,1.00\n'), self.contract, self.db)
        self.assertEqual(len(report(self.db)['batches']), 1)
        self.assertEqual(report(self.db)['record_count'], 3)


if __name__ == '__main__':
    unittest.main()
