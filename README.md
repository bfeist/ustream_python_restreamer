# IBM Video Stream Restreamer

A Python-based tool to capture and rebroadcast IBM Video (formerly Ustream) streams via RTSP, enabling playback in VLC and other standard media players.

## Features

- **Native Protocol Implementation**: Direct WebSocket + CDN communication, no browser required
- **RTSP Server**: Rebroadcast streams for viewing in VLC, OBS, or any RTSP-compatible player
- **Seamless Hash Rotation**: Automatic handling of IBM Video's authentication key rotation
- **Proactive Prefetching**: Zero-latency transitions during stream key changes
- **Local Recording**: Save streams to MP4 files
- **Long-Running Stability**: Proven 7.5+ hour continuous operation

## Quick Start

### Installation

```bash
git clone https://github.com/yourusername/ustream_python_restreamer.git
cd ustream_python_restreamer
pip install websockets requests
```

### RTSP Rebroadcasting (Recommended)

**Option 1: Built-in RTSP Server (VLC connects directly)**

```bash
python ums_rtsp.py CHANNEL_ID -p PASSWORD
```

Then in VLC: **Media → Open Network Stream → `rtsp://localhost:8554/live`**

**Option 2: Push to External RTSP Server (e.g., mediamtx)**

```bash
python ums_rtsp.py CHANNEL_ID -p PASSWORD --push rtsp://localhost:8554/mystream
```

### Local Recording

Save stream to MP4 file:

```bash
python ums_client.py CHANNEL_ID -p PASSWORD -o output.mp4 -d 3600
```

## Usage Examples

### Watch Live Stream in VLC

```bash
# Terminal 1: Start RTSP server
python ums_rtsp.py CHANNEL_ID -p PASSWORD

# Terminal 2 / VLC: Connect to stream
vlc rtsp://localhost:8554/live
```

### Push to External RTSP Server (mediamtx, etc.)

```bash
# Push to mediamtx or other RTSP server
python ums_rtsp.py CHANNEL_ID -p PASSWORD --push rtsp://localhost:8554/mystream

# Then connect with VLC to the external server
vlc rtsp://localhost:8554/mystream
```

### Record for Specific Duration

```bash
# Record 30 minutes
python ums_client.py CHANNEL_ID -p PASSWORD -o recording.mp4 -d 1800
```

### Continuous Recording

```bash
# Record until manually stopped (Ctrl+C)
python ums_client.py CHANNEL_ID -p PASSWORD -o stream.mp4
```

## Command-Line Options

### `ums_rtsp.py` (RTSP Server)

```
python ums_rtsp.py CHANNEL_ID [options]

Required:
  CHANNEL_ID          IBM Video channel ID (e.g., CHANNEL_ID)

Optional:
  -p, --password      Stream password (if required)
  --port PORT         RTSP port (default: 8554, only used in server mode)
  --path PATH         RTSP path (default: /live, only used in server mode)
  --push RTSP_URL     Push to external RTSP server (e.g., rtsp://localhost:8554/mystream)
                      When specified, acts as client instead of server
```

### `ums_client.py` (Recorder)

```
python ums_client.py CHANNEL_ID [options]

Required:
  CHANNEL_ID          IBM Video channel ID

Optional:
  -p, --password      Stream password (if required)
  -o, --output FILE   Output filename (default: output.mp4)
  -d, --duration SEC  Recording duration in seconds
```

### `ums_grid_splitter.py` (3x3 Grid Splitter)

Specialized script for streams containing a 3x3 mosaic of video feeds (e.g., ISS multi-camera downlinks). Splits a single 720p grid stream into 9 individual RTSP streams pushed to MediaMTX.

```
python ums_grid_splitter.py CHANNEL_ID [options]

Required:
  CHANNEL_ID          IBM Video channel ID

Optional:
  -p, --password      Stream password (if required)
  --server HOST:PORT  MediaMTX server address (default: localhost:8554)
  --nvenc             Use NVIDIA hardware encoding (requires NVIDIA GPU)
```

**Output Endpoints:**

