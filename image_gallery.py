"""图片预览/生成数据：只解析来源已提供的信息，不推测缺失提示词或使用资源。"""
import io
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

from comfy_metadata import PARSER_REVISION, extract_prompts

# 兼容旧引用（剪贴板阈值等）。静帧/视频的实际体积上限请看 static_limit_bytes / video_limit_bytes。
LIMIT = 25 * 1024 * 1024

# 视频源只按原样取字节：绝不显示降级，也绝不进 PIL 解码路径。仅按 URL path 尾缀判定。
VIDEO_SUFFIXES = ('.mp4', '.webm', '.mov', '.mkv', '.avi')


def _limit_mb(cfg, key, default):
    """体积上限（MB）。同一个配置键在全项目只按这一套规则解释：
    脏值/非正数退回默认，上限与 config.normalize_ui_preferences 里最宽的一条（video_max_mb 4096）对齐，
    避免用户设了大值却被这里悄悄截小。"""
    try:
        value = int(cfg.get(key) or default)
    except (AttributeError, TypeError, ValueError, OverflowError):
        value = default
    if value <= 0:
        value = default
    return max(1, min(4096, value))


def static_limit_mb(cfg):
    """静帧上限档位（MB）：默认 20MB —— C站原图均值 3.2MB，超过它多半是异常大图。"""
    return _limit_mb(cfg, 'image_max_mb', 20)


def video_limit_mb(cfg):
    """视频上限档位（MB）：默认 100MB —— 实测视频均值 19MB，套用 20MB 口径必然压线失败。"""
    return _limit_mb(cfg, 'video_max_mb', 100)


def static_limit_bytes(cfg):
    return static_limit_mb(cfg) * 1024 * 1024


def video_limit_bytes(cfg):
    return video_limit_mb(cfg) * 1024 * 1024


def store_limit_bytes(cfg):
    """缓存单文件读写阈值：跟随原图档最大口径，否则 19MB 视频永远存不下。"""
    return max(static_limit_bytes(cfg), video_limit_bytes(cfg))


def fetch_deadline_seconds(cfg):
    """远程抓取总时长（秒）。socket 级 timeout 管不住慢速流，必须另有 wall-clock 截止时间。
    范围与 config.normalize_ui_preferences 的 fetch_deadline_s 一致（5..3600），不在此处二次收窄。"""
    try:
        value = float(cfg.get('fetch_deadline_s') or 120)
    except (AttributeError, TypeError, ValueError, OverflowError):
        value = 120.0
    if value <= 0:
        value = 120.0
    return max(5.0, min(3600.0, value))


def _timeout_message(cfg):
    return f'下载超时：超过 {int(fetch_deadline_seconds(cfg))} 秒仍未完成，已中止；可稍后重试或在 C站原图页查看'


def _too_large_message(video, cfg):
    if video:
        return f'视频超过 {video_limit_mb(cfg)} MB，未完整下载；可打开 C站原图页直接播放'
    return f'图片超过 {static_limit_mb(cfg)} MB，建议在 C站原图页查看'

# 原图和生成数据为私有运行数据；不进入源码/发行包。缓存失败不得影响查看。
import hashlib
import os
import tempfile
import threading
from pathlib import Path
_cache_lock = threading.RLock()

def source_revision(item):
    """仅用于检测来源变化，不暴露路径、链接参数或账号值。"""
    source=str(item.get('url') or item.get('orig_url') or '')
    if item.get('local_path'):
        try:
            st=os.stat(item['local_path']);source=str(Path(item['local_path']).resolve())+':'+str(st.st_mtime_ns)+':'+str(st.st_size)
        except OSError:source=str(item['local_path'])+':missing'
    if not source:source=hashlib.sha256(str(item.get('b64','')).encode()).hexdigest()
    return hashlib.sha256(source.encode()).hexdigest()


def _positive_int(value, upper=None):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number <= 0:
        return None
    if upper is not None:
        number = min(number, upper)
    return number


