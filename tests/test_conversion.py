import base64
import json
from pathlib import Path
import subprocess
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class ConversionTests(unittest.TestCase):
    def test_limit_selects_first_endpoints_without_truncating_saved_list(self):
        preset=(ROOT/'config/preferred-endpoints.txt').read_text(encoding='utf-8').strip()
        result=self.convert(preferredIps=preset,nodeLimit=100)
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(result.stdout)
        self.assertEqual(data['settings']['preferredIps'],preset)
        self.assertEqual(data['counts']['preferredEndpoints'],219)
        self.assertEqual(data['counts']['selectedEndpoints'],100)
        self.assertEqual(data['counts']['outputNodes'],100)
        expected=[line.split('#')[0] for line in preset.splitlines()][:100]
        nodes=json.loads(data['formats']['json']['body'])
        self.assertEqual([n['server'] for n in nodes],expected)
        self.assertEqual(len(base64.b64decode(data['formats']['raw']['body']).decode().splitlines()),100)
        self.assertEqual(data['formats']['clash']['body'].count('type: trojan'),100)
        for invalid in (-1,1.5,1001,True,'100'):
            self.assertNotEqual(self.convert(nodeLimit=invalid).returncode,0)

    def convert(self, **changes):
        data = {'nodeLinks':'trojan://fixture-secret@proxy.example.com:443?security=tls&type=ws&host=proxy.example.com&sni=proxy.example.com&path=%2Fconnect#Fixture',
                'preferredIps':'104.16.1.2#Test\ncdnjs.com:443#CDNJS','keepOriginalHost':True,'includeOriginal':True}
        data.update(changes)
        result = subprocess.run(['node', str(ROOT/'app/convert.mjs')], input=json.dumps(data),
                                encoding='utf-8', text=True, capture_output=True)
        return result

    def test_all_presets_preserve_identity_and_protocol(self):
        result = self.convert(preferredIps=(ROOT/'config/preferred-endpoints.txt').read_text(encoding='utf-8'))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['counts']['preferredEndpoints'], 219)
        self.assertEqual(data['counts']['outputNodes'], 220)
        nodes = base64.b64decode(data['formats']['raw']['body']).decode().splitlines()
        self.assertEqual(len(nodes), 220)
        names = set()
        for raw in nodes:
            node = urlsplit(raw)
            query = parse_qs(node.query)
            self.assertEqual(node.username, 'fixture-secret')
            self.assertEqual(query['sni'], ['proxy.example.com'])
            self.assertEqual(query['host'], ['proxy.example.com'])
            self.assertEqual(query['path'], ['/connect'])
            self.assertEqual(query['type'], ['ws'])
            names.add(node.fragment)
        self.assertEqual(len(names),220)
        self.assertIn('sni: "proxy.example.com"', data['formats']['clash']['body'])
        self.assertIn('Host: "proxy.example.com"', data['formats']['clash']['body'])
        self.assertIn('ws=true', data['formats']['surge']['body'])

    def test_direct_node_is_first_and_bypasses_cdn(self):
        for limit in (0,1):
            with self.subTest(limit=limit):
                result=self.convert(directIp='203.0.113.10',nodeLimit=limit)
                self.assertEqual(result.returncode,0,result.stderr)
                data=json.loads(result.stdout)
                self.assertEqual(data['counts']['directNodes'],1)
                nodes=json.loads(data['formats']['json']['body'])
                self.assertEqual(len(nodes),(4 if limit==0 else 2))
                first=nodes[0]
                self.assertEqual((first['server'],first['port']),('203.0.113.10',443))
                self.assertEqual((first['sni'],first['hostHeader']),('proxy.example.com','proxy.example.com'))
                self.assertIn('直连',first['name'])
                self.assertNotEqual(nodes[1]['server'],'203.0.113.10')
                raw=base64.b64decode(data['formats']['raw']['body']).decode().splitlines()
                self.assertTrue(raw[0].startswith('trojan://fixture-secret@203.0.113.10:443?'))
                self.assertIn('server: "203.0.113.10"',data['formats']['clash']['body'].split('proxy-groups')[0])
        self.assertNotEqual(self.convert(directIp='not-an-ip').returncode,0)

    def test_invalid_or_unsupported_endpoints_fail(self):
        for value in ['https://example.com','104.16.1.2:80','bad domain.com','[invalid-ipv6]']:
            with self.subTest(value=value):
                self.assertNotEqual(self.convert(preferredIps=value).returncode,0)

    def test_duplicates_removed_and_labels_kept(self):
        result=self.convert(preferredIps='104.16.1.2#First\n104.16.1.2#Second\n104.16.1.2:8443#Alt')
        data=json.loads(result.stdout)
        self.assertEqual(data['counts']['preferredEndpoints'],2)
        self.assertIn('First',data['preview'][1]['name'])
        self.assertEqual(data['preview'][2]['port'],8443)

    def test_native_tcp_cannot_be_expanded_as_cdn(self):
        self.assertNotEqual(self.convert(nodeLinks='trojan://fixture@proxy.example.com:443?security=tls&type=tcp').returncode,0)

    def test_vmess_and_vless_conversion_remain_usable(self):
        vmess='vmess://'+base64.b64encode(json.dumps({'v':'2','ps':'fixture','add':'proxy.example.com','port':'443','id':'00000000-0000-4000-8000-000000000001','net':'ws','tls':'tls','host':'proxy.example.com','path':'/ws','sni':'proxy.example.com'}).encode()).decode()
        vless='vless://00000000-0000-4000-8000-000000000001@proxy.example.com:443?security=tls&type=ws&host=proxy.example.com&sni=proxy.example.com&path=%2Fws'
        for value in (vmess,vless):
            result=self.convert(nodeLinks=value)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['counts']['outputNodes'],3)


if __name__=='__main__':
    unittest.main()
