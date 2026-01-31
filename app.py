import streamlit as st
import fitz  # PyMuPDF
import os
import json
import requests
import hashlib
import urllib.parse
import tempfile
import math
import gc
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

# --- [0. 核心配置与工具] ---

class Config:
    """集中管理配置，显式区分环境变量与应用逻辑常数"""
    SECRETS = {
        "SYS_PASSWORD": os.getenv("SYS_PASSWORD", "admin888"),
        "BAIDU_AK": os.getenv("BAIDU_AK", ""),
        "BAIDU_SK": os.getenv("BAIDU_SK", ""),
    }
    
    APP = {
        "APP_FOLDER": os.getenv("APP_FOLDER", "PDF_Distributor"),
        "FILE_PREFIX": os.getenv("FILE_PREFIX", "Dist"),
        "TOKEN_FILE": "baidu_token.json",
        "RASTER_DPI": 2.5,  # 栅格化倍数，过高会导致 OOM
        "JPG_QUALITY": 80,
        "TEMP_STAY_DIR": "output_cache" # 全局缓存根目录
    }

    CHANNEL_DEFAULTS = {
        "feishu": {
            "opw": os.getenv("FEISHU_OPW", "zwg5427"), 
            "upw": os.getenv("FEISHU_UPW", "888888"), 
            "suffix": "f", "sub": "Feishu", "name": "飞书"
        },
        "wecom":  {
            "opw": os.getenv("WECOM_OPW", "zwg5427"), 
            "upw": os.getenv("WECOM_UPW", "888888"), 
            "suffix": "w","sub": "WeCom",  "name": "企微"
        },
        "red":    {
            "opw": os.getenv("RED_OPW", "zwg5427"), 
            "upw": os.getenv("RED_UPW", "888888"), 
            "suffix": "r", "sub": "Red",    "name": "小红书"
        },
    }

    DEFAULT_WM_PATHS = {
        'feishu': 'WM.Feishu.png',
        'wecom': 'WM.WeCOM.png',
        'red': 'WM.Red.png'
    }

# --- [1. 业务逻辑层] ---

