// 自绘原生窗口栏。窗体仍保留系统缩放、最小/最大化、任务栏及键盘 Snap 样式。
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Windows.Forms;
using System.Runtime.InteropServices;
namespace CftNative {
  public class Frame : NativeWindow {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
    [StructLayout(LayoutKind.Sequential)] struct POINT { public int X,Y; }
    [StructLayout(LayoutKind.Sequential)] struct MINMAX { public POINT Reserved,MaxSize,MaxPosition,MinTrack,MaxTrack; }
    [StructLayout(LayoutKind.Sequential)] struct MONITOR { public int Size; public RECT Bounds,Work; public int Flags; }
    [DllImport("user32.dll")] static extern int GetWindowLong(IntPtr h,int i);
    [DllImport("user32.dll")] static extern int SetWindowLong(IntPtr h,int i,int v);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr h,IntPtr a,int x,int y,int w,int z,uint f);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h,out RECT r);
    [DllImport("user32.dll")] static extern bool IsZoomed(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr MonitorFromWindow(IntPtr h,int f);
    [DllImport("user32.dll")] static extern bool GetMonitorInfo(IntPtr h,ref MONITOR m);
    [DllImport("user32.dll")] static extern bool ReleaseCapture();
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr h,int m,IntPtr w,IntPtr l);
    [DllImport("user32.dll")] static extern bool RedrawWindow(IntPtr h,IntPtr r,IntPtr region,uint flags);
    public void Repaint(){RedrawWindow(Handle,IntPtr.Zero,IntPtr.Zero,0x485);}
    public void BeginResize(int edge){
      if(edge<1||edge>8||IsZoomed(Handle))return;
      // 常态不添加 THICKFRAME，避免 WinForms 无框窗口留下旧尺寸非客户区白框。
      int style=GetWindowLong(Handle,-16);SetWindowLong(Handle,-16,style|0x00040000);Refresh();
      try{ReleaseCapture();SendMessage(Handle,0x112,(IntPtr)(0xf000|edge),IntPtr.Zero);}
      finally{if(Handle!=IntPtr.Zero){SetWindowLong(Handle,-16,GetWindowLong(Handle,-16)&~0x00040000);Refresh();Repaint();}}
    }
    public int TitleHeight=48, Edge=6, ControlsWidth=138;
    int oldStyle;
    public Frame(IntPtr hwnd) { AssignHandle(hwnd);oldStyle=GetWindowLong(hwnd,-16);SetWindowLong(hwnd,-16,(oldStyle & ~0x00c40000)|0x00010000|0x00020000|0x00080000);Refresh(); }
    public void Refresh() { SetWindowPos(Handle,IntPtr.Zero,0,0,0,0,0x37); }
    public int Hit(int x,int y) {
      RECT r;GetWindowRect(Handle,out r);x-=r.L;y-=r.T;
      if(!IsZoomed(Handle)) {
        bool l=x<Edge,t=y<Edge,rr=x>=r.R-r.L-Edge,b=y>=r.B-r.T-Edge;
        if(t&&l)return 13;if(t&&rr)return 14;if(b&&l)return 16;if(b&&rr)return 17;
        if(l)return 10;if(rr)return 11;if(t)return 12;if(b)return 15;
      }
      return y<TitleHeight && x<r.R-r.L-ControlsWidth ? 2 : 1;
    }
    protected override void WndProc(ref Message m) {
      if(m.Msg==0x83 && m.WParam!=IntPtr.Zero) { // WM_NCCALCSIZE: 内容扩展到整个窗体。
        // 不把监视器绝对坐标写进 NCCALCSIZE，最大化工作区在 GETMINMAXINFO 管理。
        m.Result=IntPtr.Zero;return;
      }
      if(m.Msg==0x24){ // WM_GETMINMAXINFO: 尊重任务栏、多屏和 WinForms 最小尺寸。
        base.WndProc(ref m);MONITOR v=new MONITOR();v.Size=Marshal.SizeOf(v);
        if(GetMonitorInfo(MonitorFromWindow(Handle,2),ref v)){
          MINMAX limits=(MINMAX)Marshal.PtrToStructure(m.LParam,typeof(MINMAX));
          limits.MaxPosition.X=v.Work.L-v.Bounds.L;limits.MaxPosition.Y=v.Work.T-v.Bounds.T;
          limits.MaxSize.X=v.Work.R-v.Work.L;limits.MaxSize.Y=v.Work.B-v.Work.T;
          Marshal.StructureToPtr(limits,m.LParam,false);
        }return;
      }
      if(m.Msg==0x84){long p=m.LParam.ToInt64();m.Result=(IntPtr)Hit((short)(p&0xffff),(short)((p>>16)&0xffff));return;}
      base.WndProc(ref m);
    }
    public void RestoreFrame(){IntPtr h=Handle;int state=GetWindowLong(h,-16)&0x31000000;SetWindowLong(h,-16,(oldStyle&~0x31000000)|state);ReleaseHandle();SetWindowPos(h,IntPtr.Zero,0,0,0,0,0x27);}
  }
  // 视频首帧出图：借系统外壳（Explorer 同款）的缩略图工厂，让 Windows 自己的解码器出像素。
  // 约束是不装 ffmpeg、不加第三方依赖，而 file:// 页面里 Chromium 又必然把画布标脏（toDataURL 抛
  // SecurityError），所以首帧只能由程序自己落到磁盘上。失败一律返回 null 并把原因留在 LastError，
  // 绝不把异常抛回 Python（上层据此安静回退到浏览器抓帧）。
  public static class VideoThumb {
    public static string LastError="";

    [StructLayout(LayoutKind.Sequential)] struct SIZE{public int cx;public int cy;}

    // IShellItemImageFactory。两个易错点：InterfaceType 必须是 InterfaceIsIUnknown（留默认值会去 QI
    // IDispatch，实测就是 E_NOINTERFACE 的来源）；GetImage 是这个接口 vtable 的第 0 个方法，紧跟在
    // IUnknown 三槽后面，声明顺序错一格就会调到 AddRef 上。
    [ComImport,Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellItemImageFactory {
      [PreserveSig] int GetImage(SIZE size,uint flags,out IntPtr phbm);
    }

    [DllImport("shell32.dll",CharSet=CharSet.Unicode)]
    static extern int SHCreateItemFromParsingName(
      [MarshalAs(UnmanagedType.LPWStr)] string path,IntPtr pbc,ref Guid riid,
      [MarshalAs(UnmanagedType.Interface)] out IShellItemImageFactory factory);
    [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr hObject);

    // IIGBF_*：0=RESIZETOFIT 等比放进框内；8=THUMBNAILONLY 只认真缩略图（否则拿不到解码帧时
    // 会返回一个通用文件图标，那种"成功"对海报毫无意义，必须当失败）。
    const uint IIGBF_RESIZETOFIT=0x0,IIGBF_THUMBNAILONLY=0x8;

    static string Normalize(string path){
      if(string.IsNullOrEmpty(path)||path.IndexOf('\0')>=0)return null;
      try{return Path.GetFullPath(path.Replace('/','\\'));}catch(Exception){return null;}
    }

    // 入口：返回 JPEG 字节，失败返回 null。maxSize 是长边像素上限，quality 缺省 82（与网页端海报一致）。
    public static byte[] FirstFrameJpeg(string path,int maxSize,int quality){
      LastError="";
      string full=Normalize(path);
      if(full==null){LastError="路径无效";return null;}
      if(!File.Exists(full)){LastError="文件不存在: "+full;return null;}
      if(maxSize<16)maxSize=16;else if(maxSize>4096)maxSize=4096;
      IntPtr hbmp=IntPtr.Zero;
      try{
        Guid riid=new Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b");
        IShellItemImageFactory factory;
        int hr=SHCreateItemFromParsingName(full,IntPtr.Zero,ref riid,out factory);
        if(hr!=0||factory==null){LastError="SHCreateItemFromParsingName 0x"+hr.ToString("x8");return null;}
        SIZE box=new SIZE();box.cx=maxSize;box.cy=maxSize;
        hr=factory.GetImage(box,IIGBF_RESIZETOFIT|IIGBF_THUMBNAILONLY,out hbmp);
        if(hr!=0||hbmp==IntPtr.Zero){
          hbmp=IntPtr.Zero;   // 失败时不能信 out 值，避免删到野句柄
          LastError="GetImage 0x"+hr.ToString("x8");
          return null;
        }
        using(System.Drawing.Image raw=Image.FromHbitmap(hbmp)){
          int w=raw.Width,h=raw.Height;
          if(w<2||h<2){LastError="缩略图尺寸异常 "+w+"x"+h;return null;}
          // JPEG 没有 alpha 通道。直接往 24bpp 上画会按黑底做混合，透明图就整张发暗；先无损丢掉
          // alpha 通道（Bitmap.Clone 到 Format32bppRgb 是转通道，不是合成），再缩放。
          PixelFormat fmt=raw.PixelFormat;
          bool alpha=fmt==PixelFormat.Format32bppArgb||fmt==PixelFormat.Format32bppPArgb||
                     fmt==PixelFormat.Format64bppArgb||fmt==PixelFormat.Format64bppPArgb;
          Bitmap stripped=null;
          System.Drawing.Image source=raw;
          try{
            Bitmap asBitmap=raw as Bitmap;
            if(alpha&&asBitmap!=null){stripped=asBitmap.Clone(new Rectangle(0,0,w,h),PixelFormat.Format32bppRgb);source=stripped;}
            int tw=w,th=h;
            if(w>maxSize||h>maxSize){
              double k=(double)maxSize/(double)Math.Max(w,h);
              tw=Math.Max(1,(int)Math.Round(w*k));th=Math.Max(1,(int)Math.Round(h*k));
            }
            using(Bitmap fit=new Bitmap(tw,th,PixelFormat.Format24bppRgb)){
              using(Graphics g=Graphics.FromImage(fit)){
                g.InterpolationMode=System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
                g.PixelOffsetMode=System.Drawing.Drawing2D.PixelOffsetMode.Half;
                g.SmoothingMode=System.Drawing.Drawing2D.SmoothingMode.HighQuality;
                // 目标矩形不含边界膨胀，奇数尺寸/极端比例都不会裁掉边缘像素。
                g.DrawImage(source,new Rectangle(0,0,tw,th),new Rectangle(0,0,w,h),GraphicsUnit.Pixel);
              }
              byte[] bytes=Encode(fit,quality);
              LastError="";
              return bytes;
            }
          }finally{if(stripped!=null)stripped.Dispose();}
        }
      }catch(OutOfMemoryException){LastError="GDI+ 解码失败（位图过大或格式损坏）";return null;}
      catch(Exception e){LastError=e.GetType().Name+": "+Trim(e.Message);return null;}
      finally{if(hbmp!=IntPtr.Zero)DeleteObject(hbmp);}
    }

    static string Trim(string s){try{return s.Length>160?s.Substring(0,160):s;}catch(Exception){return"";}}

    static byte[] Encode(Image img,int quality){
      if(quality<1||quality>100)quality=82;
      ImageCodecInfo codec=null;
      foreach(ImageCodecInfo c in ImageCodecInfo.GetImageEncoders()){if(c.FormatID==ImageFormat.Jpeg.Guid){codec=c;break;}}
      using(MemoryStream ms=new MemoryStream()){
        if(codec==null)img.Save(ms,ImageFormat.Jpeg);
        else{
          using(EncoderParameters p=new EncoderParameters(1)){
            p.Param[0]=new EncoderParameter(System.Drawing.Imaging.Encoder.Quality,(long)quality);
            img.Save(ms,codec,p);
          }
        }
        return ms.ToArray();
      }
    }
  }
  public class PassCaption : NativeWindow {
    public PassCaption(Control c){AssignHandle(c.Handle);}
    protected override void WndProc(ref Message m){if(m.Msg==0x84){m.Result=(IntPtr)(-1);return;}base.WndProc(ref m);}
  }
  public class CaptionButton : Button {
    public int Kind; public Color Normal,Hover; public bool Maximized;
    public CaptionButton(int kind){Kind=kind;FlatStyle=FlatStyle.Flat;FlatAppearance.BorderSize=0;TabStop=true;Text="";SetStyle(ControlStyles.UserPaint|ControlStyles.OptimizedDoubleBuffer,true);AccessibleName=kind==0?"最小化":kind==1?"最大化或还原":"关闭";}
    protected override void OnMouseEnter(EventArgs e){BackColor=Hover;base.OnMouseEnter(e);}
    protected override void OnMouseLeave(EventArgs e){BackColor=Normal;base.OnMouseLeave(e);}
    protected override void OnPaint(PaintEventArgs e){
      e.Graphics.Clear(BackColor);float s=Height/42f,cx=Width/2f,cy=Height/2f,d=5*s;
      using(Pen pen=new Pen(ForeColor,1.3f*s)){
        if(Kind==0)e.Graphics.DrawLine(pen,cx-d,cy+2*s,cx+d,cy+2*s);
        else if(Kind==2){e.Graphics.DrawLine(pen,cx-d,cy-d,cx+d,cy+d);e.Graphics.DrawLine(pen,cx-d,cy+d,cx+d,cy-d);}
        else {if(Maximized)e.Graphics.DrawRectangle(pen,cx-d+2*s,cy-d-2*s,d*2,d*2);e.Graphics.DrawRectangle(pen,cx-d,cy-d,d*2,d*2);}
      }
    }
  }
}