def cache_tier(max_width=None, quality=None):
    """显示档 -> 缓存键后缀；无档位返回 ''，与历史键完全一致（已有缓存不会作废）。"""
    width = _positive_int(max_width, 4096)
    quality = _positive_int(quality, 100)
    if width is None and quality is None:
        return ''
    return ':' + (f'w{width}' if width else '') + (f'q{quality}' if quality else '')


def cache_key(item, cfg, metadata=False, tier=''):
    if not cfg.get('cache_original_images', False):return None
    source=item.get('url') or item.get('orig_url')
    if item.get('local_path'):
        try:
            st=os.stat(item['local_path']);source=str(Path(item['local_path']).resolve())+':'+str(st.st_mtime_ns)+':'+str(st.st_size)
        except OSError:return None
    if not source:return None
    if tier:source += str(tier)
    if metadata:source += ':'+PARSER_REVISION+':meta:'+hashlib.sha256(str(cfg.get('api_key','')).encode()).hexdigest()
    import config
    return Path(config.APP_DIR)/'gallery_cache'/hashlib.sha256(source.encode()).hexdigest()

def cache_load(item, cfg, kind, tier=''):
    key=cache_key(item,cfg,kind!='original',tier)
    if key is None:return None
    p=key.with_suffix('.'+kind)
    if key.parent.is_symlink() or p.is_symlink():return None
    try:
        with _cache_lock:
            if p.stat().st_size>store_limit_bytes(cfg):return None
            data=p.read_bytes();os.utime(p,None)
            if kind=='preview':
                # 显示档预览同样要保住自己的字节档，同时顺带保住原图档（默认档行为不变）。
                for variant in dict.fromkeys(k for k in (cache_key(item,cfg,False,tier),cache_key(item,cfg,False,'')) if k):
                    original=variant.with_suffix('.original')
                    if original.is_file() and not original.is_symlink():os.utime(original,None)
        return data if kind=='original' else json.loads(data.decode(),parse_constant=lambda _:None)
    except (OSError,ValueError,UnicodeError):return None

FLAT_MAX_COLORS=4      # 缩略到 64px 后仍只有这几个不同颜色 = 没有画面
FLAT_STDDEV_FLOOR=3.0  # 三通道标准差都低于这个数 = 纯色（黑场/白场）

def is_flat_image(data):
    """整帧只有一个颜色的「伪首帧」判定；输入是图片字节，不是路径。

    浏览器在解码出画面之前 drawImage(video) 不会报错，而是端出一张纯黑画布——抓帧回传照写就把
    用户看到的「首帧空白」从空白换成了一块黑，还因为「本地已有首帧不覆盖」把后端外壳取帧这条
    真出路永久挡在门外（实测一条：4307B 纯色图 vs 同帧外壳出的 32045B/60829 色）。
    判据按 64px 缩略算，代价恒定；直方图算标准差而不是 import ImageStat——PyInstaller 的
    hiddenimports 没收了那个子模块，冻结产物里 import 失败会静默放行，等于修复没进包。
    认不出来（无 PIL/坏字节）一律返回 False：宁可放行一张可疑图，也不要把功能判死。"""
    try:
        from PIL import Image
    except Exception:
        return False
    try:
        with Image.open(io.BytesIO(data)) as im:
            im=im.convert("RGB")
            im.thumbnail((64,64))
            colors=im.getcolors(maxcolors=64*64+1)
            if colors is None:
                return False          # 颜色多到超上限，明显不是纯色
            hist=im.histogram()
            stds=[]
            for k in range(3):
                band=hist[k*256:(k+1)*256]
                n=float(sum(band)) or 1.0
                mean=sum(i*c for i,c in enumerate(band))/n
                stds.append((max(0.0,sum((i-mean)**2*c for i,c in enumerate(band))/n))**0.5)
            return len(colors)<=FLAT_MAX_COLORS or max(stds)<FLAT_STDDEV_FLOOR
    except Exception:
        return False

def is_flat_image_file(path):
    """按文件走 is_flat_image；读不到/太大都不判死（返回 False）。"""
    try:
        if not path or not os.path.isfile(path) or os.path.getsize(path)>8*1024*1024:
            return False
        with open(path,"rb") as f:
            return is_flat_image(f.read())
    except OSError:
        return False


