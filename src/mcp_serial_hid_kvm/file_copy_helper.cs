// Fixed-purpose Windows screen sender. No network, shell, clipboard or disk writes.
using System;
using System.IO;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Windows.Forms;

public class KvmFileSender : Form {
    public const string Build="__KVM_BUILD__";
    [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr value);
    const int Cols=512, Rows=240, Chunk=15000;
    byte[] data, whole, nonce; int page, total, cw, ch, gx, gy;
    Bitmap bitmap; FileStream source; string digits="", error;
    Timer expiry;
    static byte[] Hash(byte[] b) { using(var h=SHA256.Create()) return h.ComputeHash(b); }
    public static void Run(string path, string token, int limit) {
        // Keep the Forms message loop (including its quit message) off the
        // caller's PowerShell/Office COM apartment. Dispose all UI on its owner.
        Exception failure=null;
        var senderThread=new System.Threading.Thread(()=>{
            IntPtr old=SetThreadDpiAwarenessContext(new IntPtr(-4));
            try { using(var f=new KvmFileSender(path,token,limit)) Application.Run(f); }
            catch(Exception e) { failure=e; }
            finally { if(old!=IntPtr.Zero) SetThreadDpiAwarenessContext(old); }
        });
        senderThread.SetApartmentState(System.Threading.ApartmentState.STA);
        senderThread.Start(); senderThread.Join();
        if(failure!=null) throw new IOException("sender UI failed",failure);
    }
    KvmFileSender(string path,string token,int limit) {
        nonce=Convert.FromBase64String(token);
        try {
            source=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read);
            if(source.Length>limit) throw new IOException("source exceeds byte limit");
            data=new byte[(int)source.Length]; int n=0;
            while(n<data.Length) { int k=source.Read(data,n,data.Length-n); if(k==0) throw new IOException("short source read"); n+=k; }
        } catch(Exception e) { error=e.GetType().Name+": "+e.Message; data=new byte[0]; }
        whole=Hash(data); total=Math.Max(1,(data.Length+Chunk-1)/Chunk);
        FormBorderStyle=FormBorderStyle.None; StartPosition=FormStartPosition.Manual;
        Bounds=Screen.PrimaryScreen.Bounds; BackColor=Color.Black; TopMost=true; KeyPreview=true;
        cw=Math.Min(3,(Width-100)/Cols); ch=Math.Min(4,(Height-120)/Rows);
        if(cw<2 || ch<2) { if(source!=null) source.Dispose(); throw new IOException("display too small: minimum 1124x600"); }
        gx=(Width-Cols*cw)/2; gy=(Height-Rows*ch)/2;
        DoubleBuffered=true;
        expiry=new Timer(); expiry.Interval=600000; expiry.Tick+=(s,e)=>Close(); expiry.Start();
        KeyDown+=(s,e)=>{
            expiry.Stop(); expiry.Start();
            if(e.KeyCode==Keys.Escape) Close();
            else if(e.KeyCode==Keys.Right) { page=Math.Min(total-1,page+1); Render(); }
            else if(e.KeyCode==Keys.Left) { page=Math.Max(0,page-1); Render(); }
            else if(e.KeyCode==Keys.Home) { page=0; Render(); }
            else if(e.KeyCode>=Keys.D0 && e.KeyCode<=Keys.D9) { if(digits.Length<7) digits+=(int)e.KeyCode-(int)Keys.D0; }
            else if(e.KeyCode==Keys.Enter) { int p; if(int.TryParse(digits,out p)&&p>=0&&p<total) {page=p; Render();} digits=""; }
        };
        Shown+=(s,e)=>{ Activate(); Focus(); };
        Render();
    }
    void Render() {
        int count=Math.Min(Chunk,data.Length-page*Chunk);
        byte[] payload=new byte[count]; if(count>0) Buffer.BlockCopy(data,page*Chunk,payload,0,count);
        if(error!=null) { payload=System.Text.Encoding.UTF8.GetBytes(error); if(payload.Length>512) Array.Resize(ref payload,512); count=payload.Length; }
        byte[] packet;
        using(var ms=new MemoryStream()) using(var bw=new BinaryWriter(ms)) {
            bw.Write(System.Text.Encoding.ASCII.GetBytes("KVMFILE1")); bw.Write(nonce);
            bw.Write(page); bw.Write(total); bw.Write(count); bw.Write((long)data.Length);
            bw.Write(whole); bw.Write(Hash(payload)); byte[] header=ms.ToArray();
            byte[] check=Hash(header); bw.Write(check,0,16); bw.Write(error!=null?1:0); bw.Write(payload); packet=ms.ToArray();
        }
        int w=Cols*cw,h=Rows*ch; int[] pixels=new int[w*h];
        for(int y=0;y<h;y++) for(int x=0;x<w;x++) {
            int bit=(y/ch)*Cols+x/cw, b=bit/8;
            pixels[y*w+x]=(b<packet.Length && ((packet[b]>>(bit%8))&1)!=0)?unchecked((int)0xffffffff):unchecked((int)0xff000000);
        }
        var next=new Bitmap(w,h,PixelFormat.Format32bppArgb);
        var locked=next.LockBits(new Rectangle(0,0,w,h),ImageLockMode.WriteOnly,PixelFormat.Format32bppArgb);
        Marshal.Copy(pixels,0,locked.Scan0,pixels.Length); next.UnlockBits(locked);
        var previous=bitmap; bitmap=next; if(previous!=null) previous.Dispose(); Invalidate();
    }
    protected override void OnPaint(PaintEventArgs e) {
        base.OnPaint(e);
        e.Graphics.FillRectangle(Brushes.Magenta,gx-6,gy-6,Cols*cw+12,Rows*ch+12);
        e.Graphics.DrawImageUnscaled(bitmap,gx,gy);
        e.Graphics.DrawString("KVM FILE "+(page+1)+"/"+total+" | "+data.Length+" bytes | Esc closes",SystemFonts.DefaultFont,Brushes.White,12,12);
    }
    protected override void Dispose(bool disposing) {
        if(disposing) { if(source!=null) source.Dispose(); if(bitmap!=null) bitmap.Dispose(); if(expiry!=null) expiry.Dispose(); }
        base.Dispose(disposing);
    }
}
