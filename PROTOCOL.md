# UMS Protocol Documentation

This document details the reverse-engineered IBM Video (UMS) streaming protocol.

## Overview

IBM Video uses a proprietary protocol called **UMS** (Ustream Media Server) for live and recorded video streaming. Unlike standard HLS or DASH, UMS combines WebSocket signaling with CDN-delivered Fragmented MP4 segments.

## Architecture

```
┌─────────┐                    ┌──────────────┐                 ┌─────────┐
│ Client  │◄──WebSocket───────►│ UMS Server   │                 │   CDN   │
│         │   (Signaling)      │              │                 │(Akamai) │
└─────────┘                    └──────────────┘                 └─────────┘
     │                                                                 ▲
     └─────────────────HTTP GET (with hash auth)─────────────────────┘
                        (Video Segments)
```

## WebSocket Protocol

### Connection

```
wss://r{random}-1-{mediaId}-channel-wss-omega.ums.services.video.ibm.com/1/ustream
```

- `{random}`: Random server selection number
- `{mediaId}`: Numeric media/channel identifier

### Message Flow

#### 1. Client → Server: `connect`

```json
{
  "cmd": "connect",
  "args": [
    {
      "type": "viewer",
      "appId": 3,
      "appVersion": "1.0.0",
      "rsid": "{session_id}",
      "rpin": "{password}",  // Optional, for password-protected streams
      "referrer": "https://video.ibm.com/...",
      "media": "{mediaId}",
      "application": "channel"
    }
  ]
}
```

#### 2. Server → Client: `moduleInfo`

Contains complete stream configuration:

```json
{
  "cmd": "moduleInfo",
  "args": [
    {
      "stream": {
        "contentType": "channel",
        "streams": {
          "video": [
            {
              "sourceStreamVersion": 2,
              "codec": "avc1.640020",
              "bitrate": 5502000,
              "width": 1280,
              "height": 720,
              "profile": 100,
              "level": 32,
              "chunkTime": 5000,
              "chunkUrl": "https://.../chunk_{chunkId}_{hash}.m4v",
              "initUrl": "https://.../chunk_{chunkId}_{hash}.m4vh"
            }
          ]
        },
        "hashes": {
          "1764853170": "b8cd638c97",
          "1764853180": "3863ba18d2",
          // ... hash rotation schedule
        }
      }
    }
  ]
}
```

**Key Fields:**
- `chunkUrl`: Template for media segment URLs
- `initUrl`: Template for initialization segment URLs
- `hashes`: Chunk ID → Hash mapping for CDN authentication
- `chunkTime`: Segment duration in milliseconds

#### 3. Client → Server: `playing`

```json
{
  "cmd": "playing",
  "args": [
    {
      "media": "{mediaId}",
      "position": 0
    }
  ]
}
```

#### 4. Server → Client: Periodic `moduleInfo` Updates

The server sends updated `moduleInfo` messages every ~60 seconds with new hash entries for upcoming segments (hash rotation).

## CDN Segment Download

### URL Construction

**Media Segments:**
```
https://uhsakamai-a.akamaihd.net/{path}/{version}/chunk_{chunkId}_{hash}.m4v
```

**Init Segments:**
```
https://uhsakamai-a.akamaihd.net/{path}/{version}/chunk_{chunkId}_{hash}.m4vh
```

### Hash Authentication

Each segment requires a hash parameter for authentication:

1. Client receives hash table in `moduleInfo`
2. For chunk ID `N`, find hash where `start_id <= N <= start_id + 10`
3. Construct URL with matching hash
4. Hash rotates every ~60 seconds (new `moduleInfo` with updated hashes)

### Hash Rotation Handling

```python
def get_chunk_hash(chunk_id: int, hashes: dict) -> str:
    for start_id, hash_value in hashes.items():
        if int(start_id) <= chunk_id <= int(start_id) + 10:
            return hash_value
    return list(hashes.values())[0]  # Fallback
```

## Video Format

### Container: Fragmented MP4 (FMP4)

Each segment is a self-contained MP4 fragment:

```
[ftyp] File Type Box
[moof] Movie Fragment
  [mfhd] Movie Fragment Header
  [traf] Track Fragment
    [tfhd] Track Fragment Header
    [tfdt] Track Fragment Decode Time
    [trun] Track Fragment Run
[mdat] Media Data (H.264 NAL units)
```

### Codec: H.264 (AVC)

- **Profile:** High (100)
- **Level:** 3.2 (32)
- **Resolution:** 1280x720
- **Bitrate:** ~5.5 Mbps
- **Frame Rate:** Variable (~15-30 fps)

### Initialization Segment

**Critical Discovery:** The init segment is NOT transmitted separately in early protocol versions. It must be downloaded using the `initUrl` template from `moduleInfo`.

**Init Segment Structure:**
```
[ftyp] File Type Box
[moov] Movie Box
  [mvhd] Movie Header
  [trak] Track
    [tkhd] Track Header
    [mdia] Media
      [mdhd] Media Header
      [hdlr] Handler Reference
      [minf] Media Information
        [vmhd] Video Media Header
        [dinf] Data Information
        [stbl] Sample Table
          [stsd] Sample Description
            [avc1] AVC Sample Entry
              [avcC] AVC Configuration
                - SPS (Sequence Parameter Set)
                - PPS (Picture Parameter Set)
```

The `avcC` box contains critical SPS/PPS data required for H.264 decoding.

## Playback Sequence

1. **Connect** to WebSocket server with authentication
2. **Receive** `moduleInfo` with stream config and hash table
3. **Send** `playing` command
4. **Download** init segment using `initUrl` template
5. **Download** media segments sequentially using `chunkUrl` template
6. **Concatenate** init + segments for playback
7. **Monitor** for `moduleInfo` updates (hash rotation)
8. **Re-download** init segment when hash changes
9. **Insert** new init segment before continuing media segments

## Error Handling

### 404 Not Found
Segment not yet available (live edge). Retry with exponential backoff.

### 403 Forbidden
Invalid or expired hash. Wait for next `moduleInfo` update with new hashes.

## Protocol Quirks

1. **Hash Rotation:** Hashes expire every ~60 seconds, requiring WebSocket connection for updates
2. **Init Segments:** Must be re-downloaded on hash rotation for seamless playback
3. **No Audio:** Many streams are video-only (no audio track)
4. **Chunk IDs:** Sequential integers, increment by 1 per segment
5. **Live Edge:** Attempting to download future chunks returns 404

## Implementation Notes

### Recommended Approach: Local HLS Generation

Instead of direct concatenation, generate a local HLS playlist:

```m3u8
#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-MAP:URI="init_hash1.mp4"
#EXTINF:5.0,
chunk_1.m4s
#EXTINF:5.0,
chunk_2.m4s
#EXT-X-DISCONTINUITY
#EXT-X-MAP:URI="init_hash2.mp4"
#EXTINF:5.0,
chunk_3.m4s
```

This allows standard players (ffmpeg, VLC) to handle hash rotation gracefully via `#EXT-X-DISCONTINUITY` tags.

### Prefetching Strategy

Download init segments proactively when hash rotation is imminent:

```python
# When current chunk is within 10 chunks of next hash start
if next_hash_start - current_chunk < 10:
    prefetch_init_segment(next_hash)
```

This eliminates latency during hash transitions.

## References

- ISO/IEC 14496-12: ISO Base Media File Format (MP4)
- ISO/IEC 14496-15: AVC File Format
- RFC 8216: HTTP Live Streaming (HLS) - for playlist generation strategy