class BaiduManager:
    def __init__(self, ak: str, sk: str, t_file: str):
        self.ak = ak
        self.sk = sk
        self.t_file = t_file
        self.api_base = "https://pan.baidu.com/rest/2.0/xpan"
        self.headers = {'User-Agent': 'pan.baidu.com'}
        self.token_data = self._load_token()

    def _load_token(self) -> Optional[Dict]:
        if os.path.exists(self.t_file):
            try:
                with open(self.t_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return None
        return None

    def save_token(self, data: Dict):
        with open(self.t_file, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        self.token_data = data

    def refresh_token_logic(self) -> bool:
        """执行 Refresh Token 换取 Access Token """
        if not self.token_data or 'refresh_token' not in self.token_data:
            return False
            
        refresh_url = "https://openapi.baidu.com/oauth/2.0/token"
        params = {
            "grant_type": "refresh_token",
            "refresh_token": self.token_data['refresh_token'],
            "client_id": self.ak,
            "client_secret": self.sk
        }
        try:
            res = requests.get(refresh_url, params=params, timeout=10).json()
            if 'access_token' in res:
                self.save_token(res)
                return True
        except Exception:
            pass
        return False

    def check_auth(self) -> bool:
        """多级验证链路：直接验证 -> 自动尝试刷新(1次) -> 降级手动 """
        if not self.token_data or 'access_token' not in self.token_data:
            return False
        
        # 1. 尝试探测现有 token 状态
        try:
            url = f"{self.api_base}/file?method=list&access_token={self.token_data.get('access_token')}&dir=/apps&limit=1"
            res = requests.get(url, headers=self.headers, timeout=5).json()
            if res.get('errno') == 0:
                st.session_state["refresh_retry_done"] = False # 重置刷新标志位
                return True
        except Exception:
            pass
        
        # 2. 失败后尝试自动刷新一次
        if not st.session_state.get("refresh_retry_done", False):
            st.session_state["refresh_retry_done"] = True
            if self.refresh_token_logic():
                return True
        
        return False

    def upload(self, local_path: str, app_folder: str, remote_sub: str) -> Tuple[str, str]:
        """百度云三阶段分片上传逻辑 """
        try:
            p = Path(local_path)
            fn = p.name
            file_bytes = p.read_bytes()
            md5 = hashlib.md5(file_bytes).hexdigest()
            fsize = len(file_bytes)
            
            target_dir = f"/apps/{app_folder}/{remote_sub}"
            tk = self.token_data['access_token']
            
            # 1. 预创建
            pre_url = f"{self.api_base}/file?method=precreate&access_token={tk}"
            pre_data = {
                'path': f"{target_dir}/{fn}", 'size': str(fsize), 'isdir': '0',
                'autoinit': '1', 'block_list': json.dumps([md5]), 'rtype': '3'
            }
            pre = requests.post(pre_url, data=pre_data, headers=self.headers).json()
            
            if 'uploadid' not in pre:
                return "FAILED", f"预处理失败: {pre.get('errno')}"

            # 2. 分片上传 (此处为小文件单片模式)
            up_url = (f"https://d.pcs.baidu.com/rest/2.0/pcs/superfile2?method=upload&access_token={tk}"
                      f"&type=tmpfile&path={urllib.parse.quote(f'{target_dir}/{fn}')}"
                      f"&uploadid={pre['uploadid']}&partseq=0")
            requests.post(up_url, files={'file': file_bytes}, headers=self.headers)

            # 3. 合并创建
            create_url = f"{self.api_base}/file?method=create&access_token={tk}"
            create_data = {
                'path': f"{target_dir}/{fn}", 'size': str(fsize), 'isdir': '0',
                'uploadid': pre['uploadid'], 'block_list': json.dumps([md5]), 'rtype': '3'
            }
            final = requests.post(create_url, data=create_data, headers=self.headers).json()
            
            if 'fs_id' in final:
                return "SUCCESS", f"{target_dir}/{fn}"
            return "FAILED", f"落盘失败: {final.get('errno')}"
        except Exception as e:
            return "FAILED", str(e)

class PDFProcessor:
    @staticmethod
    def create_task_dir() -> Path:
        """创建隔离的任务目录，防止并发冲突 """
        task_id = f"{datetime.now().strftime('%H%M%S')}_{uuid.uuid4().hex[:6]}"
        task_path = Path(Config.APP["TEMP_STAY_DIR"]) / task_id
        task_path.mkdir(parents=True, exist_ok=True)
        return task_path

    @staticmethod
    def rasterize_pdf(input_path: Path, output_path: Path, password: str = None) -> bool:
        """PDF 去矢量化，增加显式内存回收逻辑 """
        try:
            with fitz.open(input_path) as src:
                if src.is_encrypted:
                    if not (password and src.authenticate(password)):
                        return False

                with fitz.open() as r_doc:
                    mat = fitz.Matrix(Config.APP["RASTER_DPI"], Config.APP["RASTER_DPI"])
                    for page in src:
                        pix = page.get_pixmap(matrix=mat)
                        img_bytes = pix.tobytes("jpg", Config.APP["JPG_QUALITY"])
                        
                        np = r_doc.new_page(width=page.rect.width, height=page.rect.height)
                        np.insert_image(np.rect, stream=img_bytes)
                        
                        # 内存即时释放 
                        pix = None
                        del img_bytes
                        
                    r_doc.save(output_path)
            return True
        except Exception as e:
            st.error(f"栅格化错误: {e}")
            return False
        finally:
            gc.collect() # 显式内存回收 

    @staticmethod
    def add_watermark(target_pdf_path: Path, output_path: Path, wm_bytes: Optional[bytes], 
                      owner_pw: str, user_pw: str):
        """添加全屏平铺水印并进行 AES-256 加密"""
        if not os.path.exists(target_pdf_path): return
        
        with fitz.open(target_pdf_path) as doc:
            if wm_bytes:
                # 内存打开图片构造临时 PDF 页作为水印源
                with fitz.open("png", wm_bytes) as img_doc:
                    rect = img_doc[0].rect
                    with fitz.open() as wm_pdf_doc:
                        w_page = wm_pdf_doc.new_page(width=rect.width, height=rect.height)
                        w_page.insert_image(rect, stream=wm_bytes)
                        PDFProcessor._apply_tiled_watermark(doc, wm_pdf_doc)
            
            doc.save(output_path, encryption=fitz.PDF_ENCRYPT_AES_256, 
                     owner_pw=owner_pw, user_pw=user_pw)

    @staticmethod
    def _apply_tiled_watermark(target_doc, wm_source_doc):
        """平铺算法"""
        rot, w_pct, h_mult = -60, 0.6, 2.5
        iw, ih = wm_source_doc[0].rect.width, wm_source_doc[0].rect.height
        for page in target_doc:
            vw = page.rect.width * w_pct
            vh = vw * (ih / iw)
            rad = abs(rot) * (math.pi / 180.0)
            bw = vw * math.cos(rad) + vh * math.sin(rad)
            bh = vw * math.sin(rad) + vh * math.cos(rad)
            step_y = bh * h_mult
            y = 150 + bh/2
            while y <= page.rect.height - 150 - bh/2:
                r = fitz.Rect((page.rect.width - bw) / 2, y - bh/2, 
                              (page.rect.width + bw) / 2, y + bh/2)
                page.show_pdf_page(r, wm_source_doc, 0, rotate=rot)
                y += step_y

# --- [2. UI 工具函数] ---

def cleanup_housekeeper():
    """管家机制：自动清理 24 小时前的旧任务目录 """
    base_dir = Path(Config.APP["TEMP_STAY_DIR"])
    if not base_dir.exists(): return
    
    now = datetime.now().timestamp()
    for path in base_dir.iterdir():
        if path.is_dir():
            # 检查目录最后修改时间
            if (now - path.stat().st_mtime) > 86400:
                try:
                    shutil.rmtree(path)
                except Exception:
                    pass

# --- [3. UI 展现层] ---

def main():
    st.set_page_config(page_title="PDF Distributor", layout="centered")
    cleanup_housekeeper() # 启动时清理 

    # --- 登录鉴权 ---
    if "authenticated" not in st.session_state:
        st.title("🔐 系统访问受限")
        pwd = st.text_input("请输入访问密钥", type="password")
        if st.button("解锁"):
            if pwd == Config.SECRETS["SYS_PASSWORD"]:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("密钥错误")
        st.stop()

    st.title("🚀 PDF Distributor")

    # 初始化 session 变量
    if "process_results" not in st.session_state:
        st.session_state.process_results = []
    if "refresh_retry_done" not in st.session_state:
        st.session_state.refresh_retry_done = False

    # --- 配置区 ---
    with st.expander("⚙️ 核心配置 (Secrets)", expanded=False):
        c1, c2 = st.columns(2)
        app_key = c1.text_input("Baidu AK", value=Config.SECRETS["BAIDU_AK"])
        secret_key = c2.text_input("Baidu SK", value=Config.SECRETS["BAIDU_SK"], type="password")
        target_folder = c1.text_input("网盘文件夹", value=Config.APP["APP_FOLDER"])
        file_prefix = c2.text_input("输出文件前缀", value=Config.APP["FILE_PREFIX"])

    mgr = BaiduManager(app_key, secret_key, Config.APP["TOKEN_FILE"])

    # --- 授权逻辑 UI ---
    if not mgr.check_auth():
        st.warning("⚠️ 百度云未授权或 Token 已过期")
        auth_url = f"https://openapi.baidu.com/oauth/2.0/authorize?response_type=code&client_id={app_key}&redirect_uri=oob&scope=basic,netdisk"
        st.markdown(f"1. [点击获取授权码]({auth_url})")
        code = st.text_input("2. 输入授权码:")
        if st.button("激活授权"):
            url = f"https://openapi.baidu.com/oauth/2.0/token?grant_type=authorization_code&code={code}&client_id={app_key}&client_secret={secret_key}&redirect_uri=oob"
            try:
                res = requests.get(url, timeout=10).json()
                if 'access_token' in res:
                    mgr.save_token(res)
                    st.success("授权成功！")
                    st.rerun()
                else:
                    st.error(f"失败: {res.get('error_description', res)}")
            except Exception as e:
                st.error(f"网络异常: {e}")

    # --- 分发策略设置 ---
    st.subheader("📦 分发渠道配置")
    configured_channels = []
    for ch_id, defaults in Config.CHANNEL_DEFAULTS.items():
        with st.container(border=True):
            is_active = st.checkbox(f"开启 {defaults['name']}", value=True, key=f"active_{ch_id}")
            if is_active:
                col_a, col_b = st.columns(2)
                opw = col_a.text_input("管理密码", value=defaults["opw"], key=f"opw_{ch_id}")
                upw = col_b.text_input("阅读密码", value=defaults["upw"], key=f"upw_{ch_id}")
                use_def_wm = col_a.checkbox("使用默认水印", value=True, key=f"wm_def_{ch_id}")
                custom_wm_file = None
                if not use_def_wm:
                    custom_wm_file = col_b.file_uploader("自定义水印PNG", type="png", key=f"wm_up_{ch_id}")
                
                configured_channels.append({
                    "id": ch_id, "meta": defaults, "opw": opw, "upw": upw,
                    "use_def_wm": use_def_wm, "custom_wm_file": custom_wm_file
                })

    # --- 上传与执行区 ---
    src_pdf_password = st.text_input("🔓 源 PDF 密码 (若有)", type="password")
    main_pdf = st.file_uploader("📄 上传源文件 (PDF)", type="pdf")
    
    if main_pdf and st.button("🔥 开始自动化任务", type="primary", use_container_width=True):
        if not configured_channels:
            st.warning("请至少激活一个渠道")
            st.stop()

        status = st.status("正在启动任务隔离环境...", expanded=True)
        # 创建本次任务唯一的子目录 
        task_dir = PDFProcessor.create_task_dir()
        st.session_state.process_results = [] 

        try:
            with tempfile.TemporaryDirectory() as td:
                input_path = Path(td) / "source.pdf"
                input_path.write_bytes(main_pdf.read())
                
                status.write("🔨 正在压制 PDF (去矢量化)...")
                raster_path = Path(td) / "raster_base.pdf"
                
                if not PDFProcessor.rasterize_pdf(input_path, raster_path, src_pdf_password):
                    status.update(label="❌ 处理失败", state="error")
                    st.error("无法读取源 PDF，请检查密码。")
                    shutil.rmtree(task_dir) # 失败清理
                    st.stop()

                dt_str = datetime.now().strftime('%y%m%d')
                for ch in configured_channels:
                    ch_name = ch['meta']['name']
                    status.write(f"🎨 正在生成渠道文件: {ch_name}")
                    
                    wm_bytes = None
                    if ch['use_def_wm']:
                        def_path = Config.DEFAULT_WM_PATHS.get(ch['id'])
                        if def_path and os.path.exists(def_path):
                            with open(def_path, 'rb') as f: wm_bytes = f.read()
                        else:
                            status.write(f"⚠️ 未找到 {ch_name} 默认水印文件，将不加水印")
                    elif ch['custom_wm_file']:
                        wm_bytes = ch['custom_wm_file'].getvalue()
                    
                    out_filename = f"{file_prefix}{ch['meta']['suffix']}{dt_str}(先存后看).pdf"
                    save_path = task_dir / out_filename
                    
                    PDFProcessor.add_watermark(raster_path, save_path, wm_bytes, ch['opw'], ch['upw'])
                    
                    st.session_state.process_results.append({
                        "name": ch_name,
                        "filename": out_filename,
                        "local_path": str(save_path),
                        "sub": ch['meta']['sub'],
                        "uploaded": False
                    })
                        
                status.update(label="🎉 转换任务全部完成", state="complete")
                st.balloons()
                
        except Exception as e:
            st.error(f"系统运行崩溃: {e}")
            if task_dir.exists(): shutil.rmtree(task_dir)
        finally:
            gc.collect()

    # --- 结果展示与操作区 ---
    if st.session_state.process_results:
        st.divider()
        st.subheader("⬇️ 下载与云分发")
        
        for i, res in enumerate(st.session_state.process_results):
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 1, 1])
                c1.write(f"**{res['name']}**")
                c1.caption(f"文件名: {res['filename']}")
                
                # 本地下载
                if os.path.exists(res['local_path']):
                    with open(res['local_path'], "rb") as f:
                        c2.download_button(
                            label="💾 本地下载",
                            data=f,
                            file_name=res['filename'],
                            mime="application/pdf",
                            key=f"dl_{i}"
                        )
                
                # 云端推送
                if not res['uploaded']:
                    if c3.button("☁️ 推送网盘", key=f"up_btn_{i}"):
                        with st.spinner(f"正在上传..."):
                            state, msg = mgr.upload(res['local_path'], target_folder, res['sub'])
                            if state == "SUCCESS":
                                st.success(f"上传成功")
                                st.session_state.process_results[i]['uploaded'] = True
                            else:
                                st.error(f"错误: {msg}")
                else:
                    c3.success("✅ 已云同步")

if __name__ == "__main__":
    main()