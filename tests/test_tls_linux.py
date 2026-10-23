"""Exercise installed Xray SNI rejection on an isolated loopback TLS listener."""
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest

CAN_RUN=os.name=='posix' and hasattr(os,'geteuid') and os.geteuid()==0 and Path('/opt/xray/xray').exists() and shutil.which('openssl')


@unittest.skipUnless(CAN_RUN,'Linux root with installed Xray and OpenSSL')
class TLSRetirementTests(unittest.TestCase):
    def test_unknown_sni_rejected_even_when_client_disables_certificate_checks(self):
        import grp
        with tempfile.TemporaryDirectory(prefix='proxysub-tls-test-') as folder:
            root=Path(folder);root.chmod(0o755)
            cert=root/'cert.pem';key=root/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes',
                            '-days','1','-subj','/CN=proxy.example.com','-addext','subjectAltName=DNS:proxy.example.com',
                            '-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
            key.chmod(0o640);os.chown(key,0,grp.getgrnam('xray').gr_gid)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            cfg={'log':{'loglevel':'none'},'inbounds':[{'listen':'127.0.0.1','port':port,'protocol':'trojan',
                'settings':{'clients':[{'password':'fixture-only-password'}]},'streamSettings':{'network':'tcp','security':'tls',
                'tlsSettings':{'rejectUnknownSni':True,'certificates':[{'certificateFile':str(cert),'keyFile':str(key)}]}}}],
                'outbounds':[{'protocol':'blackhole'}]}
            path=root/'config.json';path.write_text(json.dumps(cfg));path.chmod(0o644)
            process=subprocess.Popen(['runuser','-u','xray','--','/opt/xray/xray','run','-config',str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+10
                while True:
                    self.assertIsNone(process.poll())
                    try:
                        with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                    except OSError:
                        if time.monotonic()>deadline:raise
                        time.sleep(.1)
                context=ssl.create_default_context(cafile=str(cert))
                with socket.create_connection(('127.0.0.1',port),timeout=3) as plain:
                    with context.wrap_socket(plain,server_hostname='proxy.example.com') as secure:
                        self.assertTrue(secure.getpeercert())
                context.check_hostname=False;context.verify_mode=ssl.CERT_NONE
                with self.assertRaises((ssl.SSLError,ConnectionResetError)):
                    with socket.create_connection(('127.0.0.1',port),timeout=3) as plain:
                        with context.wrap_socket(plain,server_hostname='retired.example.com'):pass
            finally:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
