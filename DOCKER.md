# UMS Grid Splitter - Docker Setup

## Quick Start with Docker

### 1. Create your environment file

```bash
cp .env.example .env
```

Edit `.env` with your configuration:

```env
CHANNEL_ID=your_channel_id_here
STREAM_PASSWORD=your_password_if_needed
MEDIAMTX_SERVER=localhost:8554
USE_NVENC=0
```

### 2. Start the container

```bash
docker-compose up -d
```

### 3. View logs

```bash
docker-compose logs -f
```

### 4. Stop the container

```bash
docker-compose down
```

## Docker Commands

### Build the image

```bash
docker-compose build
```

### Restart the container

```bash
docker-compose restart
```

### Stop the container

```bash
docker-compose stop
```

### Remove the container and volumes

```bash
docker-compose down -v
```

## Hardware Encoding (NVIDIA GPU)

To enable NVIDIA GPU hardware encoding:

1. Install [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

2. Edit `docker-compose.yml` and uncomment the GPU section:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

3. Set in `.env`:

```env
USE_NVENC=1
```

4. Rebuild and restart:

```bash
docker-compose down
docker-compose build
docker-compose up -d
```

## Configuration

### Environment Variables

| Variable          | Description                                | Default          | Required |
| ----------------- | ------------------------------------------ | ---------------- | -------- |
| `CHANNEL_ID`      | UMS Channel ID                             | -                | Yes      |
| `STREAM_PASSWORD` | Stream password                            | -                | No       |
| `MEDIAMTX_SERVER` | MediaMTX server address                    | `localhost:8554` | No       |
| `USE_NVENC`       | Use NVIDIA hardware encoding (1=yes, 0=no) | `0`              | No       |

### Output Streams

The splitter creates 9 RTSP streams from a 3x3 grid:

```
rtsp://{MEDIAMTX_SERVER}/DL1_ISS  (Top-Left)
rtsp://{MEDIAMTX_SERVER}/DL2_ISS  (Top-Center)
rtsp://{MEDIAMTX_SERVER}/DL3_ISS  (Top-Right)
rtsp://{MEDIAMTX_SERVER}/DL4_ISS  (Middle-Left)
rtsp://{MEDIAMTX_SERVER}/DL5_ISS  (Middle-Center)
rtsp://{MEDIAMTX_SERVER}/DL6_ISS  (Middle-Right)
rtsp://{MEDIAMTX_SERVER}/DL7_ISS  (Bottom-Left)
rtsp://{MEDIAMTX_SERVER}/DL8_ISS  (Bottom-Center)
rtsp://{MEDIAMTX_SERVER}/DL9_ISS  (Bottom-Right)
```

## Networking

The container uses `network_mode: host` to simplify RTSP streaming. This means:

- The container shares the host's network stack
- RTSP streams are accessible on the host's IP address
- No port mapping is needed

If you need to use bridge networking instead, remove `network_mode: host` and add port mappings as needed.

## Troubleshooting

### Check container status

```bash
docker-compose ps
```

### View real-time logs

```bash
docker-compose logs -f ums-grid-splitter
```

### Enter the container shell

```bash
docker-compose exec ums-grid-splitter bash
```

### Check ffmpeg is working

```bash
docker-compose exec ums-grid-splitter ffmpeg -version
```

### Rebuild from scratch

```bash
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

## Running Without Docker

You can still run the script directly:

```bash
# Install dependencies
pip install -r requirements.txt

# Run with command line arguments
python ums_grid_splitter.py uVXuBCKCRhy -p mypassword --server 192.168.1.100:8554

# Or use .env file
cp .env.example .env
# Edit .env with your settings
python ums_grid_splitter.py
```