def is_reparse_path(path):
    """Windows 重解析点（符号链接 / junction / mount point）检测。

    `os.path.islink()` 对 junction 一律返回 False（Python 3.12 实测），而 `mklink /J` 不需要管理员权限
    就能造出来：`<名>.images` 做成指向别处的 junction 时，realpath 的「同层」校验照样通过，写盘就会
    落到一个和模型无关的目录。所以守卫必须看 FILE_ATTRIBUTE_REPARSE_POINT，不能只信 islink。"""
    try:
        st=os.stat(path,follow_symlinks=False)
    except (OSError,ValueError):
        return False
    return bool(getattr(st,'st_file_attributes',0)&0x400)


def open_exclusive_tmp(path):
    """独占创建写盘用的 .tmp 并返回已打开的 fd（调用方用 os.fdopen 接管）。

    为什么要独占：这些临时文件落在用户的模型目录里，预先放一个好名字的文件/链接就能把写入引到别处。
    为什么不能「存在就报错」：下载中断、进程被杀都会留下普通残留 .tmp，而补帧每次打开详情都会重试同一批
    目标——一律拒绝会让那一张图永远补不进去。所以只对「残留的普通文件」清掉重来，目录与链接直接抛错。"""
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
    try:
        return os.open(path,flags,0o644)
    except FileExistsError:
        if os.path.isdir(path) or is_reparse_path(path):raise
        os.unlink(path)
        return os.open(path,flags,0o644)


def cache_store(item, cfg, kind, value, tier=''):
    key=cache_key(item,cfg,kind!='original',tier)
    if key is None:return
    tmp=None
    try:
        data=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        if len(data)>store_limit_bytes(cfg):return
        with _cache_lock:
            if key.parent.is_symlink():return
            key.parent.mkdir(parents=True,exist_ok=True)
            fd,tmp=tempfile.mkstemp(dir=key.parent,prefix='.gallery-',suffix='.tmp')
            with os.fdopen(fd,'wb') as f:f.write(data)
            os.replace(tmp,key.with_suffix('.'+kind));tmp=None
            try:budget=max(64,min(65536,int(cfg.get('gallery_cache_mb') or 8192)))*1024*1024
            except (ValueError,TypeError,OverflowError):budget=8192*1024*1024
            files=[(p,p.stat()) for p in key.parent.iterdir() if p.suffix in ('.original','.preview','.metadata') and not p.is_symlink()]
            total=sum(st.st_size for _,st in files)
            for p,st in sorted(files,key=lambda v:v[1].st_mtime):
                if total<=budget:break
                p.unlink(missing_ok=True);total-=st.st_size
    except (OSError,ValueError,TypeError):pass
    finally:
        if tmp:
            try:os.unlink(tmp)
            except OSError:pass

def clear_cache():
    import config
    folder=Path(config.APP_DIR)/'gallery_cache';count=0
    if folder.is_symlink():raise ValueError('缓存目录不能是符号链接')
    with _cache_lock:
        if folder.exists():
            for p in folder.iterdir():
                if p.is_file() and not p.is_symlink() and len(p.stem)==64 and p.suffix in ('.original','.preview','.metadata'):
                    p.unlink();count+=1
    return count

