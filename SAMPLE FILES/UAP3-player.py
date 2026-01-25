
uap3-player.py

Page
1
/
1
100%
#!/usr/bin/env python3
"""
UAP3 Player Service
-------------------
This script monitors for the presence of a UAP3 audio file and a connected Bluetooth speaker.
When both are available, it automatically plays the audio file.
If the Bluetooth speaker disconnects, it stops playback.
"""
import os
import time
import subprocess
import logging
import signal
import sys
import traceback

# Set up logging
LOG_FILE = "/home/pi/uap3-player.log"

# Ensure log file is writable
try:
    # Try to create/touch the log file to check permissions
    with open(LOG_FILE, 'a') as f:
        pass
    # Make sure it's readable/writable
    os.chmod(LOG_FILE, 0o644)
except Exception as e:
    # Fall back to /tmp if home directory isn't writable
    LOG_FILE = "/tmp/uap3-player.log"
    print(f"Could not use home directory for logs, using {LOG_FILE} instead")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration
AUDIO_FILE = "/home/pi/uap3_output.wav"  # Path to the audio file
CHECK_INTERVAL = 5  # How often to check for the file and Bluetooth connection
BLUETOOTH_CHECK_CMD = "bluetoothctl info | grep 'Connected: yes'"

# Global variables
playback_process = None
running = True

def signal_handler(sig, frame):
    """Handle signals to gracefully exit"""
    global running
    logger.info(f"Received signal {sig} to terminate")
    running = False
    stop_playback()
    sys.exit(0)

def is_bluetooth_connected():
    """Check if a Bluetooth device is connected"""
    try:
        result = subprocess.run(BLUETOOTH_CHECK_CMD, shell=True, capture_output=True, text=True)
        is_connected = result.returncode == 0 and "Connected: yes" in result.stdout
        logger.debug(f"Bluetooth connection check: {is_connected}")
        return is_connected
    except Exception as e:
        logger.error(f"Error checking Bluetooth connection: {e}")
        return False

def is_file_available():
    """Check if the audio file exists"""
    exists = os.path.exists(AUDIO_FILE)
    logger.debug(f"Audio file check ({AUDIO_FILE}): {exists}")
    return exists

def start_playback():
    """Start playing the audio file"""
    global playback_process
    
    if playback_process is not None and playback_process.poll() is None:
        logger.warning("Playback already in progress")
        return
    
    try:
        logger.info(f"Starting playback of {AUDIO_FILE}")
        playback_process = subprocess.Popen(["aplay", AUDIO_FILE], 
                                            stdout=subprocess.PIPE, 
                                            stderr=subprocess.PIPE)
        logger.info(f"Playback started with PID {playback_process.pid}")
    except Exception as e:
        logger.error(f"Error starting playback: {e}")
        logger.error(traceback.format_exc())

def stop_playback():
    """Stop any ongoing playback"""
    global playback_process
    
    if playback_process is None or playback_process.poll() is not None:
        return
    
    try:
        logger.info("Stopping playback")
        playback_process.terminate()
        
        # Give it a moment to terminate gracefully
        time.sleep(1)
        
        # If it's still running, kill it
        if playback_process.poll() is None:
            playback_process.kill()
            logger.info("Playback process killed")
        
        playback_process = None
    except Exception as e:
        logger.error(f"Error stopping playback: {e}")
        logger.error(traceback.format_exc())

def main():
    """Main function"""
    # Log startup information
    logger.info("UAP3 Player Service started")
    logger.info(f"Python version: {sys.version}")
    logger.info(f"Current working directory: {os.getcwd()}")
    logger.info(f"Script path: {os.path.abspath(__file__)}")
    logger.info(f"Monitoring for audio file: {AUDIO_FILE}")
    
    # Set up signal handlers for graceful termination
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    was_playing = False
    
    try:
        while running:
            # Check conditions for playback
            bt_connected = is_bluetooth_connected()
            file_exists = is_file_available()
            
            if bt_connected and file_exists:
                if not was_playing:
                    logger.info("Bluetooth connected and audio file available - starting playback")
                    start_playback()
                    was_playing = True
            else:
                if was_playing:
                    if not bt_connected:
                        logger.info("Bluetooth disconnected - stopping playback")
                    if not file_exists:
                        logger.info("Audio file not available - stopping playback")
                    stop_playback()
                    was_playing = False
            
            # Check if playback ended (file finished playing)
            if was_playing and playback_process and playback_process.poll() is not None:
                logger.info("Playback ended - restarting")
                start_playback()
            
            # Wait before checking again
            time.sleep(CHECK_INTERVAL)
    
    except Exception as e:
        logger.error(f"Error in main loop: {e}")
        logger.error(traceback.format_exc())
    finally:
        logger.info("Cleaning up before exit")
        stop_playback()
        logger.info("UAP3 Player Service stopped")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        logger.error(traceback.format_exc())
Displaying uap3-player.py.
