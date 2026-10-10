"""Bounded, asynchronous raw file reception over existing HID/HDMI channels.

No OCR, executable file handling, network file service or arbitrary command API.
"""
from __future__ import annotations
import base64
import hashlib
import io
import json
import math
import ntpath
import os
from pathlib import Path
import re
import struct
import threading
import time
import uuid
import zlib

import numpy as np
from PIL import Image
from mcp.types import Tool

COLS, ROWS, CHUNK, HEADER = 512, 240, 15000, 128
MAX_BYTES = 16 * 1024 * 1024
MAX_SECONDS = 3600


class ProtocolError(ValueError):
    pass


def identity(info):
    """Hardware identity, independent of aliases and capture/HID dimensions."""
    serial = info.get("serial", {})
    port = serial.get("port")
    ports = info.get("ch340_ports", [])
    adapter = next((p.get("hwid") for p in ports if p.get("device") == port), None)
    capture = info.get("capture", {}).get("device")
    if not serial.get("connected") or not port or not adapter or not capture:
        raise ValueError("connected serial identity and capture device required")
    return {"serial_port": port, "adapter": adapter, "capture_device": capture}


def source_name(path):
    if not isinstance(path, str) or not re.match(r"^[A-Za-z]:\\", path):
        raise ValueError("source_path must be an absolute local Windows file path")
    if any(c in path for c in '\x00\r\n/') or ':' in path[2:] or any(p in ('.', '..') or p.endswith((' ','.')) for p in path[3:].split('\\')):
        raise ValueError("device paths, ADS and traversal are unsupported")
    name = ntpath.basename(path)
    if not name or name.endswith((' ', '.')) or any(c in name for c in '<>"|?*'):
        raise ValueError("invalid source filename")
    if name.split('.')[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1,10)), *(f"LPT{i}" for i in range(1,10))}:
        raise ValueError("reserved device filename")
    return name


def decode_frame(frame, nonce, allow_error=False):
    """Locate only the protocol's magenta border; sample calibrated cell centres."""
    a = np.asarray(frame.convert("RGB"))
    mask = (a[:,:,0] > 160) & (a[:,:,1] < 100) & (a[:,:,2] > 160)
    ys, xs = np.where(mask)
    if len(xs) < 100:
        raise ProtocolError("protocol border absent")
    x0, x1, y0, y1 = xs.min(), xs.max()+1, ys.min(), ys.max()+1
    # A six-pixel border scales with both axes, independently of HID dimensions.
    outer_w, outer_h = x1-x0, y1-y0
    # Try native sender cell sizes. This also supports independently resized capture.
    for cw in (3, 2):
        for ch in (4, 3, 2):
            bx = outer_w * 6 / (COLS*cw+12)
            by = outer_h * 6 / (ROWS*ch+12)
            xx = np.floor(x0+bx+(np.arange(COLS)+.5)*(outer_w-2*bx)/COLS).astype(int)
            yy = np.floor(y0+by+(np.arange(ROWS)+.5)*(outer_h-2*by)/ROWS).astype(int)
            bits = (a[yy[:,None],xx[None,:]].mean(axis=2)>127).reshape(-1,8)
            packet = np.packbits(bits,axis=1,bitorder="little").tobytes()
            if packet[:8] != b"KVMFILE1" or packet[8:24] != nonce:
                continue
            if hashlib.sha256(packet[:108]).digest()[:16] != packet[108:124]:
                continue
            index,total,length,size = struct.unpack_from("<IIIQ",packet,24)
            flag = struct.unpack_from("<I",packet,124)[0]
            if size > MAX_BYTES or total != max(1,math.ceil(size/CHUNK)) or index>=total or length>CHUNK:
                raise ProtocolError("invalid packet bounds")
            payload=packet[HEADER:HEADER+length]
            if hashlib.sha256(payload).digest()!=packet[76:108]:
                raise ProtocolError("chunk checksum mismatch")
            if flag == 1:
                if allow_error:
                    return {"index":index,"total":total,"size":size,"error":payload.decode("utf-8",errors="replace")}
                raise ValueError("Target source error: "+payload.decode("utf-8",errors="replace"))
            if flag or length != min(CHUNK,size-index*CHUNK):
                raise ProtocolError("invalid packet length or flags")
            return {"index":index,"total":total,"size":size,"sha256":packet[44:76].hex(),"payload":payload}
    raise ProtocolError("session/header checksum mismatch")