def comfy_generation(graph):
    """只读解析标准 ComfyUI API 图中实际连接的节点，不执行或猜测未知节点。"""
    if not isinstance(graph,dict) or len(graph)>10000:return {}
    sampler=next((n for n in graph.values() if isinstance(n,dict) and n.get('class_type') in ('KSampler','KSamplerAdvanced','SamplerCustom','SamplerCustomAdvanced')),None)
    if not sampler:return extract_prompts(graph)
    inputs=sampler.get('inputs') or {}
    if not isinstance(inputs,dict):return {}
    def nodes(reference):
        seen=set();pending=[reference];out=[]
        while pending and len(seen)<256:
            value=pending.pop()
            if not isinstance(value,list) or len(value)<2 or str(value[0]) not in graph:continue
            key=str(value[0])
            if key in seen:continue
            seen.add(key);node=graph[key]
            if not isinstance(node,dict):continue
            out.append(node);values=node.get('inputs') or {}
            if isinstance(values,dict):pending.extend(v for v in values.values() if isinstance(v,list))
        return out
    result=extract_prompts(graph)
    for key in ('seed','noise_seed','steps','cfg','sampler_name','scheduler','denoise'):
        value=inputs.get(key)
        if isinstance(value,(str,int,float)) and not isinstance(value,bool):result[{'cfg':'cfgScale','sampler_name':'sampler','noise_seed':'seed'}.get(key,key)]=value
    for node in nodes(inputs.get('latent_image')):
        values=node.get('inputs') or {}
        for key in ('width','height'):
            if isinstance(values.get(key),(int,float)):result.setdefault(key,values[key])
    resources=[]
    for node in nodes(inputs.get('model')):
        values=node.get('inputs') or {}
        for field,kind in [('ckpt_name','CHECKPOINT'),('lora_name','LORA'),('unet_name','DIFFUSION_MODEL')]:
            name=values.get(field)
            if isinstance(name,str) and name:
                resource={'name':name.replace('\\','/').split('/')[-1],'type':kind}
                if resource not in resources:resources.append(resource)
    if resources:result['resources']=resources
    return result


def resource_ids(resource):
    """只读明确的模型/版本字段、站点模型 URL 和 AIR，不把普通数字名字当模型 ID。"""
    from urllib.parse import parse_qs
    model=resource.get('model');version=resource.get('modelVersion')
    mid=resource.get('modelId') or (model.get('id') if isinstance(model,dict) else None)
    vid=resource.get('modelVersionId') or resource.get('versionId') or (version.get('id') if isinstance(version,dict) else None)
    for value in [resource.get(k) for k in ('air','resource','urn','name','url','modelUrl','modelPage')]:
        if not isinstance(value,str): continue
        air=re.search(r'urn:air:[^\s:]+:[^\s:]+:civitai:(\d+)@(\d+)',value)
        if air: mid=mid or air[1];vid=vid or air[2]
        parsed=urlsplit(value)
        if parsed.hostname in ('civitai.com','civitai.red','www.civitai.com','www.civitai.red'):
            match=re.match(r'^/models/(\d+)(?:/|$)',parsed.path)
            if match: mid=mid or match[1]
            q=parse_qs(parsed.query);vid=vid or (q.get('modelVersionId') or [None])[0]
            match=re.match(r'^/model-versions/(\d+)(?:/|$)',parsed.path)
            if match: vid=vid or match[1]
    return (int(mid) if str(mid or '').isdigit() and int(mid)>0 else None,int(vid) if str(vid or '').isdigit() and int(vid)>0 else None)


DEFAULT_SITE_BASE = 'https://civitai.com'


def image_page_url(image_id, site_base=''):
    """图片页链接：跟随调用方给的站点根（设置里的 site_domain），缺省 civitai.com。"""
    base = str(site_base or DEFAULT_SITE_BASE).strip().rstrip('/')
    if '://' not in base:
        base = 'https://' + base
    return base + '/images/' + str(image_id)


