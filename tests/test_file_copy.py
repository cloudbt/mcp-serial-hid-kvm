import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
import subprocess
from unittest.mock import Mock

import numpy as np
from PIL import Image
from mcp_serial_hid_kvm.file_copy import *

INFO={"serial":{"connected":True,"port":"MOCK1"},"ch340_ports":[{"device":"MOCK1","hwid":"unique-a"}],"capture":{"device":"video-a"}}
ENDPOINT={"host":"localhost","port":9001}
NONCE=bytes(range(16))


def frame(data, index=0, nonce=NONCE, scale=(1,1), cells=(3,4), whole=None, damage=False, error=False):
    payload=data[index*CHUNK:(index+1)*CHUNK]
    head=struct.pack('<8s16sIIIQ32s32s',b'KVMFILE1',nonce,index,max(1,math.ceil(len(data)/CHUNK)),len(payload),0 if error else len(data),whole or hashlib.sha256(b'' if error else data).digest(),hashlib.sha256(payload).digest())
    packet=head+hashlib.sha256(head).digest()[:16]+struct.pack('<I',int(error))+payload
    packet=packet.ljust(COLS*ROWS//8,b'\x00')
    bits=np.unpackbits(np.frombuffer(packet,dtype=np.uint8),bitorder='little').reshape(ROWS,COLS)*255
    cw,ch=cells
    grid=Image.fromarray(bits).convert('RGB').resize((COLS*cw,ROWS*ch),Image.Resampling.NEAREST)
    image=Image.new('RGB',(COLS*cw+92,ROWS*ch+92),'black')
    image.paste(Image.new('RGB',(COLS*cw+12,ROWS*ch+12),'magenta'),(40,40)); image.paste(grid,(46,46))
    if damage: image.paste('white',(46+400*cw,46+20*ch,46+401*cw,46+21*ch))
    image=image.resize((int(image.width*scale[0]),int(image.height*scale[1])),Image.Resampling.BILINEAR)
    b=io.BytesIO();image.save(b,format='JPEG',quality=95);return Image.open(io.BytesIO(b.getvalue())).convert('RGB')


class ProtocolTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows PowerShell compatibility')
    def test_bootstrap_parses_on_windows_powershell(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'command.txt';path.write_text(bootstrap('C:\\test.bin',NONCE.hex()),encoding='utf-8')
            check="$t=$null;$e=$null;[System.Management.Automation.Language.Parser]::ParseInput([IO.File]::ReadAllText('"+str(path)+"'),[ref]$t,[ref]$e)|Out-Null;if($e.Count){$e|Out-String|Write-Output;exit 1}"
            p=subprocess.run(['powershell.exe','-NoProfile','-Command',check],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
    def test_empty_small_binary_unicode_and_scaled_capture(self):
        for data in (b'',b'hello', '中文 日本語'.encode(),bytes(range(256))*65):
            for cells,scale in (((3,4),(1,1)),((2,2),(1,1)),((3,3),(.9,1.1)),((3,4),(1.2,.85))):
                with self.subTest(size=len(data),cells=cells,scale=scale):
                    p=decode_frame(frame(data,cells=cells,scale=scale),NONCE)
                    self.assertEqual(p['payload'],data[:CHUNK]);self.assertEqual(p['size'],len(data))
    def test_session_isolation_and_no_protocol(self):
        with self.assertRaises(ProtocolError):decode_frame(frame(b'x'),bytes(16))
        with self.assertRaises(ProtocolError):decode_frame(Image.new('RGB',(1920,1080)),NONCE)
    def test_corruption_rejected(self):
        f=frame(bytes(CHUNK)); f.paste('white',(46+400*3,46+20*4,46+401*3,46+21*4))
        with self.assertRaises(ProtocolError):decode_frame(f,NONCE)
    def test_source_error_can_be_validated_for_safe_cleanup(self):
        f=frame(b'IOException: not available',error=True)
        with self.assertRaisesRegex(ValueError,'Target source error'):decode_frame(f,NONCE)
        self.assertIn('IOException',decode_frame(f,NONCE,allow_error=True)['error'])
        with self.assertRaises(ProtocolError):decode_frame(f,bytes(16),allow_error=True)
    def test_source_paths(self):
        self.assertEqual(source_name('C:\\工具\\日本語.bin'),'日本語.bin')
        for p in ('relative.txt','\\\\server\\x','C:\\a:stream','C:\\..\\x','C:\\NUL.txt','C:\\x\n'):
            with self.assertRaises(ValueError):source_name(p)
    def test_fixed_bootstrap_contains_no_source_code_injection(self):
        command=bootstrap("C:\\中文\\file';Remove-Item.bin",NONCE.hex())
        self.assertNotIn('Remove-Item',command);self.assertNotIn('\n',command)
    def test_cached_helper_is_version_checked(self):
        cached=bootstrap('C:\\test.bin',NONCE.hex(),True)
        self.assertIn('::Build',cached);self.assertLess(len(cached),400)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.dest=self.root/'dest';self.dest.mkdir()
        self.manager=CopyManager(self.root/'state');self.client=Mock();self.client.get_device_info.return_value=INFO
        self.j=self.manager.prepare(self.client,ENDPOINT,'C:\\测试\\日本語.bin',str(self.dest),False)
        self.j=self.manager._job(self.j['job_id']);self.nonce=bytes.fromhex(self.j['job_id'])
        self.addCleanup(lambda:self.manager.lease.close() if self.manager.lease else None)
    def packet(self,data,index=0,whole=None):return decode_frame(frame(data,index,self.nonce,whole=whole),self.nonce)
    def test_out_of_order_lost_chunk_resume_and_atomic_completion(self):
        data=bytes(range(256))*180
        self.assertEqual(self.manager._accept(self.j,self.packet(data,2)),0)
        self.assertEqual(self.manager._accept(self.j,self.packet(data,0)),1)
        resumed=CopyManager(self.root/'state');j=resumed._job(self.j['job_id'])
        self.assertEqual(resumed._accept(j,self.packet(data,1)),3)
        self.assertIsNone(resumed._accept(j,self.packet(data,3)))
        self.assertFalse(Path(j['destination']).exists());resumed._finish(j)
        self.assertEqual(Path(j['destination']).read_bytes(),data);self.assertTrue(j['verified']);self.assertFalse(resumed._parts(j).exists())
    def test_whole_hash_mismatch_forbids_completion(self):
        self.manager._accept(self.j,self.packet(b'abc',whole=bytes(32)))
        with self.assertRaises(ValueError):self.manager._finish(self.j)
        self.assertFalse(Path(self.j['destination']).exists());self.assertFalse(self.j['verified'])
    def test_source_change_is_rejected_on_resume(self):
        self.manager._accept(self.j,self.packet(b'first'))
        with self.assertRaises(ValueError):self.manager._accept(self.j,self.packet(b'second'))
    def test_conflict_before_and_during_copy(self):
        dest=Path(self.j['destination']);dest.write_bytes(b'user')
        with self.assertRaises(ValueError):self.manager.prepare(self.client,ENDPOINT,'C:\\x\\日本語.bin',str(self.dest),False)
        self.manager._accept(self.j,self.packet(b'test'))
        with self.assertRaises(FileExistsError):self.manager._finish(self.j)
        self.assertEqual(dest.read_bytes(),b'user')
    def test_empty_file(self):
        self.manager._accept(self.j,self.packet(b''));self.manager._finish(self.j)
        self.assertEqual(Path(self.j['destination']).stat().st_size,0)
    def test_corrupted_staging_is_reacquired(self):
        p=self.packet(b'original');self.manager._accept(self.j,p)
        (self.manager._parts(self.j)/'0.bin').write_bytes(b'corrupt')
        self.manager._accept(self.j,p);self.manager._finish(self.j)
        self.assertEqual(Path(self.j['destination']).read_bytes(),b'original')
    def test_identity_endpoint_focus_and_interlock(self):
        self.manager.observe(ENDPOINT)
        for endpoint,confirmed in (({**ENDPOINT,'port':9002},True),(ENDPOINT,False)):
            with self.assertRaises(ValueError):self.manager._bind(self.j,self.client,endpoint,confirmed)
        self.client.get_device_info.return_value={**INFO,'capture':{'device':'video-b'}}
        with self.assertRaises(ValueError):self.manager._bind(self.j,self.client,ENDPOINT,True)
        self.client.get_device_info.return_value=INFO;self.manager.interlocked=lambda:True
        with self.assertRaises(ValueError):self.manager._bind(self.j,self.client,ENDPOINT,True)
        self.client.send_key.assert_not_called()
    def test_shared_endpoint_lease(self):
        one=EndpointLease(self.root/'leases',ENDPOINT)
        try:
            with self.assertRaises(ValueError):EndpointLease(self.root/'leases',ENDPOINT)
            two=EndpointLease(self.root/'leases',{**ENDPOINT,'port':9002});two.close()
        finally:one.close()
        again=EndpointLease(self.root/'leases',ENDPOINT);again.close()
    def test_cancel_preserves_chunks_and_does_not_send_keys(self):
        self.manager._accept(self.j,self.packet(b'abc'));self.manager.cancel(self.j['job_id'])
        self.assertEqual(self.j['state'],'cancelled');self.assertTrue(self.manager._parts(self.j).exists());self.client.send_key.assert_not_called()
    def test_worker_recaptures_corruption_then_completes(self):
        self.manager._capture=Mock(side_effect=[Image.new('RGB',(1920,1080)),frame(b'abc',nonce=self.nonce)])
        self.manager._receive(self.client,self.j)
        self.assertEqual(self.j['state'],'completed');self.assertEqual(self.j['retries'],1)
    def test_worker_respects_interlock_and_process_restart(self):
        self.manager.interlocked=lambda:True;self.manager._receive(self.client,self.j)
        self.assertEqual(self.j['state'],'paused');self.client.send_key.assert_not_called()
        self.j['state']='receiving';self.manager._save(self.j)
        restored=CopyManager(self.root/'state').status(self.j['job_id']);self.assertEqual(restored['state'],'paused')
    def test_persisted_ownership_survives_restart_and_aliases(self):
        self.j['state']='typed';self.manager._save(self.j)
        other=CopyManager(self.root/'state')
        self.assertEqual(other.busy({**ENDPOINT,'name':'alias'}),self.j['job_id'])
        self.assertIsNone(other.busy({**ENDPOINT,'port':9002}))
        self.j['target_cleanup']='caller_verified_closed';self.manager._save(self.j)
        self.assertIsNone(other.busy(ENDPOINT))


if __name__=='__main__':unittest.main()
