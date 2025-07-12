#!/usr/bin/env python3

import http.server
import socketserver
import subprocess
import threading
import os
import argparse
import socket
import sys
import time
from soco import SoCo, discover
from soco.exceptions import SoCoException

DEFAULT_PORT = 8080
OUTPUT_SAMPLE_RATE = 44100
CHANNELS = 2
BITRATE = "320k"
CAPTURE_DEVICE = "shared_capture"
# METADATA_INTERVAL = 100000  # No longer needed without ICY metadata

class StreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'  # Use HTTP/1.0 for better streaming compatibility
    def do_GET(self):
        if self.path == '/stream.mp3':
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            # Try a much larger fake length to see if it helps
            # Some users report 100GB works better than 10GB
            self.send_header('Content-Length', str(100 * 1024 * 1024 * 1024))  # 100GB fake length
            self.send_header('Cache-Control', 'no-cache')
            # Try keep-alive instead of close to prevent early disconnection
            self.send_header('Connection', 'keep-alive')
            # No ICY metadata needed for SPDIF stream
            self.end_headers()
            
            # using alsa here 
            ffmpeg_cmd = [
                'ffmpeg',
                '-nostdin',  # Prevent terminal state corruption
                '-loglevel', 'verbose',  # Verbose logging to diagnose delays
                '-stats',  # Show real-time encoding statistics
                '-thread_queue_size', '4096',  # Doubled input buffer
                '-re',
                '-f', 'alsa',
                '-ar', '44100',  # Force input to be interpreted as 44.1kHz
                '-i', CAPTURE_DEVICE,  # Use default PulseAudio source
                '-acodec', 'libmp3lame',
                '-compression_level', '0',  # Fastest encoding, lowest compression
                '-b:a', BITRATE,
                '-ar', str(OUTPUT_SAMPLE_RATE),  # Output sample rate (44100)
                '-ac', str(CHANNELS),
                '-f', 'mp3',
                '-flush_packets', '0',
                '-fflags', 'nobuffer',
                # Add more aggressive buffering to ensure consistent data flow
                # Real-time encoding settings
                '-rtbufsize', '100M',  # Large real-time buffer
                # Removed -preset ultrafast as it's for video encoding
                '-'
            ]
            
            print(f"Streaming to {self.client_address[0]}:{self.client_address[1]}")
            
            try:
                process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,  # Capture stderr for diagnostics
                    bufsize=1048576  # 1MB buffer for smoother reads
                )
                
                # Monitor ffmpeg stderr in a separate thread
                def monitor_ffmpeg_stderr():
                    for line in process.stderr:
                        if line:
                            timestamp = time.strftime('%H:%M:%S.%f')[:-3]
                            print(f"[{timestamp}] FFmpeg: {line.decode('utf-8').strip()}")
                
                stderr_thread = threading.Thread(target=monitor_ffmpeg_stderr)
                stderr_thread.daemon = True
                stderr_thread.start()
                
                try:
                    bytes_sent = 0
                    last_log_kb = 0
                    chunk_size = 2048  # 8KB chunks
                    
                    # Track read timing statistics
                    read_times = []  # Track read durations
                    slow_read_threshold = 0.1  # 100ms threshold
                    
                    # Pre-buffer 2 seconds of audio
                    # At 320kbps = 40KB/s, so 2 seconds = 80KB
                    prebuffer_size = 40960  # 80KB for 2 seconds at 320kbps
                    print(f"Pre-buffering {prebuffer_size // 1024}KB (2 seconds) before starting stream...")
                    prebuffer = b''
                    prebuffer_start = time.time()
                    
                    while len(prebuffer) < prebuffer_size:
                        data = process.stdout.read(8192)
                        if not data:
                            break
                        prebuffer += data
                    
                    prebuffer_duration = time.time() - prebuffer_start
                    print(f"Pre-buffered {len(prebuffer) // 1024}KB in {prebuffer_duration:.2f}s")
                    
                    # Send the pre-buffered data
                    if prebuffer:
                        write_start = time.time()
                        self.wfile.write(prebuffer)
                        write_duration = time.time() - write_start
                        bytes_sent = len(prebuffer)
                        print(f"Sent initial {bytes_sent // 1024}KB buffer in {write_duration:.3f}s")
                    
                    while True:
                        # Time the read operation
                        read_start = time.time()
                        data = process.stdout.read(chunk_size)
                        read_duration = time.time() - read_start
                        
                        # Track read statistics
                        read_times.append(read_duration)
                        if read_duration > slow_read_threshold:
                            timestamp = time.strftime('%H:%M:%S.%f')[:-3]
                            print(f"[{timestamp}] SLOW READ WARNING: ffmpeg read took {read_duration:.3f}s (requested {chunk_size} bytes)")
                        
                        # Log statistics every 100 reads
                        if len(read_times) >= 100:
                            avg_read_time = sum(read_times) / len(read_times)
                            max_read_time = max(read_times)
                            slow_reads = sum(1 for t in read_times if t > slow_read_threshold)
                            print(f"Read stats - Avg: {avg_read_time:.3f}s, Max: {max_read_time:.3f}s, Slow reads: {slow_reads}/100")
                            read_times = []
                        
                        if not data:
                            print(f"WARNING: ffmpeg returned no data after {read_duration:.3f}s wait")
                            break
                        
                        # Send raw MP3 data (no chunked encoding)
                        write_start = time.time()
                        self.wfile.write(data)
                        write_duration = time.time() - write_start
                        if write_duration > 0.05:  # 50ms threshold
                            print(f"SLOW WRITE WARNING: network write took {write_duration:.3f}s for {len(data)} bytes")
                        
                        bytes_sent += len(data)
                        
                        # Log every 10KB with more detail around 1MB mark
                        current_kb = bytes_sent // 10240
                        if current_kb > last_log_kb:
                            last_log_kb = current_kb
                            total_kb = current_kb * 10
                            # Extra logging around the 1MB mark where disconnects happen
                            if 1000 <= total_kb <= 1200:
                                print(f"[CRITICAL ZONE] Sent {total_kb}KB to {self.client_address[0]} - bytes_sent: {bytes_sent}")
                            else:
                                print(f"Sent {total_kb}KB to {self.client_address[0]}")
                    
                    print(f"Stream ended. Total sent: {bytes_sent / 1024:.1f}KB to {self.client_address[0]}")
                    
                except (BrokenPipeError, ConnectionResetError, OSError) as e:
                    print(f"Client {self.client_address[0]} disconnected after {bytes_sent / 1024:.1f}KB: {type(e).__name__}")
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    
            except Exception as e:
                print(f"Error streaming: {e}")
                
        elif self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b"""
            <html>
            <head><title>SPDIF PulseAudio Stream</title></head>
            <body>
                <h1>SPDIF PulseAudio Stream Server</h1>
                <p>Stream URL: <a href="/stream.mp3">/stream.mp3</a></p>
                <p>This stream is being served to a Sonos device.</p>
                <audio controls>
                    <source src="/stream.mp3" type="audio/mpeg">
                </audio>
                <hr>
                <p>To set your SPDIF as the default PulseAudio source:</p>
                <pre>
# List audio sources
pactl list sources short

# Set default source (replace with your SPDIF source name)
pactl set-default-source alsa_input.usb-xxx
                </pre>
            </body>
            </html>
            """)
        else:
            self.send_error(404)
    
    def log_message(self, format, *args):
        if '/favicon.ico' not in format % args:
            print(f"{self.client_address[0]} - {format % args}")

