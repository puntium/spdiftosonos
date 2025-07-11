#!/usr/bin/env python3

import http.server
import socketserver
import subprocess
import threading
import os

PORT = 8080
SAMPLE_RATE = 44100
CHANNELS = 2
BITRATE = "320k"

class StreamHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/stream.mp3':
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            
            # Use PulseAudio input instead of ALSA
            # 'pulse' uses the default PulseAudio source
            # You can also specify a specific source with -i pulse:source_name
            ffmpeg_cmd = [
                'ffmpeg',
                '-f', 'pulse',
                '-i', 'default',  # Use default PulseAudio source
                '-acodec', 'mp3',
                '-b:a', BITRATE,
                '-ar', str(SAMPLE_RATE),
                '-ac', str(CHANNELS),
                '-f', 'mp3',
                '-'
            ]
            
            print(f"Streaming to {self.client_address[0]}:{self.client_address[1]}")
            
            try:
                process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0
                )
                
                try:
                    while True:
                        data = process.stdout.read(8192)
                        if not data:
                            break
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    print(f"Client {self.client_address[0]} disconnected")
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

def main():
    # Show available PulseAudio sources
    print("Available PulseAudio sources:")
    subprocess.run(['pactl', 'list', 'sources', 'short'])
    print()
    
    print(f"PulseAudio Stream Server starting on port {PORT}")
    print(f"Stream URL: http://localhost:{PORT}/stream.mp3")
    print("Using default PulseAudio source")
    print("\nPress Ctrl+C to stop")
    
    with ThreadedTCPServer(("", PORT), StreamHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")

if __name__ == "__main__":
    main()