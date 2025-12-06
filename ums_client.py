"""
Native UMS client - connects directly to IBM Video UMS WebSocket protocol.

Based on protocol analysis, UMS uses WebSocket for signaling and
provides HTTPS URLs for downloading MP4 segments from CDN.

This client:
1. Connects to UMS WebSocket server
2. Sends authentication handshake
3. Receives stream segment URLs
4. Downloads MP4 fragments (video + audio)
5. Concatenates with ffmpeg

Usage:
    python ums_client.py CHANNEL_ID -p PASSWORD -o output.mp4 -d 60
"""

import asyncio
import websockets
import json
import argparse
import requests
import subprocess
import time
from pathlib import Path
from urllib.parse import urljoin
from dataclasses import dataclass
from typing import Optional, Union

@dataclass
class Segment:
    type: str # 'init' or 'media'
    data: bytes
    sequence: int
    duration: float = 0.0
    discontinuity: bool = False
    filename: str = "" # For HLS mapping



class UMSClient:
    def __init__(self, channel_id: str, password: str = None):
        self.channel_id = channel_id
        self.password = password
        self.ws = None
        self.cdn_base = None
        self.stream_config = None
        self.hashes = {}
        self.media_id = None
        
        self.media_id = None
        self.config_received = asyncio.Event()
        self.init_cache = {} # hash -> bytes
        self.prefetched_hashes = set() # hashes currently being prefetched or done
        self.failed_prefetches = {} # hash -> timestamp
        
    async def connect(self):
        """Connect to UMS WebSocket server"""
        # Construct WebSocket URL (discovered from capture)
        # Format: wss://r{random}-1-{media_id}-channel-wss-omega.ums.services.video.ibm.com/1/ustream
        
        # First, get media ID from channel page
        print(f"Getting media ID for channel {self.channel_id}...")
        channel_url = f"https://video.ibm.com/channel/{self.channel_id}/pop-out"
        
        # TODO: Parse page to get media_id
        # For now, use hardcoded value from capture
        self.media_id = "23711583"
        
        # Generate random number for server selection
        import random
        random_id = random.randint(10000000, 99999999)
        
        ws_url = f"wss://r{random_id}-1-{self.media_id}-channel-wss-omega.ums.services.video.ibm.com/1/ustream"
        
        print(f"Connecting to {ws_url}...")
        self.ws = await websockets.connect(ws_url)
        print("✓ Connected to UMS WebSocket")
        
    async def send_handshake(self):
        """Send authentication handshake"""
        handshake = {
            "cmd": "connect",
            "args": [{
                "type": "viewer",
                "appId": 3,
                "appVersion": 2,
                "rsid": "test:session",
                "rpin": "_rpin.test",
                "referrer": f"https://video.ibm.com/channel/{self.channel_id}/pop-out",
                "media": self.media_id,
                "application": "channel",
                "buildNumber": "3.0.12",
                "password": self.password
            }]
        }
        await self.ws.send(json.dumps(handshake))
        print("✓ Handshake sent")
        
    async def receive_module_info(self):
        """Receive moduleInfo messages with stream configuration"""
        msg_count = 0
        while True:
            data = await self.ws.recv()
            msg_count += 1
            
            if isinstance(data, str):
                msg = json.loads(data)
                cmd = msg.get('cmd')
                
                print(f"Received #{msg_count}: {cmd}")
                
                # Save first few messages
                if msg_count <= 5:
                    debug_dir = Path('segments')
                    debug_dir.mkdir(exist_ok=True)
                    debug_file = debug_dir / f'ws_msg_{msg_count}_{cmd}.json'
                    debug_file.write_text(json.dumps(msg, indent=2))
                    print(f"  Saved to {debug_file}")
                
                if cmd == 'moduleInfo':
                    args = msg.get('args', [{}])[0]
                    
                    # Extract stream configuration
                    if 'stream' in args:
                        stream = args['stream']
                        formats = stream.get('streamFormats', {})
                        mp4_format = formats.get('mp4/segmented', {})
                        
                        # Get CDN base URL
                        access_list = mp4_format.get('contentAccess', {}).get('accessList', [{}])
                        if access_list:
                            cdn_data = access_list[0].get('data', {})
                            protocol = cdn_data.get('protocol', 'https')
                            path = cdn_data.get('path', '')
                            self.cdn_base = f"{protocol}://uhsakamai-a.akamaihd.net/{path}"
                            
                        # Get hash lookup table
                        self.hashes = mp4_format.get('hashes', {})
                        
                        # Get stream info (video + audio)
                        self.stream_config = mp4_format.get('streams', [])
                        
                        # print(f"✓ CDN Base: {self.cdn_base}")
                        # print(f"✓ Hashes: {len(self.hashes)} entries")
                        # print(f"✓ Streams: {len(self.stream_config)} available")
                        
                        # Signal that config is ready (if waiting)
                        if not self.config_received.is_set():
                            print(f"✓ Stream config received ({len(self.hashes)} hashes)")
                            self.config_received.set()
                            
                        # Keep listening for hash updates
                        # return True <-- Don't return, keep updating hashes
                
                elif cmd == 'moduleInfo' and self.config_received.is_set():
                     # Hash update or other info
                     args = msg.get('args', [{}])[0]
                     if 'stream' in args:
                         stream = args['stream']
                         formats = stream.get('streamFormats', {})
                         mp4_format = formats.get('mp4/segmented', {})
                         new_hashes = mp4_format.get('hashes', {})
                         if new_hashes:
                             self.hashes.update(new_hashes)
                             # print(f"  Hashes updated: {len(self.hashes)} entries")
            else:
                # Binary data (shouldn't happen based on protocol analysis)
                print(f"Unexpected binary data: {len(data)} bytes")
                
    async def send_playing(self):
        """Send 'playing' command to start receiving chunk updates"""
        playing_msg = {
            "cmd": "playing",
            "args": [self.media_id, True, {
                "reason": "",
                "multiaudio": {"language": "en", "country": "US"}
            }]
        }
        await self.ws.send(json.dumps(playing_msg))
        print("✓ Sent 'playing' command")
        
    def get_chunk_hash(self, chunk_id: int) -> str:
        """Find the correct hash for a given chunk ID"""
        for key, val in self.hashes.items():
            if int(key) <= chunk_id <= int(key) + 10:
                return val
        # Fallback to first available hash if not found
        if self.hashes:
            return list(self.hashes.values())[0]
        return None

    def get_chunk_url(self, stream_version: int, chunk_id: int, content_type: str) -> str:
        """Build chunk URL from stream config"""
        # Find hash for this chunk ID
        chunk_hash = self.get_chunk_hash(chunk_id)
                
        if not chunk_hash:
            # Use first available hash as fallback
            chunk_hash = list(self.hashes.values())[0]
            
        # Extension based on content type
        ext = 'm4v' if 'video' in content_type else 'm4a'
        
        # Build URL: {cdn_base}{stream_version}/chunk_{chunk_id}_{hash}.{ext}
        chunk_filename = f"chunk_{chunk_id}_{chunk_hash}.{ext}"
        url = urljoin(self.cdn_base, f"{stream_version}/{chunk_filename}")
        
        return url

    def get_init_url(self, stream_version: int, chunk_id: int, init_template: str) -> str:
        """Build init segment URL"""
        # Template is like: 2/chunk_%_%.m4vh
        # We need chunk_id and hash
        
        # Find hash for this chunk ID
        chunk_hash = self.get_chunk_hash(chunk_id)
                
        if not chunk_hash:
            chunk_hash = list(self.hashes.values())[0]
            
        # Replace placeholders
        # First % is chunk_id, second % is hash (based on standard UMS naming)
        # BUT the template usually has % which requests/urljoin might encode.
        # It's cleaner to construct it manually if we know the format.
        
        if "chunk_%_%" in init_template:
             # Standard format
             filename = init_template.replace("chunk_%_%", f"chunk_{chunk_id}_{chunk_hash}")
        else:
            # Fallback or other format?
             print(f"Warning: Unknown init template format: {init_template}")
             filename = init_template
             
        url = urljoin(self.cdn_base, filename)
        return url
        
    def download_chunk_bytes(self, url: str) -> bytes:
        """Download MP4 segment and return bytes"""
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.content
        except requests.exceptions.HTTPError as e:
            # Silently skip 404s (hash may have changed)
            if e.response.status_code in (403, 404):
                return None
            print(f"✗ HTTP {e.response.status_code}")
            return None
        except Exception as e:
            print(f"✗ {e}")
            return None
            
    async def stream_video_chunks(self, duration: int = None):
        """Yields bytes: init segment first, then video chunks"""
        # Find best video stream (no audio - stream is video only)
        video_stream = None
        
        for s in self.stream_config:
            content_type = s.get('contentType', '')
            if 'video' in content_type:
                # Prefer original quality (version 2 = 720p)
                if s.get('sourceStreamVersion') == 2:
                    video_stream = s
                    break
                # Fallback to first video stream found
                if not video_stream:
                    video_stream = s
                    
        if not video_stream:
            print("✗ Could not find video stream")
            return
            
        print(f"Video: {video_stream['width']}x{video_stream['height']} @ {video_stream['bitrate']}kbps")
        print(f"Codec: {video_stream.get('codec', 'unknown')}")
        
        # Initialize variables
        start_chunk = min(int(k) for k in self.hashes.keys())
        init_url_template = video_stream.get('initUrl')
        downloaded_count = 0
            
        # 1. Yield Initial Init Segment (Always needed for start)
        current_hash = self.get_chunk_hash(start_chunk)
        
        if init_url_template:
            # print(f"Init Template: {init_url_template}")
            init_url = self.get_init_url(
                video_stream['sourceStreamVersion'],
                start_chunk,
                init_url_template
            )
            print(f"Downloading Init: {init_url}")
            
            init_bytes = self.download_chunk_bytes(init_url)
            if init_bytes:
                print(f"✓ Yielding init segment ({len(init_bytes)} bytes)")
                self.init_cache[current_hash] = init_bytes # Cache it
                yield Segment(
                    type='init',
                    data=init_bytes,
                    sequence=start_chunk,
                    filename=f"init_{current_hash}.mp4"
                )
            else:
                print("✗ Failed to download init segment")
                return
        else:
            print("! No initUrl found in stream config - stream may fail")

        print()
        
        # 2. Yield Segments Loop
        current_chunk = start_chunk
        start_time = time.time()
        retry_count = 0
        last_used_hash = current_hash
        
        while True:
            # Check duration limit
            if duration and (time.time() - start_time) >= duration:
                print(f"\n✓ Duration limit reached ({duration}s)")
                break
            
            # Check if hash changed for this chunk
            current_hash = self.get_chunk_hash(current_chunk)
            
            # --- PROACTIVE PREFETCHING ---
            # Look ahead for next hash change
            if init_url_template:
                # Find the next start_chunk that is > current_chunk
                upcoming = [int(k) for k in self.hashes.keys() if int(k) > current_chunk]
                if upcoming:
                    next_start_chunk = min(upcoming)
                    # If it's coming soon (within 10 chunks ~ 40 seconds)
                    if next_start_chunk - current_chunk < 10:
                        next_hash = self.hashes[str(next_start_chunk)]
                        
                        # Check backoff for failed prefetches
                        can_retry = True
                        if next_hash in self.failed_prefetches:
                            if time.time() - self.failed_prefetches[next_hash] < 10.0:
                                can_retry = False
                            else:
                                del self.failed_prefetches[next_hash]
                        
                        # Use a task to download it if not already cached/fetching
                        if can_retry and next_hash not in self.init_cache and next_hash not in self.prefetched_hashes:
                             print(f"\n[Prefetch] Triggering download for hash {next_hash[:8]}... (starts at {next_start_chunk})")
                             self.prefetched_hashes.add(next_hash) # Mark as in-progress
                             
                             path_template = init_url_template
                             stream_ver = video_stream['sourceStreamVersion']
                             
                             async def _do_prefetch(ver, cid, tpl, h_val):
                                 url = self.get_init_url(ver, cid, tpl)
                                 # print(f"Prefetching: {url}")
                                 data = await asyncio.to_thread(self.download_chunk_bytes, url) 
                                 if data:
                                     self.init_cache[h_val] = data
                                     print(f"\n[Prefetch] ✓ Ready: {h_val[:8]}")
                                 else:
                                     # Failed (likely 404 not ready yet)
                                     # print(f"[Prefetch] x Failed/Not Ready: {h_val[:8]}")
                                     self.prefetched_hashes.discard(h_val) 
                                     self.failed_prefetches[h_val] = time.time() # Backoff
                                     
                             asyncio.create_task(_do_prefetch(stream_ver, next_start_chunk, path_template, next_hash))

            # If hash changed, inject new Init Segment
            if init_url_template and (last_used_hash is not None) and (current_hash != last_used_hash):
                 print(f"\n! Hash changed ({last_used_hash} -> {current_hash}). Injecting new Init Segment.")
                 
                 new_init_bytes = self.init_cache.get(current_hash)
                 
                 if not new_init_bytes:
                     print("  (Not in cache, downloading synchronously...)")
                     init_url = self.get_init_url(
                        video_stream['sourceStreamVersion'],
                        current_chunk,
                        init_url_template
                     )
                     
                     # Retry downloading init segment (critical)
                     for init_retry in range(5):
                         new_init_bytes = self.download_chunk_bytes(init_url)
                         if new_init_bytes:
                             self.init_cache[current_hash] = new_init_bytes # Cache it
                             break
                         print(f"x (init wait {init_retry+1}/5)", end="", flush=True)
                         await asyncio.sleep(1.0)
                 else:
                     print("  (Hit cache! Zero latency.)")
                     
                 if new_init_bytes:
                     print(f"✓ Yielding new init segment ({len(new_init_bytes)} bytes)")
                     yield Segment(
                        type='init',
                        data=new_init_bytes,
                        sequence=current_chunk,
                        discontinuity=True,
                        filename=f"init_{current_hash}.mp4"
                     )
                 else:
                     print("\n✗ Failed to download new init segment after retries")

            # Update last used hash
            last_used_hash = current_hash

            # Download video segment
            video_url = self.get_chunk_url(
                video_stream['sourceStreamVersion'],
                current_chunk,
                'video/mp4'
            )
            
            chunk_bytes = self.download_chunk_bytes(video_url)
            
            if chunk_bytes:
                yield Segment(
                    type='media',
                    data=chunk_bytes,
                    sequence=current_chunk,
                    duration=5.0, # Approx duration, refine if possible
                    filename=f"chunk_{current_chunk}.m4s"
                )
                print(f".", end="", flush=True)
                downloaded_count += 1
            else:
                # Retry logic:
                if retry_count < 5:
                    print(f"x (waiting {retry_count+1}/5)", end="", flush=True)
                    await asyncio.sleep(1.0) # Wait for segment to appear
                    retry_count += 1
                    continue
                else:
                    print(f"✗ (skipped after retry)", end="", flush=True)
                    retry_count = 0
                    
            # Move to next chunk
            current_chunk += 1
            retry_count = 0
            
    async def record_stream(self, output_file: str, duration: int = None):
        """Record stream by downloading segments"""
        print("\n" + "=" * 60)
        print("RECORDING STREAM to FILE")
        print("=" * 60)
        
        # Start ffmpeg stream process
        print(f"Starting ffmpeg stream to {output_file}...")
        
        ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-f', 'mp4', # Input is FMP4 stream
            '-i', 'pipe:0', # Read from stdin
            '-c', 'copy', # Copy streams (no re-encode)
            '-movflags', '+frag_keyframe+empty_moov+default_base_moof', # Output as FMP4 (robust against interruptions)
            output_file
        ]
        
        process = subprocess.Popen(
            ffmpeg_cmd, 
            stdin=subprocess.PIPE, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
        )
        
        try:
            async for segment in self.stream_video_chunks(duration):
                try:
                    process.stdin.write(segment.data)
                    process.stdin.flush()
                except BrokenPipeError:
                    print("\n✗ ffmpeg received signal to stop/closed pipe")
                    break
                    
                # Check if ffmpeg is still running
                if process.poll() is not None:
                     print("\n✗ ffmpeg process ended unexpectedly")
                     break

        except KeyboardInterrupt:
            print("\n\n✓ Recording stopped by user")
            
        except Exception as e:
            print(f"\n✗ Error during recording: {e}")
            
        finally:
            print("\nClosing ffmpeg stream...")
            if process:
                if process.stdin:
                    process.stdin.close()
                process.wait()
                
            if Path(output_file).exists():
                 size_mb = Path(output_file).stat().st_size / (1024 * 1024)
                 print(f"Output file: {size_mb:.2f} MB")