class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

def get_server_ip():
    """Get the server's IP address on the local network"""
    try:
        # Create a socket to determine the local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Connect to a public DNS server (doesn't actually send data)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        # Fallback to localhost if we can't determine the IP
        return "127.0.0.1"

def list_sonos_devices():
    """Discover and list all Sonos devices on the network"""
    print("Discovering Sonos devices on the network...")
    print("This may take a few seconds...\n")
    
    try:
        devices = discover(timeout=10)
        
        if not devices:
            print("No Sonos devices found on the network.")
            return
        
        print(f"Found {len(devices)} Sonos device(s):\n")
        print(f"{'IP Address':<15} {'Zone Name':<25} {'Model':<20} {'Status'}")
        print("-" * 80)
        
        for device in devices:
            try:
                info = device.get_speaker_info()
                status = device.get_current_transport_info()['current_transport_state']
                print(f"{device.ip_address:<15} {info['zone_name']:<25} {info['model_name']:<20} {status}")
            except Exception as e:
                print(f"{device.ip_address:<15} {'(Unable to get info)':<25} {'Unknown':<20} {'Unknown'}")
        
        print("\nUse one of these IP addresses with the script to stream to that device.")
        print("Example: python3 pulse_stream_server.py <IP_ADDRESS>")
        
    except Exception as e:
        print(f"Error discovering Sonos devices: {e}")
        print("Make sure you're on the same network as your Sonos devices.")

