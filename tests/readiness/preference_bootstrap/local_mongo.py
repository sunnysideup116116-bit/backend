"""Disposable Mongo replica set, explicit local Docker socket and loopback only."""
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
STATE = ROOT / ".runtime/bootstrap-mongo.json"
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
MARKER = "preference-bootstrap-synthetic-only"
NAME = "ayue-bootstrap-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:10]
PORT = 27029


def command(*args):
    result = subprocess.run([*DOCKER, *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("disposable_mongo_command_failed")
    return result.stdout


def verify():
    state = json.loads(STATE.read_text())
    if state != {"name": NAME, "port": PORT, "image": "mongo:8.2.5"}:
        raise RuntimeError("disposable_mongo_state_mismatch")
    container = json.loads(command("inspect", NAME))[0]
    if container["Config"]["Labels"].get("io.ayue.readiness") != MARKER:
        raise RuntimeError("disposable_mongo_owner_mismatch")
    if container["Config"]["Image"] != state["image"]:
        raise RuntimeError("disposable_mongo_image_mismatch")
    bindings = container["HostConfig"]["PortBindings"]
    if bindings != {"27017/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(PORT)}]}:
        raise RuntimeError("disposable_mongo_not_loopback")
    if {m["Name"] for m in container["Mounts"] if m["Type"] == "volume"} != {NAME + "-data", NAME + "-config"}:
        raise RuntimeError("disposable_mongo_mount_mismatch")
    if any(m["Type"] != "volume" for m in container["Mounts"]):
        raise RuntimeError("disposable_mongo_host_mount")
    network = json.loads(command("network", "inspect", NAME))[0]
    members = set(network.get("Containers", {}))
    expected = {container["Id"]} if container["State"]["Running"] else set()
    if network["Labels"].get("io.ayue.readiness") != MARKER or members != expected:
        raise RuntimeError("disposable_mongo_network_owner_mismatch")
    for suffix in ("-data", "-config"):
        volume = json.loads(command("volume", "inspect", NAME + suffix))[0]
        if volume["Labels"].get("io.ayue.readiness") != MARKER:
            raise RuntimeError("disposable_mongo_volume_owner_mismatch")
    return state


def up():
    if STATE.exists():
        verify()
        command("start", NAME)
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", PORT))
    # No production credentials or data, no anonymous volumes, no external peers.
    for kind, name in (("container", NAME), ("network", NAME), ("volume", NAME + "-data"), ("volume", NAME + "-config")):
        found = subprocess.run([*DOCKER, kind, "inspect", name], capture_output=True)
        if found.returncode == 0:
            raise RuntimeError("unexpected_existing_disposable_resource")
    labels = ["--label", "io.ayue.readiness=" + MARKER, "--label", "io.ayue.checkout=" + str(ROOT)]
    command("network", "create", "--driver", "bridge", "--opt", "com.docker.network.bridge.host_binding_ipv4=127.0.0.1",
            "--opt", "com.docker.network.bridge.enable_icc=false", *labels, NAME)
    for suffix in ("-data", "-config"):
        command("volume", "create", *labels, NAME + suffix)
    command("run", "-d", "--name", NAME, *labels, "--network", NAME,
            "--memory", "512m", "--cpus", "1", "-p", f"127.0.0.1:{PORT}:27017",
            "-v", NAME + "-data:/data/db", "-v", NAME + "-config:/data/configdb",
            "mongo:8.2.5", "mongod", "--replSet", "bootstrap_test", "--bind_ip_all")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"name": NAME, "port": PORT, "image": "mongo:8.2.5"}))
    for attempt in range(25):
        try:
            command("exec", NAME, "mongosh", "--quiet", "--eval",
                    "rs.initiate({_id:'bootstrap_test',members:[{_id:0,host:'localhost:27017'}]})")
            break
        except RuntimeError:
            time.sleep(1)
    verify()


def wait_primary():
    verify()
    for _ in range(30):
        try:
            command("exec", NAME, "mongosh", "--quiet", "--eval",
                    "let h=db.hello();quit(h.isWritablePrimary && h.setName==='bootstrap_test' ? 0 : 2)")
            return
        except RuntimeError:
            time.sleep(1)
    raise RuntimeError("disposable_mongo_primary_unavailable")


def stop():
    verify()
    command("stop", "--time", "10", NAME)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["up"]:
        up()
        wait_primary()
    elif sys.argv[1:] == ["stop"]:
        stop()
    else:
        raise SystemExit("Use up or stop; no automatic deletion")
    print(json.dumps({"name": NAME, "image": "mongo:8.2.5", "localhost_port": PORT,
                      "synthetic_only": True, "production_compatibility": "not_claimed"}))