def generation(image=None, local_path=None, saved=None, site_base=''):
    image=image if isinstance(image,dict) else {}
    meta=dict(image.get('meta') or {}) if isinstance(image.get('meta'),dict) else {}
    source=image.get('metadata_source') or ('C站模型信息缓存' if meta else '未提供生成数据')
    data=saved if isinstance(saved,dict) else {}
    if local_path and saved is None:
        try:
            from PIL import Image
            with Image.open(local_path) as im:data=dict(im.info)
        except Exception:pass
    parameters=data.get('parameters') or meta.get('parameters')
    if isinstance(parameters,str) and parameters.strip():
        parts=re.split(r'\n(?=Steps:\s*\d)',parameters,maxsplit=1)
        positive,_,negative=parts[0].partition('\nNegative prompt:')
        meta.update({'prompt':positive.strip(),'negativePrompt':negative.strip()})
        if len(parts)>1:
            for key,value in re.findall(r'(?:^|,\s*)([^:,]+):\s*([^,]+)',parts[1]):meta[key.strip()]=value.strip()
        embedded=re.search(r'(?:^|,\s*)Civitai resources:\s*(\[.*)',parameters,re.S)
        if embedded:
            try:
                values,_=json.JSONDecoder().raw_decode(embedded[1])
                if isinstance(values,list):meta['civitaiResources']=values
            except (ValueError,TypeError):pass
        source='原图内嵌 PNG parameters'  if saved is not None else '本地图片 PNG parameters'
    elif data.get('prompt') or data.get('workflow'):
        for key in ('prompt','workflow'):
            value=data.get(key)
            if value:
                try:value=json.loads(value,parse_constant=lambda _:None) if isinstance(value,str) else value
                except (ValueError,TypeError):pass
                meta['comfyPrompt' if key=='prompt' and isinstance(value,dict) else key]=value
        source='原图内嵌 ComfyUI 元数据' if saved is not None else '本地图片 ComfyUI 元数据'
    else:
        comment=data.get('UserComment') or data.get('comment')
        if isinstance(comment,bytes):comment=comment.removeprefix(b'ASCII\0\0\0').decode('utf-8',errors='replace')
        if isinstance(comment,str):
            try:
                fields=json.loads(comment,parse_constant=lambda _:None)
                if isinstance(fields,dict):meta.update(fields);source='原图内嵌 EXIF/Comment 元数据'
            except (ValueError,TypeError):pass
    if isinstance(meta.get('comfyPrompt'),dict):
        for key,value in comfy_generation(meta['comfyPrompt']).items():
            if key in ('prompt','negativePrompt','promptExtractionNote'):meta[key]=value
            else:meta.setdefault(key,value)
    elif isinstance(meta.get('workflow'),dict):
        meta.update(extract_prompts(workflow=meta['workflow']))
    resources=meta.get('civitaiResources') or image.get('resources') or meta.get('resources') or []
    if not isinstance(resources,list):resources=[]
    return {'meta':meta,'resources':[v for v in resources if isinstance(v,dict)],'metadata_source':source,
            'image_id':image.get('id') or image.get('image_id'),'width':image.get('width'),'height':image.get('height'),
            'metadata_note':meta.get('promptExtractionNote',''),
            'orig_url':image.get('url') or image.get('orig_url') or '',
            'image_page':image.get('image_page') or (image_page_url(image['id'], site_base) if str(image.get('id','')).isdigit() else '')}


def original_url(url):
    parsed=urlsplit(str(url or ''))
    if parsed.scheme != 'https' or parsed.hostname != 'image.civitai.com' or parsed.username or parsed.password:
        raise ValueError('仅支持 C站官方图片 CDN 的 HTTPS 地址')
    return re.sub(r'/(?:width=[^/]+|original=true[^/]*)/', '/original=true/',url)


def is_video_source(url):
    """仅按 URL path 尾缀判定视频（忽略 query/fragment）。"""
    try:
        path=urlsplit(str(url or '')).path
    except ValueError:
        return False
    return path.lower().endswith(VIDEO_SUFFIXES)


def _is_video_item(item):
    """视频条目：类型标记或任一来源 URL 的尾缀是视频。视频绝不降级、绝不进 PIL。"""
    if not isinstance(item,dict):return False
    if str(item.get('type') or '').lower()=='video':return True
    return is_video_source(item.get('url') or '') or is_video_source(item.get('orig_url') or '')