def bootstrap(path, job_id, cached=False):
    cs = Path(__file__).with_name("file_copy_helper.cs").read_text(encoding="utf-8")
    build=hashlib.sha256(cs.encode()).hexdigest()
    cs=cs.replace("__KVM_BUILD__",build)
    p64 = base64.b64encode(path.encode("utf-8")).decode()
    token = base64.b64encode(bytes.fromhex(job_id)).decode()
    call="[KvmFileSender]::Run([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+p64+"')),'"+token+"',"+str(MAX_BYTES)+")"
    if cached:
        return "if([KvmFileSender]::Build -ne '"+build+"'){throw 'helper version differs; use a fresh shell'};"+call
    script = "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @'\n"+cs+"\n'@; "+call
    packed = base64.b64encode(zlib.compress(script.encode("utf-8"),9)[2:-4]).decode()
    return "$k=[IO.MemoryStream]::new([Convert]::FromBase64String('"+packed+"'));$d=[IO.Compression.DeflateStream]::new($k,[IO.Compression.CompressionMode]::Decompress);$r=[IO.StreamReader]::new($d,[Text.Encoding]::UTF8);try{& ([ScriptBlock]::Create($r.ReadToEnd()))}finally{$r.Dispose();$d.Dispose();$k.Dispose()}"


class EndpointLease:
    """OS-held advisory lock shared by copy workers across MCP processes."""
    def __init__(self, root, endpoint):
        root.mkdir(parents=True,exist_ok=True)
        key=hashlib.sha256(json.dumps(endpoint,sort_keys=True).encode()).hexdigest()
        self.file=open(root/(key+".lock"),"a+b")
        self.file.seek(0); self.file.write(b"0"); self.file.flush(); self.file.seek(0)
        try:
            if os.name=="nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except Exception:
            self.file.close(); raise ValueError("endpoint already has a copy worker")
    def close(self):
        if self.file:
            self.file.close(); self.file=None


