#!/usr/bin/env python
"""Run a command on a remote host via SSH (password auth). Usage: ssh_run.py <host> <command...>"""
import os, sys, paramiko, warnings
warnings.filterwarnings("ignore")
HOST = sys.argv[1]
CMD  = " ".join(sys.argv[2:])
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username="kram", password=os.environ.get('MESH_SSH_PASS',''), timeout=10, banner_timeout=10, auth_timeout=10,
          allow_agent=False, look_for_keys=False)
stdin, stdout, stderr = c.exec_command(CMD, timeout=600)
out = stdout.read().decode(errors="replace").strip()
err = stderr.read().decode(errors="replace").strip()
rc  = stdout.channel.recv_exit_status()
if out: print(out)
if err: print("[stderr]", err, file=sys.stderr)
sys.exit(rc)