def display_url(url, max_width=None, quality=None, natural_width=None):
    """把 C站官方图片 CDN 地址改写为显示档；返回 None 表示「不要降级，用原图」。

    实测结论（改动前请先重测）：
    - 全库 info.json 的 params 段 100% 是 original=true，C站 CDN 只认 width=/quality=；
      format=jpeg/webp/avif 与 resize=cover 会被 CDN 直接忽略，禁止使用。
    - 真实 URL 是四段式 /{auth}/{hash}/{params}/{file}，**只能替换已存在的 params 段**，
      新插一段会得到 404 空帧；没有 params 段时宁可不降级。
    - original 与 width 互斥；只请求 quality= 时也必须去掉 original=true，
      否则 CDN 仍按原图输出、quality 变成空操作。
    - 防上采样：对本来就比档位窄的图请求 width= 反而会变大（实测 682KB 原图 -> 1.2MB），
      因此已知 natural_width<=max_width 时只降 quality、不加 width=。
    """
    try:
        parsed=urlsplit(str(url or ''))
    except ValueError:
        return None
    if parsed.scheme!='https' or parsed.hostname!='image.civitai.com' or parsed.username or parsed.password:
        return None
    if is_video_source(url):
        return None
    max_width=_positive_int(max_width,4096)
    quality=_positive_int(quality,100)
    natural=_positive_int(natural_width)
    if max_width is not None and natural is not None and natural<=max_width:
        max_width=None
    if max_width is None and quality is None:return None
    parts=[seg for seg in parsed.path.split('/') if seg]
    index=next((i for i,seg in enumerate(parts) if '=' in seg),None)
    if index is None or index>=len(parts)-1:return None
    fields={}
    for entry in parts[index].split(','):
        key,sep,value=entry.partition('=')
        if sep and key:fields[key]=value
    fields.pop('original',None)
    if max_width is not None:fields['width']=str(max_width)
    if quality is not None:fields['quality']=str(quality)
    if not fields:return None
    parts[index]=','.join(f'{key}={value}' for key,value in fields.items())
    return urlunsplit((parsed.scheme,parsed.netloc,'/'+'/'.join(parts),parsed.query,''))


def _read_local(path, limit, too_large):
    with open(path,'rb') as f:data=f.read(limit+1)
    if len(data)>limit:raise ValueError(too_large)
    return data


def _read_capped(response, limit, deadline, too_large, timeout, chunk=256*1024):
    """分块读取 + wall-clock 截止时间 + 字节上限。

    opener.open(timeout=25) 只管 socket，慢速流可以挂几分钟；超时必须中止并报错，
    绝不静默返回半张图。"""
    parts=[];total=0
    while total<=limit:
        if time.monotonic()>deadline:raise ValueError(timeout)
        size=min(chunk,limit+1-total)
        piece=response.read(size)
        if not piece:break
        parts.append(piece);total+=len(piece)
        if len(piece)<size:break  # 缓冲读不到满块即代表流已结束
    if total>limit:raise ValueError(too_large)
    return b''.join(parts)


def _fetch(url, cfg, limit, deadline, too_large, timeout):
    import urllib.request
    import civitai_api
    proxy=cfg.get('proxy_address') if cfg.get('proxy_enabled') else None
    opener=civitai_api.build_opener(proxy,verify=cfg.get('ssl_verify',True))
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://civitai.com/'})
    with opener.open(req,timeout=25) as r:
        return _read_capped(r,limit,deadline,too_large,timeout)


def _video_bytes(item, cfg, deadline):
    """视频只按原样取字节：不显示降级、不进 PIL 解码；上限走 video_max_mb。"""
    limit=video_limit_bytes(cfg);too_large=_too_large_message(True,cfg)
    cached=cache_load(item,cfg,'original')
    if cached:return cached,True
    if item.get('local_path'):
        return _read_local(item['local_path'],limit,too_large),True
    url=str(item.get('url') or item.get('orig_url') or '')
    if url.startswith('file://'):
        from urllib.request import url2pathname
        return _read_local(url2pathname(urlsplit(url).path),limit,too_large),True
    if url.startswith(('http://','https://')):
        data=_fetch(url,cfg,limit,deadline,too_large,_timeout_message(cfg))
        cache_store(item,cfg,'original',data)
        return data,True
    if item.get('b64'):
        import base64
        return base64.b64decode(item['b64'],validate=True),False
    raise ValueError('没有可查看的图片来源')


