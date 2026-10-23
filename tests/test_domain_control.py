import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('domain_control',ROOT/'app/domain_control.py')
control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)


class DomainControlTests(unittest.TestCase):
    def test_domain_validation(self):
        self.assertEqual(control.normalize_domain(' NEW.Example.com. '),'new.example.com')
        for value in ('https://new.example.com','new.example.com/path','new.example.com:443','127.0.0.1','bad name.com','foo.local',None,'a;touch.example'):
            with self.subTest(value=value),self.assertRaises(ValueError):control.normalize_domain(value)

    def test_atomic_queue_and_public_status(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg={'domain':'old.example.com','data_dir':folder,'domain_admin_dir':folder}
            job=control.enqueue('new.example.com',cfg)
            self.assertEqual(json.loads((Path(folder)/'domain-request.json').read_text())['id'],job['id'])
            self.assertTrue(control.busy(cfg))
            with self.assertRaises(ValueError):control.enqueue('other.example.com',cfg)
            (Path(folder)/'domain-request.json').unlink()
            (Path(folder)/'status.json').write_text(json.dumps(job|{'status':'switching','private':'must-not-leak'}))
            self.assertNotIn('private',control.status(cfg))
            self.assertTrue(control.busy(cfg))
