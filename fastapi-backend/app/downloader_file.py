# downloader.py
from io import BytesIO
from tempfile import SpooledTemporaryFile
from fastapi import UploadFile

def tiktok_to_uploadfile(url: str) -> UploadFile:
    buf = BytesIO()

    ydl_opts = {
        "outtmpl": "-",
        "format": "mp4/best[height<=720]",  # Limit to 720p to reduce size
        "noplaylist": True,
        "quiet": True,
        "http_headers": {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        }
    }

    # Try to download directly with yt-dlp first
    try:
        # Create a temporary file for yt-dlp to write to
        import tempfile
        import os
        
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp_file:
            temp_filename = tmp_file.name
        
        # Update options to download to temp file
        ydl_opts['outtmpl'] = temp_filename
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # Read the downloaded file
        with open(temp_filename, 'rb') as f:
            buf.write(f.read())
        
        # Clean up temp file
        os.unlink(temp_filename)
        
    except Exception as e:
        # Fallback: try to get direct URL and stream
        try:
            ydl_opts_info = {
                "quiet": True,
                "http_headers": ydl_opts["http_headers"]
            }
            
            with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
                info = ydl.extract_info(url, download=False)
                video_url = info.get("url")
                if not video_url:
                    raise RuntimeError("Could not resolve media URL")

            # Stream with proper headers
            import requests
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Referer': 'https://www.tiktok.com/',
                'Accept': '*/*',
            }
            
            with requests.get(video_url, stream=True, timeout=60, headers=headers) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=1024 * 512):
                    if chunk:
                        buf.write(chunk)
        except Exception as fallback_error:
            raise RuntimeError(f"Failed to download TikTok video. Primary error: {e}, Fallback error: {fallback_error}")

    buf.seek(0)

    # Wrap as UploadFile
    spooled = SpooledTemporaryFile(max_size=50_000_000)  # Increased size limit
    spooled.write(buf.read())
    spooled.seek(0)
    return UploadFile(filename="tiktok_video.mp4", file=spooled, content_type="video/mp4")