class CopyManager:
    def __init__(self, root=None):
        self.root=Path(root or Path(os.environ.get("LOCALAPPDATA",Path.home()))/"mcp-serial-hid-kvm"/"file-copy")
        self.jobs={}; self.active=None; self.lease=None; self.thread=None
        self.stop=threading.Event(); self.mutex=threading.RLock()
        self.observed=None; self.interlocked=lambda:False

    def observe(self, endpoint):
        self.observed=(dict(endpoint),time.monotonic())

    def busy(self,endpoint):
        """Persisted ownership survives restart and guards other updated MCP processes."""
        bound={"host":endpoint["host"],"port":int(endpoint["port"])}
        if self.active: return self.active
        if self.root.exists():
            for path in self.root.glob("*.json"):
                try: j=json.loads(path.read_text(encoding="utf-8"))
                except (OSError,ValueError): continue
                if j.get("endpoint")==bound and j.get("state")!="prepared" and j.get("target_cleanup")!="caller_verified_closed":
                    return j["job_id"]
        return None

    def _save(self,j):
        self.root.mkdir(parents=True,exist_ok=True)
        target=self.root/(j["job_id"]+".json")
        temp=target.with_suffix(".new")
        temp.write_text(json.dumps(j,ensure_ascii=False),encoding="utf-8"); os.replace(temp,target)

    def _job(self,job_id):
        if not re.fullmatch(r"[0-9a-f]{32}",job_id): raise ValueError("invalid job_id")
        if job_id not in self.jobs:
            j=json.loads((self.root/(job_id+".json")).read_text(encoding="utf-8"))
            if j["state"] in ("receiving","typing"): j["state"]="paused";j["error"]="MCP process interrupted; observe and resume"
            self.jobs[job_id]=j
        return self.jobs[job_id]

    def status(self,job_id):
        with self.mutex:
            j=self._job(job_id)
            return {k:v for k,v in j.items() if k!="source_path"}

    def prepare(self,client,endpoint,source_path,host_dir,dry_run=True,overwrite=False):
        name=source_name(source_path)
        directory=Path(host_dir)
        if not directory.is_absolute() or not directory.is_dir(): raise ValueError("host_dir must be an existing absolute directory")
        directory=directory.resolve(); destination=directory/name
        if destination.is_symlink() or (destination.exists() and (not overwrite or not destination.is_file())):
            raise ValueError("destination conflict (overwrite defaults false)")
        bound={"host":endpoint["host"],"port":int(endpoint["port"])}
        ident=identity(client.get_device_info())
        result={"dry_run":dry_run,"destination":str(destination),"endpoint":bound,"identity":ident,"max_bytes":MAX_BYTES,"verified":False,"requires":"fresh capture; dedicated PowerShell -NoProfile with PSReadLine removed; ASCII focus confirmation"}
        if dry_run: return result
        job_id=uuid.uuid4().hex
        j={"job_id":job_id,"source_path":source_path,"destination":str(destination),"endpoint":bound,"identity":ident,"overwrite":bool(overwrite),"state":"prepared","received_chunks":0,"received_bytes":0,"retries":0,"verified":False,"target_cleanup":"pending","host_cleanup":"pending","created":time.time()}
        self.jobs[job_id]=j; self._save(j)
        return self.status(job_id)

    def _bind(self,j,client,endpoint,confirmed):
        bound={"host":endpoint["host"],"port":int(endpoint["port"])}
        if bound!=j["endpoint"] or identity(client.get_device_info())!=j["identity"]:
            raise ValueError("target identity/endpoint changed")
        if not confirmed or self.observed is None or self.observed[0]!=bound or time.monotonic()-self.observed[1]>60:
            raise ValueError("fresh capture and focus_confirmed=true required")
        if self.interlocked(): raise ValueError("input_locked")
        if self.active not in (None,j["job_id"]): raise ValueError("another copy job owns HID")
        busy=self.busy(endpoint)
        if busy not in (None,j["job_id"]): raise ValueError("another persisted job requires cleanup: "+busy)
        if self.lease is None: self.lease=EndpointLease(self.root/"leases",bound)
        self.active=j["job_id"]

    def advance(self,client,endpoint,job_id,action,focus_confirmed=False,helper_cached=False):
        with self.mutex:
            j=self._job(job_id)
            if self.thread and self.thread.is_alive(): raise ValueError("worker still running; poll status or cancel")
            self._bind(j,client,endpoint,focus_confirmed)
            if action=="release":
                # Caller has observed helper closed (or closes it explicitly). No blind Escape.
                j["target_cleanup"]="caller_verified_closed"; self.active=None
                self.lease.close(); self.lease=None; self._save(j); return self.status(job_id)
            if action=="cleanup":
                decode_frame(self._capture(client),bytes.fromhex(job_id),allow_error=True)
                client.send_key("escape")
                j["target_cleanup"]="escape_sent_observation_required";self._save(j)
                return self.status(job_id)
            if action=="type":
                if j["state"] not in ("prepared","paused","cancelled","typed"): raise ValueError("invalid state for typing")
                j["state"]="typing";j["target_cleanup"]="pending"; j.pop("error",None);self.stop.clear(); self._save(j)
                self.thread=threading.Thread(target=self._type,args=(client,j,helper_cached),daemon=True); self.thread.start()
            elif action in ("launch","receive"):
                if action=="launch" and j["state"]!="typed": raise ValueError("command not typed")
                if j["verified"]: raise ValueError("already verified")
                # Nonzero inset avoids adapters treating (0,0) as no movement.
                # This remains outside the centred grid at supported geometries.
                client.mouse_move(20,20)
                time.sleep(.2)
                if action=="receive": decode_frame(self._capture(client),bytes.fromhex(job_id))
                if action=="launch": client.send_key("enter")
                self.stop.clear(); j["state"]="receiving"; j["target_cleanup"]="pending";j["receive_started"]=time.time(); j.pop("error",None); self._save(j)
                self.thread=threading.Thread(target=self._receive,args=(client,j),daemon=True); self.thread.start()
            else: raise ValueError("unknown action")
            return self.status(job_id)

    def _type(self,client,j,cached=False):
        old_timing=None
        try:
            command=bootstrap(j["source_path"],j["job_id"],cached)
            old_timing=client.get_timing()
            client.set_timing({"type_key_hold":.008,"type_shift":.004})
            client.send_key("0x91")
            for start in range(0,len(command),256):
                if self.stop.is_set() or self.interlocked(): raise ValueError("typing interrupted; clear unexecuted line before retyping")
                client.type_text(command[start:start+256],char_delay_ms=0,raw=True)
            with self.mutex: j["state"]="typed"; j["command_chars"]=len(command); j["command_sha256"]=hashlib.sha256(command.encode()).hexdigest(); self._save(j)
        except Exception as e:
            with self.mutex: j["state"]="paused"; j["error"]=str(e)[:500]; self._save(j)
        finally:
            if old_timing is not None:
                try: client.set_timing(old_timing)
                except Exception as e:
                    with self.mutex: j["timing_restore_error"]=str(e)[:200];self._save(j)

    @staticmethod
    def _capture(client):
        raw,_,_=client.capture_frame_jpeg(95)
        return Image.open(io.BytesIO(raw)).convert("RGB")

    def _parts(self,j):
        return Path(j["destination"]).parent/(".kvmcopy-"+j["job_id"])

    def _accept(self,j,p):
        meta={k:p[k] for k in ("total","size","sha256")}
        if "manifest" in j and meta!=j["manifest"]: raise ValueError("source changed since earlier verified chunks")
        j["manifest"]=meta
        parts=self._parts(j); parts.mkdir(exist_ok=True)
        if parts.is_symlink(): raise ValueError("unsafe staging directory")
        piece=parts/(str(p["index"])+".bin")
        if piece.is_symlink(): raise ValueError("unsafe staging chunk")
        if piece.exists() and piece.read_bytes()!=p["payload"]:
            # Reacquire a corrupted self-created staging chunk, never publish it.
            replacement=piece.with_suffix(".new")
            with replacement.open("xb") as f: f.write(p["payload"])
            os.replace(replacement,piece)
        if not piece.exists():
            with piece.open("xb") as f: f.write(p["payload"])
        present={int(f.stem):f.stat().st_size for f in parts.glob("*.bin") if f.stem.isdigit()}
        j["received_chunks"]=len(present); j["received_bytes"]=sum(present.values())
        self._save(j)
        return next((i for i in range(p["total"]) if i not in present),None)

    def _finish(self,j):
        parts=self._parts(j); m=j["manifest"]; staged=parts/"assembled.part"
        digest=hashlib.sha256(); size=0
        with staged.open("wb") as output:
            for i in range(m["total"]):
                b=(parts/(str(i)+".bin")).read_bytes(); digest.update(b); size+=len(b); output.write(b)
            output.flush(); os.fsync(output.fileno())
        if size!=m["size"] or digest.hexdigest()!=m["sha256"]: raise ValueError("whole-file size/SHA-256 mismatch; completion forbidden")
        dest=Path(j["destination"])
        if dest.is_symlink(): raise ValueError("destination symlink conflict")
        if j["overwrite"]: os.replace(staged,dest)
        else:
            # Atomic no-clobber publication on the same filesystem.
            os.link(staged,dest); staged.unlink()
        j.update(state="completed",verified=True,size=size,sha256=digest.hexdigest(),completed=time.time())
        for i in range(m["total"]): (parts/(str(i)+".bin")).unlink()
        parts.rmdir(); j["host_cleanup"]="completed"; self._save(j)

    def _receive(self,client,j):
        started=time.monotonic(); failures=0
        try:
            while not self.stop.is_set():
                if self.interlocked(): raise ValueError("input_locked; receive paused")
                if time.monotonic()-started>MAX_SECONDS: raise ValueError("job time budget exceeded")
                if identity(client.get_device_info())!=j["identity"]: raise ValueError("target identity changed")
                try: p=decode_frame(self._capture(client),bytes.fromhex(j["job_id"]))
                except ProtocolError:
                    failures+=1; j["retries"]+=1
                    if failures>=40: raise ValueError("40 invalid frames; observe focus/helper before resume")
                    time.sleep(.25); continue
                failures=0
                with self.mutex: missing=self._accept(j,p)
                if missing is None:
                    with self.mutex: self._finish(j)
                    return
                if self.stop.is_set() or self.interlocked(): break
                # Only send navigation after a valid, nonce-bound frame was observed.
                if missing!=p["index"]:
                    if missing==p["index"]+1: client.send_key("right")
                    else: client.type_text(str(missing),char_delay_ms=0,raw=True);client.send_key("enter")
                time.sleep(.2)
            with self.mutex: j["state"]="cancelled"; self._save(j)
        except Exception as e:
            with self.mutex: j["state"]="paused"; j["error"]=str(e)[:500]; self._save(j)

    def cancel(self,job_id):
        with self.mutex:
            j=self._job(job_id)
            if self.active==job_id: self.stop.set()
            if j["state"]=="prepared": j["target_cleanup"]="caller_verified_closed";j["host_cleanup"]="not_created"
            if j["state"] not in ("completed","receiving","typing"): j["state"]="cancelled"
            self._save(j); return self.status(job_id)