| Position | Endpoint |
|----------|----------|
| Top-Left | `DL1_ISS` |
| Top-Center | `DL2_ISS` |
| Top-Right | `DL3_ISS` |
| Middle-Left | `DL4_ISS` |
| Middle-Center | `DL5_ISS` |
| Middle-Right | `DL6_ISS` |
| Bottom-Left | `DL7_ISS` |
| Bottom-Center | `DL8_ISS` |
| Bottom-Right | `DL9_ISS` |

**Example Usage:**

```bash
# Split grid stream to local mediamtx
python ums_grid_splitter.py CHANNEL_ID -p PASSWORD

# Push to remote server with hardware encoding
python ums_grid_splitter.py CHANNEL_ID -p PASSWORD --server 192.168.1.100:8554 --nvenc

# Then connect to individual streams:
vlc rtsp://localhost:8554/DL1_ISS
vlc rtsp://localhost:8554/DL2_ISS
# ... etc
```

## How It Works

### Architecture

```
┌──────────────┐    WebSocket     ┌─────────────┐
│ ums_client.py│◄───────────────►│ UMS Server  │
│              │    (signaling)   │             │
└──────┬───────┘                  └─────────────┘
       │
       │ HTTP GET
       ▼
┌──────────────┐                  ┌─────────────┐
│ Akamai CDN   │                  │ ums_rtsp.py │
│ (segments)   │                  │ (HLS gen)   │
└──────────────┘                  └──────┬──────┘
                                         │
                                         ▼
                                  ┌─────────────┐
                                  │   ffmpeg    │
                                  │ (transcode) │
                                  └──────┬──────┘
                                         │
                                         ▼
                                  ┌─────────────┐
                                  │ RTSP Server │
                                  │ :8554/live  │
                                  └─────────────┘
```

### Technical Implementation

1. **WebSocket Connection**: Authenticate and receive stream configuration
2. **Segment Download**: Fetch Fragmented MP4 segments from Akamai CDN
3. **Hash Rotation Handling**: Monitor for authentication key updates every ~60s
4. **Local HLS Generation**: Create standards-compliant HLS playlist with discontinuity tags
5. **Transcoding**: Convert to H.264 30fps for RTSP compatibility
6. **Garbage Collection**: Automatic cleanup of old segments (maintains ~50MB disk usage)

See [PROTOCOL.md](PROTOCOL.md) for detailed protocol documentation.

## Requirements

- **Python**: 3.8 or higher
- **ffmpeg**: Must be installed and available in PATH
- **Dependencies**: `websockets`, `requests`

### Installing ffmpeg

**Windows**: Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH

**macOS**: `brew install ffmpeg`

**Linux**: `sudo apt install ffmpeg` (Debian/Ubuntu) or `sudo yum install ffmpeg` (RHEL/CentOS)

## Project Structure

```
ustream_python_restreamer/
├── ums_client.py          # Core WebSocket client and recorder
├── ums_rtsp.py            # RTSP rebroadcasting server
├── ums_grid_splitter.py   # 3x3 grid stream splitter (9 RTSP outputs)
├── PROTOCOL.md            # UMS protocol documentation
├── README.md              # This file
└── .gitignore
```

## Troubleshooting

### "Connection refused" or "WebSocket error"

- Verify the channel ID is correct
- Check if the stream is currently live
- Ensure you have internet connectivity

### "404 Not Found" for segments

- Normal for live streams (waiting for next segment)
- Script will automatically retry

### VLC shows "no data" or black screen

- Wait 10-15 seconds for buffer to fill
- Ensure ffmpeg is installed and in PATH
- Try restarting the RTSP server

### High CPU usage

- Expected during transcoding (libx264)
- Reduce quality by editing `ums_rtsp.py` preset to `ultrafast`

## Advanced Configuration

### Custom RTSP Port

```bash
python ums_rtsp.py CHANNEL_ID -p PASSWORD --port 8555 --path /mystream
# Connect to: rtsp://localhost:8555/mystream
```

### Modify Transcoding Settings

Edit `ums_rtsp.py` line ~170:

```python
'-preset', 'ultrafast',  # Change to 'fast', 'medium', 'slow' for better quality
'-r', '30',              # Change frame rate
```

## Known Limitations

- **Audio**: Many IBM Video streams are video-only (no audio track)
- **Live Streams Only**: Designed for live streams, not VOD playback
- **Single Stream**: One instance per channel (no multiplexing)

## License

MIT License - see LICENSE file for details


