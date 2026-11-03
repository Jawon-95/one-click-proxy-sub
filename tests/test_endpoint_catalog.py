"""Candidate drafts must preserve user data and compile with origin Host/SNI."""
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EndpointCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / 'config/regional-endpoints.json').read_text(encoding='utf-8'))

    def test_regional_candidates_compile_without_changing_origin_identity(self):
        self.assertEqual([region['id'] for region in self.catalog['regions']], ['sg', 'ph'])
        self.assertEqual([len(region['entries']) for region in self.catalog['regions']], [15, 5])
        for region in self.catalog['regions']:
            entries = region['entries']
            self.assertEqual(len({entry['address'] for entry in entries}), len(entries))
            result = subprocess.run(['node', str(ROOT / 'app/convert.mjs')], input=json.dumps({
                'nodeLinks': 'trojan://fixture-secret@proxy.example.com:443?security=tls&type=ws&host=proxy.example.com&sni=proxy.example.com&path=%2Fconnect',
                'preferredIps': '\n'.join(entry['address']+'#'+entry['label'] for entry in entries),
                'directIp': '203.0.113.10', 'nodeLimit': 0,
            }), encoding='utf-8', capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            compiled = json.loads(result.stdout)
            self.assertEqual(compiled['counts']['outputNodes'], len(entries)+2)
            nodes = json.loads(compiled['formats']['json']['body'])
            self.assertEqual(nodes[0]['server'], '203.0.113.10')
            self.assertEqual([node['server'] for node in nodes[2:]], [e['address'] for e in entries])
            for node in nodes:
                self.assertEqual(node['sni'], 'proxy.example.com')
                self.assertEqual(node['hostHeader'], 'proxy.example.com')

    def test_draft_merge_keeps_remarks_order_and_port_distinctions(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const context=vm.createContext({});
vm.runInContext(fs.readFileSync('app/web/endpoint-presets.js','utf8'),context);
const api=vm.runInContext('RegionalEndpoints',context);
const original='WWW.VISA.COM.SG.:443#my own remark\n104.18.29.76:8443#keep port\ninvalid unsaved draft';
const result=api.merge(original,[
 {address:'www.visa.com.sg',label:'must not overwrite'},
 {address:'104.18.29.76',label:'new'},
 {address:'104.18.29.76:443',label:'duplicate'},
 {address:'162.159.152.2',label:'last'},
]);
assert.equal(result.value,original+'\n104.18.29.76#new\n162.159.152.2#last');
assert.equal(result.added,2);
assert.equal(api.merge(result.value,[{address:'162.159.152.2',label:'changed'}]).value,result.value);
assert.equal(api.merge('',[{address:'visa.com.ph',label:'first'}]).value,'visa.com.ph#first');
assert.equal(api.keys('a.example#one,b.example#two;c.example:443#three').size,3);
assert.equal(api.merge('untouched\n',[]).value,'untouched\n');
"""
        result = subprocess.run(['node', '-e', script], cwd=ROOT, encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