def read_bytes(item, cfg, max_width=None, quality=None):
    """读取来源字节，返回 (data, fmt, width, height, original)。

    max_width/quality 都为 None 时与旧实现完全一致（original_url 原图语义，
    webui 保存图片/复制图片依赖这个口径）；任一有效时改走 display_url 显示档，
    拿不到降级 URL 就回落原图。缓存按档位分键：显示档请求绝不会命中原图档条目，反之亦然。
    fmt 为 'VIDEO' 表示视频源，未经 PIL 校验。
    """
    deadline=time.monotonic()+fetch_deadline_seconds(cfg)
    if _is_video_item(item):
        data,source_original=_video_bytes(item,cfg,deadline)
        return data,'VIDEO',_positive_int(item.get('width')) or 0,_positive_int(item.get('height')) or 0,source_original
    limit=static_limit_bytes(cfg);too_large=_too_large_message(False,cfg);timeout=_timeout_message(cfg)
    persistable=bool(item.get('local_path') or item.get('url'))
    url=str(item.get('url') or '')
    # 本地文件就是原图本体，CDN 转码档位对它无意义，也不许缓存成显示档。
    downgrade=display_url(url,max_width,quality,_positive_int(item.get('width'))) if (max_width or quality) and url and not item.get('local_path') else None
    tier=cache_tier(max_width,quality) if downgrade else ''
    cached=cache_load(item,cfg,'original',tier)
    if cached:
        try:
            from PIL import Image
            with Image.open(io.BytesIO(cached)) as im:
                fmt=im.format;w,h=im.size;im.verify()
            if fmt in ('PNG','JPEG','WEBP','GIF') and len(cached)<=limit:return cached,fmt,w,h,persistable and not tier
        except Exception:pass
    if item.get('local_path'):
        data=_read_local(item['local_path'],limit,too_large)
    elif url:
        if downgrade:
            try:
                data=_fetch(downgrade,cfg,limit,deadline,too_large,timeout)
            except (OSError,ValueError):
                if time.monotonic()>deadline:raise
                # 显示档失败就回落原图，但绝不静默返回半张图。
                downgrade=None;tier=''
                data=_fetch(original_url(url),cfg,limit,deadline,too_large,timeout)
        else:
            data=_fetch(original_url(url),cfg,limit,deadline,too_large,timeout)
    elif item.get('b64'):
        import base64
        data=base64.b64decode(item['b64'],validate=True);persistable=False
    else:raise ValueError('没有可查看的图片来源')
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        if im.format not in ('PNG','JPEG','WEBP','GIF'):raise ValueError('不支持的图片格式')
        width,height=im.size;fmt=im.format
        im.verify()
    if persistable:cache_store(item,cfg,'original',data,tier)
    return data,fmt,width,height,persistable and not tier


def _original_info_bytes(item,cfg):
    """显示档字节的 CDN 转码会剥掉 EXIF/UserComment，取生成参数只认已缓存的原图字节。
    这里只读缓存，绝不为元数据再下载原图（否则又回到几分钟加载）。"""
    cached=cache_load(item,cfg,'original','')
    if not cached:return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(cached)) as im:
            saved=dict(im.info)
            try:saved['UserComment']=im.getexif().get(0x9286) or saved.get('UserComment')
            except Exception:pass
        return saved
    except Exception:
        return None


