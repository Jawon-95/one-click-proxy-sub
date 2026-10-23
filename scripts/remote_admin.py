"""SSH deployment helper. Credentials are entered interactively, never saved."""
import getpass
import argparse
import json
import os
import sys
import time

import paramiko


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--host', required=True)
parser.add_argument('--user', default='root')
parser.add_argument('--port', type=int, default=22)
args = parser.parse_args()
client = paramiko.SSHClient()
client.load_host_keys(os.path.expanduser("~/.ssh/known_hosts"))
client.set_missing_host_key_policy(paramiko.RejectPolicy())
client.connect(args.host, port=args.port, username=args.user, password=getpass.getpass("SSH password: "),
               look_for_keys=False, allow_agent=False, timeout=15)
client.get_transport().set_keepalive(20)
print("SSH connected; submit JSON operations.", flush=True)
try:
    for line in sys.stdin:
        try:
            op = json.loads(line)
            if op["op"] == "exit":
                break
            if op["op"] in ("upload", "download"):
                with client.open_sftp() as sftp:
                    if op["op"] == "upload":
                        sftp.put(op["local"], op["remote"])
                        sftp.chmod(op["remote"], op.get("mode", 0o600))
                    else:
                        sftp.get(op["remote"], op["local"])
                print("TRANSFER_OK", flush=True)
            elif op["op"] == "exec":
                channel = client.get_transport().open_session()
                channel.exec_command(op["cmd"])
                while True:
                    while channel.recv_ready():
                        print(channel.recv(32768).decode("utf-8", "replace"), end="", flush=True)
                    while channel.recv_stderr_ready():
                        print(channel.recv_stderr(32768).decode("utf-8", "replace"), end="", flush=True)
                    if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                        break
                    time.sleep(0.1)
                print("\nEXIT_STATUS", channel.recv_exit_status(), flush=True)
                channel.close()
        except Exception as exc:
            print(type(exc).__name__, str(exc), flush=True)
finally:
    client.close()
