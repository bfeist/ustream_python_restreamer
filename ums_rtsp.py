import asyncio
import argparse
import subprocess
import sys
from pathlib import Path
from ums_client import UMSClient

async def broadcast_rtsp(args):
    client = UMSClient(args.channel_id, args.password)
    
    # Determine RTSP URL and mode
    push_mode = args.push is not None
    if push_mode:
        rtsp_url = args.push  # Use the provided push URL directly
    else:
        rtsp_url = f"rtsp://localhost:{args.port}{args.path}"
    
    print("=" * 60)
    if push_mode:
        print("RTSP PUSH MODE (to external server)")
        print("=" * 60)
        print(f"Pushing to: {rtsp_url}")
        print("\nINSTRUCTIONS:")
        print(f"1. Ensure your RTSP server (e.g., mediamtx) is running")
        print(f"2. Stream will be available at: {rtsp_url}")
    else:
        print("RTSP BROADCAST SERVER")
        print("=" * 60)
        print(f"Server URL: {rtsp_url}")
        print("\nINSTRUCTIONS:")
        print(f"1. Open VLC Media Player")
        print(f"2. Go to Media -> Open Network Stream")
        print(f"3. Enter URL: {rtsp_url}")
        print(f"4. Click Play")
        print("\nWaiting for player connection...")
    
    # ffmpeg command for RTSP server (listening mode)
    # -rtsp_flags listen: ffmpeg acts as server and waits for client
    ffmpeg_cmd = [
        'ffmpeg', '-y',
        '-re', # Read at native frame rate (realtime pacing)
        '-f', 'mp4', # Input format (from our pipe)
        '-i', 'pipe:0', 
        '-c:v', 'libx264', # Transcode to H.264
        '-r', '30', # Force 30fps output
        '-preset', 'slow',
        '-tune', 'zerolatency', # Low latency for streaming
        '-f', 'rtsp', 
        '-rtsp_transport', 'tcp',
        '-rtsp_flags', 'listen',
        rtsp_url
    ]
    
    # process = subprocess.Popen(...) <-- Removed, we start it later
    process = None
    
    try:
        # Connect and authenticate
        await client.connect()
        await client.send_handshake()
        
        # Start WebSocket listener in background
        ws_task = asyncio.create_task(client.receive_module_info())
        
        print("Waiting for stream configuration...")
        await client.config_received.wait()
        
        await client.send_playing()
        
        print("\nStreaming to internal RTSP server...")
        
        print("\nStarting HLS Generator...")
        
        # Setup directories
        base_dir = Path("segments")
        base_dir.mkdir(exist_ok=True)
        # Clean old segments
        for f in base_dir.glob("*"):
            if f.name.endswith(".m4s") or f.name.endswith(".mp4") or f.name.endswith(".m3u8"):
                f.unlink()
                
        playlist_path = base_dir / "playlist.m3u8"
        
        # HLS State
        # HLS State
        segments_list = [] # For Playlist (sliding window ~10)
        disk_history = []  # For Cleanup (sliding window ~25)
        media_sequence = 0
        target_duration = 6
        started = False
        
        # Consumer: Write segments and update playlist
        async for segment in client.stream_video_chunks():
            # Write segment to disk
            seg_path = base_dir / segment.filename
            seg_path.write_bytes(segment.data)
            
            # If init segment, we don't add to playlist media segments usually, 
            # but we use it for EXT-X-MAP
            if segment.type == 'init':
                # Just store it, next media segment will use it
                current_init = segment.filename
                continue
                
            # It is a media segment
            # Update state
            segments_list.append({
                'duration': segment.duration,
                'filename': segment.filename,
                'discontinuity': segment.discontinuity,
                'init_filename': current_init
            })
            
            # Add to disk history as well
            disk_history.append({
                'filename': segment.filename,
                'init_filename': current_init
            })
            
            # Maintenance: Update Playlist Window
            if len(segments_list) > 10:
                segments_list.pop(0)
                media_sequence += 1
                
            # Maintenance: Update Disk History (Safety Buffer)
            if len(disk_history) > 30:
                disk_history.pop(0)
                
            # Trigger periodic cleanup (every ~10 chunks = 40s, or just do it every loop? every loop is fine if fast)
            # Actually, let's do it every 5 iterations to save IO
            if media_sequence % 5 == 0:
                # Cleanup: Delete anything in segments/ that is NOT in disk_history
                # This acts as a robust Garbage Collector
                safe_files = {s['filename'] for s in disk_history}
                safe_files.update({s['init_filename'] for s in disk_history})
                if 'current_init' in locals():
                    safe_files.add(current_init)
                safe_files.add("playlist.m3u8")
                
                # Scan and delete
                try:
                    for f in base_dir.glob("*"):
                        # Delete if not safe AND (is media/init OR is temp json debug file)
                        if f.name not in safe_files and (f.suffix in ['.m4s', '.mp4', '.json']):
                            try:
                                f.unlink()
                                print(f"GC: Deleted {f.name}")
                            except Exception as e:
                                pass 
                except Exception:
                    pass
                
            # Write Playlist
            with open(playlist_path, 'w') as f:
                f.write("#EXTM3U\n")
                f.write("#EXT-X-VERSION:6\n")
                f.write(f"#EXT-X-TARGETDURATION:{target_duration}\n")
                f.write(f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}\n")
                # f.write("#EXT-X-PLAYLIST-TYPE:EVENT\n") # or VOD, or nothing for live sliding window
                
                for seg in segments_list:
                    if seg['discontinuity']:
                        f.write("#EXT-X-DISCONTINUITY\n")
                    f.write(f'#EXT-X-MAP:URI="{seg["init_filename"]}"\n')
                    f.write(f"#EXTINF:{seg['duration']},\n")
                    f.write(f"{seg['filename']}\n")
            
            # print(f"Updated playlist sequence {media_sequence+len(segments_list)}")
            
            # Start ffmpeg once we have a few segments
            if not started and len(segments_list) >= 3:
                 print("\nBuffer ready. Starting ffmpeg stream...")
                 ffmpeg_cmd = [
                    'ffmpeg', '-y',
                    '-re', # Read at realtime speed from playlist (essential for live HLS input)
                    '-i', str(playlist_path),
                    '-c:v', 'libx264', # Transcode to H.264
                    '-r', '30',
                    '-preset', 'ultrafast',
                    '-tune', 'zerolatency',
                    '-f', 'rtsp', 
                    '-rtsp_transport', 'tcp',
                 ]
                 # Only add -rtsp_flags listen if in server mode (not push mode)
                 if not push_mode:
                     ffmpeg_cmd.extend(['-rtsp_flags', 'listen'])
                 ffmpeg_cmd.append(rtsp_url)
                 process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.DEVNULL, # No piping, reads file
                    stdout=sys.stdout, 
                    stderr=sys.stderr 
                 )
                 started = True

            # If ffmpeg died, restart it? 
            # For HLS input, ffmpeg might exit if playlist runs out, but we are updating it.
            # Using -re on live playlist should keep it open.
            if started and process.poll() is not None:
                print("\nffmpeg process died. Restarting...")
                process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=sys.stdout,
                    stderr=sys.stderr 
                 )

                 
    except KeyboardInterrupt:
        print("\n\nStopping stream...")
    except Exception as e:
        print(f"\nError: {e}")
    finally:
        if 'ws_task' in locals():
            ws_task.cancel()
        if client.ws: await client.ws.close()
        if process: 
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

def main():
    parser = argparse.ArgumentParser(description='Rebroadcast UMS stream as RTSP')
    parser.add_argument('channel_id', help='Channel ID (e.g., uVXuBCKCRhy)')
    parser.add_argument('-p', '--password', help='Stream password')
    parser.add_argument('--port', default='8554', help='RTSP port (default: 8554)')
    parser.add_argument('--path', default='/live', help='RTSP path (default: /live)')
    parser.add_argument('--push', metavar='RTSP_URL', 
                        help='Push to external RTSP server (e.g., rtsp://localhost:8554/mystream)')
    
    args = parser.parse_args()
    asyncio.run(broadcast_rtsp(args))

if __name__ == "__main__":
    main()