def connect_to_sonos(sonos_ip):
    """Connect to Sonos device and return the SoCo instance"""
    try:
        sonos = SoCo(sonos_ip)
        # Test the connection by getting the speaker info
        info = sonos.get_speaker_info()
        print(f"Connected to Sonos: {info['zone_name']} ({info['model_name']})")
        return sonos
    except (SoCoException, Exception) as e:
        print(f"Error connecting to Sonos at {sonos_ip}: {e}")
        return None

def play_stream_on_sonos(sonos, stream_url):
    """Tell the Sonos to play our stream"""
    try:
        # Stop current playback if any
        sonos.stop()
        
        # Clear the queue
        sonos.clear_queue()
        
        # Convert http URL to x-rincon-mp3radio URL for better streaming
        # This tells Sonos to use its radio streaming mode with better buffering
        if stream_url.startswith('http://'):
            radio_url = stream_url.replace('http://', 'x-rincon-mp3radio://', 1)
        else:
            radio_url = stream_url
        
        # Play the stream using the radio URL format
        sonos.play_uri(radio_url, title="PulseAudio Stream")
        
        print(f"Sonos is now playing the stream from {radio_url}")
        return True
    except Exception as e:
        print(f"Error playing stream on Sonos: {e}")
        return False

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='PulseAudio to Sonos streaming server')
    parser.add_argument('sonos_ip', nargs='?', help='IP address of the Sonos device')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'Port to run the stream server on (default: {DEFAULT_PORT})')
    parser.add_argument('--list', '-l', action='store_true',
                        help='List all available Sonos devices and exit')
    args = parser.parse_args()
    
    # If --list is specified, show devices and exit
    if args.list:
        list_sonos_devices()
        sys.exit(0)
    
    # If not listing, sonos_ip is required
    if not args.sonos_ip:
        parser.error("sonos_ip is required unless using --list")
        sys.exit(1)
    
    # Get server IP
    server_ip = get_server_ip()
    stream_url = f"http://{server_ip}:{args.port}/stream.mp3"
    
    print(f"Server IP: {server_ip}")
    print(f"Stream URL: {stream_url}")
    print()
    
    # Connect to Sonos
    print(f"Connecting to Sonos at {args.sonos_ip}...")
    sonos = connect_to_sonos(args.sonos_ip)
    if not sonos:
        print("Failed to connect to Sonos. Exiting.")
        sys.exit(1)
    # Show available PulseAudio sources
    print("\nAvailable PulseAudio sources:")
    subprocess.run(['pactl', 'list', 'sources', 'short'])
    print()
    
    print(f"PulseAudio Stream Server starting on port {args.port}")
    print("Using default PulseAudio source")
    
    # Start the HTTP server in a separate thread
    httpd = ThreadedTCPServer(("", args.port), StreamHandler)
    server_thread = threading.Thread(target=httpd.serve_forever)
    server_thread.daemon = True
    server_thread.start()
    
    print("\nHTTP server started")
    
    # Give the server a moment to start
    time.sleep(1)
    
    # Tell Sonos to play the stream
    print("\nConfiguring Sonos to play the stream...")
    if play_stream_on_sonos(sonos, stream_url):
        print("\nStreaming active! Press Ctrl+C to stop")
        print(f"You can also access the stream directly at: {stream_url}")
    else:
        print("\nWarning: Failed to start playback on Sonos, but stream server is running.")
        print(f"You can manually play the stream URL on your Sonos: {stream_url}")
    
    try:
        # Keep the main thread alive
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        try:
            # Try to stop Sonos playback
            if sonos:
                sonos.stop()
                print("Stopped Sonos playback")
        except:
            pass
        
        # Shutdown the HTTP server
        httpd.shutdown()
        print("HTTP server stopped")

if __name__ == "__main__":
    main()