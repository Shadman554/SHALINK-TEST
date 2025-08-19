"""
Video downloader module using yt-dlp for TikTok, Instagram, and Facebook videos.
"""

import os
import tempfile
import yt_dlp
import logging
import base64
import pathlib
import tempfile
import json
from urllib.parse import urlparse
from config import SUPPORTED_PLATFORMS, MAX_FILE_SIZE, TEMP_DIR
import requests
import time

logger = logging.getLogger(__name__)

# ---- Decode cookie env vars into temp files (Railway safe method) ----
for env_var, out_name in (('IG_COOKIES_B64', 'instagram.txt'), ('FB_COOKIES_B64', 'facebook.txt')):
    b64_data = os.getenv(env_var)
    logger.info(f"{env_var} present: %s bytes", len(b64_data or ""))
    if b64_data:
        try:
            out_path = pathlib.Path(tempfile.gettempdir()) / out_name
            out_path.write_bytes(base64.b64decode(b64_data))
            # expose path to downstream logic
            os.environ[f"{env_var[:-4]}FILE"] = str(out_path)  # sets IG_COOKIES_FILE / FB_COOKIES_FILE
            logger.info(f"Decoded {env_var} to {out_path}")
        except Exception as e:
            logger.error(f"Failed to decode {env_var}: {e}")
# --------------------------------------------------------------------
import re  # used for sanitising filenames