async def main_async(args):
    client = UMSClient(args.channel_id, args.password)
    
    try:
        # Connect and authenticate
        await client.connect()
        await client.send_handshake()
        
        # Start WebSocket listener in background
        ws_task = asyncio.create_task(client.receive_module_info())
        
        # Wait for initial config
        print("Waiting for stream configuration...")
        await client.config_received.wait()
        
        await client.send_playing()
        
        # Start recording
        # Run record_stream concurrently with ws_task
        record_task = asyncio.create_task(client.record_stream(args.output, args.duration))
        
        # Wait for recording to finish (duration or user stop)
        await record_task
        
        # Cancel WS task
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
        
    finally:
        if client.ws:
            await client.ws.close()


def main():
    parser = argparse.ArgumentParser(description='Native UMS stream recorder')
    parser.add_argument('channel_id', help='Channel ID (e.g., CHANNEL_ID)')
    parser.add_argument('-p', '--password', help='Stream password')
    parser.add_argument('-o', '--output', default='output.mp4', help='Output file')
    parser.add_argument('-d', '--duration', type=int, help='Duration in seconds (default: record until Ctrl+C)')
    
    args = parser.parse_args()
    
    print("Native UMS Stream Recorder")
    print("=" * 60)
    print(f"Channel: {args.channel_id}")
    print(f"Output: {args.output}")
    if args.duration:
        print(f"Duration: {args.duration}s")
    print("=" * 60)
    print()
    
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