def file_copy_tools():
    result = [
        Tool(name="copy_file_from_target",description="Plan or prepare copying a saved local Windows file as raw bytes over HID/HDMI. Default dry_run=true, no overwrite. Returns a job; observe a dedicated no-history PowerShell before advancing. Maximum 16 MiB (large transfers not hardware qualified).",inputSchema={"type":"object","properties":{"source_path":{"type":"string"},"host_dir":{"type":"string"},"dry_run":{"type":"boolean","default":True},"overwrite":{"type":"boolean","default":False}},"required":["source_path","host_dir"]}),
        Tool(name="advance_file_copy",description="Advance a prepared copy job. type: input fixed helper command without Enter; capture and inspect before launch. launch: Enter and background reception. receive: resume an observed helper. cleanup: Escape only after protocol validation. release: caller confirms helper closed, releases HID ownership. Every action requires fresh capture and explicit focus_confirmed. No arbitrary commands.",inputSchema={"type":"object","properties":{"job_id":{"type":"string"},"action":{"type":"string","enum":["type","launch","receive","cleanup","release"]},"focus_confirmed":{"type":"boolean","default":False}},"required":["job_id","action"]}),
        Tool(name="get_file_copy_status",description="Bounded persisted copy progress, verification and cleanup status. No internal frames or raw bytes.",inputSchema={"type":"object","properties":{"job_id":{"type":"string"}},"required":["job_id"]}),
        Tool(name="cancel_file_copy",description="Stop the copy worker without blind Target input; retain verified chunks for resume. Observe helper, cleanup and release ownership separately.",inputSchema={"type":"object","properties":{"job_id":{"type":"string"}},"required":["job_id"]}),
    ]
    result[1].inputSchema["properties"]["helper_cached"] = {"type":"boolean","default":False,"description":"Type only a short call to the identical helper already loaded in this dedicated PowerShell session. Build fingerprint is checked on Target."}
    return result