def preview(item,cfg,max_width=None,quality=None):
    from PIL import Image
    import base64
    if _is_video_item(item):
        raise ValueError('视频不能生成图片预览：请使用视频播放入口或打开 C站原图页')
    requested_tier=cache_tier(max_width,quality)
    cached=cache_load(item,cfg,'preview',requested_tier)
    if isinstance(cached,dict) and cached.get('ok') and cached.get('b64'):
        try:
            raw=base64.b64decode(cached['b64'],validate=True)
            with Image.open(io.BytesIO(raw)) as im:im.verify()
            return {**cached,'cache_hit':True}
        except Exception:pass
    data,fmt,w,h,original=read_bytes(item,cfg,max_width,quality)
    # original=False 且请求过档位 -> 实际取到的是显示档字节；回落原图时按原图档缓存。
    tier=requested_tier if (requested_tier and not original) else ''
    metadata_from_original=True
    saved=None
    if tier:
        # C站 width=/quality= 转码会剥掉 EXIF/UserComment：显示档字节一律不用于提取生成参数，
        # 只认已缓存的原图字节；没有原图缓存时退回 info.json 里已有的 meta，并如实标注。
        saved=_original_info_bytes(item,cfg)
        metadata_from_original=saved is not None
        if saved is None:saved={}
    with Image.open(io.BytesIO(data)) as im:
        info=dict(im.info)
        if not tier:
            try:info['UserComment']=im.getexif().get(0x9286) or info.get('UserComment')
            except Exception:pass
            saved=info
        fields=generation(item,saved=saved)
        im=im.convert('RGBA' if 'A' in im.getbands() else 'RGB');im.thumbnail((4096,4096))
        buf=io.BytesIO();im.save(buf,'PNG' if im.mode=='RGBA' else 'JPEG',**({} if im.mode=='RGBA' else {'quality':94}))
        mime='image/png' if im.mode=='RGBA' else 'image/jpeg'
    if tier and not metadata_from_original:
        fields['metadata_note']=fields.get('metadata_note') or '显示档为 C站转码图片，未读取原图内嵌参数；完整生成数据请用原图档或 C站图片接口查询。'
    result={'ok':True,'b64':base64.b64encode(buf.getvalue()).decode(),'mime':mime,'width':w,'height':h,
            'original_available':original,'preview_limited':max(w,h)>4096,
            'downgraded':bool(tier),'display_tier':tier or '',
            'metadata_from_original':metadata_from_original,
            'meta':fields['meta'],'resources':fields['resources'],'metadata_source':fields['metadata_source'],
            'metadata_note':fields.get('metadata_note','')}
    if item.get('local_path') or item.get('url'):
        # b64 历史预览没有稳定来源，不写缓存（与原实现一致）。
        cache_store(item,cfg,'preview',result,tier)
    return result


def clipboard_dib(data):
    """将受限图片转换成真正的 CF_DIB，不能把 JPEG blob 标成 image/png。"""
    from PIL import Image
    if not isinstance(data,bytes) or len(data)>LIMIT:raise ValueError('图片数据无效或过大')
    with Image.open(io.BytesIO(data)) as im:
        if im.width*im.height>4096*4096:raise ValueError('复制预览尺寸过大')
        image=im.convert('RGB');out=io.BytesIO();image.save(out,format='BMP');image.close()
    return out.getvalue()[14:]  # CF_DIB 不包含 BITMAPFILEHEADER。


def copy_clipboard_image(data, owner=0):
    import sys
    if sys.platform!='win32' or not owner:return False  # 其他平台由网页 PNG ClipboardItem 兜底。
    dib=clipboard_dib(data)
    import ctypes,time
    u,k=ctypes.WinDLL('user32',use_last_error=True),ctypes.WinDLL('kernel32',use_last_error=True)
    u.OpenClipboard.argtypes=[ctypes.c_void_p];u.OpenClipboard.restype=ctypes.c_bool
    u.EmptyClipboard.restype=ctypes.c_bool
    u.SetClipboardData.argtypes=[ctypes.c_uint,ctypes.c_void_p];u.SetClipboardData.restype=ctypes.c_void_p
    u.CloseClipboard.restype=ctypes.c_bool
    k.GlobalAlloc.argtypes=[ctypes.c_uint,ctypes.c_size_t];k.GlobalAlloc.restype=ctypes.c_void_p
    k.GlobalLock.argtypes=[ctypes.c_void_p];k.GlobalLock.restype=ctypes.c_void_p
    k.GlobalUnlock.argtypes=[ctypes.c_void_p];k.GlobalFree.argtypes=[ctypes.c_void_p]
    k.GlobalFree.restype=ctypes.c_void_p
    handle=k.GlobalAlloc(0x42,len(dib))
    if not handle:return False
    try:
        pointer=k.GlobalLock(handle)
        if not pointer:return False
        try:ctypes.memmove(pointer,dib,len(dib))
        finally:k.GlobalUnlock(handle)
        for attempt in range(5):
            if u.OpenClipboard(ctypes.c_void_p(owner)):break
            time.sleep(.04)
        else:return False
        try:
            if not u.EmptyClipboard():return False
            if not u.SetClipboardData(8,handle):return False  # CF_DIB，所有权交给系统。
            handle=None;return True
        finally:u.CloseClipboard()
    finally:
        if handle:k.GlobalFree(handle)