class VideoDownloader:
    def __init__(self):
        """Initialize with persistent session support."""
        os.makedirs(TEMP_DIR, exist_ok=True)
        
        # Persistent session file
        self.session_file = os.path.join(TEMP_DIR, 'instagram_session.json')
        
        # Enhanced Instagram cookie handling
        self.cookies_instagram = self._validate_cookies(
            os.getenv('IG_COOKIES_FILE'),
            os.path.join(os.getcwd(), "instagram_cookies.txt"),
            os.path.join(tempfile.gettempdir(), "instagram.txt")
        )
        
        # Facebook cookie handling
        self.cookies_facebook = self._validate_cookies(
            os.getenv('FB_COOKIES_FILE'),
            os.path.join(os.getcwd(), "facebook_cookies.txt"),
            os.path.join(tempfile.gettempdir(), "facebook.txt")
        )
        
        # Base download options
        self.ydl_opts = {
            'format': 'best',
            'outtmpl': os.path.join(TEMP_DIR, '%(title)s.%(ext)s'),
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.instagram.com/'
            },
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4'
            }]
        }
        
        # Instagram specific options
        self.instagram_opts = {
            'cookiefile': self.cookies_instagram,
            'extractor_args': {
                'instagram': {
                    'cookiefile': self.cookies_instagram,
                    'session': self._load_session() or None
                }
            }
        }
        
        # TikTok specific options
        self.tiktok_opts = {
            'extractor': 'tiktok',
            'referer': 'https://www.tiktok.com/',
            'headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate, br'
            },
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4'
            }]
        }
        
        # TikTok watermark-removal APIs (tried in order – these all return **non-watermarked** links)
        self.tiktok_apis = [
            "https://tikwm.com/api",           # GET ?url=<video_url>&hd=1  → json.data.url / json.data.hdplay
            "https://api.douyin.wtf/api",      # GET ?url=<video_url>        → json.url
            "https://api.dd01.ru/api/tiktok"   # GET ?url=<video_url>        → json.url
        ]
        
    def _validate_cookies(self, *cookie_paths):
        """Validate and return first working cookie file with all required fields."""
        required_fields = ['sessionid', 'ds_user_id', 'csrftoken']
        for path in cookie_paths:
            if path and os.path.exists(path):
                try:
                    with open(path) as f:
                        content = f.read()
                        if all(field in content for field in required_fields):
                            return path
                except Exception:
                    continue
        return None
    
    def _load_session(self):
        """Load persistent session if exists."""
        if os.path.exists(self.session_file):
            try:
                with open(self.session_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return None
    
    def _save_session(self, session):
        """Save session data persistently."""
        with open(self.session_file, 'w') as f:
            json.dump(session, f)
    
    def _setup_instagram_auth(self):
        """Comprehensive Instagram auth setup."""
        auth_methods = [
            self._try_cookie_auth,
            self._try_browser_auth,
            self._try_mobile_api_auth
        ]
        
        for method in auth_methods:
            if method():
                return True
        return False

    def _try_cookie_auth(self):
        """Try authenticating with provided cookies."""
        if not self.cookies_instagram:
            return False
            
        try:
            # Verify cookies contain required fields
            required = ['sessionid', 'ds_user_id', 'csrftoken']
            with open(self.cookies_instagram) as f:
                cookies = f.read()
                if not all(r in cookies for r in required):
                    return False
                    
            # Test authentication
            test_url = "https://www.instagram.com/accounts/edit/"
            with yt_dlp.YoutubeDL({
                'cookiefile': self.cookies_instagram,
                'quiet': True
            }) as ydl:
                info = ydl.extract_info(test_url, download=False)
                return bool(info)
        except Exception:
            return False

    def _try_browser_auth(self):
        """Try extracting fresh cookies from browser."""
        try:
            temp_opts = {
                'cookiesfrombrowser': ('chrome',),
                'quiet': True
            }
            with yt_dlp.YoutubeDL(temp_opts) as ydl:
                info = ydl.extract_info('https://www.instagram.com/', download=False)
                return bool(info)
        except Exception:
            return False

    def _try_mobile_api_auth(self):
        """Try mobile API authentication."""
        try:
            with yt_dlp.YoutubeDL({
                'http_headers': {
                    'User-Agent': 'Instagram 219.0.0.12.117 Android',
                    'X-IG-App-ID': '936619743392459'
                },
                'quiet': True
            }) as ydl:
                info = ydl.extract_info('https://www.instagram.com/', download=False)
                return bool(info)
        except Exception:
            return False
    
    def _setup_instagram_authentication(self):
        """Setup Instagram authentication using browser cookie extraction."""
        try:
            cookies_path = os.path.join(TEMP_DIR, 'instagram_cookies.txt')
            
            # Try to extract Instagram cookies from browser
            if self._try_extract_instagram_cookies(cookies_path):
                self.cookies_instagram = cookies_path
                logger.info("Instagram cookies extracted successfully")
            else:
                logger.info("Using alternative Instagram access method")
                self.cookies_instagram = None
                
        except Exception as e:
            logger.error(f"Instagram authentication setup failed: {e}")
            self.cookies_instagram = None
    
    def _try_extract_instagram_cookies(self, cookies_path: str) -> bool:
        """Try to extract Instagram cookies using yt-dlp's built-in browser cookie extraction."""
        try:
            # Use yt-dlp's built-in cookie extraction capability
            temp_opts = {
                'cookiesfrombrowser': ('chrome', None, None, None),
                'quiet': True,
                'no_warnings': True
            }
            
            # Attempt to extract cookies for Instagram
            with yt_dlp.YoutubeDL(temp_opts) as temp_ydl:
                # Test if we can extract cookies and access Instagram
                info = temp_ydl.extract_info('https://www.instagram.com/', download=False)
                if info:
                    # If successful, save these settings
                    return True
                    
        except Exception as e:
            logger.debug(f"Browser cookie extraction failed: {e}")
            
        # Try alternative browsers
        for browser in ['firefox', 'edge', 'safari']:
            try:
                temp_opts = {
                    'cookiesfrombrowser': (browser, None, None, None),
                    'quiet': True,
                    'no_warnings': True
                }
                
                with yt_dlp.YoutubeDL(temp_opts) as temp_ydl:
                    info = temp_ydl.extract_info('https://www.instagram.com/', download=False)
                    if info:
                        return True
                        
            except Exception:
                continue
                
        return False
    
    def is_supported_platform(self, url: str) -> bool:
        """Check if the URL is from a supported platform."""
        try:
            parsed_url = urlparse(url.lower())
            domain = parsed_url.netloc
            
            # Remove 'www.' prefix if present
            if domain.startswith('www.'):
                domain = domain[4:]
            
            return any(platform in domain for platform in SUPPORTED_PLATFORMS)
        except Exception as e:
            logger.error(f"Error parsing URL {url}: {e}")
            return False
    
    def _download_instagram_video(self, url: str) -> tuple[str | None, str]:
        """Enhanced Instagram downloader with better cookie handling."""
        try:
            if not self.cookies_instagram:
                return None, "instagram_auth_required"
                
            ydl_opts = {**self.ydl_opts, **self.instagram_opts}
            
            # Try with cookies first
            for attempt in range(3):
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                        if not info:
                            continue
                            
                        ydl.download([url])
                        title = info.get('title', 'instagram_video')
                        downloaded_file = self._find_downloaded_file(title)
                        
                        if downloaded_file and os.path.exists(downloaded_file):
                            return downloaded_file, title
                except Exception as e:
                    logger.warning(f"Instagram download attempt {attempt + 1} failed: {e}")
                    if attempt == 2:
                        raise
                    time.sleep(1)
                    
            return None, "instagram_download_failed"
            
        except Exception as e:
            logger.error(f"Instagram download error: {e}")
            return None, "instagram_download_failed"
            
    def _download_tiktok_video(self, url: str) -> tuple[str | None, str]:
        """TikTok downloader with multiple watermark removal options."""
        try:
            # --------------------------------------------------
            # 1. Try public API services that return NO-WATERMARK links
            # --------------------------------------------------
            for api_url in self.tiktok_apis:
                try:
                    if "tikwm.com" in api_url:
                        # tikwm expects GET params, not payload
                        response = requests.get(f"{api_url}?url={url}&hd=1", timeout=20)
                        data = response.json().get("data", {}) if response.ok else {}
                        video_url = data.get("hdplay") or data.get("url")
                        title = data.get("title") or "tiktok_video"
                    else:
                        response = requests.get(f"{api_url}?url={url}", timeout=20)
                        json_data = response.json() if response.ok else {}
                        video_url = json_data.get("url") or json_data.get("nwm_url")
                        title = json_data.get("title") or "tiktok_video"
                        
                    if video_url:
                        return self._download_from_url(video_url, title)
                except Exception as api_error:
                    logger.warning(f"TikTok API {api_url} failed: {api_error}")
            
            # Fallback to yt-dlp with enhanced options
            ydl_opts = {
                **self.ydl_opts,
                **self.tiktok_opts,
                'extractor_args': {}
            }
            
            for attempt in range(3):
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                        if not info:
                            continue
                            
                        ydl.download([url])
                        title = info.get('title', 'tiktok_video')
                        downloaded_file = self._find_downloaded_file(title)
                        
                        if downloaded_file and os.path.exists(downloaded_file):
                            return downloaded_file, title
                except Exception as e:
                    logger.warning(f"TikTok download attempt {attempt + 1} failed: {e}")
                    if attempt == 2:
                        raise
                    time.sleep(1)
                    
            return None, "tiktok_download_failed"
            
        except Exception as e:
            logger.error(f"TikTok download error: {e}")
            return None, "tiktok_download_failed"
    
    def _try_download(self, url: str, opts: dict) -> tuple[str | None, str]:
        """Try downloading video with given options."""
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None, "extract_failed"
                
                title = info.get('title', 'video')
                
                # Check if file size is available and within limits
                filesize = info.get('filesize') or info.get('filesize_approx')
                if filesize and filesize > MAX_FILE_SIZE:
                    return None, "file_too_large"
                
                # Download the video
                ydl.download([url])
                
                # Find the downloaded file
                downloaded_file = self._find_downloaded_file(title)
                
                if downloaded_file and os.path.exists(downloaded_file):
                    # Check actual file size
                    if os.path.getsize(downloaded_file) > MAX_FILE_SIZE:
                        os.remove(downloaded_file)
                        return None, "file_too_large"
                    
                    return downloaded_file, title
                else:
                    return None, "download_failed"
                    
        except yt_dlp.DownloadError as e:
            logger.error(f"yt-dlp download error: {e}")
            return None, "download_failed"
        except Exception as e:
            logger.error(f"Unexpected error downloading {url}: {e}")
            return None, "download_failed"
    
    def download_video(self, url: str) -> tuple[str | None, str]:
        """
        Download video from the given URL.
        
        Returns:
            tuple: (file_path, title) if successful, (None, error_message) if failed
        """
        try:
            if not self.is_supported_platform(url):
                return None, "unsupported_platform"
            
            # Clean up any existing files in temp directory
            self._cleanup_temp_files()
            
            # Try Instagram-specific approach if it's an Instagram URL
            if 'instagram.com' in url:
                return self._download_instagram_video(url)
            
            # Try TikTok-specific approach if it's a TikTok URL
            if 'tiktok.com' in url:
                return self._download_tiktok_video(url)
            
            # Clone options so we don't mutate the shared dict
            ydl_opts = self.ydl_opts.copy()
            
            if any(site in url for site in ("facebook.com", "fb.com")) and self.cookies_facebook:
                ydl_opts["cookiefile"] = self.cookies_facebook

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Extract info first to get title and check file size
                info = ydl.extract_info(url, download=False)
                
                if not info:
                    return None, "extract_failed"
                
                title = info.get('title', 'video')
                
                # Check if file size is available and within limits
                filesize = info.get('filesize') or info.get('filesize_approx')
                if filesize and filesize > MAX_FILE_SIZE:
                    return None, "file_too_large"
                
                # Download the video with retries
                for attempt in range(3):
                    try:
                        ydl.download([url])
                        break
                    except Exception as e:
                        if attempt == 2:
                            raise
                        logger.warning(f"Attempt {attempt + 1} failed: {e}")
                        time.sleep(1)
                
                # Find the downloaded file
                downloaded_file = self._find_downloaded_file(title)
                
                if downloaded_file and os.path.exists(downloaded_file):
                    # Check actual file size
                    if os.path.getsize(downloaded_file) > MAX_FILE_SIZE:
                        os.remove(downloaded_file)
                        return None, "file_too_large"
                    
                    return downloaded_file, title
                else:
                    return None, "download_failed"
                    
        except yt_dlp.DownloadError as e:
            logger.error(f"yt-dlp download error: {e}")
            return None, "download_failed"
        except Exception as e:
            logger.error(f"Unexpected error downloading {url}: {e}")
            return None, "download_failed"
    
    def _download_from_url(self, video_url: str, title: str) -> tuple[str | None, str]:
        """Download the file at `video_url` directly to TEMP_DIR.

        This helper is primarily used for TikTok APIs that already expose a
        non-watermarked direct link. It streams the content to disk so that
        even large files do not exhaust memory.
        """
        try:
            # Sanitise title for filesystem
            safe_title = re.sub(r"[^\w\- ]", "", title)[:50] or "tiktok_video"
            dst = os.path.join(TEMP_DIR, f"{safe_title}_{int(time.time())}.mp4")
            with requests.get(video_url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(dst, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            # Enforce file-size limit
            if os.path.getsize(dst) > MAX_FILE_SIZE:
                os.remove(dst)
                return None, "file_too_large"
            return dst, safe_title
        except Exception as e:
            logger.error(f"Direct download failed: {e}")
            # Clean up partial file
            try:
                if 'dst' in locals() and 'dst' in locals() and os.path.exists(locals().get('dst', '')):
                    os.remove(locals()['dst'])
            except Exception:
                pass
            return None, "download_failed"

    def _find_downloaded_file(self, title: str) -> str | None:
        """Find the downloaded file in the temp directory."""
        try:
            for file in os.listdir(TEMP_DIR):
                if file.startswith(title[:20]):  # Match first 20 chars of title
                    return os.path.join(TEMP_DIR, file)
            
            # If title-based search fails, get the newest file
            files = [os.path.join(TEMP_DIR, f) for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))]
            if files:
                return max(files, key=os.path.getctime)
            
            return None
        except Exception as e:
            logger.error(f"Error finding downloaded file: {e}")
            return None
    
    def download_youtube(self, url: str, format_type: str) -> tuple[str | None, str]:
        """
        Download YouTube video or audio with specific quality options.
        
        Args:
            url (str): YouTube URL
            format_type (str): 'video' for 1080p video, 'audio' for MP3
            
        Returns:
            tuple[str, str]: (file_path, result_message)
        """
        try:
            logger.info(f"Starting YouTube {format_type} download for URL: {url}")
            
            # Configure options based on format type
            if format_type == 'video':
                # Best video quality up to 1080p
                ydl_opts = {
                    'format': 'best[height<=1080]/best',
                    'outtmpl': os.path.join(TEMP_DIR, '%(title)s.%(ext)s'),
                    'postprocessors': [{
                        'key': 'FFmpegVideoConvertor',
                        'preferedformat': 'mp4'
                    }],
                    'http_headers': {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                    }
                }
            else:  # audio format
                # Best audio quality, convert to MP3
                ydl_opts = {
                    'format': 'bestaudio/best',
                    'outtmpl': os.path.join(TEMP_DIR, '%(title)s.%(ext)s'),
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                    'http_headers': {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                    }
                }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Get video info first
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None, "Failed to extract video information"
                
                # Sanitize title for filename
                title = re.sub(r'[^\w\s-]', '', info.get('title', 'youtube_video')).strip().replace(' ', '_')[:50]
                
                # Update output template with sanitized title
                if format_type == 'audio':
                    filename = f"{title}.mp3"
                else:
                    filename = f"{title}.mp4"
                
                file_path = os.path.join(TEMP_DIR, filename)
                ydl_opts['outtmpl'] = file_path.replace(f".{filename.split('.')[-1]}", ".%(ext)s")
                
                # Download the video/audio
                with yt_dlp.YoutubeDL(ydl_opts) as ydl_download:
                    ydl_download.download([url])
                
                # Find the actual downloaded file
                actual_file = None
                for ext in ['mp4', 'mp3', 'webm', 'm4a']:
                    test_path = file_path.replace(f".{filename.split('.')[-1]}", f".{ext}")
                    if os.path.exists(test_path):
                        actual_file = test_path
                        break
                
                if not actual_file or not os.path.exists(actual_file):
                    return None, f"Downloaded file not found for {format_type}"
                
                # Check file size
                file_size = os.path.getsize(actual_file)
                if file_size > MAX_FILE_SIZE:
                    self.cleanup_file(actual_file)
                    return None, f"File too large: {file_size / (1024*1024):.1f}MB (max: {MAX_FILE_SIZE / (1024*1024):.1f}MB)"
                
                logger.info(f"YouTube {format_type} downloaded successfully: {actual_file} ({file_size} bytes)")
                return actual_file, "Success"
                
        except Exception as e:
            logger.error(f"Error downloading YouTube {format_type}: {e}")
            return None, f"Download failed: {str(e)}"
    
    def _cleanup_temp_files(self):
        """Clean up temporary files older than 1 hour."""
        try:
            import time
            current_time = time.time()
            
            for filename in os.listdir(TEMP_DIR):
                file_path = os.path.join(TEMP_DIR, filename)
                if os.path.isfile(file_path):
                    file_age = current_time - os.path.getctime(file_path)
                    if file_age > 3600:  # 1 hour
                        os.remove(file_path)
                        logger.info(f"Cleaned up old file: {filename}")
        except Exception as e:
            logger.error(f"Error cleaning up temp files: {e}")
    
    def cleanup_file(self, file_path: str):
        """Remove a specific file after use."""
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Cleaned up file: {file_path}")
        except Exception as e:
            logger.error(f"Error cleaning up file {file_path}: {e}")
