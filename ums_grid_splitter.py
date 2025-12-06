"""
UMS Grid Splitter - Decodes a 3x3 grid stream and splits into 9 individual RTSP streams.

This script is designed for streams that contain a 3x3 mosaic of video feeds (like ISS
downlink cameras) and splits them into separate streams pushed to MediaMTX.

Usage:
    python ums_grid_splitter.py <channel_id> [-p PASSWORD] [--server HOST:PORT] [--nvenc]

Example:
    python ums_grid_splitter.py uVXuBCKCRhy --server 192.168.1.100:8554 --nvenc
"""

import asyncio
import argparse
import subprocess
import sys
from pathlib import Path
from ums_client import UMSClient

# Grid configuration for 720p source (1280x720)
# Each cell is approximately 426x240 pixels
GRID_CONFIG = {
    'source_width': 1280,
    'source_height': 720,
    'cols': 3,
    'rows': 3,
    'cell_width': 426,
    'cell_height': 240,
    'endpoints': [
        'DL1_ISS', 'DL2_ISS', 'DL3_ISS',  # Top row
        'DL4_ISS', 'DL5_ISS', 'DL6_ISS',  # Middle row
        'DL7_ISS', 'DL8_ISS', 'DL9_ISS',  # Bottom row
    ]
}


def build_ffmpeg_command(playlist_path: Path, server: str, use_nvenc: bool) -> list:
    """Build the ffmpeg command for splitting the grid into 9 RTSP outputs."""
    
    cfg = GRID_CONFIG
    
    # Calculate crop coordinates for each cell
    crops = []
    for row in range(cfg['rows']):
        for col in range(cfg['cols']):
            # For the rightmost column, adjust to reach the edge
            if col == cfg['cols'] - 1:
                x = cfg['source_width'] - cfg['cell_width']
            else:
                x = col * (cfg['source_width'] // cfg['cols'])
            
            # For the bottom row, adjust to reach the edge
            if row == cfg['rows'] - 1:
                y = cfg['source_height'] - cfg['cell_height']
            else:
                y = row * (cfg['source_height'] // cfg['rows'])
            
            crops.append((x, y, cfg['cell_width'], cfg['cell_height']))
    
    # Build filter_complex string
    # First, split the input into 9 streams
    filter_parts = [f"[0:v]fps=30,split=9"]
    filter_parts.append("[" + "][".join(f"a{i}" for i in range(9)) + "];")
    
    # Then crop each stream
    for i, (x, y, w, h) in enumerate(crops):
        filter_parts.append(f"[a{i}]crop={w}:{h}:{x}:{y}[v{i}];")
    
    filter_complex = "\n".join(filter_parts).rstrip(';')
    
    # Encoder settings
    if use_nvenc:
        encoder = 'h264_nvenc'
        encoder_opts = ['-preset', 'p4']  # Fast NVENC preset
    else:
        encoder = 'libx264'
        # Use superfast for 9 simultaneous encodes - ultrafast quality is too low
        encoder_opts = ['-preset', 'superfast', '-tune', 'zerolatency']
    
    # Build command
    # Note: No -re flag - RTSP push handles pacing, we want to encode as fast as possible
    cmd = [
        'ffmpeg', '-y',
        '-live_start_index', '-1',  # Start from latest segment
        '-i', str(playlist_path),
        '-filter_complex', filter_complex,
    ]
    
    # Add output for each stream
    for i, endpoint in enumerate(cfg['endpoints']):
        cmd.extend([
            '-map', f'[v{i}]',
            '-c:v', encoder,
            *encoder_opts,
            '-b:v', '600k',
            '-maxrate', '600k',
            '-bufsize', '1200k',
            '-g', '60',
            '-keyint_min', '60',
            '-bf', '0',
            '-flags', '+cgop',
            '-pix_fmt', 'yuv420p',
            '-an',  # No audio
            '-f', 'rtsp',
            '-rtsp_transport', 'tcp',
            f'rtsp://{server}/{endpoint}',
        ])
    
    return cmd


async def run_grid_splitter(args):
    """Main async function to run the grid splitter."""
    
    client = UMSClient(args.channel_id, args.password)
    
    print("=" * 60)
    print("UMS GRID SPLITTER - 3x3 to 9 RTSP Streams")
    print("=" * 60)
    print(f"Channel ID: {args.channel_id}")
    print(f"MediaMTX Server: {args.server}")
    print(f"Hardware Encoding: {'NVENC' if args.nvenc else 'Software (libx264)'}")
    print(f"\nOutput Endpoints:")
    for i, endpoint in enumerate(GRID_CONFIG['endpoints']):
        row, col = divmod(i, 3)
        pos = ['Top', 'Middle', 'Bottom'][row] + '-' + ['Left', 'Center', 'Right'][col]
        print(f"  {pos:15} -> rtsp://{args.server}/{endpoint}")
    print("=" * 60)
    
    process = None
    
    try:
        # Connect and authenticate
        await client.connect()
        await client.send_handshake()
        
        # Start WebSocket listener in background
        ws_task = asyncio.create_task(client.receive_module_info())
        
        print("\nWaiting for stream configuration...")
        await client.config_received.wait()
        
        await client.send_playing()
        
        print("Starting HLS buffer...")
        
        # Setup directories
        base_dir = Path("segments")
        base_dir.mkdir(exist_ok=True)
        
        # Clean old segments
        for f in base_dir.glob("*"):
            if f.suffix in ['.m4s', '.mp4', '.m3u8']:
                f.unlink()
        
        playlist_path = base_dir / "playlist.m3u8"
        
        # HLS State
        segments_list = []
        disk_history = []
        media_sequence = 0
        target_duration = 6
        started = False
        current_init = None
        
        # Process segments
        async for segment in client.stream_video_chunks():
            # Write segment to disk
            seg_path = base_dir / segment.filename
            seg_path.write_bytes(segment.data)
            
            if segment.type == 'init':
                current_init = segment.filename
                continue
            
            # Update state
            segments_list.append({
                'duration': segment.duration,
                'filename': segment.filename,
                'discontinuity': segment.discontinuity,
                'init_filename': current_init
            })
            
            disk_history.append({
                'filename': segment.filename,
                'init_filename': current_init
            })
            
            # Playlist window management
            if len(segments_list) > 10:
                segments_list.pop(0)
                media_sequence += 1
            
            # Disk cleanup window
            if len(disk_history) > 30:
                disk_history.pop(0)
            
            # Periodic garbage collection
            if media_sequence % 5 == 0:
                safe_files = {s['filename'] for s in disk_history}
                safe_files.update({s['init_filename'] for s in disk_history})
                if current_init:
                    safe_files.add(current_init)
                safe_files.add("playlist.m3u8")
                
                try:
                    for f in base_dir.glob("*"):
                        if f.name not in safe_files and f.suffix in ['.m4s', '.mp4', '.json']:
                            try:
                                f.unlink()
                            except Exception:
                                pass
                except Exception:
                    pass
            
            # Write playlist
            with open(playlist_path, 'w') as f:
                f.write("#EXTM3U\n")
                f.write("#EXT-X-VERSION:6\n")
                f.write(f"#EXT-X-TARGETDURATION:{target_duration}\n")
                f.write(f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}\n")
                
                for seg in segments_list:
                    if seg['discontinuity']:
                        f.write("#EXT-X-DISCONTINUITY\n")
                    f.write(f'#EXT-X-MAP:URI="{seg["init_filename"]}"\n')
                    f.write(f"#EXTINF:{seg['duration']},\n")
                    f.write(f"{seg['filename']}\n")
            
            # Start ffmpeg once buffer is ready
            if not started and len(segments_list) >= 3:
                print("\nBuffer ready. Starting ffmpeg grid splitter...")
                ffmpeg_cmd = build_ffmpeg_command(playlist_path, args.server, args.nvenc)
                
                # Log the command for debugging
                print(f"\nffmpeg command:\n{' '.join(ffmpeg_cmd[:20])}...")
                
                process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=sys.stdout,
                    stderr=sys.stderr
                )
                started = True
                print("\nStreaming to 9 endpoints. Press Ctrl+C to stop.\n")
            
            # Restart ffmpeg if it died
            if started and process.poll() is not None:
                print("\nffmpeg process died. Restarting...")
                ffmpeg_cmd = build_ffmpeg_command(playlist_path, args.server, args.nvenc)
                process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=sys.stdout,
                    stderr=sys.stderr
                )
    
    except KeyboardInterrupt:
        print("\n\nStopping grid splitter...")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if 'ws_task' in locals():
            ws_task.cancel()
        if client.ws:
            await client.ws.close()
        if process:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


def main():
    parser = argparse.ArgumentParser(
        description='Split a 3x3 grid UMS stream into 9 individual RTSP streams',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Basic usage with software encoding
    python ums_grid_splitter.py uVXuBCKCRhy

    # With password and custom server
    python ums_grid_splitter.py uVXuBCKCRhy -p mypassword --server 192.168.1.100:8554

    # With NVIDIA hardware encoding
    python ums_grid_splitter.py uVXuBCKCRhy --nvenc
        """
    )
    parser.add_argument('channel_id', help='UMS Channel ID (e.g., uVXuBCKCRhy)')
    parser.add_argument('-p', '--password', help='Stream password (if required)')
    parser.add_argument('--server', default='localhost:8554',
                        help='MediaMTX server address (default: localhost:8554)')
    parser.add_argument('--nvenc', action='store_true',
                        help='Use NVIDIA hardware encoding (requires NVIDIA GPU)')
    
    args = parser.parse_args()
    asyncio.run(run_grid_splitter(args))


if __name__ == "__main__":
    main()
